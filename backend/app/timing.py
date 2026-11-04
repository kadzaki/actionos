"""Source PTS evidence and explicit mappings between normalized media spaces."""
from bisect import bisect_left,bisect_right
from fractions import Fraction
import math
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from .errors import AppError
from .models import Geometry, Model


class CoordinateTransform(Model):
    from_space: str
    to_space: str
    matrix: list[float] = Field(min_length=9, max_length=9)

    def point(self, x: float, y: float):
        a,b,c,d,e,f,g,h,i = self.matrix
        denominator = g*x+h*y+i
        if denominator == 0:
            raise ValueError("Invalid homogeneous coordinate")
        return (a*x+b*y+c)/denominator, (d*x+e*y+f)/denominator


def coded_to_display(geometry: Geometry):
    w,h,s = geometry.coded_width,geometry.coded_height,geometry.sample_aspect_ratio
    rotation = round(geometry.rotation_degrees) % 360
    # Continuous pixel edges, not pixel-center indices. FFprobe positive display
    # rotation is counter-clockwise; account for non-square pixels before it.
    matrices = {
        0: [s,0,0, 0,1,0, 0,0,1],
        90: [0,1,0, -s,0,w*s, 0,0,1],
        180: [-s,0,w*s, 0,-1,h, 0,0,1],
        270: [0,-1,h, s,0,0, 0,0,1],
    }
    if rotation not in matrices:
        raise ValueError("Only orthogonal display rotations are supported")
    return CoordinateTransform(from_space="source_coded_pixels",to_space="source_display_pixels",
                               matrix=matrices[rotation])


def scale_transform(from_space: str, to_space: str, from_size: tuple[int,int], to_size: tuple[int,int]):
    if min(*from_size,*to_size) <= 0:
        raise ValueError("Coordinate spaces must have positive dimensions")
    return CoordinateTransform(from_space=from_space,to_space=to_space,
                               matrix=[to_size[0]/from_size[0],0,0, 0,to_size[1]/from_size[1],0, 0,0,1])


class SourceTimeline(Model):
    schema_version: Literal[1] = 1
    origin_pts_seconds: float
    source_time_convention: str
    source_frame_times_ms: list[float] = Field(min_length=1,max_length=30000)
    source_geometry: Geometry
    working_fps: Literal[30] = 30
    working_to_source: str
    source_frame_pts: list[int] = Field(default_factory=list,max_length=30000)
    source_time_base: str | None = None
    video_stream_index: int = Field(default=0,ge=0)
    source_duration_ms: int | None = Field(default=None,gt=0)
    working_frame_times_ms: list[float] = Field(default_factory=list,max_length=30000)
    working_frame_source_indices: list[int] = Field(default_factory=list,max_length=30000)
    transforms: list[CoordinateTransform] = Field(default_factory=list)
    source_asset_id: UUID | None = None
    source_sha256: str | None = Field(default=None,pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def valid(self):
        times = self.source_frame_times_ms
        if times[0] != 0 or times != sorted(times) or any(t < 0 for t in times):
            raise ValueError("Invalid source frame sequence")
        if self.source_duration_ms is not None and times[-1]>self.source_duration_ms+1:
            raise ValueError("Frame timestamps exceed the source duration")
        if self.source_frame_pts:
            if len(self.source_frame_pts) != len(times) or self.source_frame_pts != sorted(self.source_frame_pts):
                raise ValueError("Source PTS and frame times must agree")
            try:
                if Fraction(self.source_time_base or "0") <= 0:
                    raise ValueError("Invalid source time base")
            except (ZeroDivisionError,ValueError):
                raise ValueError("Invalid source time base") from None
            base=Fraction(self.source_time_base)
            if any(abs(float((p-self.source_frame_pts[0])*base*1000)-t)>.001
                   for p,t in zip(self.source_frame_pts,times)):
                raise ValueError("Frame timestamps disagree with original PTS")
        if len(self.working_frame_times_ms) != len(self.working_frame_source_indices):
            raise ValueError("Working frame map is incomplete")
        if any(i < 0 or i >= len(times) for i in self.working_frame_source_indices):
            raise ValueError("Working frame references an invalid source index")
        if self.working_frame_times_ms != sorted(self.working_frame_times_ms):
            raise ValueError("Working timeline is not chronological")
        return self

    def nearest_frame(self, time_ms: float):
        times = self.source_frame_times_ms
        index = bisect_left(times,time_ms)
        candidates = [i for i in (index-1,index) if 0 <= i < len(times)]
        # Deterministic tie chooses earlier frame. Evidence reports actual time.
        chosen=min(candidates,key=lambda i:(abs(times[i]-time_ms),i))
        return bisect_left(times,times[chosen])


def map_working_frames(source: SourceTimeline, working: SourceTimeline):
    if not source.source_frame_pts or not source.source_time_base:
        raise AppError("TIMELINE_UPGRADE_REQUIRED", "Source needs a timestamp-map refresh before analysis",409,True)
    base = Fraction(source.source_time_base)
    origin = source.source_frame_pts[0]
    # FFmpeg fps=30:round=near rescales each source PTS into output ticks,
    # then retains the last available frame with rounded PTS <= output tick.
    # This differs from naive nearest-source-time sampling, especially for VFR.
    ticks = [math.floor((pts-origin)*base*30+Fraction(1,2)) for pts in source.source_frame_pts]
    mapping = [max(0,bisect_right(ticks,round(t*30/1000))-1) for t in working.source_frame_times_ms]
    return mapping


def complete_timeline(source: SourceTimeline,working: SourceTimeline,proxy_geometry: Geometry):
    sg,wg=source.source_geometry,working.source_geometry
    source_size=(sg.display_width,sg.display_height)
    working_size=(wg.display_width,wg.display_height)
    proxy_size=(proxy_geometry.display_width,proxy_geometry.display_height)
    return SourceTimeline.model_validate({**source.model_dump(),
        "working_frame_source_indices":map_working_frames(source,working),
        "working_frame_times_ms":working.source_frame_times_ms,
        "transforms":[coded_to_display(sg),
                      scale_transform("source_display_pixels","working_pixels",source_size,working_size),
                      scale_transform("working_pixels","source_display_pixels",working_size,source_size),
                      scale_transform("working_pixels","proxy_pixels",working_size,proxy_size),
                      scale_transform("proxy_pixels","source_display_pixels",proxy_size,source_size)]})
