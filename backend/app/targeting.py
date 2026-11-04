"""User point/box prompts on immutable evidence, followed by real cloud tracking."""
import math
from typing import Literal
from uuid import UUID,uuid4

from pydantic import Field,model_validator

from .corrections import CorrectionRecord,latest_events
from .errors import AppError
from .models import ActionEvent,ActionIntent,Asset,EvidenceFrame,Model,now
from .providers.meta import MetaCloud
from .providers.sam import track_window
from .sampling import extract_window
from .editing import Track
from .motion import attach_geometry
from .scenes import inspect_scenes


class TargetPoint(Model):
    x: float = Field(ge=0,le=1)
    y: float = Field(ge=0,le=1)


class TargetRequest(Model):
    evidence_frame_id: UUID
    mode: Literal['point','box']='point'
    x: float = Field(ge=0,le=1)
    y: float = Field(ge=0,le=1)
    width: float | None = Field(default=None,gt=0,le=1)
    height: float | None = Field(default=None,gt=0,le=1)
    negative_points: list[TargetPoint] = Field(default_factory=list,max_length=8)

    @model_validator(mode='after')
    def geometry(self):
        if self.mode=='box':
            if self.width is None or self.height is None or self.x+self.width>1 or self.y+self.height>1:
                raise ValueError('Box must fit inside evidence image')
        elif self.width is not None or self.height is not None:
            raise ValueError('Point prompts do not have box dimensions')
        return self


def object_prompt(selection: TargetRequest,frame_index,width,height):
    def x(value):return min(width-1,math.floor(value*width))
    def y(value):return min(height-1,math.floor(value*height))
    text=f'<{frame_index}f>1'
    if selection.mode=='box':
        # Browser box extent is half-open; SAM's lower/right corners are inclusive.
        right=max(x(selection.x),min(width-1,math.ceil((selection.x+selection.width)*width)-1))
        bottom=max(y(selection.y),min(height-1,math.ceil((selection.y+selection.height)*height)-1))
        text+=f'<|box;x1={x(selection.x)};y1={y(selection.y)};x2={right};y2={bottom};w={width};h={height}|>'
    else:text+=f'<|point;x={x(selection.x)};y={y(selection.y)};w={width};h={height}|>'
    for negative in selection.negative_points:
        text+=f'-<|point;x={x(negative.x)};y={y(negative.y)};w={width};h={height}|>'
    return text


def select_target(store,runner,settings,job):
    event=store.load(job.project_id,'events',ActionEvent,job.request['event_id'])
    if event.id not in {e.id for e in latest_events(store.list_records(job.project_id,'events',ActionEvent))}:
        raise AppError('STALE_EVENT','Refresh the corrected event before choosing a target',409)
    selection=TargetRequest.model_validate(job.request['selection'])
    frame=store.load(job.project_id,'evidence',EvidenceFrame,selection.evidence_frame_id)
    asset=store.load(job.project_id,'assets',Asset,event.source_asset_id)
    intent=store.load(job.project_id,'intents',ActionIntent,event.intent_id)
    if frame.source_asset_id!=asset.id or frame.source_sha256!=asset.sha256:
        raise AppError('OWNERSHIP','Target frame belongs to a different source',422)
    runner.stage(job,'sampling',None)
    window=extract_window(store,settings,asset,max(0,event.start_ms-intent.context_before_ms),
                          min(asset.working_metadata.duration_ms,event.end_ms+intent.context_after_ms))
    try:local_frame=window.source_frame_indices.index(frame.source_frame_index)
    except ValueError:
        raise AppError('INVALID_TARGET_FRAME','Choose an evidence frame represented inside this action window',422) from None
    prompt=object_prompt(selection,local_frame,window.width,window.height)
    runner.stage(job,'tracking',None)
    with MetaCloud(settings) as cloud:
        tracks,provenance=track_window(cloud,window,store.path(job.project_id,window.path),prompt,label=event.primary_subject)
    if len(tracks)!=1 or tracks[0].provider_metadata.get('object_id')!=1:
        raise AppError('SUBJECT_SELECTION_REQUIRED','SAM did not confirm one target; choose a clearer point or box',409,True)
    track=tracks[0]
    track.provider_metadata.update(selection=selection.model_dump(mode='json'),selection_origin='user',prompt_frame_index=local_frame)
    store.save(job.project_id,'tracks',track)
    data=event.model_dump()
    data.update(id=uuid4(),created_at=now(),parent_event_id=event.id,primary_track_id=track.id,
                track_ids=[*event.track_ids,track.id],review_required=True,
                provider_metadata={**event.provider_metadata,'target_selection':track.provider_metadata})
    corrected=ActionEvent.model_validate(data)
    runner.stage(job,'inspecting',None)
    scenes=inspect_scenes(store,settings,window)
    available,unavailable=store.scan_records(job.project_id,'tracks',Track)
    contexts=[t for t in available if t.id in event.track_ids and t.label in event.context_subjects]
    attach_geometry(corrected,track,contexts,window,scenes)
    if unavailable:corrected.warnings.append('Some earlier track records are damaged; context visibility requires review')
    record=CorrectionRecord(project_id=job.project_id,source_asset_id=asset.id,parent_event_id=event.id,
                            event_id=corrected.id,origin='user_target',note='User-selected target on source evidence',
                            previous=event.model_dump(mode='json'),corrected=corrected.model_dump(mode='json'),
                            evidence_frame_ids=[frame.id])
    store.save(job.project_id,'corrections',record)
    store.save(job.project_id,'events',corrected)
    return {'event_id':str(corrected.id),'track_id':str(track.id),'evidence_frame_id':str(frame.id),'provider':provenance}
