import json
import os
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from app.errors import AppError
from app.models import Job, JobStatus, Project
from app.storage import FileStore
from app.jobs import JobRunner


def test_atomic_failure_preserves_last_good_record_and_removes_temporary(tmp_path, monkeypatch):
    store = FileStore(tmp_path)
    project = Project(name="before")
    store.create_project(project)
    project.name = "after"
    monkeypatch.setattr(os, "replace", lambda *args: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        store.save(project.id, "project", project)
    assert store.load(project.id, "project", Project).name == "before"
    assert list(store.project_dir(project.id).glob(".*.tmp")) == []


def test_atomic_write_survives_a_temporary_windows_sharing_lock(tmp_path,monkeypatch):
    store=FileStore(tmp_path)
    project=Project(name='last good')
    store.create_project(project)
    original_replace=os.replace
    attempts=[]
    def briefly_locked(source,destination):
        attempts.append(1)
        if len(attempts)<3:
            error=PermissionError('temporary Windows sharing lock')
            error.winerror=32
            raise error
        return original_replace(source,destination)
    monkeypatch.setattr(os,'replace',briefly_locked)
    project.name='new valid record'
    store.save(project.id,'project',project)
    assert len(attempts)==3
    assert store.load(project.id,'project',Project).name=='new valid record'
    assert not list(store.project_dir(project.id).glob('.*.tmp'))


def test_serialized_read_modify_write_does_not_lose_updates(tmp_path):
    store = FileStore(tmp_path)
    project = Project(name="counter", requested_duration_ms=1000)
    store.create_project(project)
    def increment(_):
        with store.mutation():
            value = store.load(project.id, "project", Project)
            value.requested_duration_ms += 1
            store.save(project.id, "project", value)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(increment, range(30)))
    assert store.load(project.id, "project", Project).requested_duration_ms == 1030


def test_paths_corruption_and_ownership_fail_closed(tmp_path):
    store = FileStore(tmp_path)
    project = Project(name="one")
    store.create_project(project)
    with pytest.raises(AppError):
        store.project_dir("../../other")
    with pytest.raises(AppError):
        store.path(project.id, "../../outside")
    (store.project_dir(project.id) / "project.json").write_text('{"bad":', encoding="utf-8")
    with pytest.raises(AppError, match="could not be read"):
        store.load(project.id, "project", Project)


def test_recovery_marks_unfinished_jobs_interrupted(tmp_path):
    store = FileStore(tmp_path)
    project = Project(name="recover", status="processing")
    store.create_project(project)
    job = Job(project_id=project.id, type="ingest", status=JobStatus.running, step="probing")
    project.active_job_id = job.id
    store.save(project.id, "project", project)
    store.save(project.id, "jobs", job)
    runner = JobRunner(store, 2)
    try:
        runner.recover()
        assert store.load(project.id, "jobs", Job, job.id).status == JobStatus.interrupted
        assert store.load(project.id, "project", Project).status == "failed"
    finally:
        runner.close()


def test_single_process_lock_excludes_a_second_writer(tmp_path):
    first, second = FileStore(tmp_path), FileStore(tmp_path)
    first.acquire_process_lock()
    try:
        with pytest.raises(RuntimeError, match="one worker"):
            second.acquire_process_lock()
    finally:
        first.release_process_lock()
    second.acquire_process_lock()
    second.release_process_lock()


def test_runner_serializes_work_and_rejects_duplicate_and_full_queue(tmp_path):
    from threading import Event, Lock
    store=FileStore(tmp_path)
    projects=[Project(name=str(i)) for i in range(3)]
    for project in projects: store.create_project(project)
    runner=JobRunner(store,1)
    started,release=Event(),Event()
    observed=[]
    guard=Lock()
    def work(job):
        with guard: observed.append(str(job.id))
        started.set()
        assert release.wait(5)
        return {"actual": True}
    try:
        first=Job(project_id=projects[0].id,type="analyze")
        runner.submit(first,work)
        assert started.wait(3)
        with pytest.raises(AppError,match="active job"):
            runner.submit(Job(project_id=projects[0].id,type="analyze"),work)
        second=Job(project_id=projects[1].id,type="analyze")
        runner.submit(second,work)
        with pytest.raises(AppError,match="queue is full"):
            runner.submit(Job(project_id=projects[2].id,type="analyze"),work)
        assert observed==[str(first.id)]
        release.set()
    finally:
        release.set()
        runner.close()
    assert observed==[str(first.id),str(second.id)]
    assert store.load(projects[1].id,"jobs",Job,second.id).status==JobStatus.completed
