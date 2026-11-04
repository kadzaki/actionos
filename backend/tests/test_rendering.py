from pathlib import Path
import json
import subprocess

import pytest

from app.editing import EditPlan, OutputSettings, Segment, Track, TrackSample, fixed_crop, plan_crop
from app.errors import AppError
from app.models import Asset
from app.rendering import render, save_plan, tempo_filters
from test_api import create, wait_job


def ready_asset(client,media,variant="audio"):
    project_id=create(client)
    result=client.post(f"/api/projects/{project_id}/upload",content=media[variant].read_bytes(),headers={"Content-Type":"video/mp4"}).json()
    assert wait_job(client,result["job_id"])["status"]=="completed"
    asset=Asset.model_validate(client.get(f"/api/projects/{project_id}").json()["asset"])
    return asset


def manual_plan(asset,crop):
    return EditPlan(project_id=asset.project_id,source_asset_id=asset.id,origin="manual",
                    output=OutputSettings(width=360,height=640),segments=[
                        Segment(source_in_ms=0,source_out_ms=400,speed=1,crop=crop),
                        Segment(source_in_ms=400,source_out_ms=1000,speed=.5,crop=crop),
                        Segment(source_in_ms=1000,source_out_ms=1400,speed=1,crop=crop)])


def test_verified_identical_edl_reuses_export_without_encoding_and_regenerate_is_explicit(client,settings,media,monkeypatch):
    from uuid import uuid4
    from app.rendering import reusable_export
    from app.storage import FileStore
    asset=ready_asset(client,media,'silent');store=client.app.state.store
    plan=manual_plan(asset,fixed_crop(320,180))
    first=render(store,settings,plan)
    same=plan.model_copy(update={'id':uuid4()})
    stages=[]
    with monkeypatch.context() as patch:
        patch.setattr('app.rendering.run_media',lambda *_args,**_kwargs:pytest.fail('A verified cache hit must not encode or decode again'))
        cached=render(FileStore(settings.data_dir),settings,same,lambda step,progress:stages.append(step))
    assert cached.id==first.id and cached.plan_id==plan.id and stages==['reusing']
    assert not store.path(asset.project_id,f'plans/{same.id}/v1.json').exists()
    regenerated=render(store,settings,plan,regenerate=True)
    assert regenerated.id!=first.id and regenerated.plan_id==first.plan_id
    assert reusable_export(store,asset,plan,{'ffmpeg':'different tool build','ffprobe':'different tool build'}) is None
    output=store.path(asset.project_id,regenerated.path)
    output.write_bytes(b'corrupted output')
    # A corrupt newer result must never masquerade as completed cached media.
    assert reusable_export(store,asset,plan).id==first.id


@pytest.mark.parametrize("variant",["audio","silent"])
def test_real_edl_render_audio_policy_verification_and_download(client,settings,media,variant):
    asset=ready_asset(client,media,variant)
    plan=manual_plan(asset,fixed_crop(320,180))
    store=client.app.state.store
    export=render(store,settings,plan)
    assert abs(export.duration_ms-2000)<=100
    manifest=json.loads(store.path(asset.project_id,export.manifest_path).read_text())
    assert manifest["source_sha256"]==asset.sha256
    assert manifest["output"]["audio_present"]==(variant=="audio")
    assert manifest["complete_decode_verified"] is True
    assert manifest["source_to_output"][1]["output_in_ms"]==400
    assert manifest["origin"]=="manual"
    assert client.get(f"/api/plans/{plan.id}").json()["origin"]=="manual"
    listed=client.get(f"/api/projects/{asset.project_id}/exports").json()["exports"]
    assert len(listed)==1
    assert client.get(listed[0]["stream_url"],headers={"Range":"bytes=0-99"}).status_code==206
    download=client.get(listed[0]["download_url"])
    assert download.content==store.path(asset.project_id,export.path).read_bytes()
    assert "attachment" in download.headers["content-disposition"]


def read_rgb_frame(path: Path,seconds: float):
    result=subprocess.run(["ffmpeg","-hide_banner","-loglevel","error","-ss",str(seconds),"-i",str(path),
                           "-frames:v","1","-f","rawvideo","-pix_fmt","rgb24","-"],capture_output=True,check=True,timeout=20)
    return result.stdout


def test_actual_dynamic_crop_changes_exported_pixels_at_mapped_source_times(client,settings,media,tmp_path):
    # A generated red/blue split is geometry evidence only, never provider detection.
    clip=tmp_path/"split.mp4"
    subprocess.run(["ffmpeg","-hide_banner","-loglevel","error","-y","-f","lavfi","-i",
                    "color=red:size=320x180:rate=30:duration=1.5,drawbox=x=160:y=0:w=160:h=180:color=blue:t=fill",
                    "-c:v","libx264","-pix_fmt","yuv420p",str(clip)],check=True,timeout=20)
    asset=ready_asset(client,{"audio":clip})
    track=Track(project_id=asset.project_id,source_asset_id=asset.id,label="synthetic moving target",
                origin="synthetic_test",samples=[TrackSample(time_ms=t,x=.05+.7*t/1400,y=.15,width=.1,height=.6)
                                                   for t in range(0,1401,200)])
    store=client.app.state.store
    store.save(asset.project_id,"tracks",track)
    crop=plan_crop(track,320,180,0,1400)
    plan=manual_plan(asset,crop)
    export=render(store,settings,plan)
    output=store.path(asset.project_id,export.path)
    # Output 0.2s maps to source .2s on red; output 1.8s maps to source 1.2s on blue.
    for seconds,dominant_channel in [(.2,0),(1.8,2)]:
        rgb=read_rgb_frame(output,seconds)
        offset=((640//2)*360+360//2)*3
        pixel=rgb[offset:offset+3]
        assert len(pixel)==3
        assert pixel[dominant_channel]>150 and pixel[2-dominant_channel]<100, (seconds,list(pixel))
    assert not list(store.path(asset.project_id,"tmp").glob("*.render.mp4"))


def test_invalid_plan_preserves_old_export_and_plan_is_immutable(client,settings,media):
    asset=ready_asset(client,media)
    store=client.app.state.store
    plan=manual_plan(asset,fixed_crop(320,180))
    first=render(store,settings,plan)
    old=store.path(asset.project_id,first.path).read_bytes()
    changed=plan.model_copy(deep=True)
    changed.requested_duration_ms=15000
    with pytest.raises(AppError,match="overwritten"):
        save_plan(store,changed)
    invalid=plan.model_copy(deep=True)
    invalid.segments[-1].source_out_ms=999999
    with pytest.raises(AppError,match="exceeds"):
        render(store,settings,invalid)
    assert store.path(asset.project_id,first.path).read_bytes()==old


def test_tempo_chain_covers_supported_extreme_speeds():
    assert tempo_filters(.25)=="atempo=0.5000000000,atempo=0.5000000000"
    assert tempo_filters(3)=="atempo=2.0000000000,atempo=1.5000000000"
