"""File and HTTP contracts. Action vocabulary is intentionally open text."""
from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


def now():
    return datetime.now(timezone.utc)


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True, allow_inf_nan=False)


class Record(Model):
    schema_version: Literal[1] = 1
    id: UUID = Field(default_factory=uuid4)
    created_at: datetime = Field(default_factory=now)


class Project(Record):
    name: str = Field(min_length=1, max_length=120)
    status: Literal["new", "processing", "ready", "failed"] = "new"
    active_asset_id: UUID | None = None
    active_job_id: UUID | None = None
    active_intent_id: UUID | None = None
    active_program_id: UUID | None = None
    active_run_id: UUID | None = None
    active_compilation_id: UUID | None = None
    action_prompt: str = Field(default="", max_length=8000)
    requested_duration_ms: int = Field(default=20000, ge=1000, le=120000)


class ProjectCreate(Model):
    name: str = Field(default="Untitled action", min_length=1, max_length=120)
    action_prompt: str = Field(default="", max_length=8000)
    requested_duration_ms: int = Field(default=20000, ge=1000, le=120000)


class ProjectPreferences(Model):
    action_prompt: str = Field(min_length=1, max_length=8000)
    requested_duration_ms: int = Field(default=20000, ge=1000, le=120000)


class Geometry(Model):
    # Source is the oriented display frame; coded pixels are kept separately.
    coded_width: int = Field(gt=0)
    coded_height: int = Field(gt=0)
    display_width: int = Field(gt=0)
    display_height: int = Field(gt=0)
    rotation_degrees: float
    sample_aspect_ratio: float = Field(gt=0)


class MediaMetadata(Model):
    duration_ms: int = Field(gt=0)
    fps: float = Field(gt=0)
    codec: str
    container: str
    geometry: Geometry
    audio_present: bool
    audio_codec: str | None = None
    video_start_seconds: float = 0
    video_stream_index: int = Field(default=0, ge=0)
    video_time_base: str = "1/1000"


class Asset(Record):
    project_id: UUID
    media_layout: Literal['legacy', 'actionos'] = 'legacy'
    status: Literal["processing", "ready", "failed"] = "processing"
    source_path: str
    working_path: str | None = None
    proxy_path: str | None = None
    thumbnail_path: str | None = None
    timeline_path: str | None = None
    original_name: str
    byte_size: int = Field(gt=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    metadata: MediaMetadata | None = None
    working_metadata: MediaMetadata | None = None
    proxy_metadata: MediaMetadata | None = None
    # New ingests bind derivative bytes. Legacy assets remain readable but must
    # be requalified before they can supply ActionOS run evidence.
    artifact_sha256: dict[str, Annotated[str, Field(pattern=r'^[0-9a-f]{64}$')]] = Field(default_factory=dict, max_length=8)


class EvidenceFrame(Record):
    project_id: UUID
    source_asset_id: UUID
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_frame_index: int = Field(ge=0)
    source_time_ms: float = Field(ge=0)
    source_pts: int
    source_time_base: str
    image_path: str
    image_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    width: int = Field(gt=0)
    height: int = Field(gt=0)


class JobStatus(str, Enum):
    queued = "queued"
    running = "running"
    completed = "completed"
    failed = "failed"
    interrupted = "interrupted"
    cancelled = "cancelled"


TERMINAL = {JobStatus.completed, JobStatus.failed, JobStatus.interrupted, JobStatus.cancelled}


class JobStage(Model):
    step: str
    started_at: datetime
    finished_at: datetime | None = None
    progress: int | None = Field(default=None,ge=0,le=100)


class Job(Record):
    project_id: UUID
    type: Literal["ingest", "analyze", "track", "render", "revise", "compile", "observe", "evaluate", "report", "action"]
    asset_id: UUID | None = None
    program_id: UUID | None = None
    program_version: int | None = Field(default=None, ge=1)
    program_fingerprint: str | None = Field(default=None, pattern=r'^[0-9a-f]{64}$')
    run_id: UUID | None = None
    action_id: UUID | None = None
    cancel_requested_at: datetime | None = None
    status: JobStatus = JobStatus.queued
    step: str = "queued"
    progress: int | None = Field(default=0, ge=0, le=100)
    updated_at: datetime = Field(default_factory=now)
    error: dict | None = None
    result: dict = Field(default_factory=dict)
    request: dict = Field(default_factory=dict)
    stage_history: list[JobStage] = Field(default_factory=list,max_length=2048)


class Occurrence(Model):
    mode: Literal["best", "first", "ordinal", "last", "all"] = "best"
    ordinal: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def ordinal_required(self):
        if (self.mode == "ordinal") != (self.ordinal is not None):
            raise ValueError("Only ordinal selection requires an ordinal number")
        return self


class ActionIntent(Record):
    project_id: UUID
    source_asset_id: UUID
    original_prompt: str = Field(min_length=1, max_length=8000)
    action_description: str = Field(min_length=1)
    subjects: list[str] = Field(default_factory=list)
    interactions: list[str] = Field(default_factory=list)
    temporal_predicates: list[str] = Field(default_factory=list)
    exclusions: list[str] = Field(default_factory=list)
    context_before_ms: int = Field(default=0, ge=0)
    context_after_ms: int = Field(default=0, ge=0)
    occurrence: Occurrence = Field(default_factory=Occurrence)
    clarification: str | None = None
    emphasize_key_moment: bool = False
    slow_motion_speed: float = Field(default=.6,ge=.25,le=1)
    preserve_normal_speed: bool = False
    framing_request: str = 'follow the primary subject'
    keep_context_in_frame: bool = False
    provider_metadata: dict = Field(default_factory=dict)


class Phase(Model):
    id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    role: Literal["onset", "key_moment", "completion", "context", "other"] = "other"
    time_ms: int = Field(ge=0)
    status: Literal["estimated", "observed", "user_corrected", "needs_review"]
    evidence_ms: list[int] = Field(min_length=1)


class ActionEvent(Record):
    project_id: UUID
    source_asset_id: UUID
    intent_id: UUID
    action_label: str = Field(min_length=1)
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)
    phases: list[Phase] = Field(default_factory=list)
    track_ids: list[UUID] = Field(default_factory=list)
    match_reason: str = Field(min_length=1)
    evidence_ms: list[int] = Field(min_length=1)
    review_required: bool = True
    outcome: Literal["attempted", "verified", "unknown"] = "unknown"
    primary_subject: str = ''
    context_subjects: list[str] = Field(default_factory=list)
    primary_track_id: UUID | None = None
    warnings: list[str] = Field(default_factory=list)
    confidence: dict = Field(default_factory=dict)
    provider_metadata: dict = Field(default_factory=dict)
    evidence_frame_ids: list[UUID] = Field(default_factory=list)
    parent_event_id: UUID | None = None

    @model_validator(mode="after")
    def chronological_evidence(self):
        if self.end_ms <= self.start_ms:
            raise ValueError("Event must have positive duration")
        if len({p.id for p in self.phases}) != len(self.phases):
            raise ValueError("Phase IDs must be unique")
        times = [p.time_ms for p in self.phases]
        if times != sorted(times):
            raise ValueError("Present phases must be chronological")
        evidence = self.evidence_ms + [t for p in self.phases for t in p.evidence_ms]
        if any(not self.start_ms <= t <= self.end_ms for t in times + evidence):
            raise ValueError("Phase and evidence times must lie inside the event")
        return self
