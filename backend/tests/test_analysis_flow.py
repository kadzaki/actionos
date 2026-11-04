"""Real local-media orchestration with explicit mocked cloud contracts, no network."""
from dataclasses import replace
from pathlib import Path
import subprocess
from uuid import UUID

from fastapi.testclient import TestClient
import pytest

from app.main import create_app
from app.models import Occurrence
from app.providers.muse import Candidate,CandidateBatch,IntentDraft
from app.reconciliation import CandidateGroup,Reconciliation,AnalysisRecord
from test_api import wait_job


@pytest.mark.parametrize('failure',[None,'incomplete','missing_candidate'])
def test_real_multiwindow_analysis_reconciles_before_selecting_and_never_publishes_incomplete(settings,tmp_path,monkeypatch,failure):
    settings=replace(settings,max_video_seconds=20,model_api_key='unit-test-no-network')
    clip=tmp_path/'multiwindow.mp4'
    subprocess.run(['ffmpeg','-v','error','-y','-f','lavfi','-i','testsrc2=size=160x90:rate=15:duration=14',
                    '-c:v','libx264','-threads','2',str(clip)],check=True,timeout=30)
    class Cloud:
        def __init__(self,*_):pass
        def __enter__(self):return self
        def __exit__(self,*_):pass
    monkeypatch.setattr('app.analysis.MetaCloud',Cloud)
    draft=IntentDraft(action_description='robot arm turns',subjects=['robot arm'],occurrence=Occurrence(mode='ordinal',ordinal=2))
    monkeypatch.setattr('app.analysis.muse.interpret',lambda *_:(draft,{'provider':'mocked_unit_contract'}))
    seen=[];selected=[]
    def candidate(start,end,label):
        return Candidate(action_label=label,start_ms=start,end_ms=end,primary_subject='robot arm',
                         match_reason='unit contract, not real-world recognition',evidence_ms=[start+100])
    def discovery(cloud,path,window,intent):
        assert Path(path).is_file()
        seen.append(window)
        items=[candidate(10500,11700,'arm turns')] if window.requested_in_ms==0 else [candidate(500,1700,'robot rotates'),candidate(3000,3800,'arm turns again')]
        return CandidateBatch(candidates=items,coverage_complete=failure!='incomplete'),{'provider':'mocked_unit_contract'}
    monkeypatch.setattr('app.analysis.muse.candidates',discovery)
    def reconcile(cloud,intent,events,observations):
        assert len(events)==3 and len(observations)==2
        groups=[CandidateGroup(candidate_ids=[e.id for e in events[:2]],matches_intent=True,action_label='arm turn',match_reason='same overlap occurrence')]
        if failure!='missing_candidate':groups.append(CandidateGroup(candidate_ids=[events[2].id],matches_intent=True,action_label='arm turn',match_reason='second occurrence'))
        return Reconciliation(groups=groups),{'provider':'mocked_unit_contract'}
    monkeypatch.setattr('app.analysis.muse.reconcile',reconcile)
    def refine(store,settings,cloud,asset,intent,event,stage):selected.append(event);return event
    monkeypatch.setattr('app.analysis.refine_event',refine)
    with TestClient(create_app(settings)) as client:
        project=client.post('/api/projects',json={'name':'orchestration contract test'}).json()['project_id']
        uploaded=client.post(f'/api/projects/{project}/upload',content=clip.read_bytes(),headers={'Content-Type':'video/mp4'})
        assert wait_job(client,uploaded.json()['job_id'])['status']=='completed'
        response=client.post(f'/api/projects/{project}/analyze',json={'action_prompt':'the second arm turn','requested_duration_ms':15000})
        job=wait_job(client,response.json()['job_id'])
        board=client.get(f'/api/projects/{project}/events').json()
        records=client.app.state.store.list_records(project,'analyses',AnalysisRecord)
        assert len(records)==1 and len(records[0].candidates)==3
        assert len(seen)==2
        if failure:
            assert job['status']=='failed' and not board['scan_complete'] and board['events']==[] and selected==[]
            assert records[0].status in ('incomplete','reconciliation_failed')
        else:
            assert job['status']=='completed' and board['scan_complete']
            assert len(board['events'])==2 and len(selected)==1
            assert selected[0].start_ms==13000
            assert UUID(job['result']['analysis_id'])==records[0].id
            assert 'reconciling' in [s['step'] for s in job['stage_history']]
