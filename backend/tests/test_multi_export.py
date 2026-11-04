"""Known interval fixtures exercise real rendering/recovery, not cloud recognition."""
from fastapi.testclient import TestClient
import pytest

from app.errors import AppError
from app.main import create_app
from app.models import ActionEvent,ActionIntent,Occurrence,Project
from test_api import wait_job
from test_evidence import prepare


@pytest.mark.parametrize('fail_second',[False,True])
def test_all_matches_publish_only_verified_outputs_and_preserve_partial_result_on_failure(settings,media,monkeypatch,fail_second):
    from app.rendering import render
    calls=[]
    def guarded_render(*args,**kwargs):
        calls.append(True)
        if fail_second and len(calls)==2:raise AppError('RENDER_FAILED','Simulated failure on second known interval',422)
        return render(*args,**kwargs)
    monkeypatch.setattr('app.director.render',guarded_render)
    with TestClient(create_app(settings)) as client:
        asset=prepare(client,media['silent']);store=client.app.state.store
        intent=ActionIntent(project_id=asset.project_id,source_asset_id=asset.id,original_prompt='every map unfolding',
                            action_description='map unfolds',occurrence=Occurrence(mode='all'),provider_metadata={'scan_complete':True,'origin':'unit_interval_fixture'})
        store.save(asset.project_id,'intents',intent)
        project=store.load(asset.project_id,'project',Project);project.active_intent_id=intent.id;store.save(project.id,'project',project)
        events=[]
        for start,end in [(0,500),(800,1400)]:
            event=ActionEvent(project_id=project.id,source_asset_id=asset.id,intent_id=intent.id,action_label='map unfolds',
                              start_ms=start,end_ms=end,evidence_ms=[start+100],match_reason='Known interval test fixture; no model inference claim')
            store.save(project.id,'events',event);events.append(event)
        response=client.post(f'/api/projects/{project.id}/reels',json={'requested_duration_ms':15000,'allow_fixed_crop':True,'preview':True})
        assert response.status_code==202,response.text
        job=wait_job(client,response.json()['job_id'])
        expected=1 if fail_second else 2
        assert job['status']==('failed' if fail_second else 'completed')
        assert job['result']['completed_count']==expected and job['result']['requested_count']==2
        assert job['result']['complete'] is not fail_second
        assert job['result']['completed_event_ids']==[str(e.id) for e in events[:expected]]
        if fail_second:
            assert job['result']['failed_event_id']==str(events[1].id)
            assert job['error']['code']=='RENDER_FAILED'
        outputs=client.get(f'/api/projects/{project.id}/exports').json()['exports']
        assert len(outputs)==expected
        for plan_id,event in zip(job['result']['plan_ids'],events):
            plan=client.get('/api/plans/'+plan_id).json()
            assert plan['selected_event_ids']==[str(event.id)] and plan['segments'][0]['crop']['mode']=='fixed'
        assert client.get(outputs[0]['stream_url'],headers={'Range':'bytes=0-99'}).status_code==206
        assert client.get(f'/api/projects/{project.id}').json()['asset']['status']=='ready'
    with TestClient(create_app(settings)) as client:
        persisted=client.get('/api/jobs/'+job['id']).json()
        assert persisted['result']==job['result'] and persisted['status']==job['status']
        assert len(client.get(f'/api/projects/{project.id}/exports').json()['exports'])==expected
