"""Immutable human evidence edits with retained original event bytes."""
from typing import Literal
from uuid import UUID,uuid5
from pydantic import Field

from .temporal import NodeEvidence,evaluate_program
from .runtime import records
from .entity_repository import load_binding
from .evidence_storage import existing_artifact
from ..actions.contracts import EventEvidence,StepEvidence
from ..actions.registry import validate_event
from ..errors import AppError
from ..models import Model
from ..models import EvidenceFrame
from ..programs.policy import SourceInterval
from ..programs.repository import load_program
from ..repository import Envelope,Repository,ArtifactReference,reference,fingerprint,encoded
from ..source_evidence import SourceEvidenceAdapter
from ..storage import file_sha256
import hashlib


class StepCorrection(Model):
    step_id:str=Field(pattern=r'^[a-z][a-z0-9_]{0,63}$')
    verdict:Literal['true','unknown']
    interval:SourceInterval|None=None
    source_frame_indices:list[int]=Field(default_factory=list,max_length=32)
    confirm_identity:bool=False


class CorrectionRequest(Model):
    base_fingerprint:str=Field(pattern=r'^[0-9a-f]{64}$')
    reason:str=Field(min_length=1,max_length=1000)
    steps:list[StepCorrection]=Field(min_length=1,max_length=32)


def correct(store,settings,run,event_id,body):
    repo=Repository(store);body=CorrectionRequest.model_validate(body)
    base=repo.load(run.project_id,'temporal_events',event_id)
    if base.run!=reference(run):raise AppError('OWNERSHIP','Event belongs to another run',422)
    if fingerprint(base)!=body.base_fingerprint:raise AppError('STALE_EVIDENCE','Inspect the exact event before correcting it',409)
    source=SourceEvidenceAdapter(store,settings,run.project_id,run.source_asset_id)
    original=validate_event(store,source,base)
    traces=[t for t in records(store,run.project_id,'traces',run) if 'event_ids' in t.payload]
    latest=traces[-1] if traces else None
    if latest and str(base.id) not in latest.payload['event_ids']:
        raise AppError('STALE_EVIDENCE','Correct the latest event revision',409)
    _,data=load_program(store,run.project_id,run.program.id,run.program.version)
    changes={s.step_id:s for s in body.steps}
    if len(changes)!=len(body.steps) or set(changes)-{s.step_id for s in original.steps}:
        raise AppError('INVALID_CORRECTION','Choose unique existing step IDs',422)
    nodes=[];steps=[]
    for previous in original.steps:
        change=changes.get(previous.step_id)
        if change:
            interval=change.interval
            if interval:source.interval(interval.start_ms,interval.end_ms)
            frames=[source.frame(0,source_frame_index=i) for i in change.source_frame_indices]
            state=change.verdict
            if state=='true' and not change.confirm_identity:raise AppError('IDENTITY_CONFIRMATION_REQUIRED','Positive human correction requires explicit identity confirmation',422)
            current=StepEvidence(step_id=previous.step_id,label=previous.label,verdict=state,interval=interval,
                frames=frames,reason='Human correction: '+body.reason,observation_ids=previous.observation_ids)
        else:current=previous
        steps.append(current)
        # Unedited truths retain their original association. Human-confirmed
        # identities are explicitly user assertions, never new provider output.
        identity={}
        for observation_id in previous.observation_ids:
            observation=repo.load(run.project_id,'observations',observation_id)
            for entity,binding_ref in observation.payload.get('bindings',{}).items():
                _,binding,_,_,_=load_binding(store,settings,run.project_id,run.id,binding_ref['id'])
                value=str(binding.segment_id)
                if entity in identity and identity[entity]!=value:identity[entity]='ambiguous:'+entity
                else:identity[entity]=value
        if change and change.confirm_identity:
            for node in data.program.ast.steps:
                if node.id==current.step_id:
                    for entity in node.entities:identity.setdefault(entity,'human-confirmed:'+entity)
        interval=current.interval
        if interval:
            nodes.append(NodeEvidence(record_id=base.id,step_id=current.step_id,verdict=current.verdict if current.verdict!='false' else 'unknown',
                interval=interval,window=SourceInterval(start_ms=0,end_ms=source.asset.metadata.duration_ms),frames=current.frames,
                observed_candidate=bool(current.frames),identity_bindings=identity if current.verdict=='true' else {},
                reasons=[current.reason] if current.reason else []))
    token=hashlib.sha256(encoded(body.model_dump(mode='json'))).hexdigest()
    evaluated=evaluate_program(data.program.ast,nodes,run_id=uuid5(base.id,token),source_duration_ms=source.asset.metadata.duration_ms)
    if not evaluated.cycles:raise AppError('ROOT_EVIDENCE_REQUIRED','A correction must retain a source-grounded cycle root',422)
    if len(evaluated.cycles)!=1:raise AppError('INVALID_CORRECTION','A correction must describe exactly one cycle',422)
    cycle=evaluated.cycles[0]
    interval=SourceInterval(start_ms=min(s.interval.start_ms for s in steps if s.interval),end_ms=max(s.interval.end_ms for s in steps if s.interval))
    corrected=EventEvidence(origin='user_correction',label=original.label,interval=interval,verdict=cycle.verdict.value,steps=steps,
        review_reason='' if cycle.verdict.value=='true' else '; '.join(cycle.reasons)[:2000] or body.reason,
        track_ids=original.track_ids,coverage={**original.coverage,'corrected_from':str(base.id),'correction_reason':body.reason,
            'human_assertion':True,'edges':[e.model_dump(mode='json') for e in cycle.edges],'exclusions':cycle.exclusions})
    path=repo.path(run.project_id,'temporal_events',base.id)
    artifact=ArtifactReference(path=path.relative_to(store.project_dir(run.project_id)).as_posix(),sha256=file_sha256(path),byte_size=path.stat().st_size,media_type='application/json')
    artifacts={artifact.path:artifact}
    for step in steps:
        for witness in step.frames:
            image=store.load(run.project_id,'evidence',EvidenceFrame,witness.evidence_frame_id)
            artifacts[image.image_path]=existing_artifact(store,run.project_id,image.image_path,'image/jpeg',expected_sha256=witness.image_sha256)
    identifier=uuid5(base.id,'correction:'+token)
    with store.mutation():
        if repo.path(run.project_id,'temporal_events',identifier).exists():return repo.load(run.project_id,'temporal_events',identifier)
        proposed=Envelope(kind='temporal_events',id=identifier,project_id=run.project_id,source_asset_id=run.source_asset_id,
            source_sha256=run.source_sha256,program=run.program,run=reference(run),references=base.references,artifacts=list(artifacts.values()),payload=corrected.model_dump(mode='json'))
        validate_event(store,source,proposed)
        record=repo.publish(proposed)
        if latest:
            ids=latest.payload['event_ids']
            payload={**latest.payload,'event_ids':[str(record.id) if i==str(base.id) else i for i in ids],
                'human_correction':str(record.id),'outcome':'human_review','coverage_complete':False}
            repo.publish(Envelope(kind='traces',project_id=run.project_id,source_asset_id=run.source_asset_id,source_sha256=run.source_sha256,
                program=run.program,run=reference(run),references=[reference(repo.load(run.project_id,'temporal_events',i)) for i in payload['event_ids']],payload=payload))
        return record
