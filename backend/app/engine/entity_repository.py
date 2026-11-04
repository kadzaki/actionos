"""Immutable run-owned candidate batches and explicit segment selections."""
import gzip
import hashlib
import json
from uuid import uuid5
import zlib

from pydantic import ValidationError

from .entity_tracks import EntityTrackBatch,BindingSelectionRequest,SelectedEntityBinding,batch_fingerprint,normalize_entity_tracks,select_candidate
from .muse_observations import prepare_window
from .planning import plan_run
from ..errors import AppError
from ..models import EvidenceFrame
from ..repository import ArtifactReference,Envelope,Repository,encoded,reference
from ..resources import MediaBudget
from ..validation import bounded_json

MAX_BATCH_BYTES=4*1024*1024
MAX_RAW_BYTES=20*1024*1024


def _artifact(store,project_id,path,data,media_type):
    store.write_bytes(store.path(project_id,path),data)
    return ArtifactReference(path=path,sha256=hashlib.sha256(data).hexdigest(),byte_size=len(data),media_type=media_type)


def publish_entity_batch(store,settings,project_id,run_id,planned_window_id,batch,*,raw_text):
    """Internal provider caller only; no API accepts a candidate batch from clients."""
    batch=EntityTrackBatch.model_validate(batch.model_dump(mode='json'))
    plan=plan_run(store,settings,project_id,run_id)
    prepared=prepare_window(store,settings,project_id,run_id,plan.id,planned_window_id)
    if batch.status=='unavailable':
        if raw_text is not None or batch.candidates or not batch.error_code:
            raise AppError('INVALID_TRACK_PROVENANCE','Unavailable tracking requires an explicit error and no candidate samples',422)
        # Check the same exact lineage as successful batches, including role.
        from .entity_tracks import _owned_batch
        _owned_batch(prepared,batch)
    else:
        expected=normalize_entity_tracks(prepared,batch.entity_id,raw_text,batch.provider_provenance,prompt=batch.prompt)
        if batch_fingerprint(expected)!=batch_fingerprint(batch):
            raise AppError('EVIDENCE_MISMATCH','Track batch differs from its raw provider output and original frame map',409)
    digest=batch_fingerprint(batch)
    identifier=uuid5(prepared.plan.run_id,f'entity-batch:{digest}')
    repo=Repository(store)
    with store.mutation():
        if repo.path(project_id,'entity_tracks',identifier).exists():
            return repo.load(project_id,'entity_tracks',identifier)
        from .evidence_storage import admit_perception
        admit_perception(store,project_id,run_id,len(batch.candidates))
        raw=bounded_json({'text':raw_text,'provenance':batch.provider_provenance},max_bytes=MAX_RAW_BYTES)
        packed=gzip.compress(bounded_json(batch.model_dump(mode='json'),max_bytes=MAX_BATCH_BYTES),mtime=0)
        MediaBudget(store.project_dir(project_id),settings).check(len(raw)+len(packed)+16384)
        base=f'entity_tracks/artifacts/{identifier}'
        artifacts=[_artifact(store,project_id,base+'/batch.json.gz',packed,'application/gzip'),
            _artifact(store,project_id,base+'/raw.json',raw,'application/json')]
        return repo.publish(Envelope(kind='entity_tracks',id=identifier,project_id=plan.project_id,
            source_asset_id=plan.source_asset_id,source_sha256=plan.source_sha256,program=plan.program,run=plan.run,
            references=[reference(plan)],artifacts=artifacts,payload={
                'schema_version':'entity-track-record-1','planned_window_id':planned_window_id,'plan_id':str(plan.id),
                'batch_fingerprint':digest,'entity_id':batch.entity_id,'role':batch.role,'status':batch.status,
                'candidate_count':len(batch.candidates),'window_id':str(batch.window_id),
                'evidence_scope':str(batch.provider_provenance.get('recording_scope',batch.provider_provenance.get('scope','provider output')))[:1000]}))


def load_entity_batch(store,settings,project_id,run_id,batch_id):
    repo=Repository(store)
    record=repo.load(project_id,'entity_tracks',batch_id)
    run=repo.load(project_id,'runs',run_id)
    if record.run!=reference(run):raise AppError('OWNERSHIP','Track batch belongs to another run',422)
    payload=record.payload
    if payload.get('schema_version')!='entity-track-record-1' or len(record.artifacts)!=2:
        raise AppError('CORRUPT_RECORD','Unsupported entity-track manifest',409)
    expected_paths=[f'entity_tracks/artifacts/{record.id}/batch.json.gz',f'entity_tracks/artifacts/{record.id}/raw.json']
    if [a.path for a in record.artifacts]!=expected_paths:
        raise AppError('CORRUPT_RECORD','Entity-track artifact layout differs from its owner',409)
    prepared=prepare_window(store,settings,project_id,run_id,payload.get('plan_id'),payload.get('planned_window_id'))
    try:
        with gzip.open(store.path(project_id,expected_paths[0]),'rb') as stream:decoded=stream.read(MAX_BATCH_BYTES+1)
        if len(decoded)>MAX_BATCH_BYTES:raise ValueError('Oversized decoded batch')
        data=json.loads(decoded);bounded_json(data,max_bytes=MAX_BATCH_BYTES)
        batch=EntityTrackBatch.model_validate(data)
        with store.path(project_id,expected_paths[1]).open('rb') as stream:raw_bytes=stream.read(MAX_RAW_BYTES+1)
        if len(raw_bytes)>MAX_RAW_BYTES:raise ValueError('Oversized raw response')
        raw=json.loads(raw_bytes);bounded_json(raw,max_bytes=MAX_RAW_BYTES)
    except (OSError,ValueError,EOFError,zlib.error,ValidationError,RecursionError):
        raise AppError('CORRUPT_ARTIFACT','Entity-track evidence is damaged or exceeds its declared bounds',409) from None
    if batch_fingerprint(batch)!=payload.get('batch_fingerprint'):
        raise AppError('EVIDENCE_MISMATCH','Compressed candidate evidence differs from its manifest',409)
    from .entity_tracks import _owned_batch
    _owned_batch(prepared,batch)
    if batch.status!='unavailable':
        expected=normalize_entity_tracks(prepared,batch.entity_id,raw.get('text'),raw.get('provenance',{}),prompt=batch.prompt)
        if batch_fingerprint(expected)!=batch_fingerprint(batch):
            raise AppError('EVIDENCE_MISMATCH','Candidate evidence differs from its original provider recording',409)
    elif batch.candidates or not batch.error_code or raw.get('text') is not None:
        raise AppError('INVALID_TRACK_PROVENANCE','Unavailable tracking cannot supply candidate observations',409)
    return record,batch,prepared


def publish_binding(store,settings,project_id,run_id,batch_id,request):
    request=BindingSelectionRequest.model_validate(request)
    # Media inspection takes the artifact lock. Complete it before the writer
    # lock so concurrent frame inspection cannot invert the lock order.
    record,batch,prepared=load_entity_batch(store,settings,project_id,run_id,batch_id)
    binding=select_candidate(prepared,batch,request)
    identifier=uuid5(record.id,hashlib.sha256(encoded(binding.model_dump(mode='json'))).hexdigest())
    repo=Repository(store)
    frame=prepared.source.frame(0,source_frame_index=binding.source_frame_index)
    image=prepared.source.store.load(project_id,'evidence',EvidenceFrame,frame.evidence_frame_id)
    image_path=prepared.source.store.path(project_id,image.image_path)
    payload={'schema_version':'entity-binding-record-1','batch_id':str(record.id),'binding':binding.model_dump(mode='json')}
    with store.mutation():
        if repo.path(project_id,'entity_bindings',identifier).exists():
            previous=repo.load(project_id,'entity_bindings',identifier)
            if previous.payload!=payload:raise AppError('IMMUTABLE_RECORD','Existing binding differs from this exact selection',409)
            return previous
        MediaBudget(store.project_dir(project_id),settings).check(16384)
        return repo.publish(Envelope(kind='entity_bindings',id=identifier,project_id=record.project_id,
            source_asset_id=record.source_asset_id,source_sha256=record.source_sha256,program=record.program,run=record.run,
            references=[reference(record)],artifacts=[ArtifactReference(path=image.image_path,sha256=image.image_sha256,
                byte_size=image_path.stat().st_size,media_type='image/jpeg')],
            payload=payload))


def load_binding(store,settings,project_id,run_id,binding_id):
    record=Repository(store).load(project_id,'entity_bindings',binding_id)
    try:
        if record.payload['schema_version']!='entity-binding-record-1':raise ValueError('Unknown binding schema')
        binding=SelectedEntityBinding.model_validate(record.payload['binding'])
        batch_record,batch,prepared=load_entity_batch(store,settings,project_id,run_id,record.payload['batch_id'])
    except (KeyError,ValueError,ValidationError):
        raise AppError('CORRUPT_RECORD','Saved entity binding failed typed validation',409) from None
    if record.run!=batch_record.run or record.program!=batch_record.program or reference(batch_record) not in record.references:
        raise AppError('OWNERSHIP','Entity binding belongs to another run/batch',422)
    request=BindingSelectionRequest(batch_fingerprint=binding.batch_fingerprint,candidate_id=binding.candidate_id,
        segment_id=binding.segment_id,evidence_frame_id=binding.evidence_frame_id,input_frame_index=binding.input_frame_index)
    if select_candidate(prepared,batch,request)!=binding:
        raise AppError('EVIDENCE_MISMATCH','Binding differs from its canonical source-frame selection',409)
    return record,binding,batch_record,batch,prepared
