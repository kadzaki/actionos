import json
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from app.main import create_app
from app.jobs import JobRunner
from app.models import Asset,Job,JobStatus,Project
from app.recovery import reconcile_files
from app.rendering import render
from app.storage import FileStore
from test_rendering import ready_asset,manual_plan
from app.editing import fixed_crop


def test_recovery_removes_only_known_transients_and_preserves_originals(tmp_path):
    store=FileStore(tmp_path)
    project=Project(name='interrupted files')
    store.create_project(project)
    root=store.project_dir(project.id)
    source_id,export_id=uuid4(),uuid4()
    source=store.path(project.id,f'source/{source_id}.media')
    source.parent.mkdir()
    source.write_bytes(b'original evidence')
    output=store.path(project.id,f'exports/{export_id}.mp4')
    output.parent.mkdir()
    output.write_bytes(b'unpublished output: not a completed export')
    tmp=root/'tmp'
    tmp.mkdir()
    generated=[tmp/f'{uuid4()}.upload',tmp/f'{uuid4()}.render.mp4',tmp/f'{uuid4()}.filters.txt',
               tmp/f'{uuid4()}.{uuid4().hex}.frame.jpg']
    proxy=root/'proxy'
    proxy.mkdir()
    generated.append(proxy/f'.{source_id}.working.partial.mp4')
    generated.append(root/f'.project.json.{uuid4().hex}.tmp')
    for path in generated:path.write_bytes(b'partial')
    custom=tmp/'keep-my-notes.txt'
    custom.write_bytes(b'unknown files are preserved')
    issues=reconcile_files(store)
    assert all(not path.exists() for path in generated)
    assert source.read_bytes()==b'original evidence'
    assert custom.is_file() and output.is_file()
    assert {i['code'] for i in issues}>={'ORPHAN_SOURCE','ORPHAN_EXPORT'}
    assert not list((root/'exports').glob(f'{export_id}.json'))


def test_dangling_active_references_do_not_leave_a_project_processing(tmp_path):
    store=FileStore(tmp_path)
    project=Project(name='dangling',status='processing',active_asset_id=uuid4(),active_job_id=uuid4())
    store.create_project(project)
    issues=reconcile_files(store)
    saved=store.load(project.id,'project',Project)
    assert saved.status=='failed'
    assert saved.active_asset_id is None and saved.active_job_id is None
    assert {i['code'] for i in issues}>={'ACTIVE_ASSET_UNAVAILABLE','ACTIVE_JOB_UNAVAILABLE'}


def test_corrupt_and_future_schema_records_remain_untouched(settings):
    store=FileStore(settings.data_dir)
    first=Project(name='corrupt')
    second=Project(name='newer format')
    healthy=Project(name='healthy')
    store.create_project(first);store.create_project(second);store.create_project(healthy)
    bad=store.project_dir(first.id)/'project.json'
    future=store.project_dir(second.id)/'project.json'
    bad.write_text('{broken JSON',encoding='utf-8')
    newer=second.model_dump(mode='json');newer['schema_version']=999
    future.write_text(json.dumps(newer),encoding='utf-8')
    previous=(bad.read_bytes(),future.read_bytes())
    with TestClient(create_app(settings)) as client:
        health=client.get('/api/health').json()
        assert health['status']=='degraded'
        assert len(health['recovery_errors'])==2
        listing=client.get('/api/projects')
        assert listing.status_code==200
        assert [p['id'] for p in listing.json()['projects']]==[str(healthy.id)]
        assert len(listing.json()['errors'])==2
    assert (bad.read_bytes(),future.read_bytes())==previous


@pytest.mark.parametrize('field',['source_asset_id','source_sha256','plan_fingerprint'])
def test_broken_export_lineage_is_unavailable_after_restart(settings,media,field):
    with TestClient(create_app(settings)) as client:
        asset=ready_asset(client,media)
        store=client.app.state.store
        exported=render(store,settings,manual_plan(asset,fixed_crop(320,180)))
        manifest_path=store.path(asset.project_id,exported.manifest_path)
        manifest=json.loads(manifest_path.read_text())
        manifest[field]=str(uuid4()) if field=='source_asset_id' else 'invalid-fingerprint'
        manifest_path.write_text(json.dumps(manifest))
        output=store.path(asset.project_id,exported.path)
        original=output.read_bytes()
    with TestClient(create_app(settings)) as client:
        assert client.get('/api/health').json()['status']=='degraded'
        outputs=client.get(f'/api/projects/{asset.project_id}/exports').json()['exports']
        assert outputs==[]
        response=client.get(f'/api/exports/{exported.id}/stream')
        assert response.status_code==409
        assert response.json()['code']=='EXPORT_UNAVAILABLE'
    assert output.read_bytes()==original


def test_interrupted_render_preserves_prepared_source_and_prior_good_export(settings,media):
    with TestClient(create_app(settings)) as client:
        asset=ready_asset(client,media)
        store=client.app.state.store
        exported=render(store,settings,manual_plan(asset,fixed_crop(320,180)))
        project=store.load(asset.project_id,'project',Project)
        interrupted=Job(project_id=asset.project_id,asset_id=asset.id,type='render',status=JobStatus.running,step='rendering')
        store.save(asset.project_id,'jobs',interrupted)
        project.active_job_id=interrupted.id
        project.status='processing'
        store.save(project.id,'project',project)
    with TestClient(create_app(settings)) as client:
        saved=client.get(f'/api/projects/{asset.project_id}').json()
        assert saved['asset']['status']=='ready' and saved['project']['status']=='ready'
        assert client.get(f'/api/jobs/{interrupted.id}').json()['status']=='interrupted'
        outputs=client.get(f'/api/projects/{asset.project_id}/exports').json()['exports']
        assert len(outputs)==1 and outputs[0]['id']==str(exported.id)
        assert client.get(outputs[0]['download_url']).status_code==200


def test_additive_plan_defaults_do_not_invalidate_historical_verified_exports(settings,media):
    from app.editing import fingerprint_data
    with TestClient(create_app(settings)) as client:
        asset=ready_asset(client,media);store=client.app.state.store
        plan=manual_plan(asset,fixed_crop(320,180));exported=render(store,settings,plan)
        plan_path=store.path(asset.project_id,f'plans/{plan.id}/v1.json')
        raw=json.loads(plan_path.read_text());raw.pop('event_snapshots')
        for segment in raw['segments']:segment['crop'].pop('context_track_ids')
        plan_path.write_text(json.dumps(raw))
        path=store.path(asset.project_id,exported.manifest_path)
        manifest=json.loads(path.read_text());manifest['plan_fingerprint']=fingerprint_data(raw)
        path.write_text(json.dumps(manifest))
    with TestClient(create_app(settings)) as client:
        assert client.get('/api/health').json()['status']=='ok'
        assert len(client.get(f'/api/projects/{asset.project_id}/exports').json()['exports'])==1
