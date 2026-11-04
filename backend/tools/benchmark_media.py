"""Opt-in local generated-media benchmark. Makes no model/API calls."""
from dataclasses import replace
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient
from app.config import Settings
from app.main import create_app
from app.models import Asset
from app.sampling import coarse_windows

root=Path(__file__).resolve().parents[2]
directory=root/'.tmp/media-benchmark'
directory.mkdir(parents=True,exist_ok=True)
settings=replace(Settings.from_env(),data_dir=directory/'data',model_api_key='')
report={'scope':'generated media; local ingest and coarse sampling only; no recognition or cloud latency claim',
        'platform':platform.platform(),'python':platform.python_version(),'logical_cpus':os.cpu_count(),
        'ffmpeg':subprocess.run(['ffmpeg','-version'],capture_output=True,text=True,check=True).stdout.splitlines()[0],
        'cases':[],'peak_memory':'not measured'}
with TestClient(create_app(settings)) as client:
    for seconds,width,height in [(10,1280,720),(30,1920,1080),(120,1280,720)]:
        clip=directory/f'{seconds}s-{height}p.mp4'
        subprocess.run(['ffmpeg','-v','error','-y','-f','lavfi','-i',f'testsrc2=size={width}x{height}:rate=30:duration={seconds}',
                        '-c:v','libx264','-preset','veryfast','-crf','24','-threads','2',str(clip)],check=True,timeout=300)
        project=client.post('/api/projects',json={'name':f'Local benchmark {seconds}s {height}p'}).json()['project_id']
        started=time.monotonic()
        response=client.post(f'/api/projects/{project}/upload',content=clip.read_bytes(),headers={'Content-Type':'video/mp4'})
        assert response.status_code==202,response.text
        job_id=response.json()['job_id'];deadline=time.monotonic()+900;step=None
        while time.monotonic()<deadline:
            job=client.get('/api/jobs/'+job_id).json()
            if job['step']!=step:
                step=job['step'];print(json.dumps({'source_seconds':seconds,'height':height,'stage':step}),flush=True)
            if job['status'] in ('failed','completed','interrupted'):break
            time.sleep(.25)
        assert job['status']=='completed',job.get('error')
        ingest_seconds=time.monotonic()-started
        asset=Asset.model_validate(client.get(f'/api/projects/{project}').json()['asset'])
        sampled=time.monotonic()
        windows=coarse_windows(client.app.state.store,settings,asset)
        from datetime import datetime
        durations=[{'stage':entry['step'],'seconds':round((datetime.fromisoformat(entry['finished_at'].replace('Z','+00:00'))-
                    datetime.fromisoformat(entry['started_at'].replace('Z','+00:00'))).total_seconds(),3)} for entry in job['stage_history']]
        item={'source_seconds':seconds,'width':width,'height':height,'fps':30,'source_bytes':clip.stat().st_size,
              'ingest_seconds':round(ingest_seconds,3),'sample_seconds':round(time.monotonic()-sampled,3),
              'window_count':len(windows),'window_frames':sum(len(w.source_frame_indices) for w in windows),
              'project_bytes':sum(p.stat().st_size for p in client.app.state.store.project_dir(project).rglob('*') if p.is_file()),
              'stage_durations':durations,'project_id':project}
        report['cases'].append(item)
        (directory/'report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
        print(json.dumps(item),flush=True)
