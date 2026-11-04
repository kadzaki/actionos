from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, TypeAdapter, model_validator

from ..editing import OutputSettings
from ..models import Model
from ..programs.policy import SourceInterval
from ..repository import RecordReference
from ..source_evidence import SourceEvidenceReference


class StepEvidence(Model):
    step_id: str = Field(pattern=r'^[a-z][a-z0-9_]{0,63}$')
    label: str = Field(min_length=1, max_length=1000)
    verdict: Literal['true', 'false', 'unknown']
    interval: SourceInterval | None = None
    reason: str = Field(default='', max_length=2000)
    frames: list[SourceEvidenceReference] = Field(default_factory=list, max_length=32)
    observation_ids: list[UUID] = Field(default_factory=list, max_length=64)

    @model_validator(mode='after')
    def supported(self):
        if self.verdict == 'unknown' and not self.reason:
            raise ValueError('Unknown step requires an explicit reason')
        if self.verdict == 'true' and (not self.frames or self.interval is None):
            raise ValueError('Positive step requires actual frame/interval evidence')
        return self


class EventEvidence(Model):
    origin: Literal['manual_fixture', 'evaluated', 'user_correction']
    label: str = Field(min_length=1, max_length=1000)
    interval: SourceInterval
    verdict: Literal['true', 'false', 'unknown']
    steps: list[StepEvidence] = Field(min_length=1, max_length=32)
    review_reason: str = Field(default='', max_length=2000)
    track_ids: list[UUID] = Field(default_factory=list, max_length=16)
    coverage: dict = Field(default_factory=dict)

    @model_validator(mode='after')
    def coherent(self):
        if len({s.step_id for s in self.steps}) != len(self.steps):
            raise ValueError('Step IDs must be unique')
        if self.verdict == 'true' and any(s.verdict != 'true' for s in self.steps):
            raise ValueError('A matched event cannot contain unsupported steps')
        if self.verdict != 'true' and not self.review_reason:
            raise ValueError('Review events require an explicit reason')
        for step in self.steps:
            if step.interval and not (self.interval.start_ms <= step.interval.start_ms < step.interval.end_ms <= self.interval.end_ms):
                raise ValueError('Step interval lies outside the event')
            for frame in step.frames:
                observed=frame.source_time_ms if frame.observed_at_source_ms is None else frame.observed_at_source_ms
                if not self.interval.start_ms <= observed < self.interval.end_ms:
                    raise ValueError('Evidence frame lies outside the event')
                if step.interval and not step.interval.start_ms <= observed < step.interval.end_ms:
                    raise ValueError('Evidence frame lies outside its step')
        return self


class ExtractParameters(Model):
    pre_ms: int = Field(default=0, ge=0, le=10000, strict=True)
    post_ms: int = Field(default=0, ge=0, le=10000, strict=True)
    framing: Literal['full_frame', 'fixed', 'tracked'] = 'full_frame'
    track_id: UUID | None = None
    output: OutputSettings = Field(default_factory=OutputSettings)

    @model_validator(mode='after')
    def track_selection(self):
        if (self.framing == 'tracked') != (self.track_id is not None):
            raise ValueError('Tracked framing requires an explicit track and only tracked framing accepts it')
        return self


class ReportParameters(Model):
    title: str = Field(default='Timestamp review report', min_length=1, max_length=200)


class ActionBase(Model):
    event_ids: list[UUID] = Field(min_length=1, max_length=32)
    review_only: bool = False
    regenerate: bool = False

    @model_validator(mode='after')
    def unique(self):
        if len(set(self.event_ids)) != len(self.event_ids):
            raise ValueError('Duplicate event IDs are not permitted')
        return self


class ExtractRequest(ActionBase):
    type: Literal['extract_clip']
    parameters: ExtractParameters = Field(default_factory=ExtractParameters)


class ReportRequest(ActionBase):
    type: Literal['create_report']
    parameters: ReportParameters = Field(default_factory=ReportParameters)


ActionRequest = Annotated[ExtractRequest | ReportRequest, Field(discriminator='type')]
ACTION_REQUEST = TypeAdapter(ActionRequest)


class ActionPlanData(Model):
    action_contract_version: Literal[1] = 1
    request: ActionRequest
    event_references: list[RecordReference] = Field(min_length=1, max_length=32)
    request_fingerprint: str = Field(pattern=r'^[0-9a-f]{64}$')
    run_fingerprint: str = Field(pattern=r'^[0-9a-f]{64}$')
    tools: dict[str, str]
    fixture_only: bool
    initiated_by: Literal['explicit_user', 'developer_fixture']
