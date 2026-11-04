"""Validated EDLs, exact time maps, and evidence-linked crop planning."""
from bisect import bisect_right
from fractions import Fraction
import hashlib
import json
import math
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from .errors import AppError
from .models import Asset, Model, Record


class TrackSample(Model):
    time_ms: int = Field(ge=0)
    # Normalized, oriented source-display coordinates, not coded/proxy pixels.
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    width: float = Field(gt=0, le=1)
    height: float = Field(gt=0, le=1)
    visible: bool = True

    @model_validator(mode="after")
    def inside_frame(self):
        if self.x + self.width > 1.000001 or self.y + self.height > 1.000001:
            raise ValueError("Track box exceeds the source frame")
        return self


class Track(Record):
    project_id: UUID
    source_asset_id: UUID
    label: str
    origin: Literal["meta_cloud", "synthetic_test", "user_defined"]
    samples: list[TrackSample] = Field(min_length=1, max_length=30000)
    provider_metadata: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def ordered(self):
        times = [s.time_ms for s in self.samples]
        if any(b <= a for a, b in zip(times, times[1:])):
            raise ValueError("Normalized track samples must have unique increasing timestamps")
        return self


class CropPoint(Model):
    time_ms: int = Field(ge=0)
    x: float = Field(ge=0)
    y: float = Field(ge=0)


class Crop(Model):
    mode: Literal["fixed", "track_subject", "full_frame"] = "fixed"
    track_id: UUID | None = None
    context_track_ids: list[UUID] = Field(default_factory=list,max_length=16)
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    points: list[CropPoint] = Field(min_length=1, max_length=128)
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def valid(self):
        if self.width % 2 or self.height % 2:
            raise ValueError("Crop size must be even for H.264")
        if (self.mode == "track_subject") != (self.track_id is not None):
            raise ValueError("Tracked crop requires a track ID; fixed crop cannot claim one")
        if self.mode == "fixed" and len(self.points) != 1:
            raise ValueError("Fixed crop has one position")
        times = [p.time_ms for p in self.points]
        if any(b <= a for a,b in zip(times,times[1:])):
            raise ValueError("Crop points must be strictly chronological")
        return self


class Segment(Model):
    source_in_ms: int = Field(ge=0)
    source_out_ms: int = Field(gt=0)
    speed: float = Field(default=1, ge=.25, le=3)
    crop: Crop
    event_id: UUID | None = None
    phase_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def duration(self):
        if self.source_out_ms <= self.source_in_ms:
            raise ValueError("Source segment must have positive duration")
        if (self.source_out_ms - self.source_in_ms) / self.speed < 1000 / 30:
            raise ValueError("Segment is shorter than one output frame")
        return self


class OutputSettings(Model):
    width: Literal[360, 1080] = 1080
    height: Literal[640, 1920] = 1920
    fps: Literal[30] = 30
    codec: Literal["h264"] = "h264"

    @model_validator(mode="after")
    def ratio(self):
        if self.width * 16 != self.height * 9:
            raise ValueError("Only paired 9:16 output dimensions are currently supported")
        return self


class ActionLineage(Model):
    program_id: UUID
    program_version: int = Field(ge=1)
    program_fingerprint: str = Field(pattern=r'^[0-9a-f]{64}$')
    run_id: UUID
    action_id: UUID
    event_ids: list[UUID] = Field(min_length=1, max_length=32)
    verdict: Literal['true', 'false', 'unknown']
    review_only: bool
    fixture_only: bool
    origin: Literal['manual_fixture', 'evaluated']
    action_fingerprint: str = Field(pattern=r'^[0-9a-f]{64}$')


class EditPlan(Record):
    project_id: UUID
    source_asset_id: UUID
    origin: Literal["manual", "autonomous"]
    parent_id: UUID | None = None
    version: int = Field(default=1, ge=1)
    intent_id: UUID | None = None
    selected_event_ids: list[UUID] = Field(default_factory=list)
    requested_duration_ms: int = Field(default=20000, ge=1, le=120000)
    output: OutputSettings = Field(default_factory=OutputSettings)
    segments: list[Segment] = Field(min_length=1, max_length=120)
    reasoning: list[dict] = Field(default_factory=list)
    event_snapshots: list[dict] = Field(default_factory=list)
    actionos_lineage: ActionLineage | None = None

    @model_validator(mode="after")
    def source_progresses(self):
        if any(b.source_in_ms < a.source_out_ms for a,b in zip(self.segments, self.segments[1:])):
            raise ValueError("Action Focus cannot repeat or reverse source moments")
        if self.origin == "autonomous" and (not self.intent_id or not self.selected_event_ids):
            raise ValueError("Autonomous plans require intent and event lineage")
        return self


class Export(Record):
    project_id: UUID
    plan_id: UUID
    plan_version: int
    path: str
    manifest_path: str
    duration_ms: int
    origin: Literal["manual", "autonomous"]
    actionos_lineage: ActionLineage | None = None


def timeline(plan: EditPlan):
    cursor = Fraction(0)
    mapping = []
    for i, segment in enumerate(plan.segments):
        speed = Fraction(str(segment.speed))
        duration = Fraction(segment.source_out_ms - segment.source_in_ms) / speed
        mapping.append({"segment_index": i, "source_in_ms": segment.source_in_ms,
                        "source_out_ms": segment.source_out_ms, "speed": float(speed),
                        "output_in_ms": float(cursor), "output_out_ms": float(cursor + duration)})
        cursor += duration
    return mapping


def output_to_source(plan: EditPlan, output_ms: float):
    mapping = timeline(plan)
    if not 0 <= output_ms <= mapping[-1]["output_out_ms"]:
        raise ValueError("Output timestamp outside plan")
    for row in mapping:
        if output_ms < row["output_out_ms"] or row is mapping[-1]:
            return row["source_in_ms"] + (output_ms - row["output_in_ms"]) * row["speed"]


def fingerprint(plan: EditPlan):
    return fingerprint_data(plan.model_dump(mode='json'))


def fingerprint_data(data):
    # Keep raw serialized fingerprints valid when additive model defaults are
    # introduced. Existing recorded fields must still match exactly.
    data={k:v for k,v in data.items() if k not in ('id','created_at')}
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def validate_plan(plan: EditPlan, asset: Asset):
    if asset.status != "ready" or not asset.metadata or not asset.working_metadata or not asset.working_path:
        raise AppError("MEDIA_NOT_READY", "Prepared source is required for rendering", 409)
    if plan.project_id != asset.project_id or plan.source_asset_id != asset.id:
        raise AppError("OWNERSHIP", "Plan does not reference this source", 422)
    geometry = asset.working_metadata.geometry
    end_limit = min(asset.metadata.duration_ms, asset.working_metadata.duration_ms)
    for segment in plan.segments:
        if segment.source_out_ms > end_limit:
            raise AppError("INVALID_PLAN", "Segment exceeds prepared source duration", 422)
        crop = segment.crop
        if crop.width > geometry.display_width or crop.height > geometry.display_height:
            raise AppError("INVALID_PLAN", "Crop exceeds source dimensions", 422)
        if crop.mode == 'full_frame' and (crop.width != geometry.display_width or crop.height != geometry.display_height
                                        or any(p.x != 0 or p.y != 0 for p in crop.points)):
            raise AppError('INVALID_PLAN', 'Full-frame output must preserve the entire display image', 422)
        if crop.mode != 'full_frame' and abs(crop.width / crop.height - plan.output.width / plan.output.height) > 2 / crop.height:
            raise AppError("INVALID_PLAN", "Crop does not match the target ratio", 422)
        for point in crop.points:
            if point.x + crop.width > geometry.display_width + .01 or point.y + crop.height > geometry.display_height + .01:
                raise AppError("INVALID_PLAN", "Crop position exceeds source bounds", 422)
        if crop.mode == "track_subject" and (crop.points[0].time_ms > segment.source_in_ms or crop.points[-1].time_ms < segment.source_out_ms):
            raise AppError("INVALID_PLAN", "Crop path does not cover the source interval", 422)
        if segment.event_id and segment.event_id not in plan.selected_event_ids:
            raise AppError("INVALID_PLAN", "Segment event is absent from plan lineage", 422)
    if timeline(plan)[-1]["output_out_ms"] > 120000:
        raise AppError("INVALID_PLAN", "Output exceeds the supported 120-second limit", 422)


def fixed_crop(width: int, height: int):
    crop_h = height - height % 2
    crop_w = int(crop_h * 9 / 16) // 2 * 2
    if crop_w > width:
        crop_w = width - width % 2
        crop_h = int(crop_w * 16 / 9) // 2 * 2
    return Crop(width=crop_w, height=crop_h,
                points=[CropPoint(time_ms=0, x=(width-crop_w)/2, y=(height-crop_h)/2)])


def crop_at(crop: Crop, time_ms: float):
    if time_ms <= crop.points[0].time_ms:
        return crop.points[0].x, crop.points[0].y
    if time_ms >= crop.points[-1].time_ms:
        return crop.points[-1].x, crop.points[-1].y
    index = bisect_right([p.time_ms for p in crop.points], time_ms) - 1
    a, b = crop.points[index:index+2]
    fraction = (time_ms - a.time_ms) / (b.time_ms - a.time_ms)
    return a.x + (b.x-a.x) * fraction, a.y + (b.y-a.y) * fraction


def plan_crop(track: Track, width: int, height: int, start_ms: int, end_ms: int,
              max_gap_ms=400, max_velocity=1200., max_acceleration=4000.,context_tracks=()):
    """Bounded crop smoothing in actual time. Long loss explicitly requires review."""
    base = fixed_crop(width, height)
    visible = [s for s in track.samples if s.visible]
    if len(visible) < 2:
        raise AppError("SUBJECT_LOST", "Insufficient visible tracking samples; choose a fixed crop", 422)
    times = [s.time_ms for s in visible]
    if times[0]-start_ms>max_gap_ms or end_ms-times[-1]>max_gap_ms:
        raise AppError("SUBJECT_LOST", "Tracking does not cover the requested segment", 422)
    if any(b-a > max_gap_ms for a,b in zip(times,times[1:]) if b > start_ms and a < end_ms):
        raise AppError("SUBJECT_LOST", "Tracking gap is too long to interpolate reliably", 422)
    # Bound expression size. Large intervals require a future denser execution stage.
    sample_times = sorted({start_ms, end_ms, *[t for t in times if start_ms < t < end_ms],
                           *[s.time_ms for c in context_tracks for s in c.samples if start_ms<s.time_ms<end_ms]})
    contexts=[([s for s in c.samples if s.visible],c.label) for c in context_tracks]
    desired_points, smoothed, warnings = [], [], set()
    if times[0]>start_ms or times[-1]<end_ms:
        warnings.add('Crop holds the nearest observed position across a short boundary gap')
    for time_ms in sample_times:
        index = min(len(visible)-2, max(0, bisect_right(times, time_ms)-1))
        a,b = visible[index:index+2]
        f = max(0.,min(1.,(time_ms-a.time_ms)/(b.time_ms-a.time_ms)))
        cx = (a.x + a.width/2 + f*(b.x+b.width/2-a.x-a.width/2))*width
        cy = (a.y + a.height*.45 + f*(b.y+b.height*.45-a.y-a.height*.45))*height
        if contexts:
            left=a.x+f*(b.x-a.x);top=a.y+f*(b.y-a.y)
            right=left+a.width+f*(b.width-a.width);bottom=top+a.height+f*(b.height-a.height)
            for samples,label in contexts:
                ct=[s.time_ms for s in samples]
                if not ct or time_ms<ct[0] or time_ms>ct[-1]:
                    warnings.add('Context object tracking does not cover the complete edit: '+label)
                    continue
                ci=bisect_right(ct,time_ms)-1
                ca=samples[ci];cb=samples[min(ci+1,len(samples)-1)]
                if ca.time_ms!=time_ms and cb.time_ms-ca.time_ms>max_gap_ms:
                    warnings.add('Context object has a tracking gap: '+label);continue
                cf=(time_ms-ca.time_ms)/(cb.time_ms-ca.time_ms) if cb.time_ms>ca.time_ms else 0
                lx=ca.x+cf*(cb.x-ca.x);ty=ca.y+cf*(cb.y-ca.y)
                left=min(left,lx);top=min(top,ty)
                right=max(right,lx+ca.width+cf*(cb.width-ca.width))
                bottom=max(bottom,ty+ca.height+cf*(cb.height-ca.height))
            if (right-left)*width<=base.width and (bottom-top)*height<=base.height:
                # Prefer subject centering, moving only enough to include context.
                cx=min(left*width+base.width/2,max(right*width-base.width/2,cx))
                cy=min(top*height+base.height/2,max(bottom*height-base.height/2,cy))
            else:
                warnings.add('Subject and requested context cannot fit together in the vertical crop; primary subject has priority')
        desired = (min(width-base.width, max(0., cx-base.width/2)),
                   min(height-base.height, max(0., cy-base.height/2)))
        desired_points.append(desired)
        if smoothed:
            dt=(time_ms-sample_times[len(smoothed)-1])/1000
            alpha=1-math.exp(-dt/.12)
            x=smoothed[-1][0]+alpha*(desired[0]-smoothed[-1][0])
            y=smoothed[-1][1]+alpha*(desired[1]-smoothed[-1][1])
        else:
            x,y = desired
        smoothed.append((x,y))
        if max(a.width,b.width)*width > base.width or max(a.height,b.height)*height > base.height:
            warnings.add("Subject box cannot fully fit in the vertical crop")
    # Scale the complete bounded trajectory around its first position. Velocity
    # and finite-difference acceleration both scale linearly, so this preserves
    # frame bounds without the sudden acceleration caused by edge/target snaps.
    scale=1.
    previous_velocity=(0.,0.)
    for index in range(1,len(smoothed)):
        dt=(sample_times[index]-sample_times[index-1])/1000
        velocity=tuple((b-a)/dt for a,b in zip(smoothed[index-1],smoothed[index]))
        for current,previous in zip(velocity,previous_velocity):
            if abs(current)>0: scale=min(scale,max_velocity/abs(current))
            acceleration=abs(current-previous)/dt
            if acceleration>0: scale=min(scale,max_acceleration/acceleration)
        previous_velocity=velocity
    points=[]
    initial=smoothed[0]
    for time_ms,position,desired in zip(sample_times,smoothed,desired_points):
        x,y=tuple(a+scale*(b-a) for a,b in zip(initial,position))
        if abs(x-desired[0])>base.width*.25 or abs(y-desired[1])>base.height*.25:
            warnings.add("Subject motion exceeds smooth crop limits; inspect framing")
        points.append(CropPoint(time_ms=time_ms,x=x,y=y))
    if len(points)>128:
        keep={0,len(points)-1};pending=[(0,len(points)-1)]
        while pending:
            left,right=pending.pop()
            a,b=points[left],points[right]
            worst,error=None,0.
            for index in range(left+1,right):
                p=points[index];f=(p.time_ms-a.time_ms)/(b.time_ms-a.time_ms)
                distance=math.hypot(p.x-(a.x+(b.x-a.x)*f),p.y-(a.y+(b.y-a.y)*f))
                if distance>error:worst,error=index,distance
            if worst is not None and error>1.:
                keep.add(worst);pending.extend([(left,worst),(worst,right)])
        points=[points[i] for i in sorted(keep)]
        if len(points)>128:
            raise AppError('CROP_PATH_LIMIT','Motion needs a shorter window for accurate crop execution',422)
    return Crop(mode="track_subject",track_id=track.id,context_track_ids=[c.id for c in context_tracks],width=base.width,height=base.height,
                points=points,warnings=sorted(warnings))
