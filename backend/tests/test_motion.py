from uuid import uuid4

import pytest

from app.editing import Track,TrackSample
from app.errors import AppError
from app.motion import interaction_summary,motion_features


def track(samples,source=None,project=None,label='synthetic observed geometry'):
    return Track(project_id=project or uuid4(),source_asset_id=source or uuid4(),label=label,origin='synthetic_test',
                 samples=[TrackSample(time_ms=t,x=x,y=.2,width=.1,height=.2,visible=visible) for t,x,visible in samples])


def test_relative_crossing_and_overlap_are_geometry_without_action_or_contact_claims():
    primary=track([(0,.1,True),(200,.3,True),(400,.6,True)])
    other=track([(0,.5,True),(200,.35,True),(400,.2,True)],primary.source_asset_id,primary.project_id)
    result=interaction_summary(primary,[other],expected_source_times_ms=[0,200,400,600])
    relation=result['relative_geometry'][0]['samples']
    assert relation[0]['relative_x']>0 and relation[-1]['relative_x']<0
    assert relation[1]['box_iou']==pytest.approx(1/3,abs=.0001)
    assert relation[1]['relative_vx_per_second']==pytest.approx(-1.75)
    assert result['observed_source_frame_fraction']==.75 and result['camera_motion_compensated'] is False
    assert 'not proof of contact' in result['limitations'][0]
    assert all('outcome' not in row and 'action' not in row for row in relation)


def test_velocity_does_not_bridge_occlusion_long_gaps_or_scene_cuts():
    observations=track([(0,.1,True),(100,.2,False),(200,.3,True),(1000,.4,True),(1100,.5,True)])
    features=motion_features(observations,scene_cuts_ms=[1050])
    assert [f['time_ms'] for f in features]==[0,200,1000,1100]
    assert [f.get('continuity') for f in features[1:]]==['occlusion','observation_gap','scene_cut']
    assert all('vx_per_second' not in f for f in features)
    other=track([(0,.5,True),(100,.5,True),(200,.5,True),(1000,.5,True),(1100,.5,True)],observations.source_asset_id,observations.project_id)
    relations=interaction_summary(observations,[other],scene_cuts_ms=[1050])['relative_geometry'][0]['samples']
    assert all('relative_vx_per_second' not in f for f in relations)


def test_common_image_motion_is_flagged_without_asserting_a_moving_camera():
    primary=track([(0,.1,True),(200,.3,True)])
    other=track([(0,.5,True),(200,.7,True)],primary.source_asset_id,primary.project_id)
    result=interaction_summary(primary,[other])
    assert result['relative_geometry'][0]['camera_or_shared_motion_ambiguous'] is True
    assert result['camera_motion_compensated'] is False


def test_bounded_motion_summaries_retain_actual_first_and_last_observations():
    observed=track([(t,.1+t/10000,True) for t in range(0,2000,10)])
    features=motion_features(observed)
    assert len(features)==32 and features[0]['time_ms']==0 and features[-1]['time_ms']==1990
    times={s.time_ms for s in observed.samples}
    assert all(f['time_ms'] in times for f in features)
    assert all(f['area']==.02 for f in features)


def test_unaligned_or_foreign_tracks_do_not_get_invented_joint_samples():
    first=track([(0,.1,True),(100,.2,True)])
    other=track([(50,.5,True),(150,.5,True)],first.source_asset_id,first.project_id)
    relation=interaction_summary(first,[other])['relative_geometry'][0]
    assert relation['samples']==[] and relation['joint_observed_samples']==0
    with pytest.raises(AppError):interaction_summary(first,[track([(0,.5,True)])])
