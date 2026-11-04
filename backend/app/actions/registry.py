"""Finite action planning boundary, separate from compiler action declarations."""
import hashlib
from uuid import uuid4, uuid5

from pydantic import ValidationError

from .contracts import ACTION_REQUEST, ActionPlanData, EventEvidence
from ..editing import Track
from ..errors import AppError
from ..media import media_tool_versions
from ..models import EvidenceFrame
from ..repository import Envelope, Repository, encoded, fingerprint, reference
from ..source_evidence import SourceEvidenceAdapter
from ..validation import bounded_json

ACTION_VERSIONS = {'extract_clip': 'worldcut-1', 'create_report': 'timestamp-report-1'}
P1_ACTIONS = ('compare_events', 'isolate_subject')


def parse_request(value):
    if isinstance(value, dict) and value.get('type') in P1_ACTIONS:
        raise AppError('ACTION_UNAVAILABLE', 'This optional action is not implemented', 422)
    try:
        return ACTION_REQUEST.validate_python(value)
    except ValidationError:
        raise AppError('INVALID_ACTION', 'Action contains unsupported operations or invalid parameters', 422) from None


def validate_event(store, adapter, record):
    try:
        event = EventEvidence.model_validate(record.payload)
    except ValidationError:
        raise AppError('INVALID_EVENT', 'Event evidence failed action validation', 422) from None
    adapter.interval(event.interval.start_ms, event.interval.end_ms)
    for step in event.steps:
        for observation_id in step.observation_ids:
            observation = Repository(store).load(record.project_id, 'observations', observation_id)
            if observation.run != record.run or observation.program != record.program:
                raise AppError('OWNERSHIP', 'Step observation belongs to another run/program', 422)
        for evidence in step.frames:
            if (evidence.project_id != record.project_id or evidence.source_asset_id != record.source_asset_id
                    or evidence.source_sha256 != record.source_sha256):
                raise AppError('OWNERSHIP', 'Frame evidence does not belong to this event source', 422)
            frame = store.load(record.project_id, 'evidence', EvidenceFrame, evidence.evidence_frame_id)
            if (frame.source_asset_id != record.source_asset_id or frame.source_sha256 != record.source_sha256
                    or frame.source_frame_index != evidence.source_frame_index
                    or frame.source_pts != evidence.source_pts or frame.source_time_ms != evidence.source_time_ms
                    or frame.source_time_base != evidence.source_time_base or frame.image_sha256 != evidence.image_sha256):
                raise AppError('EVIDENCE_MISMATCH', 'Frame reference does not match the stored original evidence', 409)
            adapter._verify(frame.image_path, frame.image_sha256)
            adapter.validate_witness(evidence.source_frame_index,evidence.observed_at_source_ms)
    for track_id in event.track_ids:
        track = store.load(record.project_id, 'tracks', Track, track_id)
        if track.source_asset_id != record.source_asset_id:
            raise AppError('OWNERSHIP', 'Event track belongs to another source', 422)
    return event


def build_action_plan(store, settings, project_id, run_id, request, *, initiated, manual_fixture=False):
    if initiated is not True:
        raise AppError('ACTION_INITIATION_REQUIRED', 'Output actions require explicit initiation', 409)
    request = parse_request(request)
    bounded_json(request.model_dump(mode='json'), max_bytes=131072)
    repo = Repository(store)
    run = repo.load(project_id, 'runs', run_id)
    program = repo.load(project_id, 'programs', run.program.id, run.program.version)
    if manual_fixture:
        if program.payload.get('fixture_only') is not True:
            raise AppError('INVALID_PROVENANCE','Manual fixture execution requires a fixture program',409)
    else:
        if program.payload.get('fixture_only') is True:
            raise AppError('PROGRAM_REVIEW_REQUIRED','Developer fixture programs cannot execute through the public workflow',409)
        from ..programs.repository import assert_reviewed
        assert_reviewed(store,program)
    adapter = SourceEvidenceAdapter(store, settings, project_id, run.source_asset_id)
    events = []
    for event_id in request.event_ids:
        record = repo.load(project_id, 'temporal_events', event_id)
        if record.run != reference(run) or record.program != run.program:
            raise AppError('OWNERSHIP', 'Selected event belongs to another run/program version', 422)
        event = validate_event(store, adapter, record)
        if manual_fixture and event.origin != 'manual_fixture':
            raise AppError('INVALID_PROVENANCE', 'Development actions require visibly manual event evidence', 422)
        if not manual_fixture and event.origin not in ('evaluated','user_correction'):
            raise AppError('INVALID_PROVENANCE','Public actions require evaluated evidence',422)
        if event.coverage.get('selected') is False and not request.review_only:
            raise AppError('OCCURRENCE_SELECTION_REQUIRED','This event is outside the rule occurrence selection; include it only as an explicit review output',409)
        if (event.verdict != 'true' or event.coverage.get('estimated')) and not request.review_only:
            raise AppError('REVIEW_SELECTION_REQUIRED', 'Unknown, negative or estimated events require explicit review-only output selection', 409)
        events.append(record)
    tools = {'action_version': ACTION_VERSIONS[request.type]}
    if request.type == 'extract_clip':
        tools.update(media_tool_versions())
        if request.parameters.track_id:
            track = store.load(project_id, 'tracks', Track, request.parameters.track_id)
            if track.source_asset_id != run.source_asset_id or track.origin != 'meta_cloud':
                raise AppError('INVALID_EVIDENCE', 'Tracked output requires an actual owned cloud track', 422)
            if any(track.id not in EventEvidence.model_validate(r.payload).track_ids for r in events):
                raise AppError('OWNERSHIP', 'Selected track must belong to every selected event', 422)
    semantic_request = request.model_dump(mode='json', exclude={'regenerate'})
    identity = {'source': run.source_sha256, 'program': run.program.model_dump(mode='json'),
                'run': fingerprint(run), 'events': [reference(r).model_dump(mode='json') for r in events],
                'request': semantic_request, 'tools': tools}
    payload = ActionPlanData(request=request, event_references=[reference(r) for r in events],
        request_fingerprint=hashlib.sha256(encoded(identity)).hexdigest(), run_fingerprint=fingerprint(run),
        tools=tools, fixture_only=manual_fixture, initiated_by='developer_fixture' if manual_fixture else 'explicit_user')
    action_id = uuid4() if request.regenerate else uuid5(run.id, payload.request_fingerprint)
    with store.mutation():
        if repo.path(project_id, 'action_plans', action_id).exists():
            return repo.load(project_id, 'action_plans', action_id)
        return repo.publish(Envelope(kind='action_plans', id=action_id, project_id=run.project_id,
            source_asset_id=run.source_asset_id, source_sha256=run.source_sha256,
            program=run.program, run=reference(run), references=[reference(r) for r in events],
            payload=payload.model_dump(mode='json')))
