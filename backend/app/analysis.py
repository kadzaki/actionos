"""Evidence-linked, prompt-conditioned analysis with actual Meta cloud calls."""
from uuid import uuid4
import math

from .config import Settings
from .editing import Track
from .errors import AppError
from .evidence import evidence_frame
from .models import ActionEvent,ActionIntent,Asset,Job,Phase,Project
from .providers.meta import MetaCloud
from .providers import muse
from .providers.sam import track_window,SAM_ADAPTER_VERSION
from .sampling import coarse_windows,extract_window
from .storage import FileStore
from .corrections import latest_events
from .reconciliation import AnalysisRecord,merge_groups
from .motion import motion_features,attach_geometry
from .scenes import inspect_scenes


def source_time(window,local_ms):
    index=max(0,min(len(window.source_frame_times_ms)-1,math.floor(local_ms*window.fps/1000)))
    return round(window.source_frame_times_ms[index])


def convert_phases(window,phases):
    return [Phase(id=f'phase_{i+1}',label=p.label,role=p.role,time_ms=source_time(window,p.time_ms),
                  evidence_ms=sorted({source_time(window,t) for t in p.evidence_ms}),status='estimated')
            for i,p in enumerate(phases)]


def choose_events(intent: ActionIntent,events):
    ordered=sorted(events,key=lambda e:(e.start_ms,str(e.id)))
    mode=intent.occurrence.mode
    if not ordered:return []
    if mode=='all':return ordered
    if mode=='first':return ordered[:1]
    if mode=='last':return ordered[-1:]
    if mode=='ordinal':
        n=intent.occurrence.ordinal
        return ordered[n-1:n]
    # Evidence-based suitability, not engagement or probability of success.
    rank={'low':0,'medium':1,'high':2}
    return sorted(ordered,key=lambda e:(rank.get(e.confidence.get('match_relevance'),0),len(e.phases),
                                        -len(e.warnings),e.end_ms-e.start_ms),reverse=True)[:1]


def refine_event(store,settings,cloud,asset,intent,event,stage):
    stage('tracking',None)
    start=max(0,event.start_ms-intent.context_before_ms)
    end=min(asset.working_metadata.duration_ms,event.end_ms+intent.context_after_ms)
    window=extract_window(store,settings,asset,start,end)
    primary=[];all_tracks=[]
    cached_records,cache_errors=store.scan_records(event.project_id,'tracks',Track)
    if cache_errors:
        event.provider_metadata['unavailable_cached_track_ids']=[error['record_id'] for error in cache_errors]
        warning='Damaged cached track records were excluded; original files were retained and target tracking is checked again'
        if warning not in event.warnings:event.warnings.append(warning)
    for subject in dict.fromkeys([event.primary_subject,*event.context_subjects]):
        existing=[t for t in cached_records
                  if t.source_asset_id==asset.id and t.label==subject and t.origin=='meta_cloud'
                  and t.provider_metadata.get('window_id')==str(window.id)
                  and t.provider_metadata.get('requested_model')==settings.sam_model
                  and t.provider_metadata.get('adapter_version')==SAM_ADAPTER_VERSION
                  and t.provider_metadata.get('prompt_kind')=='concept'
                  and t.provider_metadata.get('selection_origin') is None]
        if existing:
            response=max(existing,key=lambda t:t.created_at).provider_metadata.get('response_id')
            tracks=[t for t in existing if t.provider_metadata.get('response_id')==response]
        else:
            tracks,tracking_meta=track_window(cloud,window,store.path(event.project_id,window.path),subject)
            for track in tracks:store.save(event.project_id,'tracks',track)
        all_tracks.extend(tracks)
        if subject==event.primary_subject:primary=tracks
    event.track_ids=[track.id for track in all_tracks]
    if len(primary)==1:
        event.primary_track_id=primary[0].id
        stage('inspecting',None)
        scenes=inspect_scenes(store,settings,window)
        geometry=attach_geometry(event,primary[0],[t for t in all_tracks if t.id!=primary[0].id],window,scenes)
        if event.provider_metadata.get('anchors_user_corrected'):
            # Reframing/retracking must not replace user evidence with a new
            # model hypothesis, including an explicitly emptied anchor list.
            store.save(event.project_id,'events',event)
            return event
        stage('localizing',None)
        refined,phase_meta=muse.localize(cloud,store.path(event.project_id,window.path),window,
                                        event.action_label,geometry)
        phases=convert_phases(window,refined.phases)
        if all(event.start_ms<=t<=event.end_ms for p in phases for t in [p.time_ms,*p.evidence_ms]):
            event.phases=phases
        else:event.warnings.append('Refined phases exceeded the candidate; inspect boundary evidence')
        event.warnings.extend(refined.uncertainty)
        event.provider_metadata['phase_request']=phase_meta
        event.review_required=bool(event.warnings)
    else:
        event.warnings.append('Choose a target subject' if primary else 'No tracked target; choose another subject or an explicitly fixed crop')
        event.review_required=True
    store.save(event.project_id,'events',event)
    return event


def analyze(store: FileStore,runner,settings: Settings,job: Job,intent_override=None):
    asset=store.load(job.project_id,'assets',Asset,job.asset_id)
    project=store.load(job.project_id,'project',Project)
    if asset.status!='ready' or not asset.working_metadata or not asset.metadata:
        raise AppError('MEDIA_NOT_READY','Prepare source before analysis',409)
    runner.stage(job,'understanding',None)
    with MetaCloud(settings) as cloud:
        prompt=job.request.get('action_prompt',project.action_prompt)
        if intent_override is None:
            draft,provenance=muse.interpret(cloud,prompt)
            intent=ActionIntent(project_id=project.id,source_asset_id=asset.id,original_prompt=prompt,
                                **draft.model_dump(),provider_metadata=provenance)
        else:intent=intent_override
        store.save(project.id,'intents',intent)
        with store.mutation():
            project=store.load(job.project_id,'project',Project)
            project.active_intent_id=intent.id
            store.save(project.id,'project',project)
        if intent.clarification:
            return {'intent_id':str(intent.id),'clarification':intent.clarification,'event_ids':[],
                    'scan_complete':False,'no_match':False}
        runner.stage(job,'sampling',None)
        windows=coarse_windows(store,settings,asset)
        runner.stage(job,'understanding',None)
        found=[];complete=True;notes=[];observations=[]
        for window in windows:
            batch,request_meta=muse.candidates(cloud,store.path(project.id,window.path),window,intent)
            complete=complete and batch.coverage_complete
            notes.extend(batch.notes)
            observations.append({'window_id':str(window.id),'requested_in_ms':window.requested_in_ms,
                                 'requested_out_ms':window.requested_out_ms,'notes':batch.notes,
                                 'provider_metadata':request_meta,'window_sha256':window.sha256,
                                 'context_observations':[{'description':o.description,
                                    'start_ms':source_time(window,o.start_ms),'end_ms':source_time(window,o.end_ms),
                                    'evidence_ms':[source_time(window,t) for t in o.evidence_ms]}
                                    for o in batch.context_observations]})
            for candidate in batch.candidates:
                start=source_time(window,candidate.start_ms)
                end=min(asset.metadata.duration_ms,max(source_time(window,candidate.end_ms),
                                                       round(window.first_working_time_ms+candidate.end_ms)))
                phases=convert_phases(window,candidate.phases)
                evidence=sorted({source_time(window,t) for t in candidate.evidence_ms})
                if end<=start:continue
                event=ActionEvent(project_id=project.id,source_asset_id=asset.id,intent_id=intent.id,
                                  action_label=candidate.action_label,start_ms=start,end_ms=end,phases=phases,
                                  primary_subject=candidate.primary_subject,context_subjects=candidate.context_subjects,
                                  match_reason=candidate.match_reason,evidence_ms=evidence,review_required=True,
                                  warnings=candidate.uncertainty,confidence={'match_relevance':candidate.match_confidence,
                                  'anchor_precision':'estimated','track_reliability':'unknown','edit_suitability':'unassessed'},
                                  provider_metadata={**request_meta,'window_id':str(window.id),
                                                     'predicate_status':candidate.predicate_status,
                                                     'matches_exclusions':candidate.matches_exclusions})
                found.append(event)
        record=AnalysisRecord(project_id=project.id,source_asset_id=asset.id,intent_id=intent.id,
                              windows=observations,candidates=[e.model_dump(mode='json') for e in found])
        store.save(project.id,'analyses',record)
        if not complete:
            record.status='incomplete';store.save(project.id,'analyses',record)
            raise AppError('ANALYSIS_INCOMPLETE','Cloud reasoning did not confirm full source coverage; review or retry',502,True)
        if len(found)>400:
            record.status='limit_exceeded';store.save(project.id,'analyses',record)
            raise AppError('ANALYSIS_LIMIT','More than 400 candidates require a more specific request; no occurrences were silently discarded',422)
        if found and (len(windows)>1 or any(e.provider_metadata['predicate_status']=='needs_context' for e in found)):
            runner.stage(job,'reconciling',None)
            try:
                reconciled,reconcile_meta=muse.reconcile(cloud,intent,found,observations)
                events=merge_groups(found,reconciled,record.id,reconcile_meta)
            except AppError:
                record.status='reconciliation_failed';store.save(project.id,'analyses',record)
                raise
            record.reconciliation=reconciled.model_dump(mode='json')
            record.provider_metadata=reconcile_meta
        else:
            # One complete window already supplies distinct occurrences; never
            # collapse same-label repetitions merely because their times overlap.
            events=[e for e in found if not e.provider_metadata['matches_exclusions']
                    and e.provider_metadata['predicate_status']=='confirmed']
            for event in events:event.provider_metadata['analysis_id']=str(record.id)
        record.status='reconciled';store.save(project.id,'analyses',record)
        intent.provider_metadata['scan_complete']=True
        intent.provider_metadata['analysis_id']=str(record.id)
        intent.provider_metadata['window_ids']=[str(w.id) for w in windows]
        store.save(project.id,'intents',intent)
        for event in events:
            # Actual immutable-source evidence images, not model descriptions.
            for timestamp in event.evidence_ms[:4]:
                frame=evidence_frame(store,settings,asset,min(timestamp,asset.metadata.duration_ms-1))
                event.evidence_frame_ids.append(frame.id)
            store.save(project.id,'events',event)
        selected=choose_events(intent,events)
        runner.stage(job,'tracking',None)
        for event in selected:
            refine_event(store,settings,cloud,asset,intent,event,lambda step,progress:runner.stage(job,step,progress))
        with store.mutation():
            project=store.load(job.project_id,'project',Project)
            project.status='ready'
            store.save(project.id,'project',project)
        return {'intent_id':str(intent.id),'analysis_id':str(record.id),'event_ids':[str(e.id) for e in events],
                'selected_event_ids':[str(e.id) for e in selected],'scan_complete':complete,
                'no_match':not events,'notes':notes,'cloud_provenance':True}
