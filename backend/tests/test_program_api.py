from dataclasses import replace
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from app.errors import AppError
from app.main import create_app
from app.repository import Repository
from app.programs.repository import save_compilation
from app.programs.validation import ProgramValidation,ValidationIssue,validate_program,VALIDATOR_VERSION
from app.runs import snapshot_run
from test_api import wait_job
from test_evidence import prepare
from test_program_compiler import provider_proposal, run_recorded_contract


def queued_compile(client,asset,key='fixture-request'):
    return client.post('/api/programs/compile',json={'project_id':str(asset.project_id),
        'source_asset_id':str(asset.id),'instruction':provider_proposal()['instruction']},headers={'Idempotency-Key':key})


def test_revalidation_preserves_proposal_and_pins_parent_without_rewriting_history(client,settings,media):
    asset=prepare(client,media['silent']);store=client.app.state.store
    actual=run_recorded_contract(replace(settings,model_api_key='fixture-key'),provider_proposal())
    old=actual.model_copy(update={'validator_version':'program-validation-1',
        'validation':ProgramValidation(issues=[ValidationIssue(code='LEGACY_CHECKER_FIXTURE',message='Explicit cached-validator failure fixture')])})
    parent,_=save_compilation(store,asset,uuid4(),old)
    updated=old.model_copy(update={'validation':validate_program(old.draft.ast,source_duration_ms=asset.metadata.duration_ms,require_clauses=True),
        'validator_version':VALIDATOR_VERSION,'revalidation_of':parent.id})
    record,program=save_compilation(store,asset,uuid4(),updated)
    assert Repository(store).load(asset.project_id,'compilations',parent.id)==parent
    assert record.payload['compilation']['revalidation_of']==str(parent.id)
    assert not record.references and len(record.artifacts)==1
    assert record.artifacts[0].path==f'compilations/{parent.id}.json'
    assert client.get(f'/api/programs/{program.id}').json()['outcome']['state']=='ready_for_review'
    changed=updated.model_copy(update={'provider_output':{**updated.provider_output,'explanation':'Changed proposal'}})
    with pytest.raises(AppError) as exc:save_compilation(store,asset,uuid4(),changed)
    assert exc.value.code=='REVALIDATION_MISMATCH'
    store.path(asset.project_id,record.artifacts[0].path).write_bytes(b'damaged cached compilation')
    with pytest.raises(AppError) as exc:Repository(store).load(asset.project_id,'compilations',record.id)
    assert exc.value.code=='CORRUPT_ARTIFACT'


def fixture_adapter(settings,output):
    def compile_mock(cloud,instruction,source,*,record_response=None):
        result=run_recorded_contract(settings,output)
        if record_response:
            record_response(result.provider_output,{'provider':'meta_cloud','response_id':'fixture-response',
                'model':settings.muse_model,'requested_model':settings.muse_model,'usage':{},'endpoint':settings.meta_api_base_url})
        return result
    return compile_mock


def test_compile_inspect_exact_review_and_run_snapshot_binding(settings,media,monkeypatch):
    settings=replace(settings,model_api_key='fixture-server-key')
    monkeypatch.setattr('app.programs.routes.compile_instruction',fixture_adapter(settings,provider_proposal()))
    with TestClient(create_app(settings)) as client:
        asset=prepare(client,media['silent'])
        response=queued_compile(client,asset)
        assert response.status_code==202,response.text
        job=wait_job(client,response.json()['job_id'])
        assert job['status']=='completed',job
        assert job['result']['outcome']['state']=='ready_for_review'
        repeat=queued_compile(client,asset)
        assert repeat.json()['reused'] and repeat.json()['job_id']==job['id']
        program_id=job['result']['program_id']
        info=client.get(f'/api/programs/{program_id}').json()
        assert not info['review_approved'] and info['versions']==[1]
        assert info['program']['ast']['instruction']==provider_proposal()['instruction']
        assert 'fixture-server-key' not in str(info)
        store=client.app.state.store
        args=dict(muse_adapter_version='test',sam_adapter_version='test',evaluator_version='test-no-evaluation')
        with pytest.raises(AppError) as exc:snapshot_run(store,settings,asset.project_id,program_id,1,**args)
        assert exc.value.code=='PROGRAM_REVIEW_REQUIRED'
        review={'version':1,'program_fingerprint':info['program_fingerprint'],'source_sha256':asset.sha256}
        stale=client.post(f'/api/programs/{program_id}/review',json={**review,'program_fingerprint':'0'*64})
        assert stale.status_code==409 and stale.json()['code']=='STALE_REVIEW'
        approved=client.post(f'/api/programs/{program_id}/review',json=review)
        assert approved.status_code==201,approved.text
        assert client.get(f'/api/programs/{program_id}').json()['review_approved'] is True
        run=snapshot_run(store,settings,asset.project_id,program_id,1,**args)
        assert run.payload['review']['id']==approved.json()['review_id']
        assert len(run.references)==1 and run.references[0].kind=='program_reviews'
        old=Repository(store).load(asset.project_id,'programs',program_id,1)
        payload=old.model_dump(mode='json')['payload']
        payload['program']['version']=2
        payload['program']['explanation']='New version; prior review must not transfer'
        second=old.model_copy(update={'version':2,'payload':payload})
        Repository(store).publish(second)
        assert client.get(f'/api/programs/{program_id}?version=1').json()['review_approved'] is True
        assert client.get(f'/api/programs/{program_id}?version=2').json()['review_approved'] is False
        with pytest.raises(AppError):snapshot_run(store,settings,asset.project_id,program_id,2,**args)
        assert Repository(store).load(asset.project_id,'runs',run.id)==run


def test_material_clarification_is_inspectable_but_cannot_be_approved(settings,media,monkeypatch):
    settings=replace(settings,model_api_key='fixture-key')
    output=provider_proposal();output['clarifications']=[{'code':'ZONE_AMBIGUOUS','message':'Confirm the left zone boundary','material':True}]
    monkeypatch.setattr('app.programs.routes.compile_instruction',fixture_adapter(settings,output))
    with TestClient(create_app(settings)) as client:
        asset=prepare(client,media['silent'])
        job=wait_job(client,queued_compile(client,asset).json()['job_id'])
        program_id=job['result']['program_id']
        info=client.get(f'/api/programs/{program_id}').json()
        assert info['outcome']['state']=='clarification_required'
        response=client.post(f'/api/programs/{program_id}/review',json={'version':1,
            'program_fingerprint':info['program_fingerprint'],'source_sha256':asset.sha256})
        assert response.status_code==409 and response.json()['code']=='PROGRAM_NOT_REVIEWABLE'


def test_provider_failure_persists_sanitized_compilation_without_fabricated_program(settings,media,monkeypatch):
    settings=replace(settings,model_api_key='fixture-key')
    def fail(*args,**kwargs):raise AppError('PROVIDER_AUTH','Provider rejected the credential',503)
    monkeypatch.setattr('app.programs.routes.compile_instruction',fail)
    with TestClient(create_app(settings)) as client:
        asset=prepare(client,media['silent'])
        request=queued_compile(client,asset)
        job=wait_job(client,request.json()['job_id'])
        assert job['status']=='failed' and job['error']['code']=='PROVIDER_AUTH'
        record=client.get(f'/api/compilations/{job["id"]}').json()
        assert record['payload']['state']=='provider_failure'
        assert not list(client.app.state.store.path(asset.project_id,'programs').glob('*/v*.json'))
        assert client.get(f'/api/assets/{asset.id}/stream').status_code==200


def test_foreign_source_cannot_queue_a_compiler_job(settings,media):
    settings=replace(settings,model_api_key='fixture-key')
    with TestClient(create_app(settings)) as client:
        asset=prepare(client,media['silent'])
        other=client.post('/api/projects',json={'name':'other'}).json()['project_id']
        response=client.post('/api/programs/compile',json={'project_id':other,
            'source_asset_id':str(asset.id),'instruction':'Any visual instruction'})
        assert response.status_code==404
        assert not list(client.app.state.store.path(other,'jobs').glob('*.json'))


def test_assumption_confirmation_is_audited_immutable_idempotent_and_not_approval(settings,media,monkeypatch):
    settings=replace(settings,model_api_key='fixture-key')
    output=provider_proposal();output['ast']['assumptions'][0]['material']=True
    monkeypatch.setattr('app.programs.routes.compile_instruction',fixture_adapter(settings,output))
    with TestClient(create_app(settings)) as client:
        asset=prepare(client,media['silent'])
        job=wait_job(client,queued_compile(client,asset).json()['job_id'])
        program_id=job['result']['program_id'];url=f'/api/programs/{program_id}'
        before=client.get(url).json()
        request={'version':1,'program_fingerprint':before['program_fingerprint'],'source_sha256':asset.sha256,
                 'accepted_codes':['DEFAULT_TIMING']}
        bad=client.post(url+'/confirm-assumptions',json={**request,'accepted_codes':['NOT_AN_ASSUMPTION']})
        assert bad.status_code==422
        saved=client.post(url+'/confirm-assumptions',json=request)
        assert saved.status_code==201,saved.text
        assert saved.json()['version']==2 and saved.json()['review_approved'] is False
        repeated=client.post(url+'/confirm-assumptions',json=request)
        assert repeated.json()['version']==2
        old=client.get(url+'?version=1').json();new=client.get(url+'?version=2').json()
        assert old['program']==before['program'] and old['program_fingerprint']==before['program_fingerprint']
        assert old['outcome']['state']=='clarification_required'
        assert new['outcome']['state']=='ready_for_review' and not new['review_approved']
        assert new['program']['compiler']['origin']=='user_edit'
        assert new['human_edit']['base_fingerprint']==before['program_fingerprint']
        assert new['human_edit']['confirmed_assumptions'][0]['material'] is True
        approved=client.post(url+'/review',json={'version':2,'program_fingerprint':new['program_fingerprint'],'source_sha256':asset.sha256})
        assert approved.status_code==201


def test_assumption_confirmation_cannot_clear_a_distinct_material_ambiguity(settings,media,monkeypatch):
    settings=replace(settings,model_api_key='fixture-key')
    output=provider_proposal();output['ast']['assumptions'][0]['material']=True
    output['ast']['ambiguities']=[{'code':'WHICH_OBJECT','message':'Identify the intended component','material':True}]
    monkeypatch.setattr('app.programs.routes.compile_instruction',fixture_adapter(settings,output))
    with TestClient(create_app(settings)) as client:
        asset=prepare(client,media['silent'])
        job=wait_job(client,queued_compile(client,asset).json()['job_id'])
        url=f'/api/programs/{job["result"]["program_id"]}'
        info=client.get(url).json()
        response=client.post(url+'/confirm-assumptions',json={'version':1,'program_fingerprint':info['program_fingerprint'],
            'source_sha256':asset.sha256,'accepted_codes':['DEFAULT_TIMING']})
        assert response.status_code==201
        assert response.json()['outcome']['state']=='clarification_required'
        info=client.get(url).json()
        assert client.post(url+'/review',json={'version':2,'program_fingerprint':info['program_fingerprint'],
            'source_sha256':asset.sha256}).status_code==409


def test_snapshots_use_reviewed_policies_and_reject_unreviewed_runtime_overrides(settings,media,monkeypatch):
    settings=replace(settings,model_api_key='fixture-key')
    output=provider_proposal()
    output['ast']['cycle']['policy']={'max_cycle_ms':5000,'max_gap_ms':4000}
    monkeypatch.setattr('app.programs.routes.compile_instruction',fixture_adapter(settings,output))
    with TestClient(create_app(settings)) as client:
        asset=prepare(client,media['silent'])
        job=wait_job(client,queued_compile(client,asset).json()['job_id'])
        program_id=job['result']['program_id'];url=f'/api/programs/{program_id}'
        info=client.get(url).json()
        assert client.post(url+'/review',json={'version':1,'program_fingerprint':info['program_fingerprint'],
            'source_sha256':asset.sha256}).status_code==201
        args=dict(muse_adapter_version='fixture',sam_adapter_version='fixture',evaluator_version='fixture')
        run=snapshot_run(client.app.state.store,settings,asset.project_id,program_id,1,**args)
        assert run.payload['temporal_policy']['max_cycle_ms']==5000
        assert run.payload['temporal_policy']['max_gap_ms']==4000
        from app.programs.policy import TemporalPolicy, UnknownPolicy
        for overrides in ({'temporal_policy':TemporalPolicy()}, {'unknown_policy':UnknownPolicy(observation='allow_estimated')}):
            with pytest.raises(AppError) as exc:
                snapshot_run(client.app.state.store,settings,asset.project_id,program_id,1,**args,**overrides)
            assert exc.value.code=='REVIEW_POLICY_MISMATCH'
