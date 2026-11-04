"""A29 publication/replay of fused evidence from exact immutable input records."""
from typing import Literal
from uuid import UUID,uuid5

from pydantic import Field,ValidationError

from .entity_repository import load_binding
from .evidence_storage import digest,read_json_artifact,write_json_artifact,source_artifacts
from .fusion import FUSION_VERSION,FusedPredicateEvidence,fuse_predicate_window,require_evidence_policy
from .geometry import _scene_frames
from .geometry_repository import load_geometry
from .muse_observations import prepare_window
from .observation_repository import load_muse_record
from .planning import plan_run
from ..errors import AppError
from ..models import Model
from ..programs.policy import SourceInterval
from ..repository import Envelope,RecordReference,Repository,reference
from ..resources import MediaBudget
from ..runs import RunSnapshot
from ..scenes import SceneEvidence,inspect_scenes
from ..validation import bounded_json


class FusionRecordData(Model):
    schema_version: Literal['fused-observation-record-1'] = 'fused-observation-record-1'
    policy_version: Literal['predicate-evidence-policy-1'] = FUSION_VERSION
    plan_id: UUID
    planned_window_id: str
    step_id: str
    window: SourceInterval
    muse: RecordReference | None
    geometry: RecordReference | None
    bindings: dict[str,RecordReference] = Field(max_length=8)
    input_states: dict[str,str]
    evidence_scopes: list[str]
    scene_sha256: str
    fused_sha256: str


def _inputs(store,settings,project_id,run_id,plan_id,window_id,muse_ref,geometry_ref,binding_refs):
    repo=Repository(store);run=repo.load(project_id,'runs',run_id)
    require_evidence_policy(RunSnapshot.model_validate(run.payload))
    plan=plan_run(store,settings,project_id,run_id)
    if str(plan.id)!=str(plan_id):raise AppError('REFERENCE_MISMATCH','Fusion plan differs from its reviewed run',409)
    prepared=prepare_window(store,settings,project_id,run_id,plan_id,window_id)
    refs=[reference(plan)];batch=None;geometry=None;scenes=None;tracks={};states={};scopes=[]
    if muse_ref:
        if muse_ref.kind!='muse_observations':raise AppError('INVALID_REFERENCE','Fusion requires a Muse observation record',422)
        record,batch,current=load_muse_record(store,settings,project_id,run_id,muse_ref.id)
        if reference(record)!=muse_ref or current.window.id!=prepared.window.id:
            raise AppError('REFERENCE_MISMATCH','Muse evidence differs from the selected fusion window/fingerprint',409)
        refs.append(reference(record));states['muse']=record.payload['state'];scopes.append(record.payload['evidence_scope'])
    else:states['muse']='missing'
    if geometry_ref:
        if geometry_ref.kind!='geometry_evidence':raise AppError('INVALID_REFERENCE','Fusion requires a geometric evidence record',422)
        record,geometry,current,scenes=load_geometry(store,settings,project_id,run_id,geometry_ref.id)
        if reference(record)!=geometry_ref or current.window.id!=prepared.window.id:
            raise AppError('REFERENCE_MISMATCH','Geometry differs from the selected fusion window/fingerprint',409)
        refs.append(reference(record));states['geometry']='recorded';scopes+=record.payload['evidence_scopes']
    else:states['geometry']='missing'
    for entity,ref in sorted(binding_refs.items()):
        if ref.kind!='entity_bindings':raise AppError('INVALID_REFERENCE','Fusion requires immutable entity binding records',422)
        record,binding,_,candidate,current=load_binding(store,settings,project_id,run_id,ref.id)
        if reference(record)!=ref or current.window.id!=prepared.window.id or binding.entity_id!=entity:
            raise AppError('REFERENCE_MISMATCH','Selected entity evidence differs from the exact fusion input',409)
        refs.append(reference(record));tracks[entity]=(candidate,binding);states['binding:'+entity]='selected'
        scopes.append(str(candidate.provider_provenance.get('recording_scope',candidate.provider_provenance.get('scope','provider output'))))
    return plan,prepared,refs,batch,geometry,scenes,tracks,states,scopes


def publish_fused_observation(store,settings,project_id,run_id,planned_window_id,step_id,window,*,muse_id=None,geometry_id=None,binding_ids=None):
    repo=Repository(store);plan=plan_run(store,settings,project_id,run_id)
    muse_ref=reference(repo.load(project_id,'muse_observations',muse_id)) if muse_id else None
    geometry_ref=reference(repo.load(project_id,'geometry_evidence',geometry_id)) if geometry_id else None
    bindings={e:reference(repo.load(project_id,'entity_bindings',value)) for e,value in (binding_ids or {}).items()}
    plan,prepared,refs,batch,geometry,scenes,tracks,states,scopes=_inputs(store,settings,project_id,run_id,
        plan.id,planned_window_id,muse_ref,geometry_ref,bindings)
    if scenes is None:scenes=inspect_scenes(store,settings,prepared.window)
    result=fuse_predicate_window(prepared,step_id,window,batch,tracks,geometry=geometry,scenes=scenes)
    data=FusionRecordData(plan_id=plan.id,planned_window_id=planned_window_id,step_id=step_id,window=window,muse=muse_ref,
        geometry=geometry_ref,bindings=bindings,input_states=states,evidence_scopes=scopes,
        scene_sha256=digest(scenes.model_dump(mode='json')),fused_sha256=digest(result.model_dump(mode='json')))
    identifier=uuid5(prepared.plan.run_id,'fused-record:'+digest(data.model_dump(mode='json')))
    with store.mutation():
        if repo.path(project_id,'observations',identifier).exists():
            previous=repo.load(project_id,'observations',identifier)
            if previous.payload!=data.model_dump(mode='json'):raise AppError('IMMUTABLE_RECORD','Existing fused manifest differs from this exact identity',409)
            return previous
        size=len(bounded_json(result.model_dump(mode='json'),max_bytes=4*1024*1024))+len(bounded_json(scenes.model_dump(mode='json'),max_bytes=4*1024*1024))+16384
        MediaBudget(store.project_dir(project_id),settings).check(size)
        base=f'observations/artifacts/{identifier}'
        artifacts=[write_json_artifact(store,project_id,base+'/fused.json.gz',result.model_dump(mode='json'),compressed=True),
            write_json_artifact(store,project_id,base+'/scene.json',scenes.model_dump(mode='json')),
            *source_artifacts(store,project_id,prepared)]
        return repo.publish(Envelope(kind='observations',id=identifier,project_id=plan.project_id,source_asset_id=plan.source_asset_id,
            source_sha256=plan.source_sha256,program=plan.program,run=plan.run,references=refs,artifacts=artifacts,payload=data.model_dump(mode='json')))


def load_fused_observation(store,settings,project_id,run_id,record_id):
    repo=Repository(store);record=repo.load(project_id,'observations',record_id)
    try:data=FusionRecordData.model_validate(record.payload)
    except ValidationError:raise AppError('CORRUPT_RECORD','Fused observation manifest failed typed validation',409) from None
    run=repo.load(project_id,'runs',run_id)
    if record.run!=reference(run):raise AppError('OWNERSHIP','Fused evidence belongs to another run',422)
    _,prepared,refs,batch,geometry,input_scenes,tracks,states,scopes=_inputs(store,settings,project_id,run_id,
        data.plan_id,data.planned_window_id,data.muse,data.geometry,data.bindings)
    if refs!=record.references or states!=data.input_states or scopes!=data.evidence_scopes:
        raise AppError('REFERENCE_MISMATCH','Fused evidence input dependencies/state differ from the pinned manifest',409)
    base=f'observations/artifacts/{record.id}'
    paths=[base+'/fused.json.gz',base+'/scene.json',prepared.window.path,prepared.source.asset.timeline_path,prepared.source.asset.source_path]
    if [a.path for a in record.artifacts]!=paths:
        raise AppError('CORRUPT_RECORD','Fused evidence artifacts have an invalid owner/layout',409)
    try:
        scenes=SceneEvidence.model_validate(read_json_artifact(store,project_id,paths[1]))
        result=FusedPredicateEvidence.model_validate(read_json_artifact(store,project_id,paths[0],compressed=True))
    except ValidationError:raise AppError('CORRUPT_ARTIFACT','Fused/scene evidence failed typed validation',409) from None
    _scene_frames(prepared,scenes)
    if input_scenes is not None and input_scenes!=scenes:
        raise AppError('EVIDENCE_MISMATCH','Fused and geometric scene evidence differ',409)
    expected=fuse_predicate_window(prepared,data.step_id,data.window,batch,tracks,geometry=geometry,scenes=scenes)
    if (result!=expected or digest(result.model_dump(mode='json'))!=data.fused_sha256
            or digest(scenes.model_dump(mode='json'))!=data.scene_sha256):
        raise AppError('EVIDENCE_MISMATCH','Fused conclusion differs from the actual pinned source observations/policy',409)
    return record,result,prepared
