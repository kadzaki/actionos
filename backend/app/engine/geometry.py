"""A27 image-space measurements. These are candidate evidence, never rule truth."""
import math
import hashlib
from typing import Literal
from uuid import UUID,uuid5

from pydantic import Field

from .entity_tracks import _owned_batch,_span_interval
from .observations import FrameRange
from ..errors import AppError
from ..models import Model
from ..motion import center,box_iou
from ..programs.policy import SourceInterval
from ..repository import encoded
from ..validation import bounded_json

GEOMETRY_VERSION='predicate-geometry-1'
MAX_GAP_MS=400


class GeometryMeasurement(Model):
    id: UUID
    step_id: str
    predicate: str
    source_interval: SourceInterval
    input_start_frame: int
    input_end_frame: int
    source_start_frame: int
    source_end_frame: int
    source_start_pts: int
    source_end_pts: int
    source_time_base: str
    measured_elapsed_ms: float
    metrics: dict[str,float|str|bool|None]
    comparison_result: bool | None
    comparison_scope: Literal['sample_geometry_only'] = 'sample_geometry_only'
    limitations: list[str]


class GeometricEvidence(Model):
    schema_version: Literal['predicate-geometry-1'] = GEOMETRY_VERSION
    run_id: UUID
    program_fingerprint: str
    source_asset_id: UUID
    source_sha256: str
    source_timeline_sha256: str
    window_id: UUID
    step_id: str
    predicate: str
    parameters: dict
    anchor: Literal['interval','cycle_start']
    candidate_ids: dict[str,str]
    measurements: list[GeometryMeasurement] = Field(max_length=512)
    unmet_requirements: list[str]
    coordinate_space: Literal['normalized_source_display'] = 'normalized_source_display'
    physical_measurement: Literal[False] = False
    camera_motion_compensated: Literal[False] = False
    negative_coverage_verified: Literal[False] = False
    provenance: dict


def _scene_frames(prepared,scenes):
    if scenes is None:return None
    w=prepared.window
    if (scenes.project_id!=w.project_id or scenes.source_asset_id!=w.source_asset_id
            or scenes.window_id!=w.id or scenes.video_sha256!=w.sha256):
        raise AppError('OWNERSHIP','Scene evidence belongs to another tracking input',422)
    frames=[]
    for cut in scenes.cuts:
        index=cut.window_frame_index
        if (not 0<=index<len(w.source_frame_indices) or cut.source_frame_index!=w.source_frame_indices[index]
                or cut.source_time_ms!=round(w.source_frame_times_ms[index])):
            raise AppError('SOURCE_MISMATCH','Scene cut lost its actual source-frame mapping',409)
        frames.append(index)
    return sorted(set(frames))


def _continuity(prepared,series,start,end,cuts):
    reasons=[]
    if cuts is None:reasons.append('SCENE_CUTS_NOT_MEASURED')
    elif any(start<c<=end for c in cuts):reasons.append('SCENE_CUT')
    for samples in series:
        if any(i not in samples for i in range(start,end+1)):
            reasons.append('MISSING_INPUT_SAMPLES');continue
        values=[samples[i] for i in range(start,end+1)]
        for a,b in zip(values,values[1:]):
            if b.source_frame_index-a.source_frame_index>1:reasons.append('OMITTED_ORIGINAL_FRAMES')
            if b.source_time_ms-a.source_time_ms>MAX_GAP_MS:reasons.append('SOURCE_OBSERVATION_GAP')
    return sorted(set(reasons))


def derive_geometry(prepared,step_id,assignments,*,scenes=None):
    """assignments maps each required entity to (validated batch, candidate UUID).

    Candidate identity remains unknown here. Fusion must supply the selected
    binding, semantic support and coverage before any rule-level conclusion.
    """
    step=next((s for s in prepared.ast.steps if s.id==step_id),None)
    if step is None:raise AppError('UNKNOWN_STEP','Unknown geometry predicate node',422)
    if set(assignments)!=set(step.entities):
        raise AppError('INVALID_ENTITY_BINDINGS','Geometry requires the exact predicate entity arguments',422)
    candidates=[];series=[];candidate_ids={};base_limits=['ENTITY_IDENTITY_REQUIRES_FUSION','IMAGE_SPACE_ESTIMATE_NOT_PHYSICAL_DISTANCE']
    for entity in step.entities:
        batch,candidate_id=assignments[entity]
        _owned_batch(prepared,batch)
        if batch.entity_id!=entity:raise AppError('OWNERSHIP','Candidate belongs to a different program entity',422)
        candidate=next((c for c in batch.candidates if c.id==candidate_id),None)
        if candidate is None:raise AppError('UNKNOWN_TRACK','Choose a candidate from this exact entity batch',422)
        mapping={s.input_frame_index:s for s in candidate.samples}
        if len(mapping)!=len(candidate.samples):raise AppError('EVIDENCE_MISMATCH','Duplicate input-frame samples in geometry input',409)
        for index,sample in mapping.items():
            if (index>=len(prepared.window.source_frame_indices)
                    or sample.source_frame_index!=prepared.window.source_frame_indices[index]
                    or sample.source_time_ms!=prepared.window.source_frame_times_ms[index]
                    or sample.source_pts!=prepared.source.timeline.source_frame_pts[sample.source_frame_index]
                    or sample.source_time_base!=prepared.source.timeline.source_time_base):
                raise AppError('EVIDENCE_MISMATCH','Geometry sample differs from its exact original PTS map',409)
        candidate_ids[entity]=str(candidate.id);candidates.append(candidate);series.append(mapping)
    cuts=_scene_frames(prepared,scenes)
    scene_hash=hashlib.sha256(encoded(scenes.model_dump(mode='json'))).hexdigest() if scenes else None
    w=prepared.source.asset.metadata.geometry
    diagonal=math.hypot(w.display_width,w.display_height)
    def distance(a,b):
        ax,ay=center(a);bx,by=center(b)
        return math.hypot((ax-bx)*w.display_width,(ay-by)*w.display_height)/diagonal
    parameters=step.parameters.model_dump(mode='json')
    node_hash=hashlib.sha256(encoded(step.model_dump(mode='json'))).hexdigest()
    measurements=[];unmet=[]
    common=sorted(set.intersection(*(set(s) for s in series)))
    if not common:unmet.append('NO_JOINT_OBSERVED_INPUT_FRAMES')
    supported={'left_of','inside','near','overlap','moves','approaches','separates'}
    if step.predicate not in supported:
        common=[];unmet.append('SEMANTIC_OR_VISIBILITY_SUPPORT_REQUIRED; geometry alone cannot establish this predicate')
    moving=step.predicate in ('moves','approaches','separates')
    for end in common:
        start=end;values={};result=None;limitations=list(base_limits)
        if any('CONFLICTING_BOXES_FOR_HELD_ORIGINAL_FRAME' in c.uncertainty for c in candidates):
            limitations.append('CONFLICTING_BOXES_FOR_HELD_ORIGINAL_FRAME')
        if moving:
            target=series[0][end].source_time_ms-parameters['duration_ms']
            # Choose an actually observed anchor at or before the requested bound.
            # Retain its exact elapsed time; never interpolate a missing endpoint.
            anchors=[i for i in common if i<end and series[0][i].source_time_ms<=target]
            if not anchors:continue
            start=anchors[-1]
            limitations+=_continuity(prepared,series,start,end,cuts)
            a0,a1=series[0][start],series[0][end]
            elapsed=a1.source_time_ms-a0.source_time_ms
            if elapsed<=0:continue
            limitations.append('CAMERA_MOTION_UNCOMPENSATED')
            values.update(requested_duration_ms=parameters['duration_ms'],measured_elapsed_ms=elapsed)
            if abs(elapsed-parameters['duration_ms'])>1e-6:
                limitations.append('DURATION_ENDPOINT_NOT_EXACT')
                values.update(requested_anchor_source_ms=target,observed_anchor_source_ms=a0.source_time_ms)
            if step.predicate=='moves':
                displacement=distance(a0,a1)
                cx0,cy0=center(a0);cx1,cy1=center(a1)
                values.update(displacement_image_diagonal=displacement,
                    velocity_x_display_pixels_per_second=(cx1-cx0)*w.display_width*1000/elapsed,
                    velocity_y_display_pixels_per_second=(cy1-cy0)*w.display_height*1000/elapsed,
                    threshold=parameters['min_displacement_ratio'])
                result=displacement>=parameters['min_displacement_ratio']
            else:
                b0,b1=series[1][start],series[1][end]
                initial,final=distance(a0,b0),distance(a1,b1)
                change=initial-final if step.predicate=='approaches' else final-initial
                values.update(initial_distance_image_diagonal=initial,final_distance_image_diagonal=final,
                    distance_change_image_diagonal=change,threshold=parameters['min_distance_change_ratio'])
                result=change>=parameters['min_distance_change_ratio']
                ax0,ay0=center(a0);ax1,ay1=center(a1);bx0,by0=center(b0);bx1,by1=center(b1)
                av=((ax1-ax0)*w.display_width/diagonal,(ay1-ay0)*w.display_height/diagonal)
                bv=((bx1-bx0)*w.display_width/diagonal,(by1-by0)*w.display_height/diagonal)
                if math.hypot(*av)>.02 and math.hypot(av[0]-bv[0],av[1]-bv[1])<.01:
                    limitations.append('CAMERA_OR_SHARED_MOTION_AMBIGUOUS')
        else:
            a=series[0][end];cx,cy=center(a)
            values.update(center_x=cx,center_y=cy,width=a.width,height=a.height,area=a.width*a.height)
            if step.predicate=='inside':
                zone=next((z for z in prepared.ast.zones if z.id==step.zone_id),None)
                if zone is None:raise AppError('UNKNOWN_ZONE','Geometry references an unknown image zone',422)
                inside=lambda s: zone.x<=center(s)[0]<zone.x+zone.width and zone.y<=center(s)[1]<zone.y+zone.height
                result=inside(a);values['center_inside_zone']=result
                before=[i for i in common if i<end]
                if before:
                    previous=before[-1]
                    reasons=_continuity(prepared,series,previous,end,cuts)
                    if not reasons and inside(series[0][previous])!=result:
                        values['sampled_zone_transition']='entry' if result else 'exit'
                    elif reasons:limitations+=reasons
            elif step.predicate=='left_of':
                other_x=center(series[1][end])[0]
                values.update(other_center_x=other_x,center_x_difference=cx-other_x,threshold=0.0)
                result=cx<other_x
            elif step.predicate=='near':
                value=distance(a,series[1][end]);values.update(center_distance_image_diagonal=value,threshold=parameters['distance_ratio'])
                result=value<=parameters['distance_ratio']
            elif step.predicate=='overlap':
                value=box_iou(a,series[1][end]);values.update(box_iou=value,threshold=parameters['min_iou'])
                result=value>=parameters['min_iou'];limitations.append('OVERLAP_IS_NOT_CONTACT')
        blocking={'SCENE_CUT','MISSING_INPUT_SAMPLES','OMITTED_ORIGINAL_FRAMES','SOURCE_OBSERVATION_GAP','CONFLICTING_BOXES_FOR_HELD_ORIGINAL_FRAME'}
        if blocking.intersection(limitations):result=None
        first,last=series[0][start],series[0][end]
        identity=f'{GEOMETRY_VERSION}:{prepared.plan.program_fingerprint}:{node_hash}:{candidate_ids}:{start}:{end}:{scene_hash}'
        measurements.append(GeometryMeasurement(id=uuid5(prepared.plan.run_id,identity),step_id=step.id,predicate=step.predicate,
            source_interval=_span_interval(prepared,FrameRange(start_frame=start,stop_frame=end+1)),
            input_start_frame=start,input_end_frame=end,source_start_frame=first.source_frame_index,source_end_frame=last.source_frame_index,
            source_start_pts=first.source_pts,source_end_pts=last.source_pts,source_time_base=first.source_time_base,
            measured_elapsed_ms=last.source_time_ms-first.source_time_ms,metrics=values,comparison_result=result,
            limitations=sorted(set(limitations))))
    if moving and not measurements:unmet.append('REQUESTED_DURATION_NOT_OBSERVED')
    evidence=GeometricEvidence(run_id=prepared.plan.run_id,program_fingerprint=prepared.plan.program_fingerprint,
        source_asset_id=prepared.plan.source_asset_id,source_sha256=prepared.plan.source_sha256,
        source_timeline_sha256=prepared.plan.source_timeline_sha256,window_id=prepared.window.id,
        step_id=step.id,predicate=step.predicate,parameters=parameters,anchor=step.anchor,candidate_ids=candidate_ids,
        measurements=measurements,unmet_requirements=unmet,provenance={'geometry_version':GEOMETRY_VERSION,
            'max_source_gap_ms':MAX_GAP_MS,'scene_evidence_id':str(scenes.id) if scenes else None,
            'shared_motion_min_image_diagonal':.02,'shared_motion_max_residual_image_diagonal':.01,
            'duration_endpoint_tolerance_ms':1e-6,
            'scene_record_sha256':scene_hash,
            'scene_detector_version':scenes.detector_version if scenes else None,'scene_threshold':scenes.threshold if scenes else None,
            'scene_tools':scenes.tools if scenes else None,'source_display_width':w.display_width,'source_display_height':w.display_height,
            'diagonal_definition':'hypot(display_width, display_height); no normalized-square shortcut',
            'candidate_provenance':[assignments[e][0].provider_provenance for e in step.entities]})
    bounded_json(evidence.model_dump(mode='json'),max_bytes=4*1024*1024)
    return evidence


def derive_run_geometry(store,settings,project_id,run_id,step_id,assignments):
    """Owned-storage adapter; assignments are entity -> (batch ID, candidate ID).

    This measures scene changes locally and makes no provider calls. Candidate
    selection is still an evidence hypothesis; binding/coverage fusion is A28.
    """
    from .entity_repository import load_entity_batch
    from ..repository import reference
    from ..scenes import inspect_scenes
    if not isinstance(assignments,dict) or not 1<=len(assignments)<=8:
        raise AppError('INVALID_ENTITY_BINDINGS','Choose bounded candidate references for this predicate',422)
    prepared=None;resolved={};records=[]
    for entity,value in assignments.items():
        if not isinstance(value,(tuple,list)) or len(value)!=2:
            raise AppError('INVALID_ENTITY_BINDINGS','Each geometry entity requires a batch ID and candidate ID',422)
        batch_id,candidate_id=value
        try:candidate_id=UUID(str(candidate_id))
        except (ValueError,TypeError,AttributeError):
            raise AppError('INVALID_ENTITY_BINDINGS','Candidate reference must be an owned UUID',422) from None
        record,batch,current=load_entity_batch(store,settings,project_id,run_id,batch_id)
        if prepared is None:prepared=current
        resolved[entity]=(batch,candidate_id);records.append(reference(record).model_dump(mode='json'))
    scenes=inspect_scenes(store,settings,prepared.window)
    result=derive_geometry(prepared,step_id,resolved,scenes=scenes)
    result.provenance['entity_batch_references']=records
    return result
