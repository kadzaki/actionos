from threading import Event
import time

import pytest

from app.jobs import JobRunner
from app.models import Job, JobStatus
from app.repository import Envelope, Repository
from app.runs import RunDispatcher, RunSnapshot, snapshot_run
from app.storage import FileStore
from test_evidence import prepare


def setup_run(client, settings, media):
    asset = prepare(client, media['silent'])
    store = FileStore(settings.data_dir)
    program = Repository(store).publish(Envelope(kind='programs', project_id=asset.project_id,
        source_asset_id=asset.id, source_sha256=asset.sha256,
        payload={'fixture_only': True, 'instruction': 'storage/dispatcher fixture, no perception'}))
    run = snapshot_run(store, settings, asset.project_id, program.id, 1,
        muse_adapter_version='fixture-only', sam_adapter_version='fixture-only',
        evaluator_version='fixture-only')
    return store, program, run


def wait(store, job):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        saved = store.load(job.project_id, 'jobs', Job, job.id)
        if saved.status in (JobStatus.completed, JobStatus.failed, JobStatus.cancelled):
            return saved
        time.sleep(.01)
    raise AssertionError('Dispatcher did not finish')


def test_active_snapshot_is_unaffected_by_new_program_version(client, settings, media):
    store, program, run = setup_run(client, settings, media)
    runner = JobRunner(store, 2)
    entered, release = Event(), Event()
    observed = []
    def operation(snapshot, job):
        runner.stage(job, 'observing', None)
        entered.set()
        assert release.wait(5)
        observed.append(snapshot.program.version)
        runner.stage(job, 'evaluating', 50)
        return {'fixture_only': True, 'program_version': snapshot.program.version}
    try:
        job = RunDispatcher(store, runner, settings).submit(program.project_id, run.id, operation)
        assert entered.wait(3)
        Repository(store).publish(program.model_copy(update={'version': 2, 'payload': {'changed': True}}))
        release.set()
        result = wait(store, job)
        assert observed == [1]
        assert result.status == JobStatus.completed
        assert result.run_id == run.id and result.program_version == 1
        assert result.result['program_version'] == 1
        assert [s.step for s in result.stage_history] == ['queued', 'observing', 'evaluating']
        assert all(s.finished_at is not None and s.started_at <= s.finished_at for s in result.stage_history)
        assert RunSnapshot.model_validate(Repository(store).load(program.project_id, 'runs', run.id).payload).program.version == 1
        assert 'model_api_key' not in str(run.model_dump())
    finally:
        release.set()
        runner.close()


def test_evidence_policy_is_pinned_and_old_runs_are_not_silently_requalified(client,settings,media):
    from app.engine.fusion import FUSION_VERSION,require_evidence_policy
    from app.errors import AppError
    _,_,run=setup_run(client,settings,media)
    snapshot=RunSnapshot.model_validate(run.payload)
    assert snapshot.evidence_policy_version==FUSION_VERSION
    require_evidence_policy(snapshot)
    older=dict(run.payload);older.pop('evidence_policy_version')
    old=RunSnapshot.model_validate(older)
    assert old.evidence_policy_version=='unqualified'
    with pytest.raises(AppError) as exc:require_evidence_policy(old)
    assert exc.value.code=='EVIDENCE_POLICY_MISMATCH'


def test_cancel_is_requested_then_acknowledged_preserving_partial_results(client, settings, media):
    store, program, run = setup_run(client, settings, media)
    runner = JobRunner(store, 2)
    entered, release = Event(), Event()
    def operation(snapshot, job):
        runner.stage(job, 'observing', None)
        runner.checkpoint(job, {'verified_fixture_artifacts': ['fixture-1']})
        entered.set()
        assert release.wait(5)
        runner.stage(job, 'evaluating', 50)
        raise AssertionError('Cancelled operation continued')
    try:
        job = RunDispatcher(store, runner, settings).submit(program.project_id, run.id, operation)
        assert entered.wait(3)
        requested = runner.cancel(job.id)
        assert requested.status == JobStatus.running
        assert requested.cancel_requested_at is not None
        release.set()
        result = wait(store, job)
        assert result.status == JobStatus.cancelled
        assert result.cancel_requested_at is not None
        assert result.result == {'verified_fixture_artifacts': ['fixture-1']}
        assert result.error['code'] == 'JOB_CANCELLED'
    finally:
        release.set()
        runner.close()


def test_recovery_of_expanded_jobs_preserves_snapshot_and_partial_results(client, settings, media):
    store, program, run = setup_run(client, settings, media)
    for kind in ('compile', 'observe', 'evaluate', 'report', 'action'):
        job = Job(project_id=program.project_id, asset_id=run.source_asset_id,
                  program_id=program.id, program_version=1, run_id=run.id, type=kind,
                  status=JobStatus.running, step='observing', result={'partial': ['verified-fixture']})
        store.save(program.project_id, 'jobs', job)
    runner = JobRunner(store, 2)
    try:
        runner.recover()
        jobs = store.list_records(program.project_id, 'jobs', Job)
        interrupted = [j for j in jobs if j.type in ('compile', 'observe', 'evaluate', 'report', 'action')]
        assert len(interrupted) == 5
        assert all(j.status == JobStatus.interrupted and j.error['retryable'] for j in interrupted)
        assert all(j.result == {'partial': ['verified-fixture']} for j in interrupted)
        assert Repository(store).load(program.project_id, 'runs', run.id) == run
    finally:
        runner.close()
