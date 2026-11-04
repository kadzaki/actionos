"""Immutable geometric evidence with actual candidate and scene dependencies."""
from typing import Literal
from uuid import UUID,uuid5

from pydantic import Field,ValidationError

from .entity_repository import load_entity_batch
from .evidence_storage import digest,read_json_artifact,write_json_artifact,source_artifacts
from .geometry import GEOMETRY_VERSION,GeometricEvidence,derive_geometry,_scene_frames
from ..errors import AppError
from ..models import Model
from ..repository import Envelope,RecordReference,Repository,reference
from ..resources import MediaBudget
from ..scenes import SceneEvidence,inspect_scenes
from ..validation import bounded_json


class CandidateInput(Model):
    batch: RecordReference
    candidate_id: UUID


class GeometryRecordData(Model):
    schema_version: Literal['geometry-record-1'] = 'geometry-record-1'
    geometry_version: Literal['predicate-geometry-1'] = GEOMETRY_VERSION
    planned_window_id: str
    plan_id: UUID
    step_id: str
    assignments: dict[str,CandidateInput] = Field(min_length=1,max_length=8)
    scene_sha256: str
    geometry_sha256: str
    evidence_scopes: list[str] = Field(max_length=8)


def _resolve(store,settings,project_id,run_id,assignments):
    resolved={};prepared=None;records=[];scopes=[]
    for entity,value in sorted(assignments.items()):
        record,batch,current=load_entity_batch(store,settings,project_id,run_id,value.batch.id)
        if value.batch!=reference(record):raise AppError('REFERENCE_MISMATCH','Geometric candidate batch fingerprint changed',409)
        if prepared is not None and prepared.window.id!=current.window.id:
            raise AppError('OWNERSHIP','Geometric entities must share the same actual input window',422)
        prepared=current;resolved[entity]=(batch,value.candidate_id);records.append(record)
        scopes.append(record.payload['evidence_scope'])
    return prepared,resolved,records,scopes


def publish_geometry(store,settings,project_id,run_id,step_id,assignments):
    if not isinstance(assignments,dict) or not 1<=len(assignments)<=8:
        raise AppError('INVALID_ENTITY_BINDINGS','Choose bounded entity candidate references',422)
    refs={};repo=Repository(store)
    try:
        for entity,(batch_id,candidate_id) in assignments.items():
            batch_record=repo.load(project_id,'entity_tracks',batch_id)
            refs[entity]=CandidateInput(batch=reference(batch_record),candidate_id=candidate_id)
    except (ValueError,TypeError,ValidationError):
        raise AppError('INVALID_ENTITY_BINDINGS','Each entity requires an owned batch/candidate UUID',422) from None
    prepared,resolved,records,scopes=_resolve(store,settings,project_id,run_id,refs)
    scenes=inspect_scenes(store,settings,prepared.window)
    geometry=derive_geometry(prepared,step_id,resolved,scenes=scenes)
    # The plan is an explicit dependency, not an inferred current-program lookup.
    from .planning import plan_run
    plan=plan_run(store,settings,project_id,run_id)
    data=GeometryRecordData(planned_window_id=records[0].payload['planned_window_id'],plan_id=plan.id,step_id=step_id,
        assignments=refs,scene_sha256=digest(scenes.model_dump(mode='json')),geometry_sha256=digest(geometry.model_dump(mode='json')),
        evidence_scopes=scopes)
    identifier=uuid5(prepared.plan.run_id,'geometry-record:'+digest(data.model_dump(mode='json')))
    with store.mutation():
        if repo.path(project_id,'geometry_evidence',identifier).exists():
            previous=repo.load(project_id,'geometry_evidence',identifier)
            if previous.payload!=data.model_dump(mode='json'):raise AppError('IMMUTABLE_RECORD','Existing geometry manifest differs from this exact identity',409)
            return previous
        size=len(bounded_json(geometry.model_dump(mode='json'),max_bytes=4*1024*1024))+len(bounded_json(scenes.model_dump(mode='json'),max_bytes=4*1024*1024))+16384
        MediaBudget(store.project_dir(project_id),settings).check(size)
        base=f'geometry_evidence/artifacts/{identifier}'
        artifacts=[write_json_artifact(store,project_id,base+'/geometry.json.gz',geometry.model_dump(mode='json'),compressed=True),
            write_json_artifact(store,project_id,base+'/scene.json',scenes.model_dump(mode='json')),
            *source_artifacts(store,project_id,prepared)]
        return repo.publish(Envelope(kind='geometry_evidence',id=identifier,project_id=plan.project_id,
            source_asset_id=plan.source_asset_id,source_sha256=plan.source_sha256,program=plan.program,run=plan.run,
            references=[reference(plan),*(reference(r) for r in records)],artifacts=artifacts,payload=data.model_dump(mode='json')))


def load_geometry(store,settings,project_id,run_id,record_id):
    repo=Repository(store);record=repo.load(project_id,'geometry_evidence',record_id)
    run=repo.load(project_id,'runs',run_id)
    if record.run!=reference(run):raise AppError('OWNERSHIP','Geometry belongs to another run',422)
    try:data=GeometryRecordData.model_validate(record.payload)
    except ValidationError:raise AppError('CORRUPT_RECORD','Geometry manifest failed typed validation',409) from None
    prepared,resolved,records,scopes=_resolve(store,settings,project_id,run_id,data.assignments)
    from .planning import plan_run
    plan=plan_run(store,settings,project_id,run_id)
    if (plan.id!=data.plan_id or data.planned_window_id!=records[0].payload['planned_window_id']
            or record.references!=[reference(plan),*(reference(r) for r in records)] or scopes!=data.evidence_scopes):
        raise AppError('REFERENCE_MISMATCH','Geometry input dependencies differ from their immutable manifest',409)
    base=f'geometry_evidence/artifacts/{record.id}'
    paths=[base+'/geometry.json.gz',base+'/scene.json',prepared.window.path,prepared.source.asset.timeline_path,prepared.source.asset.source_path]
    if [a.path for a in record.artifacts]!=paths:
        raise AppError('CORRUPT_RECORD','Geometry artifacts have an invalid owner/layout',409)
    try:
        scenes=SceneEvidence.model_validate(read_json_artifact(store,project_id,paths[1]))
        geometry=GeometricEvidence.model_validate(read_json_artifact(store,project_id,paths[0],compressed=True))
    except ValidationError:raise AppError('CORRUPT_ARTIFACT','Geometry/scene evidence failed typed validation',409) from None
    _scene_frames(prepared,scenes)
    expected=derive_geometry(prepared,data.step_id,resolved,scenes=scenes)
    if (geometry!=expected or digest(geometry.model_dump(mode='json'))!=data.geometry_sha256
            or digest(scenes.model_dump(mode='json'))!=data.scene_sha256):
        raise AppError('EVIDENCE_MISMATCH','Geometry differs from the actual candidate/scene observations',409)
    return record,geometry,prepared,scenes
