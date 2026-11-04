"""Local integration only: no cloud requests or claimed video discoveries."""
from dataclasses import replace
from uuid import uuid4
from app.programs.repository import save_compilation
from app.providers.meta import MetaCloud
from test_evidence import prepare
from test_program_compiler import provider_proposal,run_recorded_contract
from test_api import wait_job


def test_review_plan_local_evaluation_patch_and_compatible_replay(client,settings,media,monkeypatch):
    asset=prepare(client,media['silent']);store=client.app.state.store
    compilation=run_recorded_contract(replace(settings,model_api_key='recorded-fixture-key'),provider_proposal())
    _,program=save_compilation(store,asset,uuid4(),compilation)
    url=f'/api/programs/{program.id}'
    info=client.get(url).json()
    request={'version':1,'program_fingerprint':info['program_fingerprint'],'mode':'plan_only'}
    assert client.post(url+'/run',json=request).status_code==409
    assert client.post(url+'/review',json={**{k:request[k] for k in ('version','program_fingerprint')},'source_sha256':asset.sha256}).status_code==201
    def forbidden(*args,**kwargs):raise AssertionError('Local evaluation must not call Meta')
    monkeypatch.setattr(MetaCloud,'__init__',forbidden)
    planned=client.post(url+'/run',json=request)
    assert planned.status_code==202,planned.text
    run_id=planned.json()['run_id']
    accepted=client.post(f'/api/runs/{run_id}/evaluate')
    job=wait_job(client,accepted.json()['job_id'])
    assert job['status']=='completed',job
    events=client.get(f'/api/runs/{run_id}/events').json()
    assert events['items']==[] and events['evaluation']['outcome']=='incomplete'
    revised=client.post(url+'/revise',json={'version':1,'program_fingerprint':request['program_fingerprint'],
        'operations':[{'op':'replace','path':'/selection/occurrences','value':'first'}]})
    assert revised.status_code==202,revised.text
    job=wait_job(client,revised.json()['job_id'])
    assert job['status']=='completed',job
    assert job['result']['diff']['invalidates']=='evaluate'
    updated=client.get(url).json()
    assert updated['program']['version']==2 and not updated['review_approved']
    body={'version':2,'program_fingerprint':updated['program_fingerprint'],'source_sha256':asset.sha256}
    assert client.post(url+'/review',json=body).status_code==201
    replay=client.post(url+'/run',json={'version':2,'program_fingerprint':updated['program_fingerprint'],'mode':'reuse','parent_run_id':run_id})
    assert replay.status_code==202,replay.text
    replay_job=wait_job(client,replay.json()['job_id'])
    assert replay_job['status']=='completed',replay_job
    assert client.get(f'/api/runs/{run_id}/events').json()==events
