"""A24: bounded requests and coverage requirements, never inferred observations."""
from bisect import bisect_left, bisect_right
from datetime import datetime
import hashlib
from typing import Literal
from uuid import UUID, uuid5

from pydantic import Field

from ..errors import AppError
from ..models import Model, now
from ..programs.policy import LIMITS, SourceInterval
from ..programs.validation import require_valid_program
from ..repository import Envelope, Repository, encoded, reference
from ..runs import RunSnapshot
from ..sampling import coarse_schedule
from ..source_evidence import SourceEvidenceAdapter

PLAN_VERSION='observation-plan-1'


class EvidenceRequirement(Model):
    step_id: str
    predicate: str
    entity_ids: list[str]
    zone_id: str | None = None
    phrase: str | None = None
    parameters: dict
    anchor: Literal['interval','cycle_start']
    semantic_support_required: bool
    tracking_required: bool = True
    positive_requirements: list[str]
    negative_requirements: list[str]
    sampling: str


class ObservationWindow(Model):
    id: str
    source_interval: SourceInterval
    working_interval: SourceInterval
    first_working_frame: int = Field(ge=0)
    stop_working_frame: int = Field(gt=0)
    input_source_frame_indices: list[int] = Field(min_length=1,max_length=512)
    required_source_first_frame: int = Field(ge=0)
    required_source_stop_frame: int = Field(gt=0)
    omitted_original_frames: int = Field(ge=0)
    unrepresented_tail_ms: int = Field(ge=0)
    input_map_sha256: str
    sampling_covers_encoded_source: bool


class ObservationPlan(Model):
    schema_version: Literal['observation-plan-1'] = PLAN_VERSION
    run_id: UUID
    program_fingerprint: str
    source_asset_id: UUID
    source_sha256: str
    source_duration_ms: int = Field(gt=0,le=120000)
    source_timeline_sha256: str
    proxy_sha256: str
    proxy_width: int = Field(gt=0)
    proxy_height: int = Field(gt=0)
    windows: list[ObservationWindow] = Field(min_length=1,max_length=24)
    requirements: list[EvidenceRequirement] = Field(min_length=1,max_length=32)
    entity_prompts: dict[str,str]
    muse_requests: int = Field(ge=1,le=64)
    sam_requests: int = Field(ge=1,le=64)
    planned_provider_requests: int = Field(ge=1,le=64)
    max_provider_requests: int = Field(ge=1,le=64)
    full_source_scan_required: Literal[True] = True
    request_fingerprint: str
    providers: dict


class PredicateCoverage(Model):
    attempted_intervals: list[SourceInterval] = Field(default_factory=list,max_length=24)
    observed_intervals: list[SourceInterval] = Field(default_factory=list,max_length=256)
    visibility: Literal['unknown','adequate','occluded','not_visible'] = 'unknown'
    identity: Literal['unknown','unambiguous','ambiguous','lost'] = 'unknown'
    resolution: Literal['unknown','adequate','insufficient'] = 'unknown'
    disconfirming_observation_ids: list[UUID] = Field(default_factory=list,max_length=256)
    sufficient_negative: bool = False
    reasons: list[str] = Field(default_factory=lambda:['No observations have been validated'],max_length=32)


class WindowCoverage(Model):
    interval: SourceInterval
    state: Literal['pending','attempted','completed','failed'] = 'pending'
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error_code: str | None = None


class CoverageLedger(Model):
    run_id: UUID
    plan_fingerprint: str
    windows: dict[str,WindowCoverage] = Field(max_length=24)
    predicates: dict[str,PredicateCoverage] = Field(max_length=32)

    @property
    def scan_complete(self):
        return bool(self.windows) and all(w.state=='completed' for w in self.windows.values())

    @property
    def processing_complete(self):
        return bool(self.windows) and all(w.state in ('completed','failed') for w in self.windows.values())


def evidence_requirements(ast):
    requirements=[]
    semantic={'semantic_event','touches','holds','releases'}
    for step in ast.steps:
        positive=['owned source-frame evidence','unambiguous entity binding','adequate visual resolution']
        if step.predicate in semantic:
            positive+=['visible semantic support, not model rationale alone','compatible tracks/geometry; overlap is not contact']
        elif step.predicate=='matches_attribute':
            positive+=['visible detail establishing the specified attribute']
        elif step.predicate in ('visible','present'):
            positive+=['explicit positive identification in the source image']
        else:
            positive+=['timestamped continuous track samples','declared image-space metric, units and threshold']
        negative=['entire declared predicate window covered','all predicate-relevant source samples observed',
            'identity/visibility/resolution sufficient throughout','no occlusion, missing/failed windows or track gaps',
            'explicit disconfirming observations under the predicate policy; missing output is not negative']
        if step.predicate in semantic:
            negative+=['sparse or internally sampled semantic video inspection alone cannot disprove a brief action']
        requirements.append(EvidenceRequirement(step_id=step.id,predicate=step.predicate,entity_ids=step.entities,
            zone_id=getattr(step,'zone_id',None),anchor=step.anchor,semantic_support_required=step.predicate in semantic,
            phrase=getattr(step,'phrase',None),parameters=step.parameters.model_dump(mode='json'),
            positive_requirements=positive,negative_requirements=negative,
            sampling='actual video frames plus exact source PTS; SAM explicit input-frame samples; never nominal-FPS source evidence'))
    return requirements


def build_plan(ast,timeline,*,run_id,program_fingerprint,duration_ms,working_duration_ms,
               timeline_sha256,proxy_sha256,proxy_width,proxy_height,providers):
    ast=require_valid_program(ast,source_duration_ms=duration_ms)
    if timeline.source_asset_id is None or timeline.source_sha256 is None:
        raise AppError('SOURCE_MISMATCH','Observation planning requires an owned original-frame timeline',409)
    if timeline.source_duration_ms!=duration_ms or not timeline.working_frame_source_indices:
        raise AppError('SOURCE_MISMATCH','Observation planning requires the complete prepared-to-original map',409)
    schedule=coarse_schedule(duration_ms,window_ms=12000,overlap_ms=2000)
    requirements=evidence_requirements(ast)
    used_entities={entity for req in requirements for entity in req.entity_ids}
    prompts={e.id:' '.join([*e.attributes,e.description]) for e in ast.entities if e.id in used_entities}
    muse_count=len(schedule)
    sam_count=len(schedule)*len(prompts)  # SAM supports one concept per request.
    if muse_count+sam_count>LIMITS['provider_calls']:
        raise AppError('OBSERVATION_BUDGET_EXCEEDED','Complete-source Muse/SAM requirements exceed this run\'s provider budget; reduce entities or source duration',422,
            details={'muse_requests':muse_count,'sam_requests':sam_count,'limit':LIMITS['provider_calls']})
    windows=[]
    times=timeline.working_frame_times_ms
    original=timeline.source_frame_times_ms
    for index,(start,end) in enumerate(schedule):
        prepared_end=min(end,working_duration_ms)
        first=bisect_left(times,start);stop=bisect_left(times,prepared_end)
        if prepared_end<=start or first>=stop:
            raise AppError('SAMPLING_GAP','A requested source window has no prepared source samples; no scan was silently truncated',409)
        indices=timeline.working_frame_source_indices[first:stop]
        # Include the original frame already being displayed at the interval's
        # start. VFR-held frames are real evidence, not fabricated new frames.
        required_first=max(0,bisect_right(original,start)-1)
        required_stop=bisect_left(original,end)
        represented=set(indices)
        missing=sum(i not in represented for i in range(required_first,required_stop))
        tail=max(0,end-working_duration_ms)
        windows.append(ObservationWindow(id=f'w{index:03}',source_interval=SourceInterval(start_ms=start,end_ms=end),
            working_interval=SourceInterval(start_ms=start,end_ms=prepared_end),first_working_frame=first,stop_working_frame=stop,
            input_source_frame_indices=indices,required_source_first_frame=required_first,
            required_source_stop_frame=required_stop,omitted_original_frames=missing,unrepresented_tail_ms=tail,
            input_map_sha256=hashlib.sha256(encoded(indices)).hexdigest(),sampling_covers_encoded_source=missing==0 and tail==0))
    identity={'version':PLAN_VERSION,'program_fingerprint':program_fingerprint,'source_sha256':timeline.source_sha256,
        'source_timeline_sha256':timeline_sha256,'proxy_sha256':proxy_sha256,'policy':ast.cycle.policy.model_dump(mode='json'),
        'unknown_policy':ast.unknown_policy.model_dump(mode='json'),'requirements':[r.model_dump(mode='json') for r in requirements],
        'windows':[w.model_dump(mode='json') for w in windows],'entities':prompts,'providers':providers}
    return ObservationPlan(run_id=run_id,program_fingerprint=program_fingerprint,source_asset_id=timeline.source_asset_id,
        source_sha256=timeline.source_sha256,source_duration_ms=duration_ms,source_timeline_sha256=timeline_sha256,
        proxy_sha256=proxy_sha256,proxy_width=proxy_width,proxy_height=proxy_height,windows=windows,
        requirements=requirements,entity_prompts=prompts,muse_requests=muse_count,sam_requests=sam_count,
        planned_provider_requests=muse_count+sam_count,max_provider_requests=LIMITS['provider_calls'],
        request_fingerprint=hashlib.sha256(encoded(identity)).hexdigest(),providers=providers)


def initial_coverage(plan):
    return CoverageLedger(run_id=plan.run_id,plan_fingerprint=plan.request_fingerprint,
        windows={w.id:WindowCoverage(interval=w.source_interval) for w in plan.windows},
        predicates={r.step_id:PredicateCoverage() for r in plan.requirements})


def begin_window(ledger,window_id):
    if window_id not in ledger.windows:
        raise AppError('UNKNOWN_WINDOW','Coverage references an unplanned source window',422)
    window=ledger.windows[window_id]
    if window.state!='pending':raise AppError('INVALID_COVERAGE_TRANSITION','Source window was already attempted',409)
    window.state='attempted';window.started_at=now()


def finish_window(ledger,window_id,*,error_code=None):
    if window_id not in ledger.windows or ledger.windows[window_id].state!='attempted':
        raise AppError('INVALID_COVERAGE_TRANSITION','Only an attempted source window can finish',409)
    window=ledger.windows[window_id]
    window.state='failed' if error_code else 'completed';window.finished_at=now();window.error_code=error_code
    # Deliberately do not change per-predicate visibility/identity/observations or
    # sufficient_negative. Request completion is processing evidence only.


def plan_run(store,settings,project_id,run_id):
    repo=Repository(store)
    run=repo.load(project_id,'runs',run_id)
    snapshot=RunSnapshot.model_validate(run.payload)
    from ..programs.repository import load_program, assert_reviewed
    program,data=load_program(store,project_id,run.program.id,run.program.version)
    if reference(program)!=run.program:
        raise AppError('REFERENCE_MISMATCH','Run program fingerprint differs from the observation plan input',409)
    if snapshot.review!=reference(assert_reviewed(store,program)):
        raise AppError('STALE_REVIEW','Run snapshot does not pin this exact program review',409)
    if snapshot.temporal_policy!=data.program.ast.cycle.policy or snapshot.unknown_policy!=data.program.ast.unknown_policy:
        raise AppError('REVIEW_POLICY_MISMATCH','Observation policy differs from the reviewed AST',409)
    adapter=SourceEvidenceAdapter(store,settings,project_id,run.source_asset_id)
    if adapter.asset.artifact_sha256!=snapshot.source_artifact_sha256:
        raise AppError('SOURCE_MISMATCH','Prepared source changed after the run was snapshotted',409)
    asset=adapter.asset
    plan=build_plan(data.program.ast,adapter.timeline,run_id=run.id,program_fingerprint=run.program.record_sha256,
        duration_ms=asset.metadata.duration_ms,working_duration_ms=asset.working_metadata.duration_ms,
        timeline_sha256=asset.artifact_sha256[asset.timeline_path],proxy_sha256=asset.artifact_sha256[asset.proxy_path],
        proxy_width=asset.proxy_metadata.geometry.display_width,proxy_height=asset.proxy_metadata.geometry.display_height,
        providers=snapshot.providers.model_dump(mode='json'))
    identifier=uuid5(run.id,plan.request_fingerprint)
    with store.mutation():
        if repo.path(project_id,'observation_plans',identifier).exists():
            existing=repo.load(project_id,'observation_plans',identifier)
            if existing.payload.get('plan')!=plan.model_dump(mode='json'):
                raise AppError('CORRUPT_RECORD','Stored observation plan differs from its immutable derived inputs',409)
            return existing
        return repo.publish(Envelope(kind='observation_plans',id=identifier,project_id=run.project_id,
            source_asset_id=run.source_asset_id,source_sha256=run.source_sha256,program=run.program,run=reference(run),
            payload={'plan':plan.model_dump(mode='json'),'coverage':initial_coverage(plan).model_dump(mode='json')}))
