"""Exact-source mapping and deterministic geometry; no model action recognition."""
from dataclasses import replace
import math
from uuid import uuid4

import pytest
from pydantic import TypeAdapter

from app.engine.entity_tracks import normalize_entity_tracks
from app.engine.geometry import derive_geometry
from app.errors import AppError
from app.programs.ast import Zone
from app.programs.predicates import PredicateStep
from app.scenes import SceneEvidence
from test_step_observations import context


def step_context(context,predicate,entities=('component',),parameters=None,zone=False):
    raw={'id':'geometry','predicate':predicate,'entities':list(entities)}
    if parameters is not None:raw['parameters']=parameters
    if zone:raw['zone_id']='left'
    ast=context.ast.model_copy(deep=True)
    ast.steps=[TypeAdapter(PredicateStep).validate_python(raw)]
    if zone:ast.zones=[Zone(id='left',description='Image left half',x=0.,y=0.,width=.5,height=1.)]
    return replace(context,ast=ast)


def track(context,entity,x,indices=None):
    lines=[]
    if indices is None:indices=range(len(context.window.source_frame_indices))
    for i in indices:
        original=context.window.source_frame_indices[i]
        left=x(original) if callable(x) else x
        lines.append(f'<{i}f>1<|box;x1={left};y1=70;x2={left+39};y2=109;w=320;h=180|><|mask;x=0;y=0;data=320,180,~fixture|>')
    batch=normalize_entity_tracks(context,entity,'\n'.join(lines),{'provider':'test_fixture','response_id':f'geometry-{entity}'})
    return batch,batch.candidates[0].id


def scenes(context,cuts=()):
    return SceneEvidence(project_id=context.window.project_id,source_asset_id=context.window.source_asset_id,
        window_id=context.window.id,video_sha256=context.window.sha256,tools={'ffmpeg':'fixture-only'},origin='test_fixture',
        cuts=[{'source_time_ms':round(context.window.source_frame_times_ms[i]),'source_frame_index':context.window.source_frame_indices[i],
            'window_frame_index':i,'window_time_ms':round(i*1000/context.window.fps)} for i in cuts])


def test_near_uses_actual_display_diagonal_not_normalized_square(context):
    ctx=step_context(context,'near',('component','tool'),{'distance_ratio':.5})
    assignments={'component':track(ctx,'component',20),'tool':track(ctx,'tool',220)}
    scene=scenes(ctx)
    evidence=derive_geometry(ctx,'geometry',assignments,scenes=scene)
    reordered=derive_geometry(ctx,'geometry',dict(reversed(list(assignments.items()))),scenes=scene)
    assert reordered==evidence  # Caller dictionary order cannot change immutable evidence/provenance.
    measurement=evidence.measurements[0]
    assert measurement.metrics['center_distance_image_diagonal']==pytest.approx(200/math.hypot(320,180))
    assert measurement.comparison_result is False
    assert measurement.comparison_scope=='sample_geometry_only'
    assert not evidence.physical_measurement and not evidence.negative_coverage_verified
    assert 'verdict' not in evidence.model_fields


def test_left_of_strict_equality_and_iou_never_prove_contact(context):
    ctx=step_context(context,'left_of',('component','tool'))
    assignments={'component':track(ctx,'component',20),'tool':track(ctx,'tool',20)}
    evidence=derive_geometry(ctx,'geometry',assignments,scenes=scenes(ctx))
    assert all(m.comparison_result is False for m in evidence.measurements)
    ctx=step_context(context,'overlap',('component','tool'),{'min_iou':.8})
    overlap=derive_geometry(ctx,'geometry',assignments,scenes=scenes(ctx))
    assert overlap.measurements[0].metrics['box_iou']==1
    assert 'OVERLAP_IS_NOT_CONTACT' in overlap.measurements[0].limitations
    ctx=step_context(context,'touches',('component','tool'))
    contact=derive_geometry(ctx,'geometry',assignments,scenes=scenes(ctx))
    assert contact.measurements==[] and 'SEMANTIC_OR_VISIBILITY_SUPPORT_REQUIRED' in contact.unmet_requirements[0]


def test_zone_boundary_is_half_open_and_transition_requires_adjacent_observations(context):
    ctx=step_context(context,'inside',zone=True)
    boundary=derive_geometry(ctx,'geometry',{'component':track(ctx,'component',140)},scenes=scenes(ctx))
    assert all(m.metrics['center_x']==.5 and m.comparison_result is False for m in boundary.measurements)
    first=context.window.source_frame_indices[0]
    changing=track(ctx,'component',lambda original:120 if original==first else 140)
    evidence=derive_geometry(ctx,'geometry',{'component':changing},scenes=scenes(ctx))
    assert any(m.metrics.get('sampled_zone_transition')=='exit' for m in evidence.measurements)
    missing=track(ctx,'component',lambda original:120 if original==first else 140,indices=[0,8])
    evidence=derive_geometry(ctx,'geometry',{'component':missing},scenes=scenes(ctx))
    assert not any('sampled_zone_transition' in m.metrics for m in evidence.measurements)
    assert 'MISSING_INPUT_SAMPLES' in evidence.measurements[-1].limitations


def test_motion_uses_actual_pts_elapsed_and_preserves_original_frame_identity(context):
    ctx=step_context(context,'moves',parameters={'min_displacement_ratio':.01,'duration_ms':100})
    moving=track(ctx,'component',lambda original:20+original*3)
    evidence=derive_geometry(ctx,'geometry',{'component':moving},scenes=scenes(ctx))
    assert evidence.measurements
    for m in evidence.measurements:
        assert m.measured_elapsed_ms>=100
        assert m.source_start_pts==ctx.source.timeline.source_frame_pts[m.source_start_frame]
        assert m.source_end_pts==ctx.source.timeline.source_frame_pts[m.source_end_frame]
        assert m.measured_elapsed_ms==pytest.approx(ctx.source.timeline.source_frame_times_ms[m.source_end_frame]-ctx.source.timeline.source_frame_times_ms[m.source_start_frame])
        assert 'CAMERA_MOTION_UNCOMPENSATED' in m.limitations
    assert not evidence.camera_motion_compensated


@pytest.mark.parametrize('case',['missing','cut','unmeasured','duration'])
def test_motion_does_not_hide_gaps_scene_cuts_or_insufficient_duration(context,case):
    duration=1000 if case=='duration' else 100
    ctx=step_context(context,'moves',parameters={'min_displacement_ratio':.01,'duration_ms':duration})
    indices=[0,8,17] if case=='missing' else None
    moving=track(ctx,'component',lambda original:20+original*3,indices=indices)
    evidence=derive_geometry(ctx,'geometry',{'component':moving},scenes=None if case=='unmeasured' else scenes(ctx,[8] if case=='cut' else []))
    if case=='duration':assert not evidence.measurements and 'REQUESTED_DURATION_NOT_OBSERVED' in evidence.unmet_requirements
    elif case=='missing':assert all(m.comparison_result is None and 'MISSING_INPUT_SAMPLES' in m.limitations for m in evidence.measurements)
    elif case=='cut':assert any(m.comparison_result is None and 'SCENE_CUT' in m.limitations for m in evidence.measurements)
    else:assert all('SCENE_CUTS_NOT_MEASURED' in m.limitations for m in evidence.measurements)


def test_relative_motion_and_shared_camera_ambiguity_are_image_space_only(context):
    ctx=step_context(context,'approaches',('component','tool'),{'min_distance_change_ratio':.001,'duration_ms':100})
    moving=lambda original:20+original*8
    assignments={'component':track(ctx,'component',moving),'tool':track(ctx,'tool',lambda original:100+original*8)}
    evidence=derive_geometry(ctx,'geometry',assignments,scenes=scenes(ctx))
    assert all(m.metrics['distance_change_image_diagonal']==pytest.approx(0) for m in evidence.measurements)
    assert any('CAMERA_OR_SHARED_MOTION_AMBIGUOUS' in m.limitations for m in evidence.measurements)


def test_original_frame_omissions_and_inexact_duration_are_explicit(context):
    ctx=step_context(context,'moves',parameters={'min_displacement_ratio':.01,'duration_ms':50})
    first=ctx.window.source_frame_indices[0]
    indices=[first,first+2,first+4]
    # Explicit sparse-input mapping fixture over real original PTS, not a claim
    # that the context's dense proxy was sent to a provider with this mapping.
    window=ctx.window.model_copy(update={'source_frame_indices':indices,
        'source_frame_times_ms':[ctx.source.timeline.source_frame_times_ms[i] for i in indices]})
    ctx=replace(ctx,window=window)
    evidence=derive_geometry(ctx,'geometry',{'component':track(ctx,'component',lambda original:20+original*3)},scenes=scenes(ctx))
    assert evidence.measurements and all(m.comparison_result is None for m in evidence.measurements)
    assert all('OMITTED_ORIGINAL_FRAMES' in m.limitations for m in evidence.measurements)
    assert any('DURATION_ENDPOINT_NOT_EXACT' in m.limitations for m in evidence.measurements)


def test_bad_source_node_role_and_scene_references_fail_explicitly(context):
    ctx=step_context(context,'moves',parameters={'min_displacement_ratio':.01,'duration_ms':100})
    assignment=track(ctx,'component',20)
    with pytest.raises(AppError):derive_geometry(ctx,'foreign',{'component':assignment})
    with pytest.raises(AppError):derive_geometry(ctx,'geometry',{})
    foreign=scenes(ctx);foreign.window_id=uuid4()
    with pytest.raises(AppError) as exc:derive_geometry(ctx,'geometry',{'component':assignment},scenes=foreign)
    assert exc.value.code=='OWNERSHIP'
    batch,_=assignment;batch.candidates[0].samples[0].source_pts+=1
    with pytest.raises(AppError) as exc:derive_geometry(ctx,'geometry',{'component':assignment},scenes=scenes(ctx))
    assert exc.value.code=='EVIDENCE_MISMATCH'
