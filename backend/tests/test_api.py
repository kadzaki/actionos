from pathlib import Path
import hashlib
import json
import time
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


def create(client):
    response = client.post("/api/projects", json={"name": "Footage", "action_prompt": "Extract the last spin"})
    assert response.status_code == 201
    return response.json()["project_id"]


def wait_job(client, job_id):
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        result = client.get(f"/api/jobs/{job_id}").json()
        if result["status"] in {"completed", "failed", "interrupted", "cancelled"}:
            return result
        time.sleep(.05)
    raise AssertionError("Real media job did not finish within 40 seconds")


def test_capabilities_do_not_claim_unverified_analysis(client):
    data = client.get("/api/capabilities").json()
    assert data["storage"] == "json_files"
    assert data["action_scope"] == "any_action_prompt"
    assert data["muse"]["available"] is False
    assert data["operations"]["analyze"] is False
    assert data['studio']['program']['available'] is True
    assert data['operations']['compile'] is False
    assert data['studio']['run']['available'] is False
    assert data['studio']['report']['available'] is False
    assert data['provider_contracts']=={'compiler':'not_tested','predicate_observations':'not_tested'}
    assert client.get("/api/health").status_code == 200


def test_missing_media_tools_disable_operations_before_render_is_accepted(client,monkeypatch):
    monkeypatch.setattr('app.main.media_tools',lambda:{'ffmpeg':False,'ffprobe':True})
    assert client.get('/api/capabilities').json()['operations']['render'] is False
    response=client.post(f'/api/projects/{uuid4()}/reels',json={})
    assert response.status_code==503 and response.json()['code']=='MEDIA_TOOLS_UNAVAILABLE'


@pytest.mark.parametrize("variant", ["audio", "silent", "rotated", "vfr", "mov", "sar"])
def test_real_upload_normalization_seek_and_delete(client, settings, media, variant):
    project_id = create(client)
    original = media[variant].read_bytes()
    response = client.post(f"/api/projects/{project_id}/upload", content=original,
                           headers={"Content-Type": "video/mp4", "X-File-Name": '../../odd$(name).mp4'})
    assert response.status_code == 202, response.text
    job = wait_job(client, response.json()["job_id"])
    assert job["status"] == "completed", job
    history=job['stage_history']
    assert [entry['step'] for entry in history]==['queued','probing','mapping_timestamps','normalizing','creating_preview','verifying']
    assert all(entry['started_at']<=entry['finished_at']<=job['updated_at'] for entry in history)
    result = client.get(f"/api/projects/{project_id}").json()
    assert result["project"]["status"] == "ready"
    asset = result["asset"]
    assert asset["sha256"] == hashlib.sha256(original).hexdigest()
    assert (settings.data_dir / "projects" / project_id / asset["source_path"]).read_bytes() == original
    assert asset["working_metadata"]["fps"] == 30
    assert asset["working_metadata"]["geometry"]["rotation_degrees"] == 0
    assert asset["working_metadata"]["audio_present"] == (variant in {"audio", "rotated", "mov"})
    if variant == "sar":
        assert asset["metadata"]["geometry"]["sample_aspect_ratio"] == 2
        assert asset["working_metadata"]["geometry"]["sample_aspect_ratio"] == 1
        assert asset["working_metadata"]["geometry"]["display_width"] == 640
    if variant == "rotated":
        assert round(asset["metadata"]["geometry"]["rotation_degrees"]) in (90, 270)
        assert asset["working_metadata"]["geometry"]["display_height"] > asset["working_metadata"]["geometry"]["display_width"]
    timeline = json.loads((settings.data_dir / "projects" / project_id / asset["timeline_path"]).read_text())
    if variant == "vfr":
        times = timeline["source_frame_times_ms"]
        assert len({round(b-a) for a,b in zip(times,times[1:])}) > 1
    url = f"/api/assets/{asset['id']}/stream"
    full = client.get(url)
    assert full.status_code == 200
    for header, expected in [("bytes=0-99", full.content[:100]), ("bytes=-33", full.content[-33:]), ("bytes=25-", full.content[25:])]:
        part = client.get(url, headers={"Range": header})
        assert part.status_code == 206
        assert part.content == expected
        assert int(part.headers["content-length"]) == len(expected)
    assert client.head(url).headers["content-length"] == str(len(full.content))
    assert client.get(url, headers={"Range": "bytes=999999999-"}).status_code == 416
    assert client.get(url, headers={"Range": "bytes=0-1,3-4"}).status_code == 416
    assert client.get(f"/api/assets/{asset['id']}/thumbnail").headers["content-type"] == "image/jpeg"
    assert client.post(f"/api/projects/{project_id}/upload", content=original, headers={"Content-Type":"video/mp4"}).status_code == 409
    assert client.delete(f"/api/projects/{project_id}").status_code == 204
    assert not (settings.data_dir / "projects" / project_id).exists()


def test_invalid_upload_failure_is_honest_and_project_can_be_deleted(client, settings):
    project_id = create(client)
    response = client.post(f"/api/projects/{project_id}/upload", content=b"not a video", headers={"Content-Type": "video/mp4"})
    job = wait_job(client, response.json()["job_id"])
    assert job["status"] == "failed"
    assert job["error"]["code"] == "MEDIA_PROCESS_FAILED"
    assert client.get(f"/api/projects/{project_id}").json()["project"]["status"] == "failed"
    assert client.delete(f"/api/projects/{project_id}").status_code == 204


def test_upload_size_checks_unknown_length_and_missing_project(client, settings):
    project_id = create(client)
    url = f"/api/projects/{project_id}/upload"
    assert client.post(url, content=b"hello", headers={"Content-Type": "text/html"}).status_code == 415
    assert client.post(url, content=b"", headers={"Content-Type": "video/mp4"}).status_code == 422
    response = client.post(url, content=(b"x" * 65536 for _ in range(33)), headers={"Content-Type": "video/mp4"})
    assert response.status_code == 413
    assert not list((settings.data_dir / "projects" / project_id / "tmp").glob("*.upload"))
    assert client.post(f"/api/projects/{uuid4()}/upload", content=b"x", headers={"Content-Type":"video/mp4"}).status_code == 404
    assert client.get('/api/does-not-exist').status_code == 404
    assert client.post('/api/projects', json={"name":"x", "unknown":"oops"}).json()["code"] == "VALIDATION_ERROR"


def test_project_persists_across_application_restart(settings):
    with TestClient(create_app(settings)) as client:
        project_id = create(client)
    with TestClient(create_app(settings)) as client:
        result = client.get(f"/api/projects/{project_id}")
        assert result.status_code == 200
        assert result.json()["project"]["action_prompt"] == "Extract the last spin"


def test_api_misses_never_return_spa_html(settings):
    settings.static_dir.mkdir()
    (settings.static_dir / "index.html").write_text('<!doctype html><title>VideoCut</title>')
    with TestClient(create_app(settings)) as client:
        assert client.get('/').status_code == 200
        assert client.get('/project/some-view').status_code == 200
        assert client.get('/api/missing').json()["code"] == "NOT_FOUND"
        assert client.get('/assets/missing.js').status_code == 404


def test_normalization_preserves_audio_start_offset(client,settings,media,tmp_path):
    import array
    import subprocess
    delayed=tmp_path/"delayed-audio.mp4"
    subprocess.run(["ffmpeg","-hide_banner","-loglevel","error","-y","-i",str(media["silent"]),
                    "-itsoffset","0.5","-i",str(media["audio"]),"-map","0:v:0","-map","1:a:0",
                    "-c:v","copy","-c:a","aac","-t","1.5",str(delayed)],check=True,timeout=20)
    project_id=create(client)
    response=client.post(f"/api/projects/{project_id}/upload",content=delayed.read_bytes(),headers={"Content-Type":"video/mp4"})
    assert wait_job(client,response.json()["job_id"])["status"]=="completed"
    asset=client.get(f"/api/projects/{project_id}").json()["asset"]
    working=settings.data_dir/"projects"/project_id/asset["working_path"]
    result=subprocess.run(["ffmpeg","-hide_banner","-loglevel","error","-i",str(working),"-vn","-f","s16le","-ac","1","-ar","8000","-"],capture_output=True,check=True,timeout=20)
    samples=array.array('h',result.stdout)
    before=sum(abs(s) for s in samples[:2400])/2400
    after=sum(abs(s) for s in samples[5600:8000])/2400
    assert after>100
    assert before<after*.02


def test_actual_duration_limit_rejects_short_named_but_long_video(settings,media):
    from dataclasses import replace
    limited=replace(settings,max_video_seconds=1)
    with TestClient(create_app(limited)) as client:
        project_id=create(client)
        response=client.post(f"/api/projects/{project_id}/upload",content=media['audio'].read_bytes(),headers={'Content-Type':'video/mp4'})
        job=wait_job(client,response.json()['job_id'])
        assert job['status']=='failed'
        assert job['error']['code']=='VIDEO_TOO_LONG'
