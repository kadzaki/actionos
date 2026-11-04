"""One-worker action execution, partial preservation and idempotent dispatch."""
from .contracts import ActionPlanData
from .reports import build_report
from .worldcut import extract_event
from ..errors import AppError
from ..models import Job, JobStatus, Project
from ..repository import ArtifactReference, Envelope, Repository, reference
from ..runs import RunDispatcher
from ..storage import file_sha256


def request_execution(store, runner, settings, plan):
    repo = Repository(store)
    plan = repo.load(plan.project_id, 'action_plans', plan.id)
    data = ActionPlanData.model_validate(plan.payload)
    with runner.lock, store.mutation():
        jobs = store.list_records(plan.project_id, 'jobs', Job)
        for job in sorted(jobs, key=lambda j: j.created_at, reverse=True):
            if job.action_id != plan.id:
                continue
            if job.status in (JobStatus.queued, JobStatus.running):
                return job, True
            if job.status == JobStatus.completed:
                repo.load(plan.project_id, 'action_results', job.id)
                return job, True
        job = RunDispatcher(store, runner, settings).submit(plan.project_id, plan.run.id,
            lambda snapshot, current: execute(store, runner, settings, plan, current),
            stage='action' if data.request.type == 'extract_clip' else 'report', action_id=plan.id)
        project=store.load(plan.project_id,'project',Project)
        project.active_job_id=job.id;project.active_run_id=plan.run.id;project.status='processing'
        store.save(project.id,'project',project)
        return job, False


def execute(store, runner, settings, plan, job):
    repo = Repository(store)
    plan = repo.load(plan.project_id, 'action_plans', plan.id)
    data = ActionPlanData.model_validate(plan.payload)
    completed, failed, outputs, artifacts, refs = [], [], [], [], [reference(plan)]
    requested = [str(r.id) for r in data.event_references]
    def progress():
        return {'action_id': str(plan.id), 'status': 'running', 'requested_event_ids': requested,
                'completed_event_ids': list(completed), 'failed_events': list(failed), 'outputs': list(outputs),
                'fixture_only': data.fixture_only}
    if data.request.type == 'create_report':
        runner.stage(job, 'reporting', None)
        try:
            report = build_report(store, settings, plan)
            refs.append(reference(report))
            outputs.append({'type': 'report', 'report_id': str(report.id),
                'view_url': f'/api/reports/{report.id}/view', 'json_url': f'/api/reports/{report.id}/download'})
            completed.extend(requested)
            runner.checkpoint(job, progress())
        except AppError as exc:
            failed.extend({'event_id': event_id, 'error': exc.envelope()} for event_id in requested)
    else:
        for event_ref in data.event_references:
            runner.check_cancelled(job)
            try:
                event = repo.load(plan.project_id, 'temporal_events', event_ref.id)
                export = extract_event(store, settings, plan, event,
                    lambda step, pct: runner.stage(job, step, pct))
                for relative, media_type in ((export.path, 'video/mp4'),
                        (export.manifest_path, 'application/json'),
                        (f'exports/{export.id}.json', 'application/json'),
                        (f'plans/{export.plan_id}/v{export.plan_version}.json', 'application/json')):
                    path = store.path(plan.project_id, relative)
                    artifacts.append(ArtifactReference(path=relative, sha256=file_sha256(path),
                        byte_size=path.stat().st_size, media_type=media_type))
                outputs.append({'type': 'video', 'event_id': str(event.id), 'export_id': str(export.id),
                    'stream_url': f'/api/exports/{export.id}/stream',
                    'download_url': f'/api/exports/{export.id}/download'})
                completed.append(str(event.id))
            except AppError as exc:
                if exc.code == 'JOB_CANCELLED':
                    raise
                failed.append({'event_id': str(event_ref.id), 'error': exc.envelope()})
            runner.checkpoint(job, progress())
    runner.stage(job, 'verifying', None)
    status = 'partial' if completed and failed else ('completed' if completed else 'failed')
    result = {**progress(), 'status': status}
    manifest = repo.publish(Envelope(kind='action_results', id=job.id, project_id=plan.project_id,
        source_asset_id=plan.source_asset_id, source_sha256=plan.source_sha256,
        program=plan.program, run=plan.run, references=refs, artifacts=artifacts, payload=result))
    result['result_id'] = str(manifest.id)
    runner.checkpoint(job, result)
    if status == 'failed':
        raise AppError('ACTION_FAILED', 'No selected event produced a verified output; failure details are preserved', 422, True)
    return result
