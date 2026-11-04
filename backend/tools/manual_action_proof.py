"""A15 opt-in local proof: manual source event -> verified clip + JSON/HTML report.

Generated permitted media, no provider calls, no compiler/evaluator claims.
Run from the repository root. Serve the printed DATA_DIR to inspect artifacts.
"""
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient
from app.actions.contracts import EventEvidence, StepEvidence
from app.actions.execution import request_execution
from app.actions.registry import build_action_plan
from app.config import Settings
from app.main import create_app
from app.models import Asset
from app.programs.policy import SourceInterval
from app.repository import Envelope, Repository, reference
from app.runs import snapshot_run
from app.source_evidence import SourceEvidenceAdapter
from app.storage import file_sha256


def wait(client, job_id):
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        job = client.get(f'/api/jobs/{job_id}').json()
        if job['status'] in ('completed', 'failed', 'interrupted', 'cancelled'):
            if job['status'] != 'completed':
                raise RuntimeError(json.dumps(job['error']))
            return job
        time.sleep(.1)
    raise RuntimeError('Manual action proof exceeded its deadline')


def main():
    root = Path(__file__).resolve().parents[2]
    output = root / '.tmp/manual-action-proof'
    output.mkdir(parents=True, exist_ok=True)
    settings = replace(Settings.from_env(), data_dir=output/'data', model_api_key='')
    clip = output/'generated-source.mp4'
    subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y',
        '-f', 'lavfi', '-i', 'testsrc2=size=320x180:rate=24:duration=3',
        '-f', 'lavfi', '-i', 'sine=frequency=440:duration=3',
        '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-shortest', str(clip)],
        check=True, timeout=20)
    with TestClient(create_app(settings)) as client:
        project_id = client.post('/api/projects', json={'name':'MANUAL DEVELOPER FIXTURE - ActionOS output proof'}).json()['project_id']
        accepted = client.post(f'/api/projects/{project_id}/upload', content=clip.read_bytes(),
                               headers={'Content-Type':'video/mp4'})
        accepted.raise_for_status()
        wait(client, accepted.json()['job_id'])
        asset = Asset.model_validate(client.get(f'/api/projects/{project_id}').json()['asset'])
        store, runner = client.app.state.store, client.app.state.runner
        repo = Repository(store)
        program = repo.publish(Envelope(kind='programs', project_id=asset.project_id,
            source_asset_id=asset.id, source_sha256=asset.sha256,
            payload={'fixture_only':True, 'instruction':'MANUAL DEVELOPER FIXTURE, not compiler output'}))
        run = snapshot_run(store, settings, project_id, program.id, 1,
            muse_adapter_version='manual-fixture', sam_adapter_version='manual-fixture', evaluator_version='manual-fixture')
        adapter = SourceEvidenceAdapter(store, settings, project_id, asset.id)
        event_data = EventEvidence(origin='manual_fixture', label='Manual interval on generated permitted media',
            interval=SourceInterval(start_ms=200, end_ms=2200), verdict='unknown',
            review_reason='Manual fixture; second step has no visual proof',
            steps=[StepEvidence(step_id='first', label='Manually selected first step', verdict='true',
                       interval=SourceInterval(start_ms=200,end_ms=1000), frames=[adapter.frame(500)]),
                   StepEvidence(step_id='tool', label='Tool use not established', verdict='unknown',
                       reason='No source evidence establishes a tool step; not a physical omission claim')],
            coverage={'kind':'manual_fixture','negative_proof':False})
        event = repo.publish(Envelope(kind='temporal_events', project_id=asset.project_id,
            source_asset_id=asset.id, source_sha256=asset.sha256, program=reference(program), run=reference(run),
            payload=event_data.model_dump(mode='json')))
        jobs = []
        for action_type in ('extract_clip', 'create_report'):
            request = {'type':action_type,'event_ids':[str(event.id)],'review_only':True}
            if action_type == 'extract_clip':
                request['parameters'] = {'pre_ms':100,'post_ms':100}
            plan = build_action_plan(store, settings, project_id, run.id, request, initiated=True, manual_fixture=True)
            job, _ = request_execution(store, runner, settings, plan)
            jobs.append(wait(client, job.id))
            repeated, reused = request_execution(store, runner, settings, plan)
            assert reused and repeated.id == job.id
        outputs = client.get(f'/api/projects/{project_id}/exports').json()
        assert len(outputs['exports']) == 1 and len(outputs['reports']) == 1
        video = outputs['exports'][0]
        assert client.get(video['stream_url'], headers={'Range':'bytes=-128'}).status_code == 206
        assert client.get(video['download_url']).status_code == 200
        report = outputs['reports'][0]
        assert client.get(report['view_url']).status_code == 200
        document = client.get(report['download_url']).json()
        assert document['rows'][1]['verdict'] == 'unknown'
        for row in document['rows']:
            for evidence in row['evidence']:
                assert client.get(evidence['image_url']).status_code == 200
        proof = {'evidence_kind':'manual_fixture_local_actions_only', 'provider_calls':0,
                 'data_dir':str(settings.data_dir), 'project_id':project_id, 'run_id':str(run.id),
                 'source_sha256':asset.sha256, 'jobs':jobs, 'outputs':outputs,
                 'output_sha256':file_sha256(store.path(project_id, video['path'])),
                 'browser_playback':'not_yet_checked'}
        (output/'proof.json').write_text(json.dumps(proof, indent=2), encoding='utf-8')
    # Real restart: a new process-lock/session reopens committed artifacts.
    with TestClient(create_app(settings)) as restarted:
        assert restarted.get(video['stream_url'], headers={'Range':'bytes=-128'}).status_code == 206
        assert restarted.get(report['view_url']).status_code == 200
        assert restarted.get(report['download_url']).json()['rows'][1]['verdict'] == 'unknown'
    print(json.dumps({'passed':True,'scope':proof['evidence_kind'], 'project_id':project_id,
                      'proof':str(output/'proof.json'), 'data_dir':str(settings.data_dir)}), flush=True)


if __name__ == '__main__':
    main()
