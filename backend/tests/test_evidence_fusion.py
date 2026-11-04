"""Policy fixtures are explicit mock hypotheses, not tabletop/physical truth."""
from bisect import bisect_right
from copy import deepcopy
from dataclasses import replace
from uuid import uuid4

import pytest
from pydantic import TypeAdapter

from app.engine.entity_tracks import batch_fingerprint,normalize_entity_tracks,select_candidate
from app.engine.fusion import fuse_predicate_window
from app.engine.geometry import derive_geometry
from app.engine.muse_observations import normalize_response
from app.errors import AppError
from app.programs.policy import SourceInterval,UnknownPolicy
from app.programs.predicates import PredicateStep
from test_predicate_geometry import scenes
from test_step_observations import context,PROVENANCE


def fusion_fixture(context,predicate='semantic_event',side=40,gap=False):
    ast=context.ast.model_copy(deep=True)
    raw={'id':'node','predicate':predicate,'entities':['component']}
    if predicate=='semantic_event':raw['phrase']='a component rotates'
    ast.steps=[TypeAdapter(PredicateStep).validate_python(raw)]
    ctx=replace(context,ast=ast)
    indices=list(range(len(ctx.window.source_frame_indices)))
    if gap:indices=[0,1,6,7]
    text='\n'.join(f'<{i}f>1<|box;x1=20;y1=70;x2={19+side};y2={69+side};w=320;h=180|><|mask;x=0;y=0;data=320,180,~fixture|>' for i in indices)
    batch=normalize_entity_tracks(ctx,'component',text,{'provider':'test_fixture','response_id':'fusion-candidate'})
    candidate=batch.candidates[0];segment=candidate.segments[0]
    frame=ctx.source.frame(0,source_frame_index=ctx.window.source_frame_indices[0])
    binding=select_candidate(ctx,batch,{'batch_fingerprint':batch_fingerprint(batch),'candidate_id':candidate.id,
        'segment_id':segment.id,'evidence_frame_id':frame.evidence_frame_id,'input_frame_index':0})
    representatives=[];seen=set()
    for i,k in enumerate(ctx.window.source_frame_indices):
        if k not in seen:representatives.append(i);seen.add(k)
    references=representatives[:8]
    output={'schema_version':'predicate-observations-1','window_id':str(ctx.window.id),
        'entities':[{'entity_id':'component','role':'object','description':'Labeled mock entity appearance',
            'visibility':'clear','identity':'unambiguous','evidence_frame_indices':references,'uncertainty':[]}],
        'hypotheses':[{'step_id':'node','entity_ids':['component'],'assessment':'supported',
            'interval':{'start_frame':0,'stop_frame':len(ctx.window.source_frame_indices)},
            'evidence_frame_indices':[references[0],references[-1]],'description':'Explicit mock visual support, not an actual rotation detection',
            'quality':'high','uncertainty':[],'boundary_uncertainty_ms':50}],
        'coverage':[{'step_id':'node','attempted_frames':{'start_frame':0,'stop_frame':len(ctx.window.source_frame_indices)},
            'observed_frame_ranges':[{'start_frame':0,'stop_frame':len(ctx.window.source_frame_indices)}],
            'complete_inspection_claim':True,'visibility':'clear','identity':'unambiguous','resolution':'adequate','uncertainty':[]}],
        'notes':['Hand-authored fusion QA fixture; not recognition evidence']}
    return ctx,{'component':(batch,binding)},output


def fuse(ctx,tracks,raw,**kwargs):
    return fuse_predicate_window(ctx,'node',ctx.planned_window.source_interval,
        normalize_response(ctx,raw,PROVENANCE) if raw is not None else None,tracks,scenes=scenes(ctx),**kwargs)


def test_source_grounded_high_quality_semantic_support_is_reviewable_not_physical_certification(context):
    ctx,tracks,raw=fusion_fixture(context)
    result=fuse(ctx,tracks,raw)
    assert result.verdict=='true' and result.supports
    assert result.needs_review and result.interpretation=='bounded_visual_evidence_only'
    assert result.coverage.sufficient_negative is False
    assert 'physical non-occurrence' in result.provenance['limitations']


@pytest.mark.parametrize('case',['occluded','not_visible','not_observed','identity','quality','uncertain','resolution','small','binding','gap','missing','scan_claim_only'])
def test_missing_or_ambiguous_evidence_stays_unknown_even_with_complete_scan_claim(context,case):
    ctx,tracks,raw=fusion_fixture(context,side=8 if case=='small' else 40,gap=case=='gap')
    if case in ('occluded','not_visible'):raw['entities'][0]['visibility']=case;raw['coverage'][0]['visibility']=case
    elif case=='not_observed':raw['hypotheses'][0].update(assessment='not_observed',interval=None,evidence_frame_indices=[],uncertainty=['Tool step cannot be seen'])
    elif case=='identity':raw['entities'][0]['identity']='ambiguous'
    elif case=='quality':raw['hypotheses'][0]['quality']='low'
    elif case=='uncertain':raw['hypotheses'][0].update(assessment='uncertain',uncertainty=['Tool detail is unclear'])
    elif case=='resolution':raw['coverage'][0]['resolution']='insufficient'
    elif case=='binding':tracks={}
    elif case=='missing':raw=None
    elif case=='scan_claim_only':raw['hypotheses']=[]
    result=fuse(ctx,tracks,raw)
    assert result.verdict=='unknown' and not result.supports and not result.coverage.sufficient_negative
    assert result.needs_review


def test_allow_estimated_preserves_candidate_but_never_promotes_unsupported_truth(context):
    ctx,tracks,raw=fusion_fixture(context)
    raw['hypotheses'][0].update(assessment='uncertain',uncertainty=['Boundary or action semantics are ambiguous'])
    assert fuse(ctx,tracks,raw).estimated_candidates==[]
    ctx.ast.unknown_policy=UnknownPolicy(observation='allow_estimated')
    result=fuse(ctx,tracks,raw)
    assert result.verdict=='unknown' and result.estimated_candidates and not result.supports


def test_contradictory_provider_hypotheses_cannot_become_a_match_or_negative(context):
    ctx,tracks,raw=fusion_fixture(context)
    other=deepcopy(raw['hypotheses'][0]);other['assessment']='disconfirmed';raw['hypotheses'].append(other)
    result=fuse(ctx,tracks,raw)
    assert result.verdict=='unknown' and 'CONTRADICTORY_OBSERVATIONS' in result.reasons


def test_a_dense_bounded_geometric_disconfirmation_can_be_false_without_certifying_absence(context):
    ctx,tracks,raw=fusion_fixture(context,'present')
    # Switch to a strict left-of predicate with a second independently selected
    # entity, both at the same x. Equality gives explicit disconfirming geometry.
    ast=ctx.ast.model_copy(deep=True)
    ast.steps=[TypeAdapter(PredicateStep).validate_python({'id':'node','predicate':'left_of','entities':['component','tool']})]
    ctx=replace(ctx,ast=ast)
    other_raw=deepcopy(raw['entities'][0]);other_raw.update(entity_id='tool',role='instrument')
    raw['entities'].append(other_raw);raw['hypotheses']=[]
    wire='\n'.join(f'<{i}f>1<|box;x1=20;y1=70;x2=59;y2=109;w=320;h=180|><|mask;x=0;y=0;data=320,180,~fixture|>' for i in range(len(ctx.window.source_frame_indices)))
    other=normalize_entity_tracks(ctx,'tool',wire,{'provider':'test_fixture','response_id':'fusion-tool'})
    c=other.candidates[0];s=c.segments[0];frame=ctx.source.frame(0,source_frame_index=ctx.window.source_frame_indices[0])
    binding=select_candidate(ctx,other,{'batch_fingerprint':batch_fingerprint(other),'candidate_id':c.id,'segment_id':s.id,
        'evidence_frame_id':frame.evidence_frame_id,'input_frame_index':0})
    tracks['tool']=(other,binding)
    # Reuse the exact scene record, including its provenance/ID.
    scene=scenes(ctx)
    geometry=derive_geometry(ctx,'node',{e:(b,v.candidate_id) for e,(b,v) in tracks.items()},scenes=scene)
    first=ctx.window.source_frame_indices[0]
    end=ctx.source.timeline.source_frame_times_ms[bisect_right(ctx.source.timeline.source_frame_times_ms,ctx.source.timeline.source_frame_times_ms[first])]
    window=SourceInterval(start_ms=300,end_ms=int(end))
    observations=normalize_response(ctx,raw,PROVENANCE)
    result=fuse_predicate_window(ctx,'node',window,observations,tracks,geometry=geometry,scenes=scene)
    assert result.verdict=='false' and result.coverage.sufficient_negative
    assert result.coverage.dense_source_sampling and result.coverage.identity_complete
    assert not result.supports and result.interpretation=='bounded_visual_evidence_only'
    assert result.disconfirming_geometry_ids and result.geometry_ids
    # Whole-window claims cannot override occlusion, missing frame appearances
    # or ranges that were not actually claimed observed.
    for case in ('occluded','sparse_appearances','unobserved_ranges'):
        incomplete=deepcopy(raw)
        if case=='occluded':incomplete['entities'][0]['visibility']='occluded'
        elif case=='sparse_appearances':incomplete['entities'][0]['evidence_frame_indices']=[len(ctx.window.source_frame_indices)-1]
        else:incomplete['coverage'][0]['observed_frame_ranges']=[]
        denied=fuse_predicate_window(ctx,'node',window,normalize_response(ctx,incomplete,PROVENANCE),tracks,geometry=geometry,scenes=scene)
        assert denied.verdict=='unknown' and not denied.coverage.sufficient_negative
    missing=fuse_predicate_window(ctx,'node',window,observations,{},geometry=geometry,scenes=scene)
    assert missing.verdict=='unknown' and not missing.coverage.sufficient_negative
    # A mutated comparison must not masquerade as a computed measurement.
    geometry.measurements[0].comparison_result=True
    with pytest.raises(AppError) as exc:fuse_predicate_window(ctx,'node',window,observations,tracks,geometry=geometry,scenes=scene)
    assert exc.value.code=='EVIDENCE_MISMATCH'


def test_sparse_semantic_disconfirmation_is_unknown_and_scene_cuts_break_support(context):
    ctx,tracks,raw=fusion_fixture(context)
    raw['hypotheses'][0]['assessment']='disconfirmed'
    result=fuse(ctx,tracks,raw)
    assert result.verdict=='unknown' and not result.coverage.sufficient_negative
    raw['hypotheses'][0]['assessment']='supported'
    result=fuse_predicate_window(ctx,'node',ctx.planned_window.source_interval,normalize_response(ctx,raw,PROVENANCE),tracks,scenes=scenes(ctx,[8]))
    assert result.verdict=='unknown' and 'OBSERVATION_CONTINUITY_UNVERIFIED_OR_CUT' in result.reasons


def test_foreign_or_modified_observations_fail_instead_of_becoming_unknown(context):
    ctx,tracks,raw=fusion_fixture(context)
    observations=normalize_response(ctx,raw,PROVENANCE)
    observations.run_id=uuid4()
    with pytest.raises(AppError) as exc:fuse_predicate_window(ctx,'node',ctx.planned_window.source_interval,observations,tracks,scenes=scenes(ctx))
    assert exc.value.code=='OWNERSHIP'
    observations=normalize_response(ctx,raw,PROVENANCE)
    observations.observations[0].description='Modified after normalization'
    with pytest.raises(AppError) as exc:fuse_predicate_window(ctx,'node',ctx.planned_window.source_interval,observations,tracks,scenes=scenes(ctx))
    assert exc.value.code=='EVIDENCE_MISMATCH'


def contact_fixture(context,tool_x=20):
    ctx,tracks,raw=fusion_fixture(context)
    ctx.ast.steps=[TypeAdapter(PredicateStep).validate_python({'id':'node','predicate':'touches','entities':['component','tool']})]
    appearance=deepcopy(raw['entities'][0]);appearance.update(entity_id='tool',role='instrument');raw['entities'].append(appearance)
    raw['hypotheses'][0]['entity_ids']=['component','tool']
    wire='\n'.join(f'<{i}f>1<|box;x1={tool_x};y1=70;x2={tool_x+39};y2=109;w=320;h=180|><|mask;x=0;y=0;data=320,180,~fixture|>' for i in range(len(ctx.window.source_frame_indices)))
    batch=normalize_entity_tracks(ctx,'tool',wire,{'provider':'test_fixture','response_id':'mock-contact-tool'})
    candidate=batch.candidates[0];segment=candidate.segments[0]
    frame=ctx.source.frame(0,source_frame_index=ctx.window.source_frame_indices[0])
    binding=select_candidate(ctx,batch,{'batch_fingerprint':batch_fingerprint(batch),'candidate_id':candidate.id,
        'segment_id':segment.id,'evidence_frame_id':frame.evidence_frame_id,'input_frame_index':0})
    tracks['tool']=(batch,binding)
    return ctx,tracks,raw


def test_contact_requires_semantic_support_and_compatible_boxes_never_overlap_alone(context):
    ctx,tracks,raw=contact_fixture(context)
    assert fuse(ctx,tracks,raw).verdict=='true'  # Explicit high-quality mock semantic evidence.
    raw['hypotheses']=[]
    assert fuse(ctx,tracks,raw).verdict=='unknown'  # Identical overlapping boxes alone are insufficient.
    ctx,tracks,raw=contact_fixture(context,tool_x=220)
    assert fuse(ctx,tracks,raw).verdict=='unknown'  # Distant tracks veto positive contact.


def test_dense_visible_semantic_disconfirmation_can_be_false_inside_its_exact_window(context):
    ctx,tracks,raw=contact_fixture(context,tool_x=220)
    original=ctx.window.source_frame_indices[0]
    stop=next(i for i,k in enumerate(ctx.window.source_frame_indices) if k!=original)
    raw['hypotheses'][0].update(assessment='disconfirmed',interval={'start_frame':0,'stop_frame':stop},evidence_frame_indices=[0])
    end=ctx.source.timeline.source_frame_times_ms[bisect_right(ctx.source.timeline.source_frame_times_ms,ctx.source.timeline.source_frame_times_ms[original])]
    result=fuse_predicate_window(ctx,'node',SourceInterval(start_ms=300,end_ms=int(end)),
        normalize_response(ctx,raw,PROVENANCE),tracks,scenes=scenes(ctx))
    assert result.verdict=='false' and result.coverage.sufficient_negative
    assert result.disconfirming_observation_ids and result.interpretation=='bounded_visual_evidence_only'


def test_selection_identity_cannot_cross_a_cut_even_when_support_is_entirely_after_it(context):
    ctx,tracks,raw=fusion_fixture(context)
    count=len(ctx.window.source_frame_indices)
    raw['hypotheses'][0].update(interval={'start_frame':10,'stop_frame':count},evidence_frame_indices=[10,count-1])
    raw['entities'][0]['evidence_frame_indices']=[10,count-1]
    source_scene=scenes(ctx)
    observations=normalize_response(ctx,raw,PROVENANCE)
    qualified=fuse_predicate_window(ctx,'node',ctx.planned_window.source_interval,observations,tracks,scenes=source_scene)
    assert qualified.verdict=='true'
    result=fuse_predicate_window(ctx,'node',ctx.planned_window.source_interval,observations,tracks,scenes=scenes(ctx,[8]))
    assert result.verdict=='unknown' and not result.coverage.identity_complete
    assert 'SELECTED_IDENTITY_CONTINUITY_UNVERIFIED' in result.reasons
