"""Extract inspected source frames, never pretend proxy frames are originals."""
import hashlib
import os
from bisect import bisect_right
from uuid import uuid4,uuid5

from pydantic import ValidationError

from .config import Settings
from .errors import AppError
from .media import frame_timeline,probe,run_media
from .models import Asset,EvidenceFrame
from .resources import MediaBudget
from .storage import FileStore,file_sha256,atomic_replace
from .timing import SourceTimeline,complete_timeline


def read_timeline(store: FileStore,asset: Asset,settings: Settings):
    if not asset.metadata or not asset.timeline_path:
        raise AppError("MEDIA_NOT_READY","Source timestamp evidence is not ready",409,True)
    path=store.path(asset.project_id,asset.timeline_path)
    try:
        timeline=SourceTimeline.model_validate(store.read_json(path))
    except ValidationError:
        raise AppError("CORRUPT_RECORD","Saved source timeline failed validation",500) from None
    if not timeline.source_frame_pts or timeline.source_asset_id is None or not timeline.working_frame_source_indices:
        # Additive upgrade for the initial v1 files. Reprobe immutable source,
        # never manufacture frame indices from nominal FPS.
        source=store.path(asset.project_id,asset.source_path)
        budget=MediaBudget(store.project_dir(asset.project_id),settings)
        metadata=probe(source,settings)
        fresh=frame_timeline(source,metadata,settings,budget)
        fresh.update(source_asset_id=str(asset.id),source_sha256=asset.sha256)
        if not asset.working_path or not asset.proxy_metadata:
            raise AppError("MEDIA_NOT_READY","Prepared source is missing",409,True)
        working=store.path(asset.project_id,asset.working_path)
        working_metadata=probe(working,settings)
        observed=SourceTimeline.model_validate(frame_timeline(working,working_metadata,settings,budget))
        timeline=complete_timeline(SourceTimeline.model_validate(fresh),observed,asset.proxy_metadata.geometry)
        store.write_json(path,timeline.model_dump(mode="json"))
        asset.metadata=metadata
        store.save(asset.project_id,"assets",asset)
    if timeline.source_asset_id!=asset.id or timeline.source_sha256!=asset.sha256:
        raise AppError("CORRUPT_RECORD","Source timeline belongs to different media",500)
    return timeline


def evidence_frame(store: FileStore,settings: Settings,asset: Asset,requested_ms: int,
                   *,source_frame_index: int | None = None):
    if asset.status!="ready" or not asset.metadata:
        raise AppError("MEDIA_NOT_READY","Source frames are not ready",409,True)
    if not 0<=requested_ms<asset.metadata.duration_ms:
        raise AppError("INVALID_TIMESTAMP","Choose a timestamp inside the source video",422)
    with store.media_lease(asset.project_id),store.artifact_lock:
        timeline=read_timeline(store,asset,settings)
        index=timeline.nearest_frame(requested_ms) if source_frame_index is None else source_frame_index
        if not 0<=index<len(timeline.source_frame_times_ms):
            raise AppError("INVALID_FRAME","Source frame index is outside the video",422)
        frame_id=uuid5(asset.id,f"source-frame:{asset.sha256}:{index}")
        try:
            cached=store.load(asset.project_id,"evidence",EvidenceFrame,frame_id)
        except AppError as exc:
            if exc.code!="NOT_FOUND": raise
            cached=None
        if cached:
            path=store.path(asset.project_id,cached.image_path)
            if (cached.source_asset_id!=asset.id or cached.source_sha256!=asset.sha256
                    or cached.source_frame_index!=index):
                raise AppError("CORRUPT_RECORD","Frame evidence does not reference this source",500)
            if path.is_file() and file_sha256(path)==cached.image_sha256:
                return cached
        existing=store.list_records(asset.project_id,"evidence",EvidenceFrame)
        if cached is None and sum(f.source_asset_id==asset.id for f in existing)>=settings.max_cached_frames:
            raise AppError("FRAME_CACHE_LIMIT","Source has reached the configured evidence-frame limit",429)
        budget=MediaBudget(store.project_dir(asset.project_id),settings)
        budget.check()
        g=asset.metadata.geometry
        factor=min(1.,480/g.display_width,480/g.display_height)
        width=max(2,int(g.display_width*factor)//2*2)
        height=max(2,int(g.display_height*factor)//2*2)
        relative=f"frames/{frame_id}.source.jpg"
        final=store.path(asset.project_id,relative)
        temporary=store.path(asset.project_id,f"tmp/{frame_id}.{uuid4().hex}.frame.jpg")
        temporary.parent.mkdir(parents=True,exist_ok=True)
        try:
            # Decode the selected actual frame index, not a nominal-FPS seek.
            run_media(["ffmpeg","-hide_banner","-loglevel","error","-xerror","-nostdin","-y",
                       "-i",str(store.path(asset.project_id,asset.source_path)),"-map",f"0:{asset.metadata.video_stream_index}",
                       "-vf",f"select=eq(n\\,{index}),scale={width}:{height},setsar=1",
                       "-frames:v","1","-q:v","2","-threads","1",str(temporary)],
                      settings.media_timeout_seconds,budget=budget)
            if not temporary.is_file() or temporary.stat().st_size==0:
                raise AppError("FRAME_EXTRACTION_FAILED","No inspected frame was produced",422)
            record=EvidenceFrame(id=frame_id,project_id=asset.project_id,source_asset_id=asset.id,
                                 source_sha256=asset.sha256,source_frame_index=index,
                                 source_time_ms=timeline.source_frame_times_ms[index],
                                 source_pts=timeline.source_frame_pts[index],source_time_base=timeline.source_time_base,
                                 image_path=relative,image_sha256=file_sha256(temporary),
                                 width=width,height=height)
            final.parent.mkdir(parents=True,exist_ok=True)
            atomic_replace(temporary,final)
            store.save(asset.project_id,"evidence",record)
            return record
        finally:
            temporary.unlink(missing_ok=True)


def evidence_at_working_time(store: FileStore,settings: Settings,asset: Asset,working_ms: int):
    if asset.status!='ready' or not asset.working_metadata or not asset.metadata:
        raise AppError('MEDIA_NOT_READY','Source preview is not ready',409,True)
    if not 0<=working_ms<asset.working_metadata.duration_ms:
        raise AppError('INVALID_TIMESTAMP','Choose a timestamp inside the prepared source',422)
    with store.media_lease(asset.project_id),store.artifact_lock:
        timeline=read_timeline(store,asset,settings)
        index=max(0,bisect_right(timeline.working_frame_times_ms,working_ms)-1)
        source_index=timeline.working_frame_source_indices[index]
        source_time=int(timeline.source_frame_times_ms[source_index])
        source_time=min(source_time,asset.metadata.duration_ms-1)
        return evidence_frame(store,settings,asset,source_time,source_frame_index=source_index),index
