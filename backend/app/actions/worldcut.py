"""WorldCut adapter: validated source events -> existing verified EDL renderer."""
from .contracts import ActionPlanData
from .registry import validate_event
from ..editing import Crop, CropPoint, EditPlan, Segment, fixed_crop, plan_crop, Track
from ..errors import AppError
from ..repository import Repository, reference
from ..rendering import render
from ..source_evidence import SourceEvidenceAdapter
from ..sampling import extract_window
from ..scenes import inspect_scenes


def extract_event(store, settings, plan, event_record, stage=lambda *_: None):
    data = ActionPlanData.model_validate(plan.payload)
    if data.request.type != 'extract_clip' or reference(event_record) not in data.event_references:
        raise AppError('INVALID_ACTION', 'Clip executor requires this plan\'s selected event', 422)
    checked = Repository(store).load(plan.project_id, 'action_plans', plan.id)
    if checked != plan:
        raise AppError('REFERENCE_MISMATCH', 'Action plan changed before execution', 409)
    adapter = SourceEvidenceAdapter(store, settings, plan.project_id, plan.source_asset_id)
    event = validate_event(store, adapter, event_record)
    params = data.request.parameters
    asset = adapter.asset
    start = max(0, event.interval.start_ms - params.pre_ms)
    end = min(asset.metadata.duration_ms, asset.working_metadata.duration_ms, event.interval.end_ms + params.post_ms)
    if end < event.interval.end_ms:
        raise AppError('SOURCE_MAPPING_INSUFFICIENT', 'Prepared media cannot preserve the complete selected source interval', 409)
    g = asset.working_metadata.geometry
    if params.framing == 'full_frame':
        crop = Crop(mode='full_frame', width=g.display_width, height=g.display_height,
                    points=[CropPoint(time_ms=0, x=0, y=0)],
                    warnings=['Full source frame is letterboxed; this is static framing, not tracked reframing'])
    elif params.framing == 'fixed':
        crop = fixed_crop(g.display_width, g.display_height)
        crop.warnings.append('Static crop explicitly selected; inspect relevant-entity visibility in the output')
    else:
        track = store.load(plan.project_id, 'tracks', Track, params.track_id)
        if track.id not in event.track_ids or track.origin != 'meta_cloud' or track.source_asset_id != asset.id:
            raise AppError('INVALID_EVIDENCE', 'Tracked framing requires the selected event\'s actual cloud track', 422)
        contexts = [store.load(plan.project_id, 'tracks', Track, identifier)
                    for identifier in event.track_ids if identifier != track.id]
        if any(t.source_asset_id != asset.id or t.origin != 'meta_cloud' for t in contexts):
            raise AppError('INVALID_EVIDENCE', 'Context framing requires actual cloud tracks from the same source', 422)
        window = extract_window(store, settings, asset, start, end)
        scenes = inspect_scenes(store, settings, window)
        if any(event.interval.start_ms < cut.source_time_ms < event.interval.end_ms for cut in scenes.cuts):
            raise AppError('SCENE_REVIEW_REQUIRED', 'A measured scene-change estimate crosses the event; choose explicit static framing or review the interval', 409)
        crop = plan_crop(track, g.display_width, g.display_height, start, end, context_tracks=contexts)
    lineage = {'program_id': str(plan.program.id), 'program_version': plan.program.version,
               'program_fingerprint': plan.program.record_sha256, 'run_id': str(plan.run.id),
               'action_id': str(plan.id), 'event_ids': [str(event_record.id)],
               'verdict': event.verdict, 'review_only': data.request.review_only,
               'fixture_only': data.fixture_only, 'origin': event.origin,
               'action_fingerprint': data.request_fingerprint}
    edl = EditPlan(project_id=asset.project_id, source_asset_id=asset.id, origin='manual',
        selected_event_ids=[event_record.id], output=params.output,
        segments=[Segment(source_in_ms=start, source_out_ms=end, speed=1, crop=crop, event_id=event_record.id)],
        reasoning=[{'operation': 'extract_clip', 'framing': params.framing,
                    'fixture_label': 'MANUAL DEVELOPER FIXTURE' if data.fixture_only else None}],
        event_snapshots=[event_record.model_dump(mode='json')], actionos_lineage=lineage)
    return render(store, settings, edl, stage, regenerate=data.request.regenerate)
