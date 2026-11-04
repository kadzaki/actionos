from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.editing import (Crop, CropPoint, EditPlan, OutputSettings, Segment, Track,
                         TrackSample, crop_at, fixed_crop, output_to_source, plan_crop, timeline)
from app.errors import AppError


def test_exact_piecewise_time_mapping_and_unsupported_operations():
    crop=fixed_crop(320,180)
    plan=EditPlan(project_id=uuid4(),source_asset_id=uuid4(),origin="manual",segments=[
        Segment(source_in_ms=200,source_out_ms=600,speed=1,crop=crop),
        Segment(source_in_ms=600,source_out_ms=1200,speed=.5,crop=crop)])
    assert timeline(plan)[-1]["output_out_ms"]==1600
    assert output_to_source(plan,400)==600
    assert output_to_source(plan,1000)==900
    assert output_to_source(plan,1600)==1200
    with pytest.raises(ValueError): output_to_source(plan,1601)
    with pytest.raises(ValidationError): Segment(source_in_ms=0,source_out_ms=300,speed=0,crop=crop)
    with pytest.raises(ValidationError): Segment(source_in_ms=0,source_out_ms=300,crop=crop,effect="arbitrary shell")
    with pytest.raises(ValidationError): OutputSettings(width=360,height=1920)
    with pytest.raises(ValidationError):
        EditPlan(**{**plan.model_dump(),"segments":[plan.segments[1],plan.segments[0]]})


def make_track(samples):
    return Track(project_id=uuid4(),source_asset_id=uuid4(),label="synthetic subject",
                 origin="synthetic_test",samples=samples)


def test_crop_clamps_edges_and_interpolates_short_timestamp_gaps():
    track=make_track([TrackSample(time_ms=t,x=x,y=.15,width=.12,height=.6)
                      for t,x in [(0,0),(200,.1),(400,.3),(600,.6),(800,.8)]])
    crop=plan_crop(track,320,180,0,800)
    assert crop.mode=="track_subject" and crop.track_id==track.id
    for t in range(801):
        x,y=crop_at(crop,t)
        assert 0<=x<=320-crop.width
        assert 0<=y<=180-crop.height
    assert crop_at(crop,100)[0]==pytest.approx((crop.points[0].x+crop.points[1].x)/2)
    assert crop_at(crop,800)[0]>crop_at(crop,0)[0]


def test_crop_refuses_long_loss_instead_of_faking_tracking():
    track=make_track([TrackSample(time_ms=0,x=.1,y=.1,width=.1,height=.6),
                      TrackSample(time_ms=1000,x=.7,y=.1,width=.1,height=.6)])
    with pytest.raises(AppError,match="too long"):
        plan_crop(track,320,180,0,1000)


def test_crop_velocity_and_acceleration_limits_for_abrupt_motion():
    track=make_track([TrackSample(time_ms=t,x=x,y=.1,width=.1,height=.6)
                      for t,x in [(0,0),(100,.8),(200,.8),(300,0),(400,0)]])
    crop=plan_crop(track,320,180,0,400,max_velocity=100,max_acceleration=200)
    velocities=[(b.x-a.x)/((b.time_ms-a.time_ms)/1000) for a,b in zip(crop.points,crop.points[1:])]
    assert max(abs(v) for v in velocities)<=100
    assert all(abs(b-a)<=20.001 for a,b in zip([0]+velocities,velocities))
    assert crop.warnings


def test_crop_deceleration_does_not_snap_to_a_stopping_target():
    track=make_track([TrackSample(time_ms=t,x=x,y=.1,width=.1,height=.6)
                      for t,x in [(0,.2),(100,.3),(200,.4),(300,.401),(400,.401),(500,.4)]])
    crop=plan_crop(track,320,180,0,500,max_velocity=100,max_acceleration=200)
    velocities=[(b.x-a.x)/.1 for a,b in zip(crop.points,crop.points[1:])]
    assert all(abs(b-a)<=20.001 for a,b in zip([0]+velocities,velocities))
