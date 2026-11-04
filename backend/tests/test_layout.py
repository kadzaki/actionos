from pathlib import Path

import pytest

from app.editing import fixed_crop
from app.errors import AppError
from app.layout import inventory_project, owned_media, source_path, derivative_path
from app.models import Asset
from app.rendering import render
from app.storage import file_sha256
from test_rendering import ready_asset, manual_plan


def test_inventory_is_read_only_and_preserves_both_layouts_and_export_access(client, settings, media):
    asset = ready_asset(client, media, 'silent')
    store = client.app.state.store
    assert asset.media_layout == 'actionos'
    assert asset.source_path.startswith('media/source/')
    output = render(store, settings, manual_plan(asset, fixed_crop(320, 180)))
    output_hash = file_sha256(store.path(asset.project_id, output.path))
    root = store.project_dir(asset.project_id)
    before = {p.relative_to(root).as_posix(): file_sha256(p) for p in root.rglob('*') if p.is_file()}
    report = inventory_project(store, asset.project_id)
    after = {p.relative_to(root).as_posix(): file_sha256(p) for p in root.rglob('*') if p.is_file()}
    assert report['valid'] and report['dry_run'] and report['moves'] == []
    assert before == after
    assert report['exports'][0]['sha256'] == output_hash
    # Simulate an old valid manifest without moving/deleting any current bytes.
    legacy = asset.model_copy(deep=True)
    legacy.media_layout = 'legacy'
    legacy.source_path = source_path(asset.id, 'legacy')
    legacy.working_path = derivative_path(legacy, 'working')
    legacy.proxy_path = derivative_path(legacy, 'preview')
    for old, new in [(asset.source_path, legacy.source_path),
                     (asset.working_path, legacy.working_path), (asset.proxy_path, legacy.proxy_path)]:
        store.write_bytes(store.path(asset.project_id, new), store.path(asset.project_id, old).read_bytes())
    legacy.artifact_sha256 = {}  # Initial records have no derivative integrity map.
    store.save(asset.project_id, 'assets', legacy)
    legacy_report = inventory_project(store, asset.project_id)
    assert legacy_report['valid']
    assert legacy_report['sources'][0]['derivative_integrity'] == 'legacy_unqualified'
    assert file_sha256(owned_media(store, asset.project_id, asset.id, 'source')) == asset.sha256
    assert client.get(f'/api/assets/{asset.id}/stream?variant=original').status_code == 200
    assert file_sha256(store.path(asset.project_id, output.path)) == output_hash
    assert client.get(f'/api/exports/{output.id}/stream', headers={'Range':'bytes=0-31'}).status_code == 206


def test_unpublished_migration_bytes_do_not_change_manifest_or_promote_legacy_results(client, settings, media):
    asset = ready_asset(client, media, 'silent')
    store = client.app.state.store
    manifest = store.path(asset.project_id, f'assets/{asset.id}.json')
    before = manifest.read_bytes()
    store.write_bytes(store.path(asset.project_id, f'media/proxy/{asset.id}.unpublished.mp4'), b'partial migration')
    assert inventory_project(store, asset.project_id)['valid']
    assert manifest.read_bytes() == before
    assert not list(store.path(asset.project_id, 'temporal_events').glob('*.json'))
    schema = client.get('/openapi.json').json()
    assert schema['paths']['/api/projects/{project_id}/analyze']['post']['deprecated'] is True
    assert schema['paths']['/api/projects/{project_id}/events']['get']['tags'] == ['Legacy VideoCut']
    store.path(asset.project_id, asset.source_path).write_bytes(b'wrong source bytes')
    report = inventory_project(store, asset.project_id)
    assert not report['valid']
    assert 'CORRUPT_ARTIFACT' in {item['code'] for item in report['issues']}
