from uuid import uuid4

import pytest

from app.analysis import choose_events,motion_features
from app.corrections import latest_events
from app.director import compile_event
from app.editing import Track,TrackSample,crop_at,plan_crop,timeline
from app.errors import AppError
from app.models import ActionEvent,ActionIntent,Asset,Occurrence,Phase
from app.revisions import requires_analysis
from test_evidence import prepare


def event_for(asset,intent,start=100,end=1000,**kwargs):
    return ActionEvent(project_id=asset.project_id,source_asset_id=asset.id,intent_id=intent.id,
                       action_label='robot unfolds a map',start_ms=start,end_ms=end,
                       primary_subject='robot',match_reason='visible unfolding',evidence_ms=[start],**kwargs)


def test_occurrence_semantics_precede_best_match_ranking():
    project,source=uuid4(),uuid4()
    intent=ActionIntent(project_id=project,source_asset_id=source,original_prompt='second map unfolding',action_description='unfold')
    asset=Asset(project_id=project,source_path='source/test',original_name='test',byte_size=1,sha256='0'*64)
    events=[event_for(asset,intent,start=100+i*1000,end=500+i*1000,
                      confidence={'match_relevance':'high' if i==0 else 'low'}) for i in range(3)]
    for mode,ordinal,expected in [('first',None,events[:1]),('last',None,events[-1:]),('ordinal',2,events[1:2]),
                                  ('all',None,events),('best',None,events[:1]),('ordinal',4,[])]:
        intent.occurrence=Occurrence(mode=mode,ordinal=ordinal)
        assert choose_events(intent,list(reversed(events)))==expected
    assert choose_events(intent,[])==[]


def test_motion_features_use_actual_time_and_never_assume_action():
    track=Track(project_id=uuid4(),source_asset_id=uuid4(),label='robot',origin='synthetic_test',samples=[
        TrackSample(time_ms=100,x=.1,y=.1,width=.1,height=.1),
        TrackSample(time_ms=350,x=.3,y=.1,width=.1,height=.1)])
    values=motion_features(track)
    assert values[1]['vx_per_second']==.8
    assert 'jump' not in str(values) and 'outcome' not in str(values)


def test_director_uses_event_lineage_and_only_requested_slow_motion(client,media):
    asset=prepare(client,media['silent'])
    intent=ActionIntent(project_id=asset.project_id,source_asset_id=asset.id,original_prompt='unfold the map',action_description='unfold',
                        context_before_ms=100,context_after_ms=100)
    event=event_for(asset,intent,phases=[Phase(id='key',label='map opens',role='key_moment',time_ms=600,status='estimated',evidence_ms=[600])])
    track=Track(project_id=asset.project_id,source_asset_id=asset.id,label='robot',origin='meta_cloud',samples=[
        TrackSample(time_ms=t,x=.1+t/10000,y=.1,width=.1,height=.6) for t in range(0,1201,100)])
    event.track_ids=[track.id];event.primary_track_id=track.id
    plan=compile_event(asset,intent,event,track)
    assert len(plan.segments)==1 and plan.segments[0].speed==1
    assert plan.event_snapshots[0]['id']==str(event.id)
    intent.emphasize_key_moment=True;intent.slow_motion_speed=.8
    slow=compile_event(asset,intent,event,track)
    assert [s.speed for s in slow.segments]==[1,.8,1]
    assert timeline(slow)[-1]['output_out_ms']>timeline(plan)[-1]['output_out_ms']
    intent.preserve_normal_speed=True
    assert len(compile_event(asset,intent,event,track).segments)==1
    with pytest.raises(AppError):compile_event(asset,intent,event,None)


def test_corrections_are_immutable_and_old_snapshots_survive(client,settings,media):
    asset=prepare(client,media['silent']);store=client.app.state.store
    intent=ActionIntent(project_id=asset.project_id,source_asset_id=asset.id,original_prompt='unfold',action_description='unfold')
    event=event_for(asset,intent)
    store.save(asset.project_id,'events',event)
    url=f'/api/projects/{asset.project_id}/events/{event.id}/corrections'
    body={'start_ms':100,'end_ms':1000,'phases':[{'id':'new','label':'map open','role':'completion','time_ms':500}], 'note':'Observed on source frame'}
    result=client.post(url,json=body)
    assert result.status_code==201,result.text
    corrected=ActionEvent.model_validate(result.json())
    assert corrected.parent_event_id==event.id and corrected.phases[0].status=='user_corrected'
    assert store.load(asset.project_id,'events',ActionEvent,event.id)==event
    assert latest_events([event,corrected])==[corrected]
    assert client.post(url,json=body).status_code==409
    newurl=f'/api/projects/{asset.project_id}/events/{corrected.id}/corrections'
    assert client.post(newurl,json={**body,'end_ms':50}).status_code==422
    assert client.post(newurl,json={**body,'phases':body['phases']*2}).status_code==422
    foreign=uuid4()
    assert client.post(f'/api/projects/{foreign}/events/{corrected.id}/corrections',json=body).status_code==404


def test_revision_semantics_force_reanalysis_only_when_evidence_changes():
    prior=ActionIntent(project_id=uuid4(),source_asset_id=uuid4(),original_prompt='map unfolds',action_description='map unfolds')
    refined=prior.model_copy(update={'slow_motion_speed':.9,'context_after_ms':1000,'occurrence':Occurrence(mode='last')})
    assert not requires_analysis(prior,refined)
    for field,value in [('action_description','pan flips'),('subjects',['chef']),('exclusions',['failed flips']),('temporal_predicates',['after stirring'])]:
        assert requires_analysis(prior,prior.model_copy(update={field:value}))


@pytest.mark.parametrize('stored_kind',['concept','user_selection','old_adapter'])
def test_context_retracking_preserves_explicit_user_anchor_removal(client,settings,media,monkeypatch,stored_kind):
    from types import SimpleNamespace
    from app.analysis import refine_event
    from app.providers import muse
    asset=prepare(client,media['silent']);store=client.app.state.store;window_id=uuid4()
    intent=ActionIntent(project_id=asset.project_id,source_asset_id=asset.id,original_prompt='unfold',action_description='unfold')
    event=event_for(asset,intent,provider_metadata={'anchors_user_corrected':True})
    # Test-only observed geometry record. No product fallback is involved.
    track=Track(project_id=asset.project_id,source_asset_id=asset.id,label='robot',origin='meta_cloud',
                samples=[TrackSample(time_ms=t,x=.2,y=.2,width=.1,height=.1) for t in (100,1000)],
                provider_metadata={'window_id':str(window_id),'requested_model':settings.sam_model,'response_id':'recorded-unit-group',
                                   'adapter_version':1,'prompt_kind':'concept'})
    if stored_kind=='user_selection':track.provider_metadata.update(prompt_kind='selection',selection_origin='user')
    if stored_kind=='old_adapter':track.provider_metadata['adapter_version']=0
    store.save(asset.project_id,'tracks',track)
    monkeypatch.setattr('app.analysis.extract_window',lambda *args:SimpleNamespace(id=window_id,path=asset.proxy_path,source_frame_times_ms=[100,1000]))
    monkeypatch.setattr('app.analysis.inspect_scenes',lambda *args:SimpleNamespace(id=uuid4(),cuts=[]))
    monkeypatch.setattr(muse,'localize',lambda *args:pytest.fail('Must not overwrite user-corrected anchors'))
    fresh=track.model_copy(update={'id':uuid4()});requests=[]
    def track_again(*args):requests.append(True);return [fresh],{}
    monkeypatch.setattr('app.analysis.track_window',track_again)
    refined=refine_event(store,settings,None,asset,intent,event,lambda *_:None)
    assert refined.phases==[] and refined.primary_track_id==(track.id if stored_kind=='concept' else fresh.id)
    assert len(requests)==(0 if stored_kind=='concept' else 1)


def test_director_trims_unobserved_optional_context_but_never_cuts_the_action(client,media):
    asset=prepare(client,media['silent'])
    intent=ActionIntent(project_id=asset.project_id,source_asset_id=asset.id,original_prompt='unfold',action_description='unfold',
                        context_before_ms=500,context_after_ms=500)
    event=event_for(asset,intent,start=500,end=1000)
    track=Track(project_id=asset.project_id,source_asset_id=asset.id,label='robot',origin='meta_cloud',samples=[
        TrackSample(time_ms=t,x=.2,y=.1,width=.1,height=.6) for t in (500,600,700,800,900,1000)])
    event.track_ids=[track.id]
    plan=compile_event(asset,intent,event,track)
    assert plan.segments[0].source_in_ms==500 and plan.segments[-1].source_out_ms==1000
    assert any('Optional context was trimmed' in w for w in plan.segments[0].crop.warnings)
    # Missing evidence inside the action is still a failure, not an automatic
    # action truncation disguised as a context adjustment.
    event.start_ms=0
    with pytest.raises(AppError,match='does not cover'):compile_event(asset,intent,event,track)


def test_cross_scene_tracking_requires_review_and_explicit_fixed_preference_is_honored(client,media):
    asset=prepare(client,media['silent'])
    intent=ActionIntent(project_id=asset.project_id,source_asset_id=asset.id,original_prompt='unfold',action_description='unfold')
    event=event_for(asset,intent,provider_metadata={'geometry_summary':{'scene_cuts_ms':[600]}})
    track=Track(project_id=asset.project_id,source_asset_id=asset.id,label='robot',origin='meta_cloud',samples=[
        TrackSample(time_ms=t,x=.2,y=.1,width=.1,height=.6) for t in (100,300,500,700,900,1000)])
    event.track_ids=[track.id]
    with pytest.raises(AppError) as exc:compile_event(asset,intent,event,track)
    assert exc.value.code=='SCENE_REVIEW_REQUIRED'
    plan=compile_event(asset,intent,event,track,allow_fixed_crop=True)
    assert all(s.crop.mode=='fixed' and s.crop.track_id is None for s in plan.segments)
    assert any('explicitly selected' in w for w in plan.segments[0].crop.warnings)


@pytest.mark.parametrize('key_ms',[251,749])
def test_subframe_speed_transitions_preserve_every_source_interval(client,media,key_ms):
    asset=prepare(client,media['silent'])
    intent=ActionIntent(project_id=asset.project_id,source_asset_id=asset.id,original_prompt='emphasize unfolding',
                        action_description='unfold',emphasize_key_moment=True)
    event=event_for(asset,intent,start=0,end=1000,
                    phases=[Phase(id='key',label='map opens',role='key_moment',time_ms=key_ms,status='estimated',evidence_ms=[key_ms])])
    plan=compile_event(asset,intent,event,None,allow_fixed_crop=True)
    assert plan.segments[0].source_in_ms==0 and plan.segments[-1].source_out_ms==1000
    assert all(a.source_out_ms==b.source_in_ms for a,b in zip(plan.segments,plan.segments[1:]))
    assert sum(s.source_out_ms-s.source_in_ms for s in plan.segments)==1000
    assert any('preserve the complete source action' in w for w in plan.segments[0].crop.warnings)
    intent.slow_motion_speed=1
    normal=compile_event(asset,intent,event,None,allow_fixed_crop=True)
    assert len(normal.segments)==1 and normal.segments[0].speed==1


def test_dense_crop_path_and_context_composition_are_bounded():
    project,source=uuid4(),uuid4()
    def track(x,width=.04):
        return Track(project_id=project,source_asset_id=source,label='observed test geometry',origin='synthetic_test',samples=[
            TrackSample(time_ms=t,x=x+t/200000,y=.2,width=width,height=.2) for t in range(0,6001,20)])
    primary=track(.3);context=track(.45)
    crop=plan_crop(primary,320,180,0,6000,context_tracks=[context])
    assert len(crop.points)<=128 and crop.context_track_ids==[context.id]
    assert crop.points[0].time_ms==0 and crop.points[-1].time_ms==6000
    x,_=crop_at(crop,0)
    assert x<=.3*320 and x+crop.width>=.49*320
    far=plan_crop(primary,320,180,0,6000,context_tracks=[track(.8)])
    assert any('cannot fit together' in w for w in far.warnings)
