"""Bounded coarse video windows with explicit frame-to-source provenance.

This prepares actual cloud inputs locally; it never claims a model analyzed them.
"""
from bisect import bisect_left
from fractions import Fraction
import math
from uuid import UUID,uuid4,uuid5

from pydantic import Field

from .config import Settings
from .errors import AppError
from .evidence import read_timeline
from .media import frame_timeline,probe,run_media
from .models import Asset,Record
from .resources import MediaBudget
from .storage import FileStore,atomic_replace,file_sha256
from .timing import SourceTimeline


class VideoWindow(Record):
    project_id: UUID
    source_asset_id: UUID
    source_sha256: str
    proxy_sha256: str
    path: str
    sha256: str
    requested_in_ms: int
    requested_out_ms: int
    first_working_frame_index: int
    first_working_time_ms: float
    duration_ms: int
    width: int
    height: int
    fps: int = 30
    source_frame_indices: list[int] = Field(min_length=1,max_length=30000)
    source_frame_times_ms: list[float] = Field(min_length=1,max_length=30000)
    input_kind: str = 'normalized_proxy_video'
    analysis_status: str = 'not_analyzed'


def coarse_schedule(duration_ms: int,window_ms=12000,overlap_ms=2000):
    if duration_ms<=0 or window_ms<=0 or not 0<=overlap_ms<window_ms:
        raise ValueError('Invalid coarse-window parameters')
    step=window_ms-overlap_ms
    result=[]
    for start in range(0,duration_ms,step):
        end=min(duration_ms,start+window_ms)
        result.append((start,end))
        if end==duration_ms:break
    if len(result)>24:
        raise AppError('SAMPLING_LIMIT','This source needs too many coarse analysis windows',422)
    return result


def extract_window(store: FileStore,settings: Settings,asset: Asset,start_ms: int,end_ms: int):
    if asset.status!='ready' or not asset.proxy_path or not asset.proxy_metadata or not asset.working_metadata:
        raise AppError('MEDIA_NOT_READY','Prepared source is required for sampling',409,True)
    if not 0<=start_ms<end_ms<=asset.working_metadata.duration_ms:
        raise AppError('INVALID_WINDOW','Choose a nonempty window inside prepared source media',422)
    with store.media_lease(asset.project_id),store.artifact_lock:
        timeline=read_timeline(store,asset,settings)
        times=timeline.working_frame_times_ms
        first=bisect_left(times,start_ms)
        stop=bisect_left(times,end_ms)
        if first>=stop:
            raise AppError('INVALID_WINDOW','Window contains no complete source frame',422)
        proxy=store.path(asset.project_id,asset.proxy_path)
        proxy_hash=file_sha256(proxy)
        identity=f'window:v1:{asset.sha256}:{proxy_hash}:{first}:{stop}'
        window_id=uuid5(asset.id,identity)
        try:
            cached=store.load(asset.project_id,'windows',VideoWindow,window_id)
        except AppError as exc:
            if exc.code!='NOT_FOUND':raise
            cached=None
        if cached:
            video=store.path(asset.project_id,cached.path)
            if (cached.source_asset_id!=asset.id or cached.source_sha256!=asset.sha256
                or cached.proxy_sha256!=proxy_hash):
                raise AppError('CORRUPT_RECORD','Window refers to inconsistent source evidence',500)
            if video.is_file() and file_sha256(video)==cached.sha256:return cached
        relative=f'proxy/windows/{window_id}.mp4'
        final=store.path(asset.project_id,relative)
        temporary=store.path(asset.project_id,f'tmp/{window_id}.window.mp4')
        temporary.parent.mkdir(parents=True,exist_ok=True)
        budget=MediaBudget(store.project_dir(asset.project_id),settings)
        # Frame-index trim avoids millisecond rounding changing a boundary at
        # 33.333... ms. Both video and audio use the actual first working tick.
        actual_start=Fraction(first,30)
        actual_duration=Fraction(stop-first,30)
        filters=[f'[0:v:0]trim=start_frame={first}:end_frame={stop},setpts=PTS-STARTPTS[v]']
        audio=asset.proxy_metadata.audio_present
        if audio:
            filters.append(f'[0:a:0]atrim=start={float(actual_start):.9f}:duration={float(actual_duration):.9f},asetpts=PTS-STARTPTS[a]')
        try:
            args=['ffmpeg','-hide_banner','-loglevel','error','-xerror','-nostdin','-y','-i',str(proxy),
                  '-filter_complex_threads','1','-filter_complex',';'.join(filters),'-map','[v]']
            if audio:args+=['-map','[a]','-c:a','aac','-b:a','96k']
            args+=['-c:v','libx264','-crf','24','-preset','veryfast','-pix_fmt','yuv420p',
                   '-threads','2','-fps_mode','cfr','-r','30','-map_metadata','-1','-movflags','+faststart',str(temporary)]
            run_media(args,settings.media_timeout_seconds,budget=budget)
            metadata=probe(temporary,settings,enforce_limits=False)
            observed=SourceTimeline.model_validate(frame_timeline(temporary,metadata,settings,budget))
            expected=stop-first
            if len(observed.source_frame_pts)!=expected or abs(metadata.duration_ms-float(actual_duration*1000))>40:
                raise AppError('WINDOW_VERIFICATION_FAILED','Sample window lost its source-frame mapping',422,
                               details={'expected_frames':expected,'actual_frames':len(observed.source_frame_pts),
                                        'expected_duration_ms':float(actual_duration*1000),'actual_duration_ms':metadata.duration_ms})
            indices=timeline.working_frame_source_indices[first:stop]
            window=VideoWindow(id=window_id,project_id=asset.project_id,source_asset_id=asset.id,
                               source_sha256=asset.sha256,proxy_sha256=proxy_hash,path=relative,sha256=file_sha256(temporary),
                               requested_in_ms=start_ms,requested_out_ms=end_ms,first_working_frame_index=first,
                               first_working_time_ms=times[first],duration_ms=metadata.duration_ms,
                               width=metadata.geometry.display_width,height=metadata.geometry.display_height,
                               source_frame_indices=indices,
                               source_frame_times_ms=[timeline.source_frame_times_ms[i] for i in indices])
            final.parent.mkdir(parents=True,exist_ok=True)
            atomic_replace(temporary,final)
            store.save(asset.project_id,'windows',window)
            return window
        finally:
            temporary.unlink(missing_ok=True)


def coarse_windows(store: FileStore,settings: Settings,asset: Asset,window_ms=12000,overlap_ms=2000):
    if not asset.working_metadata:
        raise AppError('MEDIA_NOT_READY','Prepare source media first',409,True)
    schedule=coarse_schedule(asset.working_metadata.duration_ms,window_ms,overlap_ms)
    return [extract_window(store,settings,asset,start,end) for start,end in schedule]
