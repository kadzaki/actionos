"""Measured proxy scene-change estimates, mapped to original frame evidence."""
import math
from uuid import UUID,uuid5
from pydantic import Field

from .errors import AppError
from .media import media_tool_versions,run_media
from .models import Record,Model
from .resources import MediaBudget

DETECTOR_VERSION=1
THRESHOLD=10


class SceneCut(Model):
    source_time_ms: int = Field(ge=0)
    source_frame_index: int = Field(ge=0)
    window_frame_index: int = Field(ge=0)
    window_time_ms: int = Field(ge=0)


class SceneEvidence(Record):
    project_id: UUID
    source_asset_id: UUID
    window_id: UUID
    video_sha256: str
    detector_version: int = DETECTOR_VERSION
    threshold: int = THRESHOLD
    tools: dict
    cuts: list[SceneCut] = Field(max_length=30000)
    origin: str = 'measured_proxy_scene_change_estimate'


def inspect_scenes(store,settings,window):
    tools=media_tool_versions()
    identity=uuid5(window.id,f'scdet:{DETECTOR_VERSION}:{THRESHOLD}:{window.sha256}:{tools["ffmpeg"]}')
    try:
        cached=store.load(window.project_id,'scenes',SceneEvidence,identity)
    except AppError as exc:
        if exc.code!='NOT_FOUND':raise
        cached=None
    if cached:
        if (cached.window_id!=window.id or cached.source_asset_id!=window.source_asset_id or cached.video_sha256!=window.sha256
            or cached.detector_version!=DETECTOR_VERSION or cached.threshold!=THRESHOLD):
            raise AppError('CORRUPT_RECORD','Scene-change evidence has inconsistent source lineage',500)
        for cut in cached.cuts:
            index=cut.window_frame_index
            if (index>=len(window.source_frame_indices) or cut.source_frame_index!=window.source_frame_indices[index]
                or cut.source_time_ms!=round(window.source_frame_times_ms[index]) or cut.window_time_ms!=round(index*1000/window.fps)):
                raise AppError('CORRUPT_RECORD','Scene-change evidence no longer matches actual source frames',500)
        return cached
    output=run_media(['ffmpeg','-hide_banner','-loglevel','error','-xerror','-nostdin','-i',str(store.path(window.project_id,window.path)),
                      '-vf',f'scdet=threshold={THRESHOLD},metadata=mode=print:key=lavfi.scd.time:file=-',
                      '-an','-threads','2','-f','null','-'],settings.media_timeout_seconds,
                     budget=MediaBudget(store.project_dir(window.project_id),settings),max_stdout_bytes=1024*1024)
    cuts={}
    for line in output.splitlines():
        if not line.startswith('lavfi.scd.time='):continue
        try:local_ms=float(line.split('=',1)[1])*1000
        except ValueError:raise AppError('SCENE_METADATA_INVALID','Could not read measured scene-change timing',422) from None
        if not math.isfinite(local_ms) or not 0<=local_ms<=window.duration_ms+1:
            raise AppError('SCENE_METADATA_INVALID','Measured scene change exceeds this source window',422)
        # Metadata timestamps are rounded decimals of the known CFR window.
        # Round to the actual frame, rather than flooring a 10.033 s decimal.
        index=min(len(window.source_frame_indices)-1,round(local_ms*window.fps/1000))
        source_ms=round(window.source_frame_times_ms[index])
        cuts[source_ms]={'source_time_ms':source_ms,'source_frame_index':window.source_frame_indices[index],
                         'window_frame_index':index,'window_time_ms':round(index*1000/window.fps)}
    record=SceneEvidence(id=identity,project_id=window.project_id,source_asset_id=window.source_asset_id,
                         window_id=window.id,video_sha256=window.sha256,tools=tools,cuts=[cuts[t] for t in sorted(cuts)])
    store.save(window.project_id,'scenes',record)
    return record
