"""Real media/review/storage with visibly hand-authored tracking hypotheses."""
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.engine.entity_repository import load_entity_batch,publish_entity_batch
from app.engine.entity_tracks import ENTITY_ADAPTER_VERSION,normalize_entity_tracks
from app.engine.muse_observations import OBSERVATION_ADAPTER_VERSION,prepare_window
from app.engine.planning import plan_run
from app.programs.compiler import Compilation
from app.programs.repository import approve_program,save_compilation
from app.programs.validation import validate_program
from app.repository import Repository,fingerprint
from app.runs import snapshot_run
from app.storage import FileStore
from test_entity_tracks import wire
from test_evidence import prepare
from test_program_ast import draft


@pytest.fixture
def tracking(client,settings,media):
    asset=prepare(client,media['vfr']);store=client.app.state.store
    raw=draft()
    raw['clauses']=[{'start_char':0,'end_char':len(raw['instruction']),'text':raw['instruction'],
        'status':'represented','represented_by':[{'kind':'step','id':'place'},{'kind':'step','id':'use'}]}]
    compilation=Compilation(original_instruction=raw['instruction'],source={'duration_ms':asset.metadata.duration_ms,
        'width':320,'height':180,'audio_present':False},draft={'instruction':raw['instruction'],'ast':raw},
        validation=validate_program(raw,source_duration_ms=asset.metadata.duration_ms,require_clauses=True),
        compiler={'origin':'test_fixture','compiler_version':'fixture','prompt_version':'fixture'},latency_seconds=0,
        provider_output={'fixture_only':True})
    _,program=save_compilation(store,asset,uuid4(),compilation)
    approve_program(store,settings,asset.project_id,program.id,{'version':1,'program_fingerprint':fingerprint(program),'source_sha256':asset.sha256})
    run=snapshot_run(store,settings,asset.project_id,program.id,1,muse_adapter_version=OBSERVATION_ADAPTER_VERSION,
        sam_adapter_version=ENTITY_ADAPTER_VERSION,evaluator_version='fixture-no-evaluation')
    plan=plan_run(store,settings,asset.project_id,run.id)
    prepared=prepare_window(store,settings,asset.project_id,run.id,plan.id,'w000')
    text=wire([0,1,3,4],objects=(1,2))
    batch=normalize_entity_tracks(prepared,'component',text,{'provider':'test_fixture','response_id':'fixture-response',
        'recording_scope':'HAND-AUTHORED tracking QA fixture; no cloud action recognition'})
    record=publish_entity_batch(store,settings,asset.project_id,run.id,'w000',batch,raw_text=text)
    base=f'/api/projects/{asset.project_id}/runs/{run.id}/entity-tracks/{record.id}'
    return SimpleNamespace(store=store,asset=asset,program=program,run=run,plan=plan,prepared=prepared,
        text=text,batch=batch,record=record,base=base)


def test_inspection_pagination_and_compressed_evidence_restart(client,settings,tracking):
    t=tracking
    response=client.get(f'/api/projects/{t.asset.project_id}/entity-tracks',params={'program_id':str(t.program.id),'version':1,'limit':1})
    assert response.status_code==200
    body=response.json()
    assert body['program_origin']=='test_fixture' and body['errors']==[] and body['next_offset'] is None
    assert body['items'][0]['candidate_count']==2 and 'HAND-AUTHORED' in body['items'][0]['evidence_scope']
    detail=client.get(t.base).json()
    assert detail['batch_fingerprint']==t.record.payload['batch_fingerprint']
    assert all(c['identity']=='unknown' for c in detail['batch']['candidates'])
    fresh=FileStore(settings.data_dir)
    record,batch,_=load_entity_batch(fresh,settings,t.asset.project_id,t.run.id,t.record.id)
    assert record==t.record and batch==t.batch
    assert len(record.artifacts)==2 and record.artifacts[0].media_type=='application/gzip'
    again=publish_entity_batch(t.store,settings,t.asset.project_id,t.run.id,'w000',t.batch,raw_text=t.text)
    assert again==record


def binding_request(client,t,index=0):
    candidate=t.batch.candidates[0];segment=candidate.segments[0]
    response=client.get(t.base+'/frame',params={'input_frame_index':index})
    assert response.status_code==200
    frame=response.json()
    assert client.get(frame['image_url']).status_code==200
    return {'batch_fingerprint':t.record.payload['batch_fingerprint'],'candidate_id':str(candidate.id),
        'segment_id':str(segment.id),'evidence_frame_id':frame['frame']['evidence_frame_id'],'input_frame_index':index}


def test_selection_is_explicit_immutable_source_bound_and_idempotent(client,tracking):
    t=tracking;old=t.record.model_dump_json();request=binding_request(client,t)
    first=client.post(t.base+'/bindings',json=request)
    assert first.status_code==200,first.text
    second=client.post(t.base+'/bindings',json=request)
    assert first.json()==second.json()
    value=first.json()
    assert value['binding']['scope']=='selected_segment_only'
    assert value['binding']['cross_window_identity_verified'] is False
    stored=Repository(t.store).load(t.asset.project_id,'entity_bindings',value['binding_id'])
    assert stored.references[0].id==t.record.id and stored.artifacts[0].media_type=='image/jpeg'
    assert Repository(t.store).load(t.asset.project_id,'entity_tracks',t.record.id).model_dump_json()==old
    assert client.get(t.base).json()['batch']['candidates'][0]['identity']=='unknown'
    assert len(list(t.store.path(t.asset.project_id,'entity_bindings').glob('*.json')))==1
    saved=client.get(t.base+'/bindings').json()
    assert saved['errors']==[] and saved['items'][0]['binding_id']==value['binding_id']
    assert saved['items'][0]['binding']['input_frame_index']==0


@pytest.mark.parametrize('change,code',[
    ({'batch_fingerprint':'a'*64},'STALE_TRACK_SELECTION'),({'candidate_id':str(uuid4())},'INVALID_TRACK_SELECTION'),
    ({'input_frame_index':2},'INVALID_TRACK_SELECTION'),({'evidence_frame_id':str(uuid4())},'EVIDENCE_MISMATCH')])
def test_invalid_selections_cannot_publish_bindings(client,tracking,change,code):
    t=tracking
    result=client.post(t.base+'/bindings',json={**binding_request(client,t),**change})
    assert result.status_code in (409,422) and result.json()['code']==code
    assert not list(t.store.path(t.asset.project_id,'entity_bindings').glob('*.json'))


def test_foreign_run_and_out_of_window_frames_are_rejected(client,settings,tracking):
    t=tracking
    other=snapshot_run(t.store,settings,t.asset.project_id,t.program.id,1,muse_adapter_version=OBSERVATION_ADAPTER_VERSION,
        sam_adapter_version=ENTITY_ADAPTER_VERSION,evaluator_version='fixture-no-evaluation')
    result=client.get(t.base.replace(str(t.run.id),str(other.id)))
    assert result.status_code==422 and result.json()['code']=='OWNERSHIP'
    result=client.get(t.base+'/frame',params={'input_frame_index':511})
    assert result.status_code==422 and result.json()['code']=='INVALID_FRAME'


def test_corrupt_track_artifact_is_reported_and_never_becomes_empty_success(client,tracking):
    t=tracking
    artifact=t.store.path(t.asset.project_id,t.record.artifacts[0].path)
    artifact.write_bytes(b'damaged gzip artifact')
    result=client.get(t.base)
    assert result.status_code==409 and result.json()['code']=='CORRUPT_ARTIFACT'
    result=client.get(f'/api/projects/{t.asset.project_id}/entity-tracks',params={'program_id':str(t.program.id),'version':1})
    assert result.status_code==200
    assert result.json()['items']==[] and result.json()['errors'][0]['code']=='CORRUPT_ARTIFACT'


def test_public_api_cannot_accept_raw_provider_batches_or_trigger_inference(client,tracking):
    t=tracking
    response=client.post(t.base,json=t.batch.model_dump(mode='json'))
    assert response.status_code==405
    response=client.post(t.base+'/bindings',json={**binding_request(client,t),'batch':t.batch.model_dump(mode='json')})
    assert response.status_code==422


def test_run_geometry_reads_owned_artifacts_measures_scenes_and_requires_semantic_support(client,settings,tracking):
    from app.engine.geometry import derive_run_geometry
    t=tracking
    actor=normalize_entity_tracks(t.prepared,'operator',t.text,{'provider':'test_fixture','response_id':'fixture-actor'})
    actor_record=publish_entity_batch(t.store,settings,t.asset.project_id,t.run.id,'w000',actor,raw_text=t.text)
    result=derive_run_geometry(t.store,settings,t.asset.project_id,t.run.id,'place',{
        'component':(t.record.id,t.batch.candidates[0].id),'operator':(actor_record.id,actor.candidates[0].id)})
    assert result.measurements==[] and 'SEMANTIC_OR_VISIBILITY_SUPPORT_REQUIRED' in result.unmet_requirements[0]
    assert result.provenance['scene_evidence_id'] and result.provenance['scene_record_sha256']
    assert len(result.provenance['entity_batch_references'])==2
