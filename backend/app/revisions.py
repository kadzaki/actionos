"""Cloud-interpreted revisions, immutable child plans and evidence reuse."""
from uuid import uuid4

from pydantic import Field

from .analysis import analyze,choose_events,refine_event
from .corrections import latest_events
from .director import make_reels
from .errors import AppError
from .models import ActionEvent,ActionIntent,Asset,Job,Model,Project,now
from .providers.meta import MetaCloud
from .providers import muse
from .rendering import load_plan


class RevisionRequest(Model):
    instruction: str = Field(min_length=1,max_length=8000)


SEMANTIC_FIELDS=('action_description','subjects','interactions','temporal_predicates','exclusions')


def requires_analysis(prior,updated):
    return any(getattr(prior,field)!=getattr(updated,field) for field in SEMANTIC_FIELDS)


def revise(store,runner,settings,job: Job):
    parent=load_plan(store,job.request['parent_plan_id'])
    if parent.project_id!=job.project_id or parent.source_asset_id!=job.asset_id or not parent.intent_id:
        raise AppError('OWNERSHIP','Revision requires an action plan from this source',422)
    prior=store.load(job.project_id,'intents',ActionIntent,parent.intent_id)
    events=latest_events([e for e in store.list_records(job.project_id,'events',ActionEvent) if e.intent_id==prior.id])
    runner.stage(job,'understanding',None)
    with MetaCloud(settings) as cloud:
        proposal,provenance=muse.revision(cloud,prior,parent,events,job.request['instruction'])
    if proposal.unsupported_operations:
        raise AppError('UNSUPPORTED_EDIT','Requested effects are not supported by the renderer',422,
                       details={'operations':proposal.unsupported_operations})
    if proposal.intent.clarification:
        return {'clarification':proposal.intent.clarification,'parent_plan_id':str(parent.id),'export_ids':[]}
    description=prior.original_prompt+'\nRevision: '+job.request['instruction']
    if len(description)>8000:
        raise AppError('PROMPT_LIMIT','Combined revision context exceeds 8000 characters; start a new action request',422)
    intent=ActionIntent(project_id=job.project_id,source_asset_id=job.asset_id,original_prompt=description,
                        **proposal.intent.model_dump(),provider_metadata={**provenance,'parent_intent_id':str(prior.id)})
    changed=[field for field in proposal.intent.model_fields if getattr(prior,field)!=getattr(intent,field)]
    new_analysis=requires_analysis(prior,intent) or not prior.provider_metadata.get('scan_complete')
    diff={'operation':'revision','instruction':job.request['instruction'],'changed_fields':changed,
          'evidence_reanalyzed':new_analysis,'explanation':proposal.explanation,'parent_plan_id':str(parent.id)}
    job.request.update(intent_id=str(intent.id),requested_duration_ms=proposal.requested_duration_ms,revision_diff=diff)
    if new_analysis:
        result=analyze(store,runner,settings,job,intent_override=intent)
        if result.get('no_match') or result.get('clarification'):
            return {**result,'revision_diff':diff,'export_ids':[]}
    else:
        intent.provider_metadata['scan_complete']=True
        intent.provider_metadata['reused_intent_id']=str(prior.id)
        store.save(job.project_id,'intents',intent)
        clones=[]
        for event in events:
            data=event.model_dump()
            data.update(id=uuid4(),created_at=now(),intent_id=intent.id,parent_event_id=event.id)
            if (intent.context_before_ms,intent.context_after_ms)!=(prior.context_before_ms,prior.context_after_ms):
                data['primary_track_id']=None
            clone=ActionEvent.model_validate(data)
            store.save(job.project_id,'events',clone);clones.append(clone)
        with store.mutation():
            project=store.load(job.project_id,'project',Project)
            project.active_intent_id=intent.id
            store.save(job.project_id,'project',project)
    result=make_reels(store,runner,settings,job)
    return {**result,'revision_diff':diff,'intent_id':str(intent.id)}
