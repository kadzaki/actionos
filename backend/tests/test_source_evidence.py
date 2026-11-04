from dataclasses import replace

import pytest

from app.errors import AppError
from app.models import Asset
from app.sampling import extract_window
from app.source_evidence import SourceEvidenceAdapter
from app.storage import FileStore
from test_evidence import prepare


@pytest.mark.parametrize('variant', ['vfr', 'rotated', 'sar'])
def test_adapter_preserves_actual_pts_window_offsets_and_display_boxes(client, settings, media, variant):
    asset = prepare(client, media[variant])
    store = FileStore(settings.data_dir)
    adapter = SourceEvidenceAdapter(store, settings, asset.project_id, asset.id)
    window = extract_window(store, settings, asset, 300, 900)
    evidence = adapter.window_frame(window, 2)
    source_index = window.source_frame_indices[2]
    assert evidence.source_frame_index == source_index
    assert evidence.source_pts == adapter.timeline.source_frame_pts[source_index]
    assert evidence.source_time_ms == adapter.timeline.source_frame_times_ms[source_index]
    assert evidence.image_url.startswith('/api/evidence/')
    assert client.get(evidence.image_url).status_code == 200
    assert evidence.source_url.startswith(f'/api/assets/{asset.id}/stream?variant=original#t=')
    transform = evidence.transforms[-1]
    g = asset.metadata.geometry
    assert transform.point(g.display_width, g.display_height) == pytest.approx((1, 1))
    assert adapter.normalized_box(window, 0, 0, window.width-1, window.height-1) == (0, 0, 1, 1)
    working = adapter.working_frame(401)
    from bisect import bisect_right
    wi = bisect_right(adapter.timeline.working_frame_times_ms, 401)-1
    assert working.source_frame_index == adapter.timeline.working_frame_source_indices[wi]
    with pytest.raises(AppError):
        adapter.interval(0, asset.metadata.duration_ms+1)
    with pytest.raises(AppError):
        adapter.window_frame(window.model_copy(update={'source_sha256': '0'*64}), 0)
    with pytest.raises(AppError):
        adapter.window_frame(window, len(window.source_frame_indices))


def test_missing_modified_source_or_derivative_and_legacy_integrity_fail_explicitly(client, settings, media):
    asset = prepare(client, media['silent'])
    store = FileStore(settings.data_dir)
    base = lambda: SourceEvidenceAdapter(store, settings, asset.project_id, asset.id)
    assert base().frame(0).source_frame_index == 0
    original = store.path(asset.project_id, asset.source_path)
    content = original.read_bytes()
    original.write_bytes(b'modified original')
    with pytest.raises(AppError) as exc:
        base()
    assert exc.value.code == 'CORRUPT_ARTIFACT'
    original.write_bytes(content)
    store.path(asset.project_id, asset.proxy_path).unlink()
    with pytest.raises(AppError) as exc:
        base()
    assert exc.value.code == 'CORRUPT_ARTIFACT'
    legacy = store.load(asset.project_id, 'assets', Asset, asset.id)
    legacy.artifact_sha256 = {}
    store.save(asset.project_id, 'assets', legacy)
    with pytest.raises(AppError) as exc:
        base()
    assert exc.value.code == 'MEDIA_INTEGRITY_UNQUALIFIED'
