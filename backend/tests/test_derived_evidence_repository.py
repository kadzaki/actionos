"""A29 replay/recovery on generated media with explicit mock observations."""
from copy import deepcopy
from uuid import uuid4

import pytest

from app.engine.entity_repository import publish_binding,publish_entity_batch
from app.engine.entity_tracks import batch_fingerprint,normalize_entity_tracks
from app.engine.fusion_repository import load_fused_observation,publish_fused_observation
from app.engine.geometry_repository import load_geometry,publish_geometry
from app.engine.observation_repository import load_muse_record,publish_muse_record
from app.errors import AppError
from app.repository import Repository,reference
from app.storage import FileStore
from test_entity_api import tracking
from test_entity_tracks import wire
from test_step_observations import fixture_response,PROVENANCE


def derived_bundle(t,settings):
    raw=fixture_response(t.prepared)
    raw['entities']=[{'entity_id':entity,'role':role,'description':'Mock appearance on generated QA media',
        'visibility':'clear','identity':'unambiguous','evidence_frame_indices':[1,4],'uncertainty':[]}
        for entity,role in [('operator','actor'),('component','object')]]
    raw['hypotheses'][0]['evidence_frame_indices']=[1,4]
    muse=publish_muse_record(t.store,settings,t.asset.project_id,t.run.id,'w000',raw,PROVENANCE,
        origin='developer_fixture',evidence_scope='A29 hand-authored integration hypotheses, not live tabletop recognition')
    bindings={};candidates={}
    for entity in ('operator','component'):
        text=wire(range(len(t.prepared.window.source_frame_indices)))
        batch=normalize_entity_tracks(t.prepared,entity,text,{'provider':'test_fixture','response_id':'A29-dense-'+entity,
            'recording_scope':'A29 hand-authored tracking fixture on generated source'})
        record=publish_entity_batch(t.store,settings,t.asset.project_id,t.run.id,'w000',batch,raw_text=text)
        c=batch.candidates[0];s=c.segments[0];frame=t.prepared.source.frame(0,source_frame_index=t.prepared.window.source_frame_indices[0])
        binding=publish_binding(t.store,settings,t.asset.project_id,t.run.id,record.id,{'batch_fingerprint':batch_fingerprint(batch),
            'candidate_id':c.id,'segment_id':s.id,'evidence_frame_id':frame.evidence_frame_id,'input_frame_index':0})
        bindings[entity]=binding.id;candidates[entity]=(record.id,c.id)
    geometry=publish_geometry(t.store,settings,t.asset.project_id,t.run.id,'place',candidates)
    fused=publish_fused_observation(t.store,settings,t.asset.project_id,t.run.id,'w000','place',t.prepared.planned_window.source_interval,
        muse_id=muse.id,geometry_id=geometry.id,binding_ids=bindings)
    return muse,geometry,fused,bindings


def test_geometry_and_fusion_reload_and_resolve_exact_record_dependencies(client,settings,tracking):
    t=tracking;muse,geometry,fused,bindings=derived_bundle(t,settings)
    fresh=FileStore(settings.data_dir)
    record,measured,_,scene=load_geometry(fresh,settings,t.asset.project_id,t.run.id,geometry.id)
    assert record==geometry and measured.measurements==[]  # Placement requires semantics, not boxes.
    assert measured.unmet_requirements and scene.video_sha256==t.prepared.window.sha256
    saved,result,_=load_fused_observation(fresh,settings,t.asset.project_id,t.run.id,fused.id)
    assert saved==fused and result.verdict=='true' and result.needs_review
    assert result.observation_ids and result.supports[0].observation_ids
    assert {r.kind for r in fused.references}=={'observation_plans','muse_observations','geometry_evidence','entity_bindings'}
    assert result.provenance['bindings']['component']['entity_id']=='component'
    assert 'hand-authored' in ' '.join(fused.payload['evidence_scopes'])
    again=publish_fused_observation(t.store,settings,t.asset.project_id,t.run.id,'w000','place',t.prepared.planned_window.source_interval,
        muse_id=muse.id,geometry_id=geometry.id,binding_ids=bindings)
    assert again==fused
    root=f'/api/projects/{t.asset.project_id}/runs/{t.run.id}'
    inspection=client.get(root+'/observations/'+str(fused.id))
    assert inspection.status_code==200,inspection.text
    assert inspection.json()['result']['interpretation']=='bounded_visual_evidence_only'
    for link in inspection.json()['evidence_links']:
        source=client.get(link['url'])
        assert source.status_code==200,source.text
        for frame in source.json().get('frames',[]):
            assert client.get(frame['image_url']).status_code==200
            assert client.get(frame['source_url'].split('#')[0]).status_code==200
    listing=client.get(root+'/evidence').json()
    assert listing['errors']==[] and len(listing['items'])==3
    assert all(item['evidence_scopes'] for item in listing['items'])
    assert client.post(root+'/observations/'+str(fused.id),json={'verdict':'true'}).status_code==405


def test_unavailable_provider_input_remains_explicit_unknown(client,settings,tracking):
    t=tracking
    raw=fixture_response(t.prepared);raw['hypotheses'][0]['step_id']='invalid'
    failed=publish_muse_record(t.store,settings,t.asset.project_id,t.run.id,'w000',raw,PROVENANCE,
        origin='developer_fixture',evidence_scope='Explicit malformed provider QA fixture')
    record=publish_fused_observation(t.store,settings,t.asset.project_id,t.run.id,'w000','place',t.prepared.planned_window.source_interval,muse_id=failed.id)
    _,result,_=load_fused_observation(t.store,settings,t.asset.project_id,t.run.id,record.id)
    assert result.verdict=='unknown' and not result.coverage.sufficient_negative
    assert record.payload['input_states']['muse']=='unavailable'
    assert reference(failed) in record.references and failed.payload['error']['code']=='PROVIDER_STEP'


@pytest.mark.parametrize('kind',['fused','geometry','scene','binding_image','raw_muse'])
def test_damaged_upstream_artifact_cannot_recover_as_verified_empty_or_unknown(client,settings,tracking,kind):
    t=tracking;muse,geometry,fused,bindings=derived_bundle(t,settings)
    if kind=='fused':artifact=fused.artifacts[0]
    elif kind=='geometry':artifact=geometry.artifacts[0]
    elif kind=='scene':artifact=geometry.artifacts[1]
    elif kind=='raw_muse':artifact=muse.artifacts[1]
    else:artifact=Repository(t.store).load(t.asset.project_id,'entity_bindings',bindings['component']).artifacts[0]
    t.store.path(t.asset.project_id,artifact.path).write_bytes(b'corrupt artifact for explicit recovery test')
    with pytest.raises(AppError) as exc:load_fused_observation(t.store,settings,t.asset.project_id,t.run.id,fused.id)
    assert exc.value.code=='CORRUPT_ARTIFACT'


def test_run_without_pinned_policy_cannot_publish_fused_results(client,settings,tracking):
    t=tracking
    payload=deepcopy(t.run.payload);payload.pop('evidence_policy_version')
    older=t.run.model_copy(update={'id':uuid4(),'payload':payload})
    Repository(t.store).publish(older)
    with pytest.raises(AppError) as exc:
        publish_fused_observation(t.store,settings,t.asset.project_id,older.id,'w000','place',t.prepared.planned_window.source_interval)
    assert exc.value.code=='EVIDENCE_POLICY_MISMATCH'


def test_new_explicit_selection_produces_new_derived_record_without_mutating_prior_evidence(client,settings,tracking):
    t=tracking;muse,geometry,fused,bindings=derived_bundle(t,settings)
    old=fused.model_dump_json();repo=Repository(t.store)
    previous=repo.load(t.asset.project_id,'entity_bindings',bindings['component'])
    selected=previous.payload['binding'];frame=t.prepared.source.frame(0,source_frame_index=t.prepared.window.source_frame_indices[4])
    changed=publish_binding(t.store,settings,t.asset.project_id,t.run.id,previous.payload['batch_id'],{
        'batch_fingerprint':selected['batch_fingerprint'],'candidate_id':selected['candidate_id'],'segment_id':selected['segment_id'],
        'evidence_frame_id':frame.evidence_frame_id,'input_frame_index':4})
    bindings['component']=changed.id
    updated=publish_fused_observation(t.store,settings,t.asset.project_id,t.run.id,'w000','place',t.prepared.planned_window.source_interval,
        muse_id=muse.id,geometry_id=geometry.id,binding_ids=bindings)
    assert updated.id!=fused.id and updated.payload['muse']==fused.payload['muse']
    assert repo.load(t.asset.project_id,'observations',fused.id).model_dump_json()==old
    assert load_fused_observation(t.store,settings,t.asset.project_id,t.run.id,updated.id)[1].verdict=='true'


def test_evidence_index_reports_corruption_without_hiding_healthy_records(client,settings,tracking):
    t=tracking;muse,geometry,fused,_=derived_bundle(t,settings)
    t.store.path(t.asset.project_id,fused.artifacts[0].path).write_bytes(b'damaged fused view')
    root=f'/api/projects/{t.asset.project_id}/runs/{t.run.id}'
    listing=client.get(root+'/evidence').json()
    assert {item['kind'] for item in listing['items']}=={'muse_observations','geometry_evidence'}
    assert listing['errors'][0]['code']=='CORRUPT_ARTIFACT'
    assert client.get(root+'/observations/'+str(fused.id)).status_code==409


def test_spatial_measurement_frames_resolve_to_actual_original_pts(client,settings,tracking):
    from app.engine.entity_tracks import ENTITY_ADAPTER_VERSION
    from app.engine.muse_observations import OBSERVATION_ADAPTER_VERSION,prepare_window
    from app.engine.planning import plan_run
    from app.programs.compiler import Compilation
    from app.programs.repository import approve_program,save_compilation
    from app.programs.validation import validate_program
    from app.repository import fingerprint
    from app.runs import snapshot_run
    from test_program_ast import draft
    t=tracking;raw=draft();raw['instruction']+=' Only count components starting in the left image half.'
    raw['zones']=[{'id':'left','description':'Reviewed left image half','x':0.,'y':0.,'width':.5,'height':1.}]
    raw['steps'].append({'id':'left_start','predicate':'inside','entities':['component'],'zone_id':'left','anchor':'cycle_start'})
    raw['clauses']=[{'start_char':0,'end_char':len(raw['instruction']),'text':raw['instruction'],'status':'represented',
        'represented_by':[{'kind':'step','id':i} for i in ('place','use','left_start')]}]
    compilation=Compilation(original_instruction=raw['instruction'],source={'duration_ms':t.asset.metadata.duration_ms,
        'width':320,'height':180,'audio_present':False},draft={'instruction':raw['instruction'],'ast':raw},
        validation=validate_program(raw,source_duration_ms=t.asset.metadata.duration_ms,require_clauses=True),
        compiler={'origin':'test_fixture','compiler_version':'A29-geometry-fixture','prompt_version':'fixture'},latency_seconds=0,
        provider_output={'fixture_only':True})
    _,program=save_compilation(t.store,t.asset,uuid4(),compilation)
    approve_program(t.store,settings,t.asset.project_id,program.id,{'version':1,'program_fingerprint':fingerprint(program),'source_sha256':t.asset.sha256})
    run=snapshot_run(t.store,settings,t.asset.project_id,program.id,1,muse_adapter_version=OBSERVATION_ADAPTER_VERSION,
        sam_adapter_version=ENTITY_ADAPTER_VERSION,evaluator_version='A29-no-temporal-evaluation')
    plan=plan_run(t.store,settings,t.asset.project_id,run.id);prepared=prepare_window(t.store,settings,t.asset.project_id,run.id,plan.id,'w000')
    text=wire(range(len(prepared.window.source_frame_indices)))
    batch=normalize_entity_tracks(prepared,'component',text,{'provider':'test_fixture','response_id':'A29-left-geometry',
        'recording_scope':'Explicit mock boxes for source-frame API qualification'})
    track=publish_entity_batch(t.store,settings,t.asset.project_id,run.id,'w000',batch,raw_text=text)
    record=publish_geometry(t.store,settings,t.asset.project_id,run.id,'left_start',{'component':(track.id,batch.candidates[0].id)})
    root=f'/api/projects/{t.asset.project_id}/runs/{run.id}/geometry/{record.id}'
    view=client.get(root)
    assert view.status_code==200 and view.json()['measurement_links']
    frames=client.get(view.json()['measurement_links'][0]['frames_url'])
    assert frames.status_code==200,frames.text
    body=frames.json()
    assert body['measurement']['source_start_pts']==body['frames'][0]['source_pts']
    assert client.get(body['frames'][0]['image_url']).status_code==200
    assert client.get(root+'/measurements/'+str(uuid4())+'/frames').status_code==404
