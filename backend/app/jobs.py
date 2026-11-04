from concurrent.futures import ThreadPoolExecutor
from threading import RLock
from typing import Callable
from uuid import UUID
import logging
from pathlib import Path

from .errors import AppError
from .models import Asset, Job, JobStage, JobStatus, Project, TERMINAL, now
from .storage import FileStore
from .recovery import reconcile_files

log = logging.getLogger(__name__)


class JobRunner:
    def __init__(self, store: FileStore, max_queued: int):
        self.store = store
        self.capacity = max_queued + 1
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="videocut")
        self.lock = RLock()
        self.futures = {}
        self.accepting = True
        self.recovery_errors = []
        self.unavailable_exports = set()
        self.cancel_requests = set()

    def recover(self):
        # One manifest at a time; report corruption without discarding user data.
        for path in self.store.projects.glob("*/jobs/*.json"):
            try:
                job = self.store.load(path.parent.parent.name, "jobs", Job, path.stem)
                if job.status not in TERMINAL:
                    job.status = JobStatus.interrupted
                    job.step = "interrupted"
                    job.progress = None
                    job.updated_at = now()
                    if job.stage_history and job.stage_history[-1].finished_at is None:
                        job.stage_history[-1].finished_at = job.updated_at
                    message=("Source preparation stopped; upload again to retry" if job.type=='ingest'
                             else "Processing stopped; retry the previous plan or request from source")
                    job.error = AppError("JOB_INTERRUPTED",message,409,True).envelope()
                    self.store.save(job.project_id, "jobs", job)
                    project = self.store.load(job.project_id, "project", Project)
                    if project.active_job_id == job.id:
                        project.status = "failed"
                        self.store.save(project.id, "project", project)
                    if job.asset_id and job.type == "ingest":
                        asset = self.store.load(job.project_id, "assets", Asset, job.asset_id)
                        if asset.status == "processing":
                            asset.status = "failed"
                            self.store.save(job.project_id, "assets", asset)
            except AppError as exc:
                self.recovery_errors.append({"code": exc.code, "message": exc.message})
        self.recovery_errors.extend(reconcile_files(self.store))
        self.unavailable_exports={item['record_id'] for item in self.recovery_errors
                                  if item.get('record_kind')=='exports' and item.get('record_id')}

    def has_active(self, project_id):
        with self.lock:
            return any(pid == str(project_id) and not future.done()
                       for pid, future in self.futures.values())

    def submit(self, job: Job, operation: Callable[[Job], dict]):
        with self.lock:
            self.futures = {k: v for k, v in self.futures.items() if not v[1].done()}
            if not self.accepting or len(self.futures) >= self.capacity:
                raise AppError("QUEUE_FULL", "Processing queue is full; try again shortly", 429, True)
            if self.has_active(job.project_id):
                raise AppError("PROJECT_BUSY", "This project already has an active job", 409, True)
            self.store.save(job.project_id, "jobs", job)
            # Lock held until registration, so delete cannot miss a submitted job.
            future = self.executor.submit(self._run, job, operation)
            self.futures[str(job.id)] = (str(job.project_id), future)
        return job

    def stage(self, job: Job, step: str, progress: int | None):
        self.check_cancelled(job)
        timestamp=now()
        if not job.stage_history:
            job.stage_history.append(JobStage(step=job.step,started_at=job.created_at,finished_at=timestamp,progress=job.progress))
        if job.stage_history[-1].step!=step:
            job.stage_history[-1].finished_at=timestamp
            job.stage_history.append(JobStage(step=step,started_at=timestamp,progress=progress))
        else:job.stage_history[-1].progress=progress
        job.status = JobStatus.running
        job.step = step
        job.progress = progress
        job.updated_at = now()
        self.store.save(job.project_id, "jobs", job)

    def _run(self, job: Job, operation):
        try:
            self.check_cancelled(job)
            result = operation(job)
            self.check_cancelled(job)
            job.result = result
            job.status = JobStatus.completed
            job.step = "completed"
            job.progress = 100
            with self.store.mutation():
                project=self.store.load(job.project_id,'project',Project)
                if project.active_job_id==job.id and project.status=='processing':
                    project.status='ready'
                    self.store.save(project.id,'project',project)
        except Exception as exc:
            failed_step=job.step
            error = exc if isinstance(exc, AppError) else AppError("JOB_FAILED", "Processing failed; source remains available", 500, True)
            job.status = JobStatus.cancelled if error.code == 'JOB_CANCELLED' else JobStatus.failed
            job.step = job.status.value
            job.progress = None
            job.error = error.envelope()
            # Exception type only: never log provider keys or untrusted stderr.
            relative_file=None
            if isinstance(exc,OSError) and exc.filename:
                try:
                    relative_file=Path(exc.filename).resolve().relative_to(self.store.root).as_posix()
                except ValueError:
                    relative_file="outside_project_storage"
            log.warning("job_failed job=%s step=%s error=%s exception_type=%s winerror=%s storage_file=%s",
                        job.id,failed_step,error.code,type(exc).__name__,getattr(exc,'winerror',None),relative_file)
            if job.asset_id and job.type == "ingest":
                asset = self.store.load(job.project_id, "assets", Asset, job.asset_id)
                asset.status = "failed"
                self.store.save(job.project_id, "assets", asset)
            with self.store.mutation():
                project = self.store.load(job.project_id, "project", Project)
                project.status = "failed"
                self.store.save(project.id, "project", project)
        finally:
            job.updated_at = now()
            if job.stage_history:job.stage_history[-1].finished_at=job.updated_at
            self.store.save(job.project_id, "jobs", job)

    def check_cancelled(self, job):
        if str(job.id) in self.cancel_requests or job.cancel_requested_at is not None:
            if job.cancel_requested_at is None:
                job.cancel_requested_at = self.store.load(job.project_id, 'jobs', Job, job.id).cancel_requested_at
            raise AppError('JOB_CANCELLED', 'Processing cancelled; already verified artifacts are preserved', 409, True)

    def checkpoint(self, job, result):
        """Persist actual partial outputs before another stage can fail/cancel."""
        job.result = dict(result)
        self.store.save(job.project_id, 'jobs', job)

    def cancel(self, job_id):
        """Cooperative cancellation, acknowledged only at an actual boundary."""
        with self.lock, self.store.mutation():
            job = self.find(job_id)
            if job.status in TERMINAL:
                return job
            job.cancel_requested_at = now()
            self.cancel_requests.add(str(job.id))
            self.store.save(job.project_id, 'jobs', job)
            return job

    def find(self, job_id):
        # UUID lookup through project manifests; no persisted global index.
        for path in self.store.projects.glob(f"*/jobs/{UUID(str(job_id))}.json"):
            return self.store.load(path.parent.parent.name, "jobs", Job, job_id)
        raise AppError("NOT_FOUND", "Job not found", 404)

    def close(self):
        with self.lock:
            self.accepting = False
        self.executor.shutdown(wait=True)
