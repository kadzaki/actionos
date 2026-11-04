"""Exact node/source/provider compatible replay for temporal/output-only revisions."""
from copy import deepcopy

from .runtime import records,evaluate_run
from .observation_repository import load_muse_record,publish_muse_record
from .entity_repository import load_entity_batch,load_binding,publish_entity_batch,publish_binding
from .entity_tracks import normalize_entity_tracks,batch_fingerprint,BindingSelectionRequest
from .evidence_storage import read_json_artifact,existing_artifact
from .planning import plan_run
from .muse_observations import prepare_window
from ..errors import AppError
from ..programs.repository import load_program
from ..repository import Repository,reference
from ..runs import RunSnapshot


def check_compatible(store,project,program,parent):
    _,old=load_program(store,project,parent.program.id,parent.program.version)
    _,new=load_program(store,project,program.id,program.version)
    if parent.source_sha256!=program.source_sha256 or parent.source_asset_id!=program.source_asset_id:
        raise AppError('REOBSERVATION_REQUIRED','Source differs; start a new observation run',409)
    for field in ('entities','zones','steps','unknown_policy'):
        if getattr(old.program.ast,field)!=getattr(new.program.ast,field):
            raise AppError('REOBSERVATION_REQUIRED','Entities, predicates, zones or evidence policy changed; observe again',409)


def parent_artifacts(store,parent):
    """Pin parent records AND raw inputs, without same-layer repository edges."""
    repo=Repository(store);project=parent.project_id;artifacts={}
    for record in [parent,*records(store,project,'muse_observations',parent),*records(store,project,'entity_tracks',parent),*records(store,project,'entity_bindings',parent)]:
        path=repo.path(project,record.kind,record.id,record.version)
        relative=path.relative_to(store.project_dir(project)).as_posix()
        artifacts[relative]=existing_artifact(store,project,relative,'application/json')
        for artifact in record.artifacts:artifacts[artifact.path]=artifact
    return list(artifacts.values())


def replay_run(store,runner,settings,child,job):
    snapshot=RunSnapshot.model_validate(child.payload);repo=Repository(store);project=child.project_id
    parent=repo.load(project,'runs',snapshot.reuse_parent.id)
    if reference(parent)!=snapshot.reuse_parent:raise AppError('REFERENCE_MISMATCH','Replay parent fingerprint changed',409)
    program=repo.load(project,'programs',child.program.id,child.program.version)
    check_compatible(store,project,program,parent)
    old_snapshot=RunSnapshot.model_validate(parent.payload)
    if snapshot.providers!=old_snapshot.providers or snapshot.evidence_policy_version!=old_snapshot.evidence_policy_version or snapshot.source_artifact_sha256!=old_snapshot.source_artifact_sha256:
        raise AppError('REOBSERVATION_REQUIRED','Provider, adapter, evidence policy or prepared source changed',409)
    plan=plan_run(store,settings,project,child.id)
    old_plan=plan_run(store,settings,project,parent.id)
    if plan.payload['plan']['windows']!=old_plan.payload['plan']['windows']:raise AppError('REOBSERVATION_REQUIRED','Observation frame maps changed',409)
    for record in records(store,project,'muse_observations',parent):
        runner.stage(job,'reusing_observations',None)
        _,batch,old_prepared=load_muse_record(store,settings,project,parent.id,record.id)
        if batch is None:continue
        prepared=prepare_window(store,settings,project,child.id,plan.id,record.payload['planned_window_id'])
        output=deepcopy(batch.raw_provider_response)
        output['window_id']=str(prepared.window.id)
        provenance={**batch.provider_provenance,'recording_scope':'Compatible saved evidence replay; no new inference',
            'reused_from':reference(record).model_dump(mode='json'),'original_window_id':str(old_prepared.window.id)}
        publish_muse_record(store,settings,project,child.id,record.payload['planned_window_id'],output,provenance,
            origin='recorded_replay',evidence_scope='Compatible saved evidence replay; original response window ID explicitly rebound')
    translated={}
    for record in records(store,project,'entity_tracks',parent):
        runner.check_cancelled(job)
        _,batch,_=load_entity_batch(store,settings,project,parent.id,record.id)
        if batch.status=='unavailable':continue
        prepared=prepare_window(store,settings,project,child.id,plan.id,record.payload['planned_window_id'])
        raw=read_json_artifact(store,project,f'entity_tracks/artifacts/{record.id}/raw.json',max_bytes=20*1024*1024)
        provenance={**batch.provider_provenance,'recording_scope':'Compatible saved tracking replay; no new inference','reused_from':reference(record).model_dump(mode='json')}
        new_batch=normalize_entity_tracks(prepared,batch.entity_id,raw['text'],provenance,prompt=batch.prompt)
        new_record=publish_entity_batch(store,settings,project,child.id,record.payload['planned_window_id'],new_batch,raw_text=raw['text'])
        translated[record.id]=(batch,new_batch,new_record,prepared)
    for record in records(store,project,'entity_bindings',parent):
        _,binding,batch_record,_,_=load_binding(store,settings,project,parent.id,record.id)
        if batch_record.id not in translated:continue
        old,new,new_record,prepared=translated[batch_record.id]
        index=next(i for i,c in enumerate(old.candidates) if c.id==binding.candidate_id)
        segment_index=next(i for i,s in enumerate(old.candidates[index].segments) if s.id==binding.segment_id)
        candidate=new.candidates[index];segment=candidate.segments[segment_index]
        request=BindingSelectionRequest(batch_fingerprint=batch_fingerprint(new),candidate_id=candidate.id,segment_id=segment.id,
            evidence_frame_id=binding.evidence_frame_id,input_frame_index=binding.input_frame_index)
        publish_binding(store,settings,project,child.id,new_record.id,request)
    return evaluate_run(store,runner,settings,child,job)
