"""Normalize the verified SAM box/mask wire grammar without inventing samples."""
import re
from uuid import uuid5

from ..editing import Track,TrackSample
from ..errors import AppError
from ..sampling import VideoWindow
from .meta import MetaCloud

FRAME=re.compile(r'<(\d+)f>')
RECORD=re.compile(r'(\d+)<\|box;x1=(\d+);y1=(\d+);x2=(\d+);y2=(\d+);w=(\d+);h=(\d+)(?:;c=([0-9.]+))?\|>'
                  r'<\|mask;x=0;y=0(?:;c=[0-9.]+)?;data=(\d+),(\d+),([~!])(.+?)\|>(?:,(?=\d+<\|box;)|\s*$)',re.DOTALL)


def parse_sam(text: str,frame_count: int,width: int,height: int):
    if not text.strip():return {}
    objects={}
    for line in text.splitlines():
        line=line.strip()
        if not line:continue
        marker=FRAME.match(line)
        if not marker:
            raise AppError('PROVIDER_SCHEMA','SAM output has an invalid frame grammar',502)
        frame=int(marker.group(1))
        if not 0<=frame<frame_count:
            raise AppError('PROVIDER_SCHEMA','SAM frame lies outside the supplied video window',502)
        segment=line[marker.end():].strip()
        cursor=0
        for record in RECORD.finditer(segment):
            if segment[cursor:record.start()].strip():
                raise AppError('PROVIDER_SCHEMA','SAM output contains malformed object records',502)
            object_id,x1,y1,x2,y2,w,h=map(int,record.groups()[:7])
            if (w,h)!=(width,height) or not 0<=x1<=x2<w or not 0<=y1<=y2<h:
                raise AppError('PROVIDER_GEOMETRY','SAM geometry does not match supplied media',502)
            value=(x1/w,y1/h,(x2-x1+1)/w,(y2-y1+1)/h)
            existing=objects.setdefault(object_id,{})
            if frame in existing and existing[frame]!=value:
                raise AppError('PROVIDER_SCHEMA','SAM returned conflicting samples for one object/frame',502)
            existing[frame]=value
            cursor=record.end()
        if cursor!=len(segment):
            raise AppError('PROVIDER_SCHEMA','SAM output contains an incomplete object record',502)
    return objects


SAM_ADAPTER_VERSION=1


def sam_video_response(cloud: MetaCloud,window: VideoWindow,path,prompt: str,*,selection=False):
    """Shared actual transport; callers retain exact local frame IDs when needed."""
    cleanup={}
    try:
        with cloud.video_file(path) as (file_id,cleanup):
            text,provenance=cloud.stream_response(cloud.settings.sam_model,[{'type':'input_text','text':prompt},
                                                                    {'type':'input_video','file_id':file_id}])
    except AppError as exc:
        raise AppError(exc.code,exc.message,exc.status,exc.retryable,
            {**exc.details,'window_id':str(window.id),'remote_file_deleted':cleanup.get('deleted',False)}) from None
    provenance['remote_file_deleted']=cleanup.get('deleted',False)
    provenance.update(adapter_version=SAM_ADAPTER_VERSION,prompt_kind='selection' if selection else 'concept')
    return text,provenance


def track_window(cloud: MetaCloud,window: VideoWindow,path,prompt: str,label=None):
    text,provenance=sam_video_response(cloud,window,path,prompt,selection=label is not None)
    parsed=parse_sam(text,len(window.source_frame_indices),window.width,window.height)
    tracks=[]
    for object_id,frames in parsed.items():
        samples=[]
        # Deduplicate source timestamps repeated by CFR conversion. A missing
        # observation remains a gap; interpolation must respect crop gap limits.
        observed={}
        for local_index,bbox in sorted(frames.items()):
            source_ms=round(window.source_frame_times_ms[local_index])
            observed[source_ms]=TrackSample(time_ms=source_ms,x=bbox[0],y=bbox[1],width=bbox[2],height=bbox[3])
        samples=[observed[t] for t in sorted(observed)]
        tracks.append(Track(id=uuid5(window.id,f'sam:{prompt}:{object_id}:{provenance["response_id"]}'),
                            project_id=window.project_id,source_asset_id=window.source_asset_id,
                            label=label or prompt,origin='meta_cloud',samples=samples,
                            provider_metadata={**provenance,'window_id':str(window.id),'object_id':object_id}))
    return tracks,provenance
