"""Window-local SAM candidates. Provider object IDs never establish global identity."""
from bisect import bisect_right
import hashlib
import math
from typing import Literal
from uuid import UUID,uuid5

from pydantic import Field,model_validator

from .observations import FrameRange
from ..editing import Track,TrackSample
from ..errors import AppError
from ..models import EvidenceFrame,Model
from ..programs.predicates import NodeId
from ..programs.policy import SourceInterval
from ..providers.sam import parse_sam,sam_video_response
from ..repository import encoded
from ..targeting import TargetRequest,object_prompt
from ..timing import CoordinateTransform,scale_transform
from ..validation import bounded_json

ENTITY_ADAPTER_VERSION='program-entity-tracks-1'


class SourceTrackSample(Model):
    input_frame_index: int = Field(ge=0,le=511,strict=True)
    source_frame_index: int = Field(ge=0,strict=True)
    source_time_ms: float = Field(ge=0)
    source_pts: int
    source_time_base: str
    x: float = Field(ge=0,lt=1)
    y: float = Field(ge=0,lt=1)
    width: float = Field(gt=0,le=1)
    height: float = Field(gt=0,le=1)

    @model_validator(mode='after')
    def inside(self):
        if self.x+self.width>1.000001 or self.y+self.height>1.000001:
            raise ValueError('Normalized box exceeds original display')
        return self


class TrackSegment(Model):
    id: UUID
    input_frames: FrameRange
    source_first_frame_index: int
    source_last_frame_index: int
    source_interval: SourceInterval
    continuity: Literal['consecutive_input_samples_only'] = 'consecutive_input_samples_only'


class EntityTrackCandidate(Model):
    id: UUID
    provider_object_id: int
    samples: list[SourceTrackSample] = Field(min_length=1,max_length=512)
    segments: list[TrackSegment] = Field(min_length=1,max_length=512)
    missing_input_ranges: list[FrameRange] = Field(max_length=512)
    identity: Literal['unknown'] = 'unknown'
    selection_required: Literal[True] = True
    lost_at_window_end: bool
    uncertainty: list[str]


class EntityTrackBatch(Model):
    schema_version: Literal['program-entity-tracks-1'] = ENTITY_ADAPTER_VERSION
    run_id: UUID
    program_fingerprint: str
    source_asset_id: UUID
    source_sha256: str
    source_timeline_sha256: str
    window_id: UUID
    input_sha256: str
    input_frame_count: int = Field(gt=0,le=512)
    entity_id: NodeId
    role: Literal['actor','object','instrument','context']
    prompt: str
    status: Literal['candidates','not_observed','unavailable']
    candidates: list[EntityTrackCandidate] = Field(max_length=32)
    coordinate_space: Literal['normalized_source_display'] = 'normalized_source_display'
    transforms: list[CoordinateTransform]
    negative_coverage_verified: Literal[False] = False
    uncertainty: list[str]
    provider_provenance: dict
    error_code: str | None = None


class BindingSelectionRequest(Model):
    batch_fingerprint: str = Field(pattern=r'^[0-9a-f]{64}$')
    candidate_id: UUID
    segment_id: UUID
    evidence_frame_id: UUID
    input_frame_index: int = Field(ge=0,le=511,strict=True)


class SelectedEntityBinding(Model):
    run_id: UUID
    program_fingerprint: str
    source_asset_id: UUID
    source_sha256: str
    window_id: UUID
    entity_id: NodeId
    batch_fingerprint: str
    candidate_id: UUID
    segment_id: UUID
    evidence_frame_id: UUID
    source_frame_index: int
    input_frame_index: int
    identity: Literal['user_selected_segment'] = 'user_selected_segment'
    origin: Literal['explicit_user'] = 'explicit_user'
    scope: Literal['selected_segment_only'] = 'selected_segment_only'
    cross_window_identity_verified: Literal[False] = False


def batch_fingerprint(batch):
    return hashlib.sha256(encoded(batch.model_dump(mode='json'))).hexdigest()


def _entity(prepared,entity_id):
    entity=next((e for e in prepared.ast.entities if e.id==entity_id),None)
    if entity is None or entity_id not in prepared.plan.entity_prompts:
        raise AppError('UNKNOWN_ENTITY','Choose an entity required by this reviewed observation plan',422)
    return entity


def _ranges(indices):
    result=[]
    for index in sorted(indices):
        if result and result[-1].stop_frame==index:result[-1].stop_frame=index+1
        else:result.append(FrameRange(start_frame=index,stop_frame=index+1))
    return result


def _base(prepared,entity_id,prompt,provenance):
    entity=_entity(prepared,entity_id)
    geometry=prepared.source.asset.metadata.geometry
    transforms=[*prepared.source.timeline.transforms,
        scale_transform('source_display_pixels','normalized_source_display',
            (geometry.display_width,geometry.display_height),(1,1))]
    return dict(run_id=prepared.plan.run_id,program_fingerprint=prepared.plan.program_fingerprint,
        source_asset_id=prepared.plan.source_asset_id,source_sha256=prepared.plan.source_sha256,
        source_timeline_sha256=prepared.plan.source_timeline_sha256,window_id=prepared.window.id,
        input_sha256=prepared.window.sha256,input_frame_count=len(prepared.window.source_frame_indices),
        entity_id=entity_id,role=entity.role,prompt=prompt,transforms=transforms,
        provider_provenance={**provenance,'entity_adapter_version':ENTITY_ADAPTER_VERSION,
            'plan_fingerprint':prepared.plan.request_fingerprint})


def normalize_entity_tracks(prepared,entity_id,text,provenance,*,prompt=None,remaining_samples=4096):
    _entity(prepared,entity_id)
    prompt=prompt or prepared.plan.entity_prompts[entity_id]
    if type(remaining_samples) is not int or not 0<=remaining_samples<=4096:
        raise AppError('OBSERVATION_LIMIT','Invalid remaining track-sample budget',422)
    if not isinstance(text,str) or len(text.encode('utf-8'))>16*1024*1024:
        raise AppError('PROVIDER_OUTPUT_LIMIT','SAM track output exceeds its bounded raw representation',502)
    count=len(prepared.window.source_frame_indices)
    objects=parse_sam(text,count,prepared.window.width,prepared.window.height)
    if len(objects)>32 or sum(len(v) for v in objects.values())>remaining_samples:
        raise AppError('OBSERVATION_LIMIT','SAM candidate/sample count exceeds remaining capacity; nothing was silently truncated',422)
    raw_hash=hashlib.sha256(text.encode('utf-8')).hexdigest()
    candidates=[];times=prepared.source.timeline.source_frame_times_ms
    for object_id,frames in sorted(objects.items()):
        identity=f'sam-entity:{entity_id}:{prepared.window.id}:{provenance.get("response_id")}:{raw_hash}:{object_id}'
        candidate_id=uuid5(prepared.plan.run_id,identity)
        samples=[];held={};conflicts=False
        for index,box in sorted(frames.items()):
            source_index=prepared.window.source_frame_indices[index]
            if (not 0<=source_index<len(times) or prepared.window.source_frame_times_ms[index]!=times[source_index]):
                raise AppError('SOURCE_MISMATCH','SAM input frame lost its exact original PTS identity',409)
            if source_index in held and held[source_index]!=box:conflicts=True
            held[source_index]=box
            samples.append(SourceTrackSample(input_frame_index=index,source_frame_index=source_index,
                source_time_ms=times[source_index],source_pts=prepared.source.timeline.source_frame_pts[source_index],
                source_time_base=prepared.source.timeline.source_time_base,x=box[0],y=box[1],width=box[2],height=box[3]))
        spans=_ranges(frames)
        segments=[TrackSegment(id=uuid5(candidate_id,f'segment:{span.start_frame}:{span.stop_frame}'),input_frames=span,
            source_first_frame_index=prepared.window.source_frame_indices[span.start_frame],
            source_last_frame_index=prepared.window.source_frame_indices[span.stop_frame-1],
            source_interval=_span_interval(prepared,span)) for span in spans]
        reasons=['NOUN_MATCH_IS_NOT_ENTITY_IDENTITY','PROVIDER_IDS_ARE_RESPONSE_LOCAL',
            'CONTINUITY_BETWEEN_INPUT_SAMPLES_IS_UNVERIFIED']
        if len(objects)>1:reasons.append('MULTIPLE_ENTITY_CANDIDATES')
        if len(segments)>1:reasons.append('IDENTITY_NOT_PROPAGATED_ACROSS_MISSING_SAMPLES')
        if conflicts:reasons.append('CONFLICTING_BOXES_FOR_HELD_ORIGINAL_FRAME')
        candidates.append(EntityTrackCandidate(id=candidate_id,provider_object_id=object_id,samples=samples,segments=segments,
            missing_input_ranges=_ranges(set(range(count))-set(frames)),lost_at_window_end=max(frames)<count-1,uncertainty=reasons))
    batch=EntityTrackBatch(**_base(prepared,entity_id,prompt,{**provenance,'raw_response_sha256':raw_hash}),
        status='candidates' if candidates else 'not_observed',candidates=candidates,
        uncertainty=['No entity identity or absence is certified by this response'] if candidates else ['No SAM samples returned; physical absence is unverified'])
    bounded_json(batch.model_dump(mode='json'),max_bytes=4*1024*1024)
    return batch


def _span_interval(prepared,span):
    times=prepared.source.timeline.source_frame_times_ms
    first=prepared.window.source_frame_indices[span.start_frame]
    last=prepared.window.source_frame_indices[span.stop_frame-1]
    stop=bisect_right(times,times[last])
    end=times[stop] if stop<len(times) else prepared.source.asset.metadata.duration_ms
    start=max(prepared.planned_window.source_interval.start_ms,math.ceil(times[first]))
    end=min(prepared.planned_window.source_interval.end_ms,math.ceil(end))
    if start>=end:raise AppError('PROVIDER_WINDOW_MAPPING','SAM segment lies outside the requested source interval',502)
    return SourceInterval(start_ms=start,end_ms=end)


def _owned_batch(prepared,batch):
    if (batch.run_id!=prepared.plan.run_id or batch.program_fingerprint!=prepared.plan.program_fingerprint
            or batch.window_id!=prepared.window.id or batch.source_sha256!=prepared.plan.source_sha256
            or batch.source_asset_id!=prepared.plan.source_asset_id or batch.input_sha256!=prepared.window.sha256
            or batch.source_timeline_sha256!=prepared.plan.source_timeline_sha256):
        raise AppError('OWNERSHIP','Track candidates belong to another source/run/window',422)
    if batch.role!=_entity(prepared,batch.entity_id).role:
        raise AppError('OWNERSHIP','Track candidate role differs from the reviewed entity',422)


def selection_prompt(prepared,entity_id,selection):
    _entity(prepared,entity_id)
    selection=TargetRequest.model_validate(selection)
    frame=prepared.source.store.load(prepared.source.asset.project_id,'evidence',EvidenceFrame,selection.evidence_frame_id)
    if frame.source_asset_id!=prepared.plan.source_asset_id or frame.source_sha256!=prepared.plan.source_sha256:
        raise AppError('OWNERSHIP','Target evidence belongs to another original source',422)
    original=prepared.source.frame(0,source_frame_index=frame.source_frame_index)
    if original.evidence_frame_id!=frame.id or original.image_sha256!=frame.image_sha256:
        raise AppError('EVIDENCE_MISMATCH','Target frame identity differs from original source evidence',409)
    try:index=prepared.window.source_frame_indices.index(frame.source_frame_index)
    except ValueError:
        raise AppError('INVALID_TARGET_FRAME','Target evidence is not represented in this input window',422) from None
    return object_prompt(selection,index,prepared.window.width,prepared.window.height),index


def select_candidate(prepared,batch,request):
    request=BindingSelectionRequest.model_validate(request)
    _owned_batch(prepared,batch)
    if request.batch_fingerprint!=batch_fingerprint(batch):
        raise AppError('STALE_TRACK_SELECTION','Inspect these exact candidates again before binding an entity',409)
    _entity(prepared,batch.entity_id)
    candidate=next((c for c in batch.candidates if c.id==request.candidate_id),None)
    segment=next((s for s in candidate.segments if s.id==request.segment_id),None) if candidate else None
    if segment is None or not segment.input_frames.start_frame<=request.input_frame_index<segment.input_frames.stop_frame:
        raise AppError('INVALID_TRACK_SELECTION','Choose an observed frame inside this candidate segment',422)
    source_index=prepared.window.source_frame_indices[request.input_frame_index]
    frame=prepared.source.frame(0,source_frame_index=source_index)
    if frame.evidence_frame_id!=request.evidence_frame_id:
        raise AppError('EVIDENCE_MISMATCH','Selection evidence is not the original frame shown for this candidate',409)
    return SelectedEntityBinding(run_id=batch.run_id,program_fingerprint=batch.program_fingerprint,entity_id=batch.entity_id,
        source_asset_id=batch.source_asset_id,source_sha256=batch.source_sha256,window_id=batch.window_id,
        batch_fingerprint=request.batch_fingerprint,candidate_id=candidate.id,segment_id=segment.id,
        evidence_frame_id=frame.evidence_frame_id,source_frame_index=source_index,input_frame_index=request.input_frame_index)


def compatibility_track(prepared,batch,candidate_id,segment_id):
    """Rendering/storage adapter; retains exact local->original map in metadata."""
    _owned_batch(prepared,batch)
    candidate=next((c for c in batch.candidates if c.id==candidate_id),None)
    segment=next((s for s in candidate.segments if s.id==segment_id),None) if candidate else None
    if segment is None:raise AppError('UNKNOWN_TRACK','Unknown entity candidate segment',422)
    if 'CONFLICTING_BOXES_FOR_HELD_ORIGINAL_FRAME' in candidate.uncertainty:
        raise AppError('AMBIGUOUS_TRACK','Conflicting held-frame boxes cannot be collapsed into a rendering track',409)
    exact=[s for s in candidate.samples if segment.input_frames.start_frame<=s.input_frame_index<segment.input_frames.stop_frame]
    observed={}
    for sample in exact:
        ms=round(sample.source_time_ms)
        value=TrackSample(time_ms=ms,x=sample.x,y=sample.y,width=sample.width,height=sample.height)
        if ms in observed and observed[ms]!=value:
            raise AppError('AMBIGUOUS_TRACK','Rounding would merge different source-frame boxes; exact evidence remains available',409)
        observed[ms]=value
    return Track(id=segment.id,project_id=prepared.source.asset.project_id,source_asset_id=batch.source_asset_id,
        label=batch.entity_id,origin='meta_cloud',samples=[observed[t] for t in sorted(observed)],
        provider_metadata={**batch.provider_provenance,'entity_id':batch.entity_id,'entity_binding':'unknown',
            'run_id':str(batch.run_id),'program_fingerprint':batch.program_fingerprint,'window_id':str(batch.window_id),
            'candidate_id':str(candidate.id),'segment_id':str(segment.id),'source_sha256':batch.source_sha256,
            'source_timeline_sha256':batch.source_timeline_sha256,'exact_samples':[s.model_dump(mode='json') for s in exact],
            'transforms':[t.model_dump(mode='json') for t in batch.transforms],
            'uncertainty':candidate.uncertainty})


def observe_entity(cloud,prepared,entity_id,*,selection=None,remaining_samples=4096,record_response=None):
    _entity(prepared,entity_id)
    if type(remaining_samples) is not int or not 1<=remaining_samples<=4096:
        raise AppError('OBSERVATION_LIMIT','No bounded track-sample capacity remains; no request was made',422)
    pinned=prepared.plan.providers
    if (pinned.get('endpoint')!=cloud.settings.meta_api_base_url.rstrip('/') or pinned.get('sam_model')!=cloud.settings.sam_model
            or pinned.get('sam_adapter_version')!=ENTITY_ADAPTER_VERSION):
        raise AppError('PROVIDER_CONFIGURATION_MISMATCH','SAM configuration differs from the run snapshot; create a new run',409)
    prompt=prepared.plan.entity_prompts[entity_id]
    if selection is not None:prompt,_=selection_prompt(prepared,entity_id,selection)
    source=prepared.source
    source._verify(source.asset.source_path,source.asset.sha256)
    source._verify(source.asset.timeline_path,prepared.plan.source_timeline_sha256)
    source._verify(prepared.window.path,prepared.window.sha256)
    provenance={}
    try:
        text,provenance=sam_video_response(cloud,prepared.window,source.store.path(source.asset.project_id,prepared.window.path),prompt,
            selection=selection is not None)
        if record_response:record_response(text,provenance)
        return normalize_entity_tracks(prepared,entity_id,text,provenance,prompt=prompt,remaining_samples=remaining_samples)
    except AppError as exc:
        # A failed request is distinguishable from a successful empty response.
        # Resource/admission/integrity errors remain errors, never empty tracks.
        if not exc.code.startswith('PROVIDER_'):raise
        return EntityTrackBatch(**_base(prepared,entity_id,prompt,{**provenance,'error':exc.envelope()}),status='unavailable',candidates=[],
            uncertainty=['Tracking unavailable; entity presence and identity are unknown'],error_code=exc.code)
