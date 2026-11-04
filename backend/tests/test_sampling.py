from app.sampling import coarse_schedule,coarse_windows,extract_window
from test_evidence import prepare,marked_media,gray_frames
from app.timing import SourceTimeline
import pytest
import subprocess


def test_coarse_windows_cover_full_source_with_bounded_overlap():
    schedule=coarse_schedule(120000)
    assert schedule[0][0]==0 and schedule[-1][1]==120000
    assert len(schedule)==12
    assert all(end-start<=12000 for start,end in schedule)
    assert all(b[0]<=a[1] for a,b in zip(schedule,schedule[1:]))
    assert coarse_schedule(750)==[(0,750)]
    with pytest.raises(ValueError):coarse_schedule(120000,window_ms=10,overlap_ms=10)


def test_actual_proxy_windows_preserve_frame_identity_and_are_not_analysis(client,settings,marked_media):
    asset=prepare(client,marked_media['vfr'])
    store=client.app.state.store
    windows=coarse_windows(store,settings,asset,window_ms=500,overlap_ms=100)
    timeline=SourceTimeline.model_validate(client.get(f'/api/assets/{asset.id}/timeline').json())
    covered=set()
    root=store.project_dir(asset.project_id)
    for window in windows:
        assert window.analysis_status=='not_analyzed'
        assert window.input_kind=='normalized_proxy_video'
        assert window.source_sha256==asset.sha256
        decoded=gray_frames(root/window.path,window.width,window.height)
        assert len(decoded)==len(window.source_frame_indices)
        offset=window.first_working_frame_index
        assert window.source_frame_indices==timeline.working_frame_source_indices[offset:offset+len(decoded)]
        covered.update(range(offset,offset+len(decoded)))
        assert extract_window(store,settings,asset,window.requested_in_ms,window.requested_out_ms).id==window.id
    assert covered==set(range(len(timeline.working_frame_times_ms)))


def test_low_fps_source_keeps_final_frame_through_full_analysis_window(client,settings,tmp_path):
    clip=tmp_path/'slow-source.mp4'
    subprocess.run(['ffmpeg','-v','error','-y','-f','lavfi','-i','testsrc2=size=320x180:rate=15:duration=4',
                    '-c:v','libx264',str(clip)],check=True,timeout=20)
    asset=prepare(client,clip)
    assert abs(asset.working_metadata.duration_ms-4000)<=34
    window=coarse_windows(client.app.state.store,settings,asset)[0]
    decoded=gray_frames(client.app.state.store.path(asset.project_id,window.path),window.width,window.height)
    assert len(decoded)==120
    assert window.source_frame_indices[-1]==59
