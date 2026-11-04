import asyncio
from dataclasses import replace
import shutil
import subprocess
import sys
from types import SimpleNamespace

from fastapi.responses import Response
from fastapi.testclient import TestClient
import pytest

from app.errors import AppError
from app.main import create_app
from app.media import run_media
from app.resources import MEDIA_SLOT,MediaBudget
from app.storage import FileStore
from app.models import Project
from app.streaming import lease_response
from test_api import create


def test_unknown_length_upload_checks_project_disk_budget_and_cleans_up(settings):
    limited=replace(settings,max_project_mb=1,max_upload_mb=10)
    with TestClient(create_app(limited)) as client:
        project_id=create(client)
        result=client.post(f'/api/projects/{project_id}/upload',
                           content=(b'x'*65536 for _ in range(20)),headers={'Content-Type':'video/mp4'})
        assert result.status_code==507
        assert result.json()['code']=='PROJECT_STORAGE_LIMIT'
        root=limited.data_dir/'projects'/project_id
        assert not list(root.glob('tmp/*.upload'))
        assert not list(root.glob('source/*'))
        assert str(project_id) not in client.app.state.uploading


def test_low_disk_space_rejects_before_accepting_media(settings,monkeypatch):
    monkeypatch.setattr(shutil,'disk_usage',lambda _:SimpleNamespace(free=10*1024*1024))
    with TestClient(create_app(settings)) as client:
        project_id=create(client)
        response=client.post(f'/api/projects/{project_id}/upload',content=b'x',headers={'Content-Type':'video/mp4'})
        assert response.status_code==507
        assert response.json()['code']=='DISK_SPACE_LOW'


def test_timed_out_process_is_killed_and_media_slot_released(monkeypatch):
    actual_popen=subprocess.Popen
    processes=[]
    def record(*args,**kwargs):
        process=actual_popen(*args,**kwargs)
        processes.append(process)
        return process
    monkeypatch.setattr(subprocess,'Popen',record)
    with pytest.raises(AppError) as exc:
        run_media([sys.executable,'-c','import time; time.sleep(5)'],.15)
    assert exc.value.code=='MEDIA_TIMEOUT'
    assert processes[0].poll() is not None
    assert MEDIA_SLOT.acquire(timeout=.1)
    MEDIA_SLOT.release()


def test_process_output_and_growing_media_are_bounded(settings,tmp_path,monkeypatch):
    with pytest.raises(AppError) as exc:
        run_media([sys.executable,'-c',"print('x'*100000)"],3,max_stdout_bytes=1000)
    assert exc.value.code=='MEDIA_OUTPUT_LIMIT'
    project=tmp_path/'growth'
    project.mkdir()
    output=project/'partial.bin'
    code="import pathlib,sys,time; pathlib.Path(sys.argv[1]).write_bytes(b'x'*2000000); time.sleep(5)"
    budget=MediaBudget(project,replace(settings,max_project_mb=1))
    with pytest.raises(AppError) as exc:
        run_media([sys.executable,'-c',code,str(output)],3,budget=budget)
    assert exc.value.code=='PROJECT_STORAGE_LIMIT'


def test_subprocess_arguments_remain_literal_data():
    value="$(do-not-execute); & 'literal argument'"
    result=run_media([sys.executable,'-c','import sys; print(sys.argv[1])',value],3)
    assert result.strip()==value


def test_active_reader_blocks_deletion_and_failed_send_releases_lease(tmp_path):
    store=FileStore(tmp_path)
    project=Project(name='read lease')
    store.create_project(project)
    response=lease_response(store,project.id,lambda:Response(b'frame'))
    with pytest.raises(AppError,match='being read'):
        store.delete_project(project.id)
    async def receive(): return {'type':'http.disconnect'}
    async def failed_send(message): raise RuntimeError('client disconnected')
    with pytest.raises(RuntimeError,match='client disconnected'):
        asyncio.run(response({'type':'http'},receive,failed_send))
    assert store._media_leases=={}
    store.delete_project(project.id)
    assert not store.project_dir(project.id).exists()
