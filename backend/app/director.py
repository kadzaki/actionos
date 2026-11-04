"""Compile event-grounded Action Focus edits, then execute the existing renderer."""
from uuid import UUID

from .analysis import choose_events,refine_event
from .providers.meta import MetaCloud
from .config import Settings
from .editing import EditPlan,OutputSettings,Segment,Track,plan_crop,fixed_crop,timeline
from .errors import AppError
from .models import ActionEvent,ActionIntent,Asset,Job,Project
from .rendering import render
from .storage import FileStore
from .corrections import latest_events
from .motion import attach_geometry,MOTION_FEATURE_VERSION
from .scenes import inspect_scenes
from .sampling import extract_window


def compile_event(asset: Asset,intent: ActionIntent,event: ActionEvent,track: Track | None,
                  *,duration_ms=20000,preview=False,allow_fixed_crop=False,context_tracks=()):
    if event.source_asset_id!=asset.id or intent.source_asset_id!=asset.id or event.intent_id!=intent.id:
        raise AppError('OWNERSHIP','Selected action does not belong to this source and intent',422)
    metadata=asset.working_metadata
    if not metadata or not asset.metadata:raise AppError('MEDIA_NOT_READY','Prepare source before directing',409)
    start=max(0,event.start_ms-intent.context_before_ms)
    end=min(metadata.duration_ms,asset.metadata.duration_ms,event.end_ms+intent.context_after_ms)
    # Remove optional context first when fitting a shorter target. Never cut a
    # matched action in half merely to satisfy a nominal duration control.
    overflow=max(0,end-start-duration_ms)
    remove_before=min(event.start_ms-start,overflow//2)
    start+=remove_before;overflow-=remove_before
    remove_after=min(end-event.end_ms,overflow)
    end-=remove_after;overflow-=remove_after
    start+=min(event.start_ms-start,overflow)
    if end-start<100:
        raise AppError('EVENT_TOO_SHORT','This candidate has too little visible source context',422)
    width,height=metadata.geometry.display_width,metadata.geometry.display_height
    if allow_fixed_crop:
        crop=fixed_crop(width,height)
        crop.warnings.append('Fixed framing explicitly selected: this output does not follow the subject or guarantee interacting-object visibility')
    elif track:
        if track.source_asset_id!=asset.id or track.id not in event.track_ids or track.origin!='meta_cloud':
            raise AppError('INVALID_EVIDENCE','Automatic crop requires this event’s actual Meta track',422)
        for context in context_tracks:
            if context.source_asset_id!=asset.id or context.id not in event.track_ids or context.origin!='meta_cloud':
                raise AppError('INVALID_EVIDENCE','Context framing requires this event’s actual Meta tracks',422)
        cuts=event.provider_metadata.get('geometry_summary',{}).get('scene_cuts_ms',[])
        if any(event.start_ms<t<event.end_ms for t in cuts):
            raise AppError('SCENE_REVIEW_REQUIRED','Estimated scene change crosses the action; correct boundaries or explicitly choose fixed framing',409,True)
        requested_start,requested_end=start,end
        visible=[s for s in track.samples if s.visible]
        if visible:
            # Optional context does not justify inventing target positions on
            # blank/occluded frames. Preserve the whole event and let the crop
            # planner reject unreliable gaps inside the actual action.
            start=max(start,min(event.start_ms,visible[0].time_ms))
            end=min(end,max(event.end_ms,visible[-1].time_ms))
        crop=plan_crop(track,width,height,start,end,context_tracks=context_tracks)
        if (start,end)!=(requested_start,requested_end):
            crop.warnings.append('Optional context was trimmed where the tracked subject was not observed; the matched action was preserved')
        if intent.keep_context_in_frame and not context_tracks:
            crop.warnings.append('Requested context has no observed track; inspect context visibility in the output')
    else:
        raise AppError('SUBJECT_SELECTION_REQUIRED','Select a tracked subject or explicitly choose fixed framing',409,True)
    spans=[(start,end,1.)]
    key=next((p for p in event.phases if p.role=='key_moment'),None)
    if key and intent.emphasize_key_moment and not intent.preserve_normal_speed and intent.slow_motion_speed<1:
        slow_start=max(start,key.time_ms-250)
        slow_end=min(end,key.time_ms+250)
        if slow_end-slow_start>=100:
            spans=[(start,slow_start,1.),(slow_start,slow_end,intent.slow_motion_speed),(slow_end,end,1.)]
    spans=[span for span in spans if span[1]>span[0]]
    # Output cannot represent a speed section shorter than one output frame.
    # Coalesce it into its neighbor instead of silently dropping source time.
    index=0;coalesced=False
    while index<len(spans) and len(spans)>1:
        a,b,s=spans[index]
        if (b-a)/s<1000/30:
            if index==0:
                _,next_end,next_speed=spans[1];spans[1]=(a,next_end,next_speed)
            else:
                prev_start,_,prev_speed=spans[index-1];spans[index-1]=(prev_start,b,prev_speed)
            spans.pop(index);index=max(0,index-1);coalesced=True
        else:index+=1
    if coalesced:crop.warnings.append('A speed transition shorter than one output frame was merged to preserve the complete source action')
    segments=[Segment(source_in_ms=a,source_out_ms=b,speed=s,crop=crop,event_id=event.id,
                      phase_ids=[p.id for p in event.phases if a<=p.time_ms<=b])
              for a,b,s in spans]
    plan=EditPlan(project_id=asset.project_id,source_asset_id=asset.id,origin='autonomous',intent_id=intent.id,
                  selected_event_ids=[event.id],requested_duration_ms=duration_ms,segments=segments,
                  output=OutputSettings(width=360 if preview else 1080,height=640 if preview else 1920),
                  event_snapshots=[event.model_dump(mode='json')],
                  reasoning=[{'event_id':str(event.id),'operation':'action_focus','trigger':key.id if key else 'event_bounds',
                              'evidence_ms':event.evidence_ms,'context_before_ms':intent.context_before_ms,
                              'context_after_ms':intent.context_after_ms,'warnings':crop.warnings,
                              'actual_context_before_ms':event.start_ms-start,'actual_context_after_ms':end-event.end_ms,
                              'outcome':event.outcome}])
    return plan


def make_reels(store: FileStore,runner,settings: Settings,job: Job):
    project=store.load(job.project_id,'project',Project)
    asset=store.load(job.project_id,'assets',Asset,job.asset_id)
    intent_id=job.request.get('intent_id') or project.active_intent_id
    if not intent_id:raise AppError('ANALYSIS_REQUIRED','Analyze an action prompt before directing',409)
    intent=store.load(project.id,'intents',ActionIntent,intent_id)
    events=latest_events([e for e in store.list_records(project.id,'events',ActionEvent) if e.intent_id==intent.id])
    chosen=job.request.get('event_id')
    selected=[e for e in events if str(e.id)==chosen] if chosen else choose_events(intent,events)
    if not selected:raise AppError('NO_MATCH','No matching occurrence was available to render',409)
    def process(event):
        if not event.primary_track_id and not job.request.get('track_id') and not job.request.get('allow_fixed_crop'):
            with MetaCloud(settings) as cloud:
                event=refine_event(store,settings,cloud,asset,intent,event,
                                   lambda step,progress:runner.stage(job,step,progress))
        runner.stage(job,'planning',None)
        track_id=None if job.request.get('allow_fixed_crop') else (job.request.get('track_id') or event.primary_track_id)
        track_id=UUID(str(track_id)) if track_id else None
        track=store.load(project.id,'tracks',Track,track_id) if track_id else None
        context_tracks=[]
        if intent.keep_context_in_frame and not job.request.get('allow_fixed_crop'):
            context_tracks=[store.load(project.id,'tracks',Track,t) for t in event.track_ids if t!=track_id]
            context_tracks=[t for t in context_tracks if t.label in event.context_subjects]
        if track and not job.request.get('allow_fixed_crop'):
            runner.stage(job,'inspecting',None)
            window=extract_window(store,settings,asset,max(0,event.start_ms-intent.context_before_ms),
                                  min(asset.working_metadata.duration_ms,event.end_ms+intent.context_after_ms))
            previous=event.provider_metadata.get('geometry_summary',{})
            if (previous.get('feature_version')!=MOTION_FEATURE_VERSION or previous.get('window_id')!=str(window.id)
                or previous.get('primary_track_id')!=str(track.id)):
                scenes=inspect_scenes(store,settings,window)
                attach_geometry(event,track,context_tracks,window,scenes)
                store.save(project.id,'events',event)
        runner.stage(job,'planning',None)
        plan=compile_event(asset,intent,event,track,duration_ms=job.request.get('requested_duration_ms',project.requested_duration_ms),
                           preview=job.request.get('preview',False),allow_fixed_crop=job.request.get('allow_fixed_crop',False),context_tracks=context_tracks)
        if job.request.get('parent_plan_id'):
            from .rendering import load_plan
            parent=load_plan(store,job.request['parent_plan_id'])
            if parent.project_id!=project.id or parent.source_asset_id!=asset.id:
                raise AppError('OWNERSHIP','Parent plan does not belong to this source',422)
            plan.parent_id=parent.id
            plan.version=parent.version+1
            plan.reasoning.append(job.request.get('revision_diff',{}))
        export=render(store,settings,plan,lambda step,progress:runner.stage(job,step,progress),regenerate=job.request.get('regenerate',False))
        return plan,export

    result={'export_ids':[],'plan_ids':[],'completed_event_ids':[],'requested_event_ids':[str(e.id) for e in selected],
            'requested_count':len(selected),'completed_count':0,'complete':False,'origin':'autonomous','notes':[]}
    notes=[]
    for event in selected:
        result['active_event_id']=str(event.id)
        job.result=result
        store.save(job.project_id,'jobs',job)
        try:plan,export=process(event)
        except Exception:
            result['failed_event_id']=str(event.id)
            job.result=result
            raise
        result['export_ids'].append(str(export.id));result['plan_ids'].append(str(export.plan_id))
        result['completed_event_ids'].append(str(event.id));result['completed_count']+=1
        actual=timeline(plan)[-1]['output_out_ms']
        if actual<plan.requested_duration_ms-100:
            notes.append('The visible action produced a shorter natural edit; no source moments were repeated to fill duration')
        result['notes']=sorted(set(notes))
        job.result=result
        store.save(job.project_id,'jobs',job)
    result.pop('active_event_id',None)
    result['complete']=True
    return result
