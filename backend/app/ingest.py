from .config import Settings
from .jobs import JobRunner
from .media import frame_timeline, normalize, probe, thumbnail
from .models import Asset, Job, Project
from .storage import FileStore, file_sha256
from .resources import MediaBudget
from .timing import SourceTimeline,complete_timeline
from .errors import AppError
from .layout import derivative_path


def ingest(store: FileStore, runner: JobRunner, settings: Settings, job: Job):
    asset = store.load(job.project_id, "assets", Asset, job.asset_id)
    source = store.path(job.project_id, asset.source_path)
    budget = MediaBudget(store.project_dir(job.project_id),settings)
    budget.check()
    runner.stage(job, "probing", 0)
    metadata = probe(source, settings)
    asset.metadata = metadata
    store.save(job.project_id, "assets", asset)
    runner.stage(job, "mapping_timestamps", 15)
    timeline = frame_timeline(source, metadata, settings,budget)
    timeline.update(source_asset_id=str(asset.id),source_sha256=asset.sha256)
    timeline_relative = f"frames/{asset.id}.timeline.json"
    store.write_json(store.path(job.project_id, timeline_relative), timeline)
    runner.stage(job, "normalizing", 30)
    working_relative = derivative_path(asset, 'working')
    working = store.path(job.project_id, working_relative)
    working_metadata = normalize(source, working, settings,
                                 source_origin_seconds=timeline["origin_pts_seconds"],
                                 duration_ms=metadata.duration_ms,source_metadata=metadata,budget=budget)
    runner.stage(job, "creating_preview", 65)
    proxy_relative = derivative_path(asset, 'preview')
    preview_metadata = normalize(working, store.path(job.project_id, proxy_relative), settings, preview=True,
                                 duration_ms=working_metadata.duration_ms,source_metadata=working_metadata,budget=budget)
    runner.stage(job, "verifying", 90)
    thumb_relative = f"frames/{asset.id}.thumbnail.jpg"
    thumbnail(working, store.path(job.project_id, thumb_relative), settings,budget=budget)
    observed_working = SourceTimeline.model_validate(frame_timeline(working,working_metadata,settings,budget))
    observed_proxy=SourceTimeline.model_validate(frame_timeline(store.path(job.project_id,proxy_relative),preview_metadata,settings,budget))
    if observed_working.source_frame_times_ms!=observed_proxy.source_frame_times_ms:
        raise AppError('PROXY_TIMELINE_MISMATCH','Source preview did not retain the prepared frame timeline',422,True)
    source_timeline = SourceTimeline.model_validate(timeline)
    source_timeline=complete_timeline(source_timeline,observed_working,preview_metadata.geometry)
    store.write_json(store.path(job.project_id,timeline_relative),source_timeline.model_dump(mode="json"))
    asset.working_path = working_relative
    asset.proxy_path = proxy_relative
    asset.thumbnail_path = thumb_relative
    asset.timeline_path = timeline_relative
    asset.working_metadata = working_metadata
    asset.proxy_metadata = preview_metadata
    asset.artifact_sha256 = {relative: file_sha256(store.path(job.project_id, relative))
                             for relative in (working_relative, proxy_relative, thumb_relative, timeline_relative)}
    asset.status = "ready"
    store.save(job.project_id, "assets", asset)
    with store.mutation():
        project = store.load(job.project_id, "project", Project)
        project.status = "ready"
        project.active_asset_id = asset.id
        store.save(project.id, "project", project)
    return {"asset_id": str(asset.id)}
