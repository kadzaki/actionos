"""Typed Muse hypotheses and provider-claimed coverage, not evaluated truth."""
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, model_validator

from ..models import Model
from ..programs.policy import SourceInterval
from ..programs.predicates import NodeId
from ..source_evidence import SourceEvidenceReference

FrameIndex=Annotated[int,Field(ge=0,le=511,strict=True)]
Note=Annotated[str,Field(min_length=1,max_length=1000)]


class FrameRange(Model):
    start_frame: int = Field(ge=0,le=511,strict=True)
    stop_frame: int = Field(gt=0,le=512,strict=True)

    @model_validator(mode='after')
    def nonempty(self):
        if self.stop_frame<=self.start_frame:raise ValueError('Frame interval must be nonempty and half-open')
        return self


class EntityAppearance(Model):
    entity_id: NodeId
    role: Literal['actor','object','instrument','context']
    description: str = Field(min_length=1,max_length=1000)
    visibility: Literal['clear','occluded','not_visible','unknown']
    identity: Literal['unambiguous','ambiguous','unknown']
    evidence_frame_indices: list[FrameIndex] = Field(default_factory=list,max_length=8)
    uncertainty: list[Note] = Field(default_factory=list,max_length=16)

    @model_validator(mode='after')
    def grounded(self):
        if self.visibility=='clear' and not self.evidence_frame_indices:
            raise ValueError('Clear entity appearance requires actual input-frame references')
        if len(set(self.evidence_frame_indices))!=len(self.evidence_frame_indices):
            raise ValueError('Entity evidence frame references must be unique')
        return self


class StepHypothesisDraft(Model):
    step_id: NodeId
    entity_ids: list[NodeId] = Field(default_factory=list,max_length=8)
    assessment: Literal['supported','disconfirmed','uncertain','not_observed']
    interval: FrameRange | None = None
    evidence_frame_indices: list[FrameIndex] = Field(default_factory=list,max_length=8)
    description: str = Field(min_length=1,max_length=2000)
    uncertainty: list[Note] = Field(default_factory=list,max_length=16)
    quality: Literal['low','medium','high']
    boundary_uncertainty_ms: int = Field(default=100,ge=0,le=2000,strict=True)

    @model_validator(mode='after')
    def support(self):
        if len(set(self.entity_ids))!=len(self.entity_ids) or len(set(self.evidence_frame_indices))!=len(self.evidence_frame_indices):
            raise ValueError('Entity/evidence references must be unique')
        if self.assessment in ('supported','disconfirmed') and (self.interval is None or not self.evidence_frame_indices):
            raise ValueError('Positive or disconfirming hypotheses require actual interval/frame references')
        if self.assessment in ('uncertain','not_observed') and not self.uncertainty:
            raise ValueError('Uncertain or not-observed hypotheses require explicit reasons')
        if self.evidence_frame_indices and self.interval is None:
            raise ValueError('Evidence frames require an explicit hypothesis interval')
        if self.interval and any(not self.interval.start_frame<=i<self.interval.stop_frame for i in self.evidence_frame_indices):
            raise ValueError('Evidence frame lies outside its hypothesis interval')
        return self


class StepCoverageClaim(Model):
    step_id: NodeId
    attempted_frames: FrameRange
    observed_frame_ranges: list[FrameRange] = Field(default_factory=list,max_length=64)
    complete_inspection_claim: bool
    visibility: Literal['clear','occluded','not_visible','unknown']
    identity: Literal['unambiguous','ambiguous','unknown']
    resolution: Literal['adequate','insufficient','unknown']
    uncertainty: list[Note] = Field(default_factory=list,max_length=16)


class ObservationResponse(Model):
    schema_version: Literal['predicate-observations-1']
    window_id: UUID
    entities: list[EntityAppearance] = Field(default_factory=list,max_length=16)
    hypotheses: list[StepHypothesisDraft] = Field(default_factory=list,max_length=256)
    coverage: list[StepCoverageClaim] = Field(default_factory=list,max_length=32)
    notes: list[Note] = Field(default_factory=list,max_length=16)


class StepObservation(Model):
    id: UUID
    run_id: UUID
    program_fingerprint: str
    source_asset_id: UUID
    source_sha256: str
    window_id: UUID
    step_id: NodeId
    entity_ids: list[NodeId]
    entity_roles: dict[str,str]
    predicate: str
    phrase: str | None
    hypothesis: Literal['supported','disconfirmed','uncertain','not_observed']
    provider_assessment: Literal['supported','disconfirmed','uncertain','not_observed']
    source_interval: SourceInterval | None
    attempted_source_interval: SourceInterval
    frames: list[SourceEvidenceReference] = Field(max_length=8)
    description: str
    uncertainty: list[str]
    quality: Literal['low','medium','high']
    boundary_uncertainty_ms: int
    origin: Literal['meta_cloud_hypothesis'] = 'meta_cloud_hypothesis'
    estimated: Literal[True] = True
    provider_provenance: dict


class NormalizedCoverageClaim(Model):
    step_id: NodeId
    requested_source_interval: SourceInterval
    attempted_source_interval: SourceInterval | None
    mapped_observed_intervals: list[SourceInterval]
    provider_claim: StepCoverageClaim | None
    sampling_covers_encoded_source: bool
    negative_coverage_verified: Literal[False] = False
    reasons: list[str]


class ObservationBatch(Model):
    run_id: UUID
    window_id: UUID
    program_fingerprint: str
    observations: list[StepObservation] = Field(max_length=256)
    entities: list[EntityAppearance] = Field(max_length=16)
    coverage: list[NormalizedCoverageClaim] = Field(max_length=32)
    missing_step_ids: list[NodeId] = Field(max_length=32)
    provider_provenance: dict
    raw_provider_response: dict
