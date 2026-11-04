"""Opt-in real cloud test of overlapping windows and repeated-occurrence selection.

Makes billable Meta requests. Generated motion proves integration, not cross-domain recognition.
"""
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient
from app.config import Settings
from app.main import create_app
root=Path(__file__).resolve().parents[2]
folder=root/'.tmp/live-occurrences';folder.mkdir(parents=True,exist_ok=True)
clip=folder/'repeated-motion.mp4'
subprocess.run(['ffmpeg','-v','error','-y','-f','lavfi','-i',
    "color=c=black:s=320x180:r=15:d=16[bg];color=c=red:s=40x40:r=15:d=16[box];[bg][box]overlay=x='20+75*mod(t,4)':y=70:enable='lt(mod(t,4),3)':shortest=1",
    '-c:v','libx264','-threads','2','-pix_fmt','yuv420p',str(clip)],check=True,timeout=30)
settings=replace(Settings.from_env(),data_dir=folder/'data')
def wait(client,jid):
    deadline=time.monotonic()+600;step=None
    while time.monotonic()<deadline:
        job=client.get('/api/jobs/'+jid).json()
        if job['step']!=step:step=job['step'];print(json.dumps({'job':job['type'],'stage':step}),flush=True)
        if job['status'] in ('completed','failed','interrupted'):return job
        time.sleep(.5)
    raise RuntimeError('Inspect active persisted job before retrying')
with TestClient(create_app(settings)) as client:
    project=client.post('/api/projects',json={'name':'Live repeated-occurrence integration'}).json()['project_id']
    uploaded=client.post(f'/api/projects/{project}/upload',content=clip.read_bytes(),headers={'Content-Type':'video/mp4'})
    assert uploaded.status_code==202,uploaded.text
    assert wait(client,uploaded.json()['job_id'])['status']=='completed'
    prompt='Extract the second distinct episode in which a red square travels from left to right. Separate episodes are divided by a black screen. Keep normal speed.'
    analyzed=client.post(f'/api/projects/{project}/analyze',json={'action_prompt':prompt,'requested_duration_ms':15000})
    assert analyzed.status_code==200,analyzed.text
    job=wait(client,analyzed.json()['job_id'])
    board=client.get(f'/api/projects/{project}/events').json()
    report={'project_id':project,'analysis':job,'board':board}
    (folder/'report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    assert job['status']=='completed',job.get('error')
    print(json.dumps({'intervals':[(e['start_ms'],e['end_ms']) for e in board['events']],
                      'selected':job['result'].get('selected_event_ids'),'scan_complete':board['scan_complete']}),flush=True)
    assert board['scan_complete'] and len(board['events'])==4,'Expected four distinct generated traversals'
    selected=next(e for e in board['events'] if e['id'] in job['result']['selected_event_ids'])
    assert 3500<=selected['start_ms']<=4500 and 6500<=selected['end_ms']<=7500,'Second occurrence timing differs from generated source'
    response=client.post(f'/api/projects/{project}/reels',json={'requested_duration_ms':15000,'preview':True})
    assert response.status_code==202,response.text
    rendered=wait(client,response.json()['job_id']);report['render']=rendered
    (folder/'report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    assert rendered['status']=='completed',rendered.get('error')
    plan=client.get('/api/plans/'+rendered['result']['plan_ids'][0]).json()
    assert plan['selected_event_ids']==[selected['id']]
    print(json.dumps({'live_multiwindow_occurrence':True,'distinct_events':4,'second_selected_and_rendered':True}),flush=True)
