"""Bounded run orchestration. Provider hypotheses never bypass evidence fusion."""
from uuid import uuid5

from .planning import ObservationPlan,plan_run
from .muse_observations import prepare_window,observe_window
from .observation_repository import publish_muse_record,load_muse_record
from .entity_tracks import observe_entity
from .entity_repository import publish_entity_batch,load_binding
from .geometry_repository import publish_geometry,load_geometry
from .fusion import PredicateSupport
from .fusion_repository import publish_fused_observation,load_fused_observation
from .temporal import NodeEvidence,evaluate_program,EVALUATOR_VERSION
from .truth import Truth
from ..actions.contracts import EventEvidence,StepEvidence
from ..errors import AppError
from ..programs.repository import load_program
from ..providers.meta import MetaCloud
from ..repository import Envelope,Repository,reference,fingerprint,encoded
from ..validation import WorkBudget
from ..runs import RunSnapshot
from ..resources import MediaBudget
from ..models import EvidenceFrame
from .evidence_storage import existing_artifact
import hashlib


def _support_groups(supports):
    """Join contiguous computed geometry, retaining every measurement ID."""
    semantic=[s for s in supports if s.observation_ids or not s.geometry_ids]
    geometry=sorted([s for s in supports if s.geometry_ids and not s.observation_ids],key=lambda s:(s.interval.start_ms,s.interval.end_ms))
    groups=[]
    for support in geometry:
        if groups and support.interval.start_ms<=groups[-1].interval.end_ms and support.estimated_boundaries==groups[-1].estimated_boundaries:
            previous=groups[-1]
            groups[-1]=PredicateSupport(interval={'start_ms':previous.interval.start_ms,'end_ms':max(previous.interval.end_ms,support.interval.end_ms)},
                geometry_ids=list(dict.fromkeys([*previous.geometry_ids,*support.geometry_ids])),estimated_boundaries=previous.estimated_boundaries)
        else:groups.append(support)
    return [*semantic,*groups]


def records(store,project_id,kind,run):
    repo=Repository(store)
    result=[]
    for path in store.path(project_id,kind).glob('*.json'):
        header=repo._read(project_id,kind,path.stem,1)
        if header.run and header.run.id==run.id:
            record=repo.load(project_id,kind,path.stem)
            if record.run!=reference(run):raise AppError('REFERENCE_MISMATCH','Run evidence fingerprint differs',409)
            result.append(record)
    return sorted(result,key=lambda r:(r.created_at,str(r.id)))


def observe_run(store,runner,settings,run,job):
    """Explicit Run starts cloud work; a shared budget bounds retries as well."""
    plan_record=plan_run(store,settings,run.project_id,run.id)
    plan=ObservationPlan.model_validate(plan_record.payload['plan'])
    budget=WorkBudget()
    with MetaCloud(settings,budget=budget) as cloud:
        for number,window in enumerate(plan.windows):
            runner.stage(job,'observing',round(100*number/len(plan.windows)))
            prepared=prepare_window(store,settings,run.project_id,run.id,plan_record.id,window.id)
            capture={}
            try:
                observe_window(cloud,prepared,record_response=lambda output,provenance:capture.update(output=output,provenance=provenance),
                    record_text_response=lambda raw,provenance:capture.update(raw=raw,provenance=provenance))
                publish_muse_record(store,settings,run.project_id,run.id,window.id,capture['output'],capture['provenance'],
                    origin='meta_cloud',evidence_scope='Explicit Studio run')
            except AppError as exc:
                if not exc.code.startswith('PROVIDER_'):raise
                provenance=capture.get('provenance',{'provider':'meta_cloud','requested_model':plan.providers['muse_model'],'endpoint':plan.providers['endpoint']})
                publish_muse_record(store,settings,run.project_id,run.id,window.id,
                    capture.get('output',{'unparsed_text':capture['raw']} if 'raw' in capture else None),provenance,
                    origin='meta_cloud',evidence_scope='Explicit Studio run; failed window',error=exc.envelope())
                runner.checkpoint(job,{'run_id':str(run.id),'failed_window':window.id,'provider_calls':budget.used})
                raise
            for entity in plan.entity_prompts:
                runner.stage(job,'tracking',None)
                raw={}
                batch=observe_entity(cloud,prepared,entity,record_response=lambda text,provenance:raw.update(text=text))
                publish_entity_batch(store,settings,run.project_id,run.id,window.id,batch,
                    raw_text=raw.get('text') if batch.status!='unavailable' else None)
                if batch.status=='unavailable':
                    raise AppError('PROVIDER_TRACKING_UNAVAILABLE','Tracking failed; completed evidence is preserved. Evaluate saved evidence or retry with a new run.',502,True)
    return evaluate_run(store,runner,settings,run,job)


def evaluate_run(store,runner,settings,run,job):
    """Replay exact saved evidence without any provider calls or auto actions."""
    runner.stage(job,'evaluating',None)
    if RunSnapshot.model_validate(run.payload).evaluator_version!=EVALUATOR_VERSION:
        raise AppError('EVALUATOR_VERSION_MISMATCH','This saved run pins an older evaluator. Use compatible saved-evidence replay to create a new local run.',409)
    repo=Repository(store);project=run.project_id
    program,data=load_program(store,project,run.program.id,run.program.version)
    ast=data.program.ast
    plan_record=plan_run(store,settings,project,run.id)
    plan=ObservationPlan.model_validate(plan_record.payload['plan'])
    muse={r.payload['planned_window_id']:r for r in records(store,project,'muse_observations',run)}
    bindings={}
    for record in records(store,project,'entity_bindings',run):
        _,binding,batch_record,_,prepared=load_binding(store,settings,project,run.id,record.id)
        bindings[(prepared.planned_window.id,binding.entity_id)]=(record,binding,batch_record)
    nodes=[];fused_records={};complete=True
    for window in plan.windows:
        runner.check_cancelled(job)
        mr=muse.get(window.id);batch=None
        if mr:_,batch,_=load_muse_record(store,settings,project,run.id,mr.id)
        for step in ast.steps:
            chosen={e:bindings[(window.id,e)] for e in step.entities if (window.id,e) in bindings}
            geometry=None
            if chosen and set(chosen)==set(step.entities):
                geometry=publish_geometry(store,settings,project,run.id,step.id,
                    {e:(values[2].id,values[1].candidate_id) for e,values in chosen.items()})
            fused=publish_fused_observation(store,settings,project,run.id,window.id,step.id,window.source_interval,
                muse_id=mr.id if mr else None,geometry_id=geometry.id if geometry else None,
                binding_ids={e:values[0].id for e,values in chosen.items()})
            _,result,prepared=load_fused_observation(store,settings,project,run.id,fused.id)
            fused_records[fused.id]=fused
            complete=complete and result.coverage.sufficient_negative
            by_id={o.id:o for o in batch.observations} if batch else {}
            identity={e:str(values[1].segment_id) for e,values in chosen.items()}
            measurements={}
            if geometry:
                _,geometric,_,_=load_geometry(store,settings,project,run.id,geometry.id)
                measurements={m.id:m for m in geometric.measurements}
            supported_observations={i for support in [*result.supports,*result.estimated_candidates] for i in support.observation_ids}
            for verified,items in ((True,result.supports),(False,result.estimated_candidates)):
              for support in _support_groups(items):
                frames={f.evidence_frame_id:f for oid in support.observation_ids if oid in by_id for f in by_id[oid].frames
                    if support.interval.start_ms<=(f.observed_at_source_ms if f.observed_at_source_ms is not None else f.source_time_ms)<support.interval.end_ms}
                for measurement_id in support.geometry_ids:
                    measurement=measurements.get(measurement_id)
                    if measurement:
                        for index in {measurement.source_start_frame,measurement.source_end_frame}:
                            at=max(prepared.source.timeline.source_frame_times_ms[index],support.interval.start_ms)
                            if at<support.interval.end_ms:
                                witness=prepared.source.frame_witness(index,at)
                                frames[witness.evidence_frame_id]=witness
                reasons=list(result.reasons)
                if len(frames)>32:
                    values=sorted(frames.values(),key=lambda f:f.source_frame_index)
                    frames={values[round(i*(len(values)-1)/31)].evidence_frame_id:values[round(i*(len(values)-1)/31)] for i in range(32)}
                    reasons.append('DISPLAY_WITNESSES_SAMPLED; complete measurement/observation lineage retained in fused record')
                if frames:nodes.append(NodeEvidence(record_id=fused.id,step_id=step.id,verdict=Truth.TRUE if verified else Truth.UNKNOWN,interval=support.interval,
                    window=window.source_interval,frames=list(frames.values()),identity_bindings=identity,
                    identity_key=hashlib.sha256(encoded(identity)).hexdigest() if identity else None,estimated=support.estimated_boundaries,
                    observed_candidate=not verified,reasons=reasons))
            # Preserve source-grounded uncertain candidates for inspection. They
            # remain unknown even when the provider called them supported.
            if batch:
                for observation in batch.observations:
                    if (observation.id not in supported_observations and observation.step_id==step.id and observation.source_interval
                            and observation.frames and observation.hypothesis in ('supported','uncertain')):
                        nodes.append(NodeEvidence(record_id=fused.id,step_id=step.id,verdict=Truth.UNKNOWN,
                            interval=observation.source_interval,window=window.source_interval,frames=observation.frames,
                            identity_bindings=identity,observed_candidate=True,estimated=True,
                            reasons=list(dict.fromkeys([*result.reasons,*observation.uncertainty,'PROVIDER_HYPOTHESIS_REQUIRES_FUSION']))))
            if result.verdict=='false':nodes.append(NodeEvidence(record_id=fused.id,step_id=step.id,verdict=Truth.FALSE,
                window=window.source_interval,negative_complete=result.coverage.sufficient_negative,reasons=result.reasons))
    digest=hashlib.sha256(encoded({'nodes':[n.model_dump(mode='json') for n in nodes],
        'fused_inputs':[reference(r).model_dump(mode='json') for r in sorted(fused_records.values(),key=lambda r:str(r.id))],
        'evaluator_version':EVALUATOR_VERSION})).hexdigest()
    evaluation=evaluate_program(ast,nodes,run_id=uuid5(run.id,digest),source_duration_ms=plan.source_duration_ms,coverage_complete=complete)
    events=[]
    for cycle in evaluation.cycles:
        steps=[StepEvidence(step_id=s.step_id,label=next((getattr(x,'phrase',None) or x.source_wording or x.id) for x in ast.steps if x.id==s.step_id),
            verdict=s.verdict.value,interval=s.interval,reason='; '.join([*s.reasons,*(['Boundaries estimated from sampled evidence'] if s.estimated else [])])[:2000] or ('Insufficient evidence' if s.verdict==Truth.UNKNOWN else ''),
            frames=s.frames,observation_ids=s.evidence_ids) for s in cycle.steps]
        event=EventEvidence(origin='evaluated',label=f'{"Cycle" if cycle.verdict==Truth.TRUE else "Review candidate"} {len(events)+1}',interval=cycle.interval,verdict=cycle.verdict.value,
            steps=steps,review_reason='; '.join(cycle.reasons)[:2000] if cycle.verdict!=Truth.TRUE else '',
            coverage={'evaluator_version':EVALUATOR_VERSION,'source_complete':complete,'selected':cycle.selected,
                'rule_verdict':cycle.rule_verdict.value if cycle.rule_verdict else cycle.verdict.value,
                'selection_certain':cycle.selection_certain,'estimated':any(s.estimated for s in cycle.steps),
                'edges':[e.model_dump(mode='json') for e in cycle.edges],'exclusions':cycle.exclusions})
        dependencies=list(dict.fromkeys(i for s in cycle.steps for i in s.evidence_ids))
        if repo.path(project,'temporal_events',cycle.id).exists():record=repo.load(project,'temporal_events',cycle.id)
        else:
            MediaBudget(store.project_dir(project),settings).check(len(encoded(event.model_dump(mode='json')))+16384)
            artifacts={}
            for step in steps:
                for witness in step.frames:
                    image=store.load(project,'evidence',EvidenceFrame,witness.evidence_frame_id)
                    artifacts[image.image_path]=existing_artifact(store,project,image.image_path,'image/jpeg',expected_sha256=witness.image_sha256)
                    path=f'evidence/{witness.evidence_frame_id}.json'
                    artifacts[path]=existing_artifact(store,project,path,'application/json')
            record=repo.publish(Envelope(kind='temporal_events',id=cycle.id,project_id=project,source_asset_id=run.source_asset_id,
                source_sha256=run.source_sha256,program=run.program,run=reference(run),references=[reference(fused_records[i]) for i in dependencies],
                artifacts=list(artifacts.values()),payload=event.model_dump(mode='json')))
        events.append(record)
    trace_id=uuid5(run.id,'evaluation:'+digest)
    payload={'run_id':str(run.id),'outcome':evaluation.outcome,'coverage_complete':complete,'reasons':evaluation.reasons,
        'event_ids':[str(e.id) for e in events],'evaluator_version':EVALUATOR_VERSION,'input_fingerprint':digest,
        'graph':{'rule_nodes':[{'id':s.id,'label':getattr(s,'phrase',None) or s.source_wording or s.id,'predicate':s.predicate} for s in ast.steps],
                 'rule_edges':[r.model_dump(mode='json',by_alias=True) for r in ast.relations],
                 'entities':[e.model_dump(mode='json') for e in ast.entities],
                 'event_nodes':[{'event_id':str(c.id),'step_id':s.step_id,'verdict':s.verdict.value,
                     'evidence_ids':[str(i) for i in s.evidence_ids]} for c in evaluation.cycles for s in c.steps],
                 'event_edges':[{'event_id':str(c.id),'evidence_scope':'computed_temporal_relation',**e.model_dump(mode='json')} for c in evaluation.cycles for e in c.edges],
                 'identity_edges':[{'entity_id':entity,'type':'same_track','binding_id':str(value[0].id),
                     'segment_id':str(value[1].segment_id),'scope':'explicitly_selected_window_segment'} for (_,entity),value in bindings.items()],
                 'limitations':['No hidden contact, physical force or causation inferred','Provider IDs are not global identities']}}
    if not repo.path(project,'traces',trace_id).exists():
        MediaBudget(store.project_dir(project),settings).check(len(encoded(payload))+16384)
        covered={r.id for event in events for r in event.references if r.kind=='observations'}
        references=[reference(plan_record),*[reference(e) for e in events],
            *[reference(r) for identifier,r in fused_records.items() if identifier not in covered]]
        if len(references)>512:raise AppError('TRACE_LIMIT','Evaluation dependency trace exceeds the bounded repository quota; completed evidence/events are preserved',422)
        repo.publish(Envelope(kind='traces',id=trace_id,project_id=project,
            source_asset_id=run.source_asset_id,source_sha256=run.source_sha256,program=run.program,run=reference(run),
            references=references,payload=payload))
    return {**payload,'trace_id':str(trace_id)}
