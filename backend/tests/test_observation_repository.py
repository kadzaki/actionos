"""A29 storage tests use labeled mock observations on owned generated media."""
from copy import deepcopy
import json
from uuid import uuid4

import pytest

from app.engine.evidence_storage import run_perception_usage
from app.engine.observation_repository import load_muse_record,publish_muse_record
from app.errors import AppError
from app.programs.policy import LIMITS
from app.repository import Repository,encoded
from app.storage import FileStore
from test_entity_api import tracking
from test_step_observations import fixture_response,PROVENANCE


def publish(t,settings,output=None,**kwargs):
    if output is None:output=fixture_response(t.prepared)
    return publish_muse_record(t.store,settings,t.asset.project_id,t.run.id,'w000',output,PROVENANCE,
        origin='developer_fixture',evidence_scope='Hand-authored A29 QA observations; no live tabletop recognition',**kwargs)


def test_source_grounded_batch_reloads_after_restart_and_reuses_immutable_record(client,settings,tracking):
    t=tracking;record=publish(t,settings)
    assert record.payload['state']=='validated' and record.payload['observation_count']==1
    fresh=FileStore(settings.data_dir)
    saved,batch,prepared=load_muse_record(fresh,settings,t.asset.project_id,t.run.id,record.id)
    assert saved==record and batch.observations[0].source_sha256==t.asset.sha256
    assert batch.missing_step_ids==['use']
    assert all(c.negative_coverage_verified is False for c in batch.coverage)
    assert prepared.window.sha256==record.payload['input_sha256']
    assert publish(t,settings)==record
    assert run_perception_usage(fresh,t.asset.project_id,t.run.id)==(3,2)  # Two SAM candidate trajectories + one Muse hypothesis.
    paths={a.path for a in record.artifacts}
    assert prepared.window.path in paths and prepared.source.asset.timeline_path in paths
    for observation in batch.observations:
        for frame in observation.frames:
            assert f'evidence/{frame.evidence_frame_id}.json' in paths
            assert client.get(frame.image_url).status_code==200


def test_valid_empty_and_failed_provider_records_are_distinct(client,settings,tracking):
    t=tracking
    empty=fixture_response(t.prepared);empty['hypotheses']=[]
    empty_record=publish(t,settings,empty)
    _,batch,_=load_muse_record(t.store,settings,t.asset.project_id,t.run.id,empty_record.id)
    assert empty_record.payload['state']=='validated' and batch.observations==[]
    invalid=fixture_response(t.prepared);invalid['hypotheses'][0]['step_id']='foreign'
    bad=publish(t,settings,invalid)
    saved,batch,_=load_muse_record(t.store,settings,t.asset.project_id,t.run.id,bad.id)
    assert saved.payload['state']=='unavailable' and batch is None
    assert saved.payload['error']['code']=='PROVIDER_STEP'
    failure=publish(t,settings,error=AppError('PROVIDER_TIMEOUT','Explicit mock provider timeout',504,True))
    assert failure.payload['state']=='unavailable' and failure.payload['error']['code']=='PROVIDER_TIMEOUT'


def test_unparseable_reply_is_preserved_as_unavailable_and_never_exposed_as_valid_batch(client,settings,tracking):
    t=tracking;reply='```json\n{"truncated": <untrusted text>\n```'
    record=publish_muse_record(t.store,settings,t.asset.project_id,t.run.id,'w000',{'unparsed_text':reply},PROVENANCE,
        origin='developer_fixture',evidence_scope='Explicit malformed reply fixture, not live inference',
        error=AppError('PROVIDER_SCHEMA','Provider returned unparseable structured output',502,True))
    saved,batch,_=load_muse_record(FileStore(settings.data_dir),settings,t.asset.project_id,t.run.id,record.id)
    assert batch is None and saved.payload['state']=='unavailable' and saved.payload['observation_count']==0
    raw=json.loads(t.store.path(t.asset.project_id,record.artifacts[1].path).read_text(encoding='utf-8'))
    assert raw['output']=={'unparsed_text':reply}
    response=client.get(f'/api/projects/{t.asset.project_id}/runs/{t.run.id}/muse-observations/{record.id}')
    assert response.status_code==200 and response.json()['batch'] is None
    assert reply not in response.text


def test_stored_prompt_version_must_match_the_run_not_a_global_default(client,settings,tracking):
    t=tracking;record=publish(t,settings)
    assert record.payload['adapter_version']=='muse-step-observations-3'
    tampered=record.model_dump(mode='json');tampered['payload']['prompt_version']='program-window-evidence-1'
    t.store.write_bytes(Repository(t.store).path(t.asset.project_id,'muse_observations',record.id),encoded(tampered))
    with pytest.raises(AppError) as exc:load_muse_record(t.store,settings,t.asset.project_id,t.run.id,record.id)
    assert exc.value.code=='EVIDENCE_MISMATCH'


@pytest.mark.parametrize('kind',['raw','normalized','image','frame_metadata','timeline','video','source'])
def test_damaged_evidence_never_becomes_a_verified_empty_batch(client,settings,tracking,kind):
    t=tracking;record=publish(t,settings)
    _,batch,prepared=load_muse_record(t.store,settings,t.asset.project_id,t.run.id,record.id)
    if kind=='raw':path=record.artifacts[1].path
    elif kind=='normalized':path=record.artifacts[2].path
    elif kind=='image':path=next(a.path for a in record.artifacts if a.media_type=='image/jpeg')
    elif kind=='frame_metadata':path=f'evidence/{batch.observations[0].frames[0].evidence_frame_id}.json'
    elif kind=='timeline':path=prepared.source.asset.timeline_path
    elif kind=='video':path=prepared.window.path
    else:path=prepared.source.asset.source_path
    t.store.path(t.asset.project_id,path).write_bytes(b'corrupt QA artifact')
    with pytest.raises(AppError) as exc:load_muse_record(t.store,settings,t.asset.project_id,t.run.id,record.id)
    assert exc.value.code in ('CORRUPT_ARTIFACT','CORRUPT_RECORD')


def test_mutated_source_identity_and_provider_origin_are_rejected(client,settings,tracking):
    t=tracking;record=publish(t,settings)
    with pytest.raises(AppError):load_muse_record(t.store,settings,t.asset.project_id,uuid4(),record.id)
    with pytest.raises(AppError) as exc:
        publish_muse_record(t.store,settings,t.asset.project_id,t.run.id,'w000',fixture_response(t.prepared),PROVENANCE,
            origin='meta_cloud',evidence_scope='Must fail; fixture provenance is not a live provider call')
    assert exc.value.code=='PROVIDER_CONFIGURATION_MISMATCH'


def test_aggregate_budget_admission_counts_existing_sam_and_muse_records(client,settings,tracking,monkeypatch):
    t=tracking;record=publish(t,settings)
    samples,_=run_perception_usage(t.store,t.asset.project_id,t.run.id)
    monkeypatch.setitem(LIMITS,'observations',samples)
    raw=fixture_response(t.prepared);raw['notes']=['Different explicit mock attempt to exercise admission']
    before={p.name for p in t.store.path(t.asset.project_id,'muse_observations').glob('*.json')}
    with pytest.raises(AppError) as exc:publish(t,settings,raw)
    assert exc.value.code=='OBSERVATION_LIMIT'
    assert {p.name for p in t.store.path(t.asset.project_id,'muse_observations').glob('*.json')}==before
    assert Repository(t.store).load(t.asset.project_id,'muse_observations',record.id)==record


def test_tampered_manifest_count_does_not_hide_observations_on_reload(client,settings,tracking):
    t=tracking;record=publish(t,settings)
    raw=record.model_dump(mode='json');raw['payload']['observation_count']=0
    t.store.write_bytes(Repository(t.store).path(t.asset.project_id,'muse_observations',record.id),encoded(raw))
    with pytest.raises(AppError) as exc:load_muse_record(t.store,settings,t.asset.project_id,t.run.id,record.id)
    assert exc.value.code=='EVIDENCE_MISMATCH'


def test_damaged_unrelated_run_does_not_block_new_run_admission_or_pollute_its_index(client,settings,tracking):
    from app.engine.entity_tracks import ENTITY_ADAPTER_VERSION
    from app.engine.muse_observations import OBSERVATION_ADAPTER_VERSION
    from app.runs import snapshot_run
    t=tracking;old=publish(t,settings)
    t.store.path(t.asset.project_id,old.artifacts[1].path).write_bytes(b'damaged old run response')
    run=snapshot_run(t.store,settings,t.asset.project_id,t.program.id,1,muse_adapter_version=OBSERVATION_ADAPTER_VERSION,
        sam_adapter_version=ENTITY_ADAPTER_VERSION,evaluator_version='separate-run-QA')
    current=publish_muse_record(t.store,settings,t.asset.project_id,run.id,'w000',fixture_response(t.prepared),PROVENANCE,
        origin='developer_fixture',evidence_scope='Separate run with explicit mock hypotheses; old damaged run is preserved')
    assert load_muse_record(t.store,settings,t.asset.project_id,run.id,current.id)[1].observations
    response=client.get(f'/api/projects/{t.asset.project_id}/runs/{run.id}/evidence').json()
    assert response['errors']==[] and [i['id'] for i in response['items']]==[str(current.id)]
