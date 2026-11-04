import subprocess

import pytest

from app.errors import AppError
from app.sampling import extract_window
from app.scenes import SceneEvidence,inspect_scenes
from test_evidence import prepare


def test_measured_scene_cuts_retain_actual_original_frame_identity_and_reuse(tmp_path,client,settings,monkeypatch):
    clip=tmp_path/'cuts.mp4'
    source='color=red:s=160x90:r=30:d=1.033333[a];color=blue:s=160x90:r=30:d=1[b];color=white:s=160x90:r=30:d=1[c];[a][b][c]concat=n=3:v=1:a=0'
    subprocess.run(['ffmpeg','-v','error','-y','-f','lavfi','-i',source,'-c:v','libx264','-threads','2',str(clip)],check=True,timeout=30)
    asset=prepare(client,clip);store=client.app.state.store
    window=extract_window(store,settings,asset,0,asset.working_metadata.duration_ms)
    result=inspect_scenes(store,settings,window)
    assert [c.window_frame_index for c in result.cuts]==[31,61]
    assert [c.source_time_ms for c in result.cuts]==[1033,2033]
    assert all(c.source_frame_index==window.source_frame_indices[c.window_frame_index] for c in result.cuts)
    monkeypatch.setattr('app.scenes.run_media',lambda *_args,**_kwargs:pytest.fail('Compatible scene evidence should reuse measured results'))
    assert inspect_scenes(store,settings,window)==result
    record=store.load(asset.project_id,'scenes',SceneEvidence,result.id)
    record.cuts[0].source_frame_index+=1
    store.save(asset.project_id,'scenes',record)
    with pytest.raises(AppError) as exc:inspect_scenes(store,settings,window)
    assert exc.value.code=='CORRUPT_RECORD'
