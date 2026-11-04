"""Opt-in live Muse/SAM/upload/render proof using a generated motion clip.

Run from the project root after installing requirements-test.txt and configuring .env.
Synthetic geometry validates integration, not real-world action recognition.
"""
from pathlib import Path
import json,sys,time
from dataclasses import replace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient
from app.config import Settings
from app.main import create_app

root=Path(__file__).resolve().parents[2]
settings=replace(Settings.from_env(),data_dir=root/'.tmp/live-smoke-data')
# This command deliberately makes billable live cloud calls. It is never run by pytest.
import subprocess
clip=root/'.tmp/live-smoke-input.mp4'
clip.parent.mkdir(parents=True,exist_ok=True)
subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-y','-f','lavfi','-i',
               "color=c=black:s=320x180:r=15:d=4[bg];color=c=red:s=40x40:r=15:d=4[box];[bg][box]overlay=x='20+50*t':y=70:shortest=1",
               '-c:v','libx264','-pix_fmt','yuv420p',str(clip)],check=True,timeout=20)
def wait(client,job_id):
    deadline=time.monotonic()+500;step=None
    while time.monotonic()<deadline:
        response=client.get('/api/jobs/'+job_id)
        job=response.json()
        if job['step']!=step:
            step=job['step'];print(json.dumps({'job':job['type'],'stage':step}),flush=True)
        if job['status'] in ('completed','failed','interrupted'):
            if job['status']!='completed':print(json.dumps({'error':job['error']}),flush=True)
            return job
        time.sleep(.5)
    raise RuntimeError('Active job exceeded smoke deadline; inspect persisted state before retrying')
with TestClient(create_app(settings)) as client:
    prompt='Extract the red square moving to the right. Keep it centered, with brief slow motion at the middle.'
    project=client.post('/api/projects',json={'name':'Live Meta action flow','action_prompt':prompt}).json()['project_id']
    accepted=client.post(f'/api/projects/{project}/upload',content=clip.read_bytes(),headers={'Content-Type':'video/mp4'})
    assert accepted.status_code==202,accepted.text
    assert wait(client,accepted.json()['job_id'])['status']=='completed'
    request=client.post(f'/api/projects/{project}/analyze',json={'action_prompt':prompt,'requested_duration_ms':15000})
    assert request.status_code==200,request.text
    analyzed=wait(client,request.json()['job_id'])
    assert analyzed['status']=='completed',analyzed['error']
    board=client.get(f'/api/projects/{project}/events').json()
    print(json.dumps({'events':len(board['events']),'scan_complete':board['scan_complete'],
                      'tracked':[bool(e['primary_track_id']) for e in board['events']],
                      'phases':[[p['label'] for p in e['phases']] for e in board['events']]}),flush=True)
    assert board['events'],'No action candidates from generated motion'
    response=client.post(f'/api/projects/{project}/reels',json={'requested_duration_ms':15000})
    assert response.status_code==202,response.text
    rendered=wait(client,response.json()['job_id'])
    assert rendered['status']=='completed',rendered['error']
    exports=client.get(f'/api/projects/{project}/exports').json()['exports']
    assert exports and exports[0]['origin']=='autonomous'
    assert client.get(exports[0]['stream_url'],headers={'Range':'bytes=0-99'}).status_code==206
    (root/'.tmp/live-smoke-report.json').write_text(json.dumps({'project_id':project,'analysis':analyzed,
                                                               'render':rendered,'events':board,'exports':exports},indent=2))
    print(json.dumps({'live_cloud_flow':True,'export_count':len(exports),'project_id':project}),flush=True)
