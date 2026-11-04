import json
import time
from uuid import uuid4

import pytest

from app.actions.contracts import EventEvidence, StepEvidence
from app.actions.execution import request_execution
from app.actions.registry import build_action_plan, parse_request, validate_event
from app.actions.reports import build_report
from app.actions.worldcut import extract_event
from app.errors import AppError
from app.models import Job
from app.programs.policy import SourceInterval
from app.repository import Envelope, Repository, reference
from app.runs import snapshot_run
from app.source_evidence import SourceEvidenceAdapter
from app.storage import file_sha256
from test_evidence import prepare


def manual_bundle(client, settings, media, unknown=True):
    asset = prepare(client, media['audio'])
    store = client.app.state.store
    repo = Repository(store)
    program = repo.publish(Envelope(kind='programs', project_id=asset.project_id,
        source_asset_id=asset.id, source_sha256=asset.sha256,
        payload={'fixture_only': True, 'instruction': 'MANUAL ACTION FIXTURE; no compiler or inference'}))
    run = snapshot_run(store, settings, asset.project_id, program.id, 1,
        muse_adapter_version='manual-fixture', sam_adapter_version='manual-fixture', evaluator_version='manual-fixture')
    adapter = SourceEvidenceAdapter(store, settings, asset.project_id, asset.id)
    first = StepEvidence(step_id='place', label='Manually selected source step', verdict='true',
        interval=SourceInterval(start_ms=0, end_ms=500), frames=[adapter.frame(150)])
    second = (StepEvidence(step_id='use', label='Tool step not observed (manual fixture)', verdict='unknown', reason='Fixture contains no evidence establishing tool use')
        if unknown else StepEvidence(step_id='use', label='Second manually selected step', verdict='true',
            interval=SourceInterval(start_ms=500, end_ms=1200), frames=[adapter.frame(700)]))
    value = EventEvidence(origin='manual_fixture', label='MANUAL DEVELOPER FIXTURE',
        interval=SourceInterval(start_ms=0, end_ms=1400), verdict='unknown' if unknown else 'true',
        steps=[first, second], review_reason='Manual fixture requires review' if unknown else '',
        coverage={'kind': 'manual_fixture', 'negative_proof': False})
    event = repo.publish(Envelope(kind='temporal_events', project_id=asset.project_id,
        source_asset_id=asset.id, source_sha256=asset.sha256, program=reference(program), run=reference(run),
        payload=value.model_dump(mode='json')))
    return store, asset, run, event


def plan_for(store, settings, asset, run, events, kind, **kwargs):
    return build_action_plan(store, settings, asset.project_id, run.id,
        {'type':kind, 'event_ids':[str(e.id) for e in events], 'review_only':True, **kwargs},
        initiated=True, manual_fixture=True)


def test_registry_requires_explicit_initiation_and_review_selection(client, settings, media):
    store, asset, run, event = manual_bundle(client, settings, media)
    request = {'type':'create_report', 'event_ids':[str(event.id)]}
    for extra, code in [({'initiated':False, 'manual_fixture':True}, 'ACTION_INITIATION_REQUIRED'),
                        ({'initiated':True}, 'PROGRAM_REVIEW_REQUIRED'),
                        ({'initiated':True, 'manual_fixture':True}, 'REVIEW_SELECTION_REQUIRED')]:
        with pytest.raises(AppError) as exc:
            build_action_plan(store, settings, asset.project_id, run.id, request, **extra)
        assert exc.value.code == code
    for kind in ('send_email', 'shell', 'compare_events', 'isolate_subject'):
        with pytest.raises(AppError): parse_request({**request, 'type':kind})
    with pytest.raises(AppError): parse_request({**request, 'path':'../../outside.html'})
    with pytest.raises(AppError): parse_request({**request, 'event_ids':[str(event.id)]*33})
    with pytest.raises(AppError):
        build_action_plan(store, settings, asset.project_id, uuid4(), request, initiated=True, manual_fixture=True)


def test_held_frame_witness_keeps_original_pts_and_cannot_claim_another_display_interval(client,settings,media):
    store,asset,run,record=manual_bundle(client,settings,media)
    adapter=SourceEvidenceAdapter(store,settings,asset.project_id,asset.id)
    frame=adapter.frame_witness(0,20)
    assert frame.source_time_ms==0 and frame.observed_at_source_ms==20
    interval=SourceInterval(start_ms=10,end_ms=80)
    event=EventEvidence(origin='manual_fixture',label='Held frame test',interval=interval,verdict='true',
        steps=[StepEvidence(step_id='held',label='Explicit manual witness',verdict='true',interval=interval,frames=[frame])])
    bound=record.model_copy(update={'payload':event.model_dump(mode='json')})
    assert validate_event(store,adapter,bound).steps[0].frames[0].source_pts==frame.source_pts
    forged=event.model_dump(mode='json')
    forged['steps'][0]['frames'][0]['observed_at_source_ms']=70
    with pytest.raises(AppError) as exc:
        validate_event(store,adapter,record.model_copy(update={'payload':forged}))
    assert exc.value.code=='EVIDENCE_TIME_MISMATCH'


def test_manual_clip_is_verified_audio_preserving_source_complete_and_not_tracked(client, settings, media):
    store, asset, run, event = manual_bundle(client, settings, media)
    plan = plan_for(store, settings, asset, run, [event], 'extract_clip',
                    parameters={'output':{'width':360,'height':640}, 'post_ms':0})
    export = extract_event(store, settings, plan, event)
    manifest = store.read_json(store.path(asset.project_id, export.manifest_path))
    assert manifest['complete_decode_verified'] is True
    assert manifest['output']['audio_present'] is True
    assert abs(export.duration_ms - 1400) <= 100
    assert manifest['source_to_output'][0]['source_in_ms'] == 0
    assert manifest['source_to_output'][0]['source_out_ms'] == 1400
    assert manifest['crop_modes'] == ['full_frame']
    assert manifest['actionos_lineage']['run_id'] == str(run.id)
    assert manifest['actionos_lineage']['fixture_only'] is True
    assert manifest['actionos_lineage']['verdict'] == 'unknown'
    assert client.get(f'/api/exports/{export.id}/stream',headers={'Range':'bytes=-32'}).status_code == 206


def test_report_rows_are_csv_ready_escaped_and_link_to_actual_evidence(client, settings, media):
    store, asset, run, event = manual_bundle(client, settings, media)
    plan = plan_for(store, settings, asset, run, [event], 'create_report',
                    parameters={'title':'<script>alert("fixture")</script>'})
    report = build_report(store, settings, plan)
    html = client.get(f'/api/reports/{report.id}/view')
    assert html.status_code == 200
    assert '<script>' not in html.text and '&lt;script&gt;' in html.text
    assert 'MANUAL DEVELOPER FIXTURE' in html.text
    assert "default-src 'none'" in html.headers['content-security-policy']
    response = client.get(f'/api/reports/{report.id}/download')
    assert response.status_code == 200 and 'attachment' in response.headers['content-disposition']
    body = response.json()
    assert body['run_id'] == str(run.id) and body['program_version'] == 1
    assert body['rows'][1]['verdict'] == 'unknown'
    assert 'does not establish physical non-occurrence' in body['rows'][1]['reason']
    assert body['rows'][0]['evidence'][0]['source_pts'] >= 0
    for row in body['rows']:
        assert client.get(row['source_url'].split('#')[0]).status_code == 200
        for evidence in row['evidence']:
            assert client.get(evidence['image_url']).status_code == 200
    listed = client.get(f'/api/projects/{asset.project_id}/exports').json()['reports']
    assert listed[0]['report_id'] == str(report.id)


def test_action_dispatch_reuses_active_and_completed_work_and_preserves_partial_failures(client, settings, media, monkeypatch):
    store, asset, run, event = manual_bundle(client, settings, media)
    repo = Repository(store)
    second = repo.publish(event.model_copy(update={'id':uuid4()}))
    plan = plan_for(store, settings, asset, run, [event, second], 'extract_clip',
                    parameters={'output':{'width':360,'height':640}})
    same = plan_for(store, settings, asset, run, [event, second], 'extract_clip',
                    parameters={'output':{'width':360,'height':640}})
    assert same.id == plan.id
    original = extract_event
    def partly_fail(store, settings, plan, current, stage):
        if current.id == second.id: raise AppError('FIXTURE_RENDER_FAILURE','Deliberate fixture failure',422)
        return original(store, settings, plan, current, stage)
    monkeypatch.setattr('app.actions.execution.extract_event', partly_fail)
    runner = client.app.state.runner
    job, reused = request_execution(store, runner, settings, plan)
    assert not reused
    again, reused = request_execution(store, runner, settings, plan)
    assert reused and again.id == job.id
    from test_api import wait_job
    result = wait_job(client, job.id)
    assert result['status'] == 'completed'
    assert result['result']['status'] == 'partial'
    assert result['result']['completed_event_ids'] == [str(event.id)]
    assert result['result']['failed_events'][0]['event_id'] == str(second.id)
    manifest = repo.load(asset.project_id, 'action_results', job.id)
    assert manifest.payload['status'] == 'partial' and len(manifest.artifacts) == 4
    previous = file_sha256(store.path(asset.project_id, manifest.artifacts[0].path))
    again, reused = request_execution(store, runner, settings, plan)
    assert reused and again.id == job.id
    assert file_sha256(store.path(asset.project_id, manifest.artifacts[0].path)) == previous
    store.path(asset.project_id, manifest.artifacts[1].path).unlink()
    with pytest.raises(AppError) as exc:
        request_execution(store, runner, settings, plan)
    assert exc.value.code == 'CORRUPT_ARTIFACT'


def test_missing_report_does_not_hide_valid_video_and_failed_publication_has_no_report_record(client, settings, media, monkeypatch):
    store, asset, run, event = manual_bundle(client, settings, media)
    clip_plan = plan_for(store, settings, asset, run, [event], 'extract_clip', parameters={'output':{'width':360,'height':640}})
    export = extract_event(store, settings, clip_plan, event)
    report_plan = plan_for(store, settings, asset, run, [event], 'create_report')
    report = build_report(store, settings, report_plan)
    html = next(a for a in report.artifacts if a.media_type == 'text/html')
    store.path(asset.project_id, html.path).unlink()
    listing = client.get(f'/api/projects/{asset.project_id}/exports').json()
    assert len(listing['exports']) == 1 and not listing['reports']
    assert listing['report_errors'][0]['code'] == 'CORRUPT_ARTIFACT'
    assert client.get(f'/api/exports/{export.id}/stream').status_code == 200
    existing = set(store.path(asset.project_id,'reports').glob('*.json'))
    original = store.write_bytes
    def fail_html(path, raw):
        if path.suffix == '.html': raise OSError('fixture disk failure')
        return original(path, raw)
    monkeypatch.setattr(store, 'write_bytes', fail_html)
    with pytest.raises(OSError):
        build_report(store, settings, report_plan)
    assert set(store.path(asset.project_id,'reports').glob('*.json')) == existing


def test_full_frame_letterbox_preserves_both_sides_of_source_pixels(client, settings, tmp_path):
    import subprocess
    from test_rendering import read_rgb_frame
    source = tmp_path/'permitted-split.mp4'
    subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-y','-f','lavfi','-i',
        'color=red:size=320x180:rate=24:duration=1.5,drawbox=x=160:y=0:w=160:h=180:color=blue:t=fill',
        '-c:v','libx264','-pix_fmt','yuv420p',str(source)],check=True,timeout=20)
    store, asset, run, event = manual_bundle(client, settings, {'audio':source})
    plan = plan_for(store, settings, asset, run, [event], 'extract_clip', parameters={'output':{'width':360,'height':640}})
    export = extract_event(store, settings, plan, event)
    pixels = read_rgb_frame(store.path(asset.project_id,export.path), .5)
    def rgb(x,y):
        offset = (y*360+x)*3
        return pixels[offset:offset+3]
    assert rgb(20,320)[0] > 180 and rgb(20,320)[2] < 60
    assert rgb(340,320)[2] > 180 and rgb(340,320)[0] < 60
    assert max(rgb(180,10)) < 10
