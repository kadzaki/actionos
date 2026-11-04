"""A29 immutable Muse records. Provider errors are never valid empty batches."""
from typing import Literal
from uuid import UUID,uuid5
import gzip
import json

from pydantic import Field,ValidationError

from .evidence_storage import admit_perception,digest,existing_artifact,read_json_artifact,write_json_artifact,source_artifacts
from .muse_observations import (observation_versions,observation_text_format,
    build_observation_request,normalize_response,prepare_window)
from .observations import ObservationBatch
from .planning import plan_run
from ..errors import AppError
from ..models import EvidenceFrame,Model
from ..repository import Envelope,Repository,reference
from ..resources import MediaBudget
from ..validation import bounded_json


class MuseRecordData(Model):
    schema_version: Literal['muse-observation-record-1'] = 'muse-observation-record-1'
    state: Literal['validated','unavailable']
    plan_id: UUID
    planned_window_id: str = Field(min_length=1,max_length=32)
    input_window_id: UUID
    input_sha256: str
    origin: Literal['meta_cloud','recorded_replay','developer_fixture']
    evidence_scope: str = Field(min_length=1,max_length=1000)
    observation_count: int = Field(ge=0,le=256,strict=True)
    request_sha256: str
    raw_sha256: str
    batch_sha256: str | None
    error: dict | None
    adapter_version: Literal['muse-step-observations-1','muse-step-observations-2','muse-step-observations-3','muse-step-observations-4'] = 'muse-step-observations-1'
    prompt_version: Literal['program-window-evidence-1','program-window-evidence-2'] = 'program-window-evidence-1'


def request_snapshot(prepared):
    content,schema=build_observation_request(prepared)
    result={**observation_versions(prepared),
        'provider_configuration':prepared.plan.providers,'input_video':prepared.window.model_dump(mode='json'),
        'request':{'model':prepared.plan.providers['muse_model'],'stream':False,'input':[{'type':'message','role':'user','content':[
            {'type':'input_text','text':'Return only a JSON object conforming to this application schema: '+json.dumps(schema,separators=(',',':'))},
            *content,{'type':'input_video','file_id':'<transient Files upload ID; media identified by SHA-256>'}]}]},
        'application_schema':schema,'files_purpose':'user_data','remote_file_cleanup':'delete in finally',
        'sanitization':'No credential/header recorded; transient remote file ID is a placeholder, not an exact wire recording'}
    native=observation_text_format(prepared,schema)
    if native is not None:result['request']['text']={'format':native}
    return result


def _prepare(store,settings,project_id,run_id,plan_id,window_id):
    prepared=prepare_window(store,settings,project_id,run_id,plan_id,window_id)
    observation_versions(prepared)
    return prepared


def _context_artifacts(store,project_id,prepared,batch):
    items=source_artifacts(store,project_id,prepared)
    if batch:
        seen=set()
        for observation in batch.observations:
            for witness in observation.frames:
                if witness.evidence_frame_id in seen:continue
                seen.add(witness.evidence_frame_id)
                frame=store.load(project_id,'evidence',EvidenceFrame,witness.evidence_frame_id)
                items.extend([existing_artifact(store,project_id,frame.image_path,'image/jpeg',expected_sha256=witness.image_sha256),
                    existing_artifact(store,project_id,f'evidence/{frame.id}.json','application/json')])
    return items


def publish_muse_record(store,settings,project_id,run_id,planned_window_id,output,provenance,*,origin,evidence_scope,error=None):
    """Internal provider/replay boundary. No public endpoint accepts these inputs."""
    plan=plan_run(store,settings,project_id,run_id)
    prepared=_prepare(store,settings,project_id,run_id,plan.id,planned_window_id)
    if origin=='meta_cloud' and (provenance.get('provider')!='meta_cloud'
            or provenance.get('requested_model')!=prepared.plan.providers['muse_model']
            or str(provenance.get('endpoint','')).rstrip('/')!=prepared.plan.providers['endpoint']):
        raise AppError('PROVIDER_CONFIGURATION_MISMATCH','Live observation provenance differs from its pinned provider configuration',409)
    batch=None
    if error is None:
        try:batch=normalize_response(prepared,output,provenance)
        except AppError as exc:
            if not exc.code.startswith('PROVIDER_'):raise
            error=exc.envelope()
    else:
        if isinstance(error,AppError):error=error.envelope()
        if not isinstance(error,dict) or not str(error.get('code','')).startswith('PROVIDER_'):
            raise AppError('INVALID_EVIDENCE_ERROR','Unavailable Muse records require an explicit sanitized provider error',422)
    request=request_snapshot(prepared)
    raw={'output':output,'provenance':provenance,'error':error}
    bounded_json(request,max_bytes=2*1024*1024);bounded_json(raw,max_bytes=4*1024*1024)
    data=MuseRecordData(state='validated' if batch else 'unavailable',plan_id=plan.id,planned_window_id=planned_window_id,
        input_window_id=prepared.window.id,input_sha256=prepared.window.sha256,origin=origin,evidence_scope=evidence_scope,
        observation_count=len(batch.observations) if batch else 0,request_sha256=digest(request),raw_sha256=digest(raw),
        batch_sha256=digest(batch.model_dump(mode='json')) if batch else None,error=error,**observation_versions(prepared))
    identifier=uuid5(prepared.plan.run_id,'muse-record:'+digest(data.model_dump(mode='json')))
    repo=Repository(store)
    with store.mutation():
        if repo.path(project_id,'muse_observations',identifier).exists():
            previous=repo.load(project_id,'muse_observations',identifier)
            if previous.payload!=data.model_dump(mode='json'):
                raise AppError('IMMUTABLE_RECORD','Existing Muse manifest differs from this exact request/response identity',409)
            return previous
        admit_perception(store,project_id,run_id,data.observation_count)
        estimated_bytes=len(bounded_json(request,max_bytes=2*1024*1024))+len(bounded_json(raw,max_bytes=4*1024*1024))+16384
        if batch:estimated_bytes+=len(gzip.compress(bounded_json(batch.model_dump(mode='json'),max_bytes=4*1024*1024),mtime=0))
        MediaBudget(store.project_dir(project_id),settings).check(estimated_bytes)
        base=f'muse_observations/artifacts/{identifier}'
        artifacts=[write_json_artifact(store,project_id,base+'/request.json',request),
            write_json_artifact(store,project_id,base+'/raw.json',raw)]
        if batch:artifacts.append(write_json_artifact(store,project_id,base+'/batch.json.gz',batch.model_dump(mode='json'),compressed=True))
        artifacts+=_context_artifacts(store,project_id,prepared,batch)
        return repo.publish(Envelope(kind='muse_observations',id=identifier,project_id=plan.project_id,
            source_asset_id=plan.source_asset_id,source_sha256=plan.source_sha256,program=plan.program,run=plan.run,
            references=[reference(plan)],artifacts=artifacts,payload=data.model_dump(mode='json')))


def load_muse_record(store,settings,project_id,run_id,record_id):
    repo=Repository(store);record=repo.load(project_id,'muse_observations',record_id)
    run=repo.load(project_id,'runs',run_id)
    if record.run!=reference(run):raise AppError('OWNERSHIP','Muse observations belong to another run',422)
    try:data=MuseRecordData.model_validate(record.payload)
    except ValidationError:raise AppError('CORRUPT_RECORD','Muse observation manifest failed typed validation',409) from None
    prepared=_prepare(store,settings,project_id,run_id,data.plan_id,data.planned_window_id)
    if {'adapter_version':data.adapter_version,'prompt_version':data.prompt_version}!=observation_versions(prepared):
        raise AppError('EVIDENCE_MISMATCH','Stored Muse contract versions differ from the run-pinned request',409)
    if data.input_window_id!=prepared.window.id or data.input_sha256!=prepared.window.sha256:
        raise AppError('EVIDENCE_MISMATCH','Muse input differs from the exact prepared source window',409)
    base=f'muse_observations/artifacts/{record.id}'
    expected=[base+'/request.json',base+'/raw.json']+([base+'/batch.json.gz'] if data.state=='validated' else [])
    if [a.path for a in record.artifacts[:len(expected)]]!=expected:
        raise AppError('CORRUPT_RECORD','Muse artifacts have an invalid owner/layout',409)
    request=read_json_artifact(store,project_id,expected[0])
    raw=read_json_artifact(store,project_id,expected[1])
    if (digest(request)!=data.request_sha256 or request!=request_snapshot(prepared) or digest(raw)!=data.raw_sha256
            or not isinstance(raw,dict) or raw.get('error')!=data.error):
        raise AppError('EVIDENCE_MISMATCH','Muse request/response artifacts differ from the pinned manifest',409)
    batch=None
    if data.state=='validated':
        try:batch=ObservationBatch.model_validate(read_json_artifact(store,project_id,expected[2],compressed=True))
        except ValidationError:raise AppError('CORRUPT_ARTIFACT','Muse normalized batch failed typed validation',409) from None
        canonical=normalize_response(prepared,raw.get('output'),raw.get('provenance',{}))
        if (digest(canonical.model_dump(mode='json'))!=data.batch_sha256 or batch!=canonical
                or len(batch.observations)!=data.observation_count or data.error is not None):
            raise AppError('EVIDENCE_MISMATCH','Muse batch differs from its actual raw hypotheses/source evidence',409)
    elif data.observation_count or data.batch_sha256 is not None or not data.error or not str(data.error.get('code','')).startswith('PROVIDER_'):
        raise AppError('CORRUPT_RECORD','Unavailable Muse records cannot supply successful observations',409)
    context=_context_artifacts(store,project_id,prepared,batch)
    if [a.model_dump(mode='json') for a in record.artifacts[len(expected):]]!=[a.model_dump(mode='json') for a in context]:
        raise AppError('EVIDENCE_MISMATCH','Muse source frame/timeline artifacts differ from their manifest',409)
    return record,batch,prepared
