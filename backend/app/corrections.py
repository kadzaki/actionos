"""Immutable user corrections grounded in actual source frames."""
from uuid import UUID

from pydantic import Field, ValidationError

from .errors import AppError
from .evidence import evidence_frame
from .models import ActionEvent, Asset, Model, Phase, Record


class AnchorInput(Model):
    id: str = Field(min_length=1, max_length=80)
    label: str = Field(min_length=1, max_length=200)
    role: Phase.__annotations__['role'] = 'other'
    time_ms: int = Field(ge=0)


class EventCorrection(Model):
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)
    phases: list[AnchorInput] = Field(max_length=12)
    note: str = Field(default='', max_length=1000)


class CorrectionRecord(Record):
    project_id: UUID
    source_asset_id: UUID
    parent_event_id: UUID
    event_id: UUID
    origin: str = 'user_corrected'
    note: str
    previous: dict
    corrected: dict
    evidence_frame_ids: list[UUID]


def latest_events(events):
    superseded = {e.parent_event_id for e in events if e.parent_event_id}
    return [e for e in events if e.id not in superseded]


def correct_event(store, settings, project_id, event_id, body: EventCorrection):
    event = store.load(project_id, 'events', ActionEvent, event_id)
    if event.id not in {e.id for e in latest_events(store.list_records(project_id, 'events', ActionEvent))}:
        raise AppError('STALE_EVENT', 'This event already has a newer correction; refresh the evidence board', 409)
    asset = store.load(project_id, 'assets', Asset, event.source_asset_id)
    limit = min(asset.metadata.duration_ms, asset.working_metadata.duration_ms)
    if not body.start_ms < body.end_ms <= limit:
        raise AppError('INVALID_CORRECTION', 'Corrected boundaries must be ordered and inside the prepared source', 422)
    if any(not body.start_ms <= p.time_ms <= body.end_ms for p in body.phases):
        raise AppError('INVALID_CORRECTION', 'Every anchor must lie inside the corrected boundaries', 422)
    # Retain the requested boundary (a cut can fall between frames), while each
    # anchor reports the actual displayed original frame used as evidence.
    frames = [evidence_frame(store, settings, asset, p.time_ms) for p in body.phases]
    phases = [Phase(**{**p.model_dump(), 'time_ms': round(f.source_time_ms)},
                    evidence_ms=[round(f.source_time_ms)], status='user_corrected')
              for p, f in zip(body.phases, frames)]
    boundary = evidence_frame(store, settings, asset, body.start_ms)
    evidence = sorted({round(f.source_time_ms) for f in [boundary, *frames]
                       if body.start_ms <= round(f.source_time_ms) <= body.end_ms})
    if not evidence:
        raise AppError('INVALID_CORRECTION', 'Choose boundaries containing an actual source evidence frame', 422)
    from uuid import uuid4
    from .models import now
    data = event.model_dump()
    data.update(id=uuid4(), created_at=now(), parent_event_id=event.id,
                start_ms=body.start_ms, end_ms=body.end_ms, phases=phases,
                evidence_ms=evidence, evidence_frame_ids=[f.id for f in [boundary, *frames]],
                provider_metadata={**event.provider_metadata,'anchors_user_corrected':True},
                confidence={**event.confidence, 'anchor_precision': 'user_corrected'})
    try:
        corrected = ActionEvent.model_validate(data)
    except ValidationError:
        raise AppError('INVALID_CORRECTION', 'Anchors must have unique IDs, chronological order and in-range evidence', 422) from None
    record = CorrectionRecord(project_id=project_id, source_asset_id=asset.id,
                              parent_event_id=event.id, event_id=corrected.id, note=body.note,
                              previous=event.model_dump(mode='json'), corrected=corrected.model_dump(mode='json'),
                              evidence_frame_ids=corrected.evidence_frame_ids)
    store.save(project_id, 'corrections', record)
    store.save(project_id, 'events', corrected)
    return corrected
