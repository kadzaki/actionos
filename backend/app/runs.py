"""Immutable run input snapshots and the single-worker dispatch seam.

No perception/evaluation implementation or program approval is inferred here.
The program review gate is added by A21 before exposing a public run endpoint.
"""
from typing import Literal

from pydantic import Field

from .errors import AppError
from .models import Job, Model
from .programs.policy import TemporalPolicy, UnknownPolicy, POLICY_VERSION, EVIDENCE_POLICY_VERSION
from .repository import Envelope, RecordReference, Repository, fingerprint, reference
from .source_evidence import SourceEvidenceAdapter


class ProviderConfiguration(Model):
    endpoint: Literal['https://api.meta.ai/v1'] = 'https://api.meta.ai/v1'
    muse_model: str = Field(min_length=1, max_length=128)
    sam_model: str = Field(min_length=1, max_length=128)
    muse_adapter_version: str = Field(min_length=1, max_length=64)
    sam_adapter_version: str = Field(min_length=1, max_length=64)


class RunSnapshot(Model):
    snapshot_schema_version: Literal[1] = 1
    program: RecordReference
    review: RecordReference | None = None
    source_artifact_sha256: dict[str, str]
    temporal_policy: TemporalPolicy
    unknown_policy: UnknownPolicy
    providers: ProviderConfiguration
    evaluator_version: str = Field(min_length=1, max_length=64)
    evidence_policy_version: str = Field(default='unqualified',min_length=1,max_length=64)
    reuse_parent: RecordReference | None = None


def snapshot_run(store, settings, project_id, program_id, program_version, *,
                 temporal_policy=None, unknown_policy=None,
                 muse_adapter_version, sam_adapter_version, evaluator_version,reuse_parent=None,reuse_artifacts=None):
    repo = Repository(store)
    with store.media_lease(project_id), store.mutation():
        program = repo.load(project_id, 'programs', program_id, program_version)
        review = None
        if program.payload.get('fixture_only') is not True:
            from .programs.repository import assert_reviewed, load_program
            review = assert_reviewed(store,program)
            _,data = load_program(store,project_id,program_id,program_version)
            reviewed_temporal = data.program.ast.cycle.policy
            reviewed_unknown = data.program.ast.unknown_policy
            if ((temporal_policy is not None and TemporalPolicy.model_validate(temporal_policy)!=reviewed_temporal)
                    or (unknown_policy is not None and UnknownPolicy.model_validate(unknown_policy)!=reviewed_unknown)):
                raise AppError('REVIEW_POLICY_MISMATCH','Execution policy differs from the reviewed program; create and review a new version',409)
            temporal_policy = reviewed_temporal
            unknown_policy = reviewed_unknown
        source = SourceEvidenceAdapter(store, settings, project_id, program.source_asset_id)
        if source.asset.sha256 != program.source_sha256:
            raise AppError('SOURCE_MISMATCH', 'Program is not bound to the current source bytes', 409)
        snapshot = RunSnapshot(program=reference(program),review=reference(review) if review else None,
            source_artifact_sha256=dict(source.asset.artifact_sha256),
            temporal_policy=temporal_policy or TemporalPolicy(),
            unknown_policy=unknown_policy or UnknownPolicy(),
            providers=ProviderConfiguration(endpoint=settings.meta_api_base_url.rstrip('/'),
                muse_model=settings.muse_model, sam_model=settings.sam_model,
                muse_adapter_version=muse_adapter_version, sam_adapter_version=sam_adapter_version),
            evaluator_version=evaluator_version,evidence_policy_version=EVIDENCE_POLICY_VERSION,reuse_parent=reference(reuse_parent) if reuse_parent else None)
        return repo.publish(Envelope(kind='runs', project_id=program.project_id,
            source_asset_id=program.source_asset_id, source_sha256=program.source_sha256,
            program=reference(program),references=[reference(review)] if review else [],artifacts=reuse_artifacts or [],payload=snapshot.model_dump(mode='json')))


class RunDispatcher:
    def __init__(self, store, runner, settings):
        self.store, self.runner, self.settings = store, runner, settings

    def submit(self, project_id, run_id, operation, *, stage='observe', action_id=None):
        if stage not in ('observe', 'evaluate', 'report', 'action'):
            raise AppError('INVALID_OPERATION', 'Unsupported run operation', 422)
        run = Repository(self.store).load(project_id, 'runs', run_id)
        snapshot = RunSnapshot.model_validate(run.payload)
        if snapshot.program != run.program:
            raise AppError('REFERENCE_MISMATCH', 'Snapshot program differs from its envelope', 409)
        source = SourceEvidenceAdapter(self.store, self.settings, project_id, run.source_asset_id)
        if source.asset.artifact_sha256 != snapshot.source_artifact_sha256:
            raise AppError('SOURCE_MISMATCH', 'Prepared source differs from the run snapshot', 409)
        if action_id is not None:
            action = Repository(self.store).load(project_id, 'action_plans', action_id)
            if action.run != reference(run):
                raise AppError('OWNERSHIP', 'Action is not owned by the dispatched run', 409)
        job = Job(project_id=run.project_id, type=stage, asset_id=run.source_asset_id,
            run_id=run.id, action_id=action_id, program_id=run.program.id,
            program_version=run.program.version, program_fingerprint=run.program.record_sha256,
            request={'run_fingerprint': fingerprint(run), 'policy_version': POLICY_VERSION})
        # Capture a deep snapshot, never a mutable current-program lookup.
        captured = snapshot.model_copy(deep=True)
        return self.runner.submit(job, lambda current: operation(captured, current))
