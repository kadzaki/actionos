"""A28 bounded visual-evidence policy; no physical absence or process certification."""
from bisect import bisect_left,bisect_right
import hashlib
import math
from typing import Literal
from uuid import UUID

from pydantic import Field

from .entity_tracks import _owned_batch,BindingSelectionRequest,select_candidate
from .geometry import _scene_frames,derive_geometry
from .muse_observations import normalize_response
from ..errors import AppError
from ..models import Model
from ..programs.policy import SourceInterval,EVIDENCE_POLICY_VERSION
from ..repository import encoded
from ..validation import bounded_json

FUSION_VERSION=EVIDENCE_POLICY_VERSION
MIN_SIDE_PIXELS={'geometry':8,'semantic':16,'attribute':24}
CONTACT_COMPATIBILITY_GAP=.03  # Image diagonal; a veto, never positive contact proof.


def require_evidence_policy(snapshot):
    if snapshot.evidence_policy_version!=FUSION_VERSION:
        raise AppError('EVIDENCE_POLICY_MISMATCH','This run does not pin the current evidence policy; create a new run before fusion',409)


class CoverageAudit(Model):
    window: SourceInterval
    required_original_frames: int
    required_frame_indices_sha256: str
    represented_original_frames: int
    dense_source_sampling: bool
    identity_complete: bool
    visibility_complete: bool
    resolution_adequate: bool
    scene_continuity_measured: bool
    sufficient_negative: bool
    reasons: list[str]


class PredicateSupport(Model):
    interval: SourceInterval
    observation_ids: list[UUID] = Field(default_factory=list,max_length=256)
    geometry_ids: list[UUID] = Field(default_factory=list,max_length=512)
    estimated_boundaries: bool = True


class FusedPredicateEvidence(Model):
    policy_version: Literal['predicate-evidence-policy-1'] = FUSION_VERSION
    run_id: UUID
    program_fingerprint: str
    source_asset_id: UUID
    source_sha256: str
    source_timeline_sha256: str
    window_id: UUID
    step_id: str
    predicate: str
    verdict: Literal['true','false','unknown']
    interpretation: Literal['bounded_visual_evidence_only'] = 'bounded_visual_evidence_only'
    needs_review: bool
    supports: list[PredicateSupport] = Field(max_length=512)
    estimated_candidates: list[PredicateSupport] = Field(max_length=768)
    observation_ids: list[UUID]
    geometry_ids: list[UUID]
    disconfirming_observation_ids: list[UUID]
    disconfirming_geometry_ids: list[UUID]
    coverage: CoverageAudit
    reasons: list[str]
    provenance: dict


def _inside(inner,outer):
    return outer.start_ms<=inner.start_ms<inner.end_ms<=outer.end_ms


def _covers(intervals,window):
    at=window.start_ms
    for interval in sorted(intervals,key=lambda i:(i.start_ms,i.end_ms)):
        if interval.end_ms<=at:continue
        if interval.start_ms>at:return False
        at=max(at,interval.end_ms)
    return at>=window.end_ms


def fuse_predicate_window(prepared,step_id,window,observations,tracks,*,geometry=None,scenes=None):
    """Fuse validated A25/A26/A27 records inside one declared source window.

    tracks maps entity ID -> (owned EntityTrackBatch, SelectedEntityBinding).
    Callers must load batch artifacts with entity_repository, never client JSON.
    No scan-complete/negative-verified flag from a provider is trusted here.
    """
    window=SourceInterval.model_validate(window)
    if not _inside(window,prepared.planned_window.source_interval):
        raise AppError('INVALID_PREDICATE_WINDOW','Declare a bounded predicate interval inside this owned source window',422)
    step=next((s for s in prepared.ast.steps if s.id==step_id),None)
    if step is None:raise AppError('UNKNOWN_STEP','Unknown fusion predicate node',422)
    if set(tracks)-set(step.entities):raise AppError('INVALID_ENTITY_BINDINGS','Fusion received foreign predicate entities',422)
    times=prepared.source.timeline.source_frame_times_ms
    first=max(0,bisect_right(times,window.start_ms)-1);stop=bisect_left(times,window.end_ms)
    required=set(range(first,stop));input_map=prepared.window.source_frame_indices
    represented=required.intersection(input_map)
    relevant_input={i for i,k in enumerate(input_map) if k in required}
    dense=represented==required and bool(required)
    reasons=[];bound={};binding_provenance={}
    semantic=step.predicate in ('semantic_event','touches','holds','releases')
    family='semantic' if semantic else ('attribute' if step.predicate=='matches_attribute' else 'geometry')
    if prepared.planned_window.unrepresented_tail_ms and window.end_ms>prepared.planned_window.working_interval.end_ms:
        dense=False;reasons.append('UNREPRESENTED_SOURCE_TAIL')
    if not dense:reasons.append('SOURCE_SAMPLING_INCOMPLETE')
    if len({times[i] for i in required})!=len(required):reasons.append('AMBIGUOUS_DUPLICATE_SOURCE_PTS')
    for entity in step.entities:
        if entity not in tracks:
            reasons.append('MISSING_ENTITY_BINDING:'+entity);continue
        batch,binding=tracks[entity];_owned_batch(prepared,batch)
        if batch.entity_id!=entity:raise AppError('OWNERSHIP','Selected candidate belongs to another entity',422)
        request=BindingSelectionRequest(batch_fingerprint=binding.batch_fingerprint,candidate_id=binding.candidate_id,
            segment_id=binding.segment_id,evidence_frame_id=binding.evidence_frame_id,input_frame_index=binding.input_frame_index)
        if select_candidate(prepared,batch,request)!=binding:
            raise AppError('EVIDENCE_MISMATCH','Binding differs from its canonical source-frame selection',409)
        candidate=next(c for c in batch.candidates if c.id==binding.candidate_id)
        segment=next(s for s in candidate.segments if s.id==binding.segment_id)
        samples={s.input_frame_index:s for s in candidate.samples if segment.input_frames.start_frame<=s.input_frame_index<segment.input_frames.stop_frame}
        if 'CONFLICTING_BOXES_FOR_HELD_ORIGINAL_FRAME' in candidate.uncertainty:
            reasons.append('CONFLICTING_TRACK_BOXES:'+entity)
        bound[entity]=(samples,segment)
        binding_provenance[entity]=binding.model_dump(mode='json')
    identity_complete=len(bound)==len(step.entities) and all(relevant_input<=set(s) for s,_ in bound.values())
    if not identity_complete:reasons.append('IDENTITY_OR_TRACK_COVERAGE_INCOMPLETE')
    entity_appearance={};claim=None;node_observations=[]
    if observations is None:reasons.append('MISSING_MUSE_EVIDENCE')
    else:
        if (observations.run_id!=prepared.plan.run_id or observations.program_fingerprint!=prepared.plan.program_fingerprint
                or observations.window_id!=prepared.window.id):
            raise AppError('OWNERSHIP','Muse evidence belongs to another source/run/window',422)
        # Revalidate raw node/frame/role mappings and immutable image identities.
        canonical=normalize_response(prepared,observations.raw_provider_response,observations.provider_provenance)
        if encoded(canonical.model_dump(mode='json'))!=encoded(observations.model_dump(mode='json')):
            raise AppError('EVIDENCE_MISMATCH','Muse observations differ from their source-grounded raw recording',409)
        entity_appearance={e.entity_id:e for e in observations.entities}
        entry=next((c for c in observations.coverage if c.step_id==step_id),None)
        claim=entry.provider_claim if entry else None
        if claim and claim.visibility in ('occluded','not_visible'):reasons.append('PREDICATE_OCCLUDED_OR_NOT_VISIBLE')
        node_observations=[o for o in observations.observations if o.step_id==step_id
            and (o.source_interval is None or max(o.source_interval.start_ms,window.start_ms)<min(o.source_interval.end_ms,window.end_ms))]
        for e in observations.entities:
            if e.entity_id not in step.entities:continue
            if e.visibility in ('occluded','not_visible'):reasons.append('ENTITY_NOT_CLEARLY_VISIBLE:'+e.entity_id)
            if e.identity!='unambiguous':reasons.append('ENTITY_IDENTITY_AMBIGUOUS:'+e.entity_id)
        for o in node_observations:
            if o.quality!='high':reasons.append('OBSERVATION_QUALITY_INSUFFICIENT')
            if o.uncertainty:reasons.append('OBSERVATION_UNCERTAIN')
            if 'CONTRADICTORY_PROVIDER_HYPOTHESES' in o.uncertainty:reasons.append('CONTRADICTORY_OBSERVATIONS')
    def appearance_frames(entity):
        e=entity_appearance.get(entity)
        if not e or e.visibility!='clear' or e.identity!='unambiguous' or e.uncertainty:return set()
        return {input_map[i] for i in e.evidence_frame_indices}
    visibility_complete=bool(claim and claim.visibility=='clear' and claim.identity=='unambiguous'
        and not claim.uncertainty and claim.complete_inspection_claim
        and all(required<=appearance_frames(e) for e in step.entities))
    if not visibility_complete:reasons.append('PREDICATE_VISIBILITY_OR_IDENTITY_UNVERIFIED')
    def adequate(sample):
        return min(sample.width*prepared.window.width,sample.height*prepared.window.height)>=MIN_SIDE_PIXELS[family]
    resolution_complete=bool(bound) and len(bound)==len(step.entities) and all(
        relevant_input<=set(samples) and all(adequate(samples[i]) for i in relevant_input) for samples,_ in bound.values())
    if claim and claim.resolution!='adequate':resolution_complete=False
    if not resolution_complete:reasons.append('VISUAL_RESOLUTION_INSUFFICIENT_OR_UNVERIFIED')
    cuts=_scene_frames(prepared,scenes)
    def linked_to_selection(entity,index):
        anchor=binding_provenance[entity]['input_frame_index']
        lo,hi=sorted((anchor,index))
        return cuts is not None and not any(lo<cut<=hi for cut in cuts)
    if identity_complete and not all(linked_to_selection(e,i) for e in bound for i in relevant_input):
        identity_complete=False;reasons.append('SELECTED_IDENTITY_CONTINUITY_UNVERIFIED')
    continuous=cuts is not None and not any(i in relevant_input for i in cuts if i>min(relevant_input,default=0))
    if not continuous:reasons.append('SCENE_CONTINUITY_UNVERIFIED_OR_CUT')
    if claim:
        claimed={i for span in claim.observed_frame_ranges for i in range(span.start_frame,span.stop_frame)}
        if not relevant_input<=claimed:reasons.append('PROVIDER_OBSERVED_RANGES_INCOMPLETE')
    else:reasons.append('MISSING_PREDICATE_COVERAGE_CLAIM')
    minimum=2 if semantic and step.predicate!='touches' else 1
    def grounded(indices,interval):
        if len(indices)<minimum:return False
        for entity in step.entities:
            if entity not in bound or not indices<=appearance_frames(entity):return False
            samples,segment=bound[entity]
            if not _inside(interval,segment.source_interval):return False
            for original in indices:
                matching=[s for s in samples.values() if s.source_frame_index==original]
                if not matching or not all(adequate(s) and linked_to_selection(entity,s.input_frame_index) for s in matching):return False
        return not any(r.startswith('CONFLICTING_TRACK_BOXES:') for r in reasons)
    def compatible_contact(indices):
        if step.predicate not in ('touches','holds','releases'):return True
        a,b=(bound[e][0] for e in step.entities)
        for original in indices:
            left=next((s for s in a.values() if s.source_frame_index==original),None)
            right=next((s for s in b.values() if s.source_frame_index==original),None)
            if left is None or right is None:return False
            dx=max(0,left.x-right.x-right.width,right.x-left.x-left.width)*prepared.window.width
            dy=max(0,left.y-right.y-right.height,right.y-left.y-left.height)*prepared.window.height
            if math.hypot(dx,dy)/math.hypot(prepared.window.width,prepared.window.height)>CONTACT_COMPATIBILITY_GAP:return False
        return True
    supports=[];estimated=[];negative_frames=set();negative_intervals=[];negative_obs=[];negative_geometry=[];geometry_ids=[]
    for observation in node_observations:
        if not observation.source_interval or not _inside(observation.source_interval,window):continue
        indices={f.source_frame_index for f in observation.frames}
        support=PredicateSupport(interval=observation.source_interval,observation_ids=[observation.id])
        good=(observation.quality=='high' and not observation.uncertainty and grounded(indices,observation.source_interval)
            and (observation.hypothesis!='supported' or compatible_contact(indices))
            and not (claim and (claim.visibility in ('occluded','not_visible') or claim.resolution=='insufficient')))
        if cuts is None or any(observation.source_interval.start_ms<prepared.window.source_frame_times_ms[i]<observation.source_interval.end_ms for i in cuts):
            good=False;reasons.append('OBSERVATION_CONTINUITY_UNVERIFIED_OR_CUT')
        if observation.hypothesis=='supported' and good and step.predicate not in ('left_of','inside','near','overlap','moves','approaches','separates'):
            supports.append(support)
        elif observation.hypothesis=='disconfirmed' and good:
            negative_frames.update(indices);negative_intervals.append(observation.source_interval)
            negative_obs.append(observation.id)
        elif observation.hypothesis in ('supported','uncertain') and prepared.ast.unknown_policy.observation=='allow_estimated':
            estimated.append(support)
    if geometry is not None and (geometry.run_id!=prepared.plan.run_id or geometry.program_fingerprint!=prepared.plan.program_fingerprint
            or geometry.source_asset_id!=prepared.plan.source_asset_id or geometry.source_sha256!=prepared.plan.source_sha256
            or geometry.window_id!=prepared.window.id or geometry.step_id!=step.id or geometry.predicate!=step.predicate):
        raise AppError('OWNERSHIP','Geometry belongs to another source/run/predicate',422)
    if geometry is not None and len(bound)!=len(step.entities):
        reasons.append('GEOMETRY_REQUIRES_COMPLETE_BINDINGS')
        geometry=None
    if geometry is not None:
        if (geometry.run_id!=prepared.plan.run_id or geometry.program_fingerprint!=prepared.plan.program_fingerprint
                or geometry.source_asset_id!=prepared.plan.source_asset_id or geometry.source_sha256!=prepared.plan.source_sha256
                or geometry.window_id!=prepared.window.id or geometry.step_id!=step.id or geometry.predicate!=step.predicate
                or geometry.parameters!=step.parameters.model_dump(mode='json') or geometry.anchor!=step.anchor
                or geometry.candidate_ids!={e:str(tracks[e][1].candidate_id) for e in step.entities if e in tracks}):
            raise AppError('EVIDENCE_MISMATCH','Geometric measurements differ from this exact predicate/binding',409)
        expected=derive_geometry(prepared,step_id,{e:(tracks[e][0],tracks[e][1].candidate_id) for e in step.entities},scenes=scenes)
        actual=geometry.model_dump(mode='json');actual['provenance'].pop('entity_batch_references',None)
        if encoded(actual)!=encoded(expected.model_dump(mode='json')):
            raise AppError('EVIDENCE_MISMATCH','Geometric comparisons differ from the actual candidate samples and scenes',409)
        for measurement in geometry.measurements:
            if not _inside(measurement.source_interval,window):continue
            geometry_ids.append(measurement.id)
            indices={prepared.window.source_frame_indices[i] for i in range(measurement.input_start_frame,measurement.input_end_frame+1)}
            blockers={'SCENE_CUT','MISSING_INPUT_SAMPLES','OMITTED_ORIGINAL_FRAMES','SOURCE_OBSERVATION_GAP',
                'CONFLICTING_BOXES_FOR_HELD_ORIGINAL_FRAME','SCENE_CUTS_NOT_MEASURED','DURATION_ENDPOINT_NOT_EXACT','CAMERA_OR_SHARED_MOTION_AMBIGUOUS'}
            endpoints={measurement.source_start_frame,measurement.source_end_frame}
            good=not blockers.intersection(measurement.limitations) and grounded(endpoints,measurement.source_interval)
            if any((o.quality!='high' or o.uncertainty) and o.source_interval
                    and max(o.source_interval.start_ms,measurement.source_interval.start_ms)<min(o.source_interval.end_ms,measurement.source_interval.end_ms)
                    for o in node_observations):good=False
            opposed='disconfirmed' if measurement.comparison_result is True else 'supported'
            if any(o.hypothesis==opposed and o.quality=='high' and o.source_interval
                    and max(o.source_interval.start_ms,measurement.source_interval.start_ms)<min(o.source_interval.end_ms,measurement.source_interval.end_ms)
                    and indices.intersection(f.source_frame_index for f in o.frames) for o in node_observations):
                good=False;reasons.append('CONFLICTING_OBSERVATION_AND_GEOMETRY')
            if measurement.comparison_result is True and good:
                supports.append(PredicateSupport(interval=measurement.source_interval,geometry_ids=[measurement.id],estimated_boundaries=False))
            elif measurement.comparison_result is False and good:
                negative_frames.add(measurement.source_end_frame);negative_intervals.append(measurement.source_interval)
                negative_geometry.append(measurement.id)
            elif measurement.comparison_result is True and prepared.ast.unknown_policy.observation=='allow_estimated':
                estimated.append(PredicateSupport(interval=measurement.source_interval,geometry_ids=[measurement.id]))
    # A false verdict requires actual disconfirming evidence for all required
    # original samples, not a provider's whole-window coverage assertion.
    enough_negative=(not supports and dense and identity_complete and visibility_complete and resolution_complete and continuous
        and not any(r in reasons for r in ('PROVIDER_OBSERVED_RANGES_INCOMPLETE','AMBIGUOUS_DUPLICATE_SOURCE_PTS','UNREPRESENTED_SOURCE_TAIL',
            'CONTRADICTORY_OBSERVATIONS','CONFLICTING_OBSERVATION_AND_GEOMETRY'))
        and required<=negative_frames and _covers(negative_intervals,window))
    if supports:verdict='true'
    elif enough_negative:verdict='false'
    else:verdict='unknown';reasons.append('NO_SUFFICIENT_SUPPORTED_OR_DISCONFIRMING_EVIDENCE')
    coverage=CoverageAudit(window=window,required_original_frames=len(required),required_frame_indices_sha256=hashlib.sha256(encoded(sorted(required))).hexdigest(),
        represented_original_frames=len(represented),dense_source_sampling=dense,identity_complete=identity_complete,
        visibility_complete=visibility_complete,resolution_adequate=resolution_complete,scene_continuity_measured=continuous,
        sufficient_negative=bool(enough_negative),reasons=sorted(set(reasons)))
    result=FusedPredicateEvidence(run_id=prepared.plan.run_id,program_fingerprint=prepared.plan.program_fingerprint,
        source_asset_id=prepared.plan.source_asset_id,source_sha256=prepared.plan.source_sha256,
        source_timeline_sha256=prepared.plan.source_timeline_sha256,window_id=prepared.window.id,
        step_id=step.id,predicate=step.predicate,verdict=verdict,needs_review=verdict=='unknown' or any(s.estimated_boundaries for s in supports),
        supports=supports,estimated_candidates=estimated,observation_ids=[o.id for o in node_observations],coverage=coverage,
        geometry_ids=geometry_ids,disconfirming_observation_ids=negative_obs,disconfirming_geometry_ids=negative_geometry,
        reasons=sorted(set(reasons)),provenance={'policy_version':FUSION_VERSION,'minimum_bbox_side_pixels':MIN_SIDE_PIXELS,
            'plan_fingerprint':prepared.plan.request_fingerprint,'input_sha256':prepared.window.sha256,
            'contact_compatibility_max_image_diagonal':CONTACT_COMPATIBILITY_GAP,'bindings':binding_provenance,
            'unknown_policy':prepared.ast.unknown_policy.model_dump(mode='json'),
            'limitations':'Visual evidence under this declared recording window; no physical non-occurrence, defect or causal certification'})
    bounded_json(result.model_dump(mode='json'),max_bytes=4*1024*1024)
    return result
