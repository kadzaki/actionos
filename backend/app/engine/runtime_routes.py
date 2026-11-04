"""Reviewed version-bound runs, deterministic replay and explicit output actions."""
from typing import Literal
from uuid import UUID

from fastapi import Query
from pydantic import Field

from .runtime import observe_run,evaluate_run,records
from .temporal import EVALUATOR_VERSION
from .muse_observations import COMPLETE_OBSERVATION_ADAPTER_VERSION
from .entity_tracks import ENTITY_ADAPTER_VERSION
from .event_corrections import CorrectionRequest,correct
from .reuse import replay_run,check_compatible,parent_artifacts
from .tools import BoundedToolRegistry,NoArguments,for_job
from ..actions.registry import build_action_plan
from ..actions.contracts import ActionRequest
from ..actions.execution import request_execution
from ..errors import AppError
from ..models import Model,Project
from ..programs.repository import assert_reviewed
from ..repository import Repository,fingerprint,reference
from ..runs import snapshot_run,RunDispatcher


class RunRequest(Model):
    version:int=Field(ge=1,strict=True)
    program_fingerprint:str=Field(pattern=r'^[0-9a-f]{64}$')
    mode:Literal['observe_evaluate','plan_only','reuse']='observe_evaluate'
    parent_run_id:UUID|None=None


class LocalArguments(Model):
    pass


def register_runtime_routes(app,settings):
    def owner(kind,record_id):
        suffix=f'programs/{record_id}/v*.json' if kind=='programs' else f'{kind}/{record_id}.json'
        matches=list(app.state.store.projects.glob('*/'+suffix))
        projects={p.parents[2].name if kind=='programs' else p.parents[1].name for p in matches}
        if len(projects)!=1:raise AppError('NOT_FOUND','Record not found or ambiguous ownership',404)
        return next(iter(projects))

    def run_record(run_id):
        return Repository(app.state.store).load(owner('runs',run_id),'runs',run_id)

    def submit(run,operation,stage):
        store=app.state.store;runner=app.state.runner
        with runner.lock,store.mutation():
            def execute(snapshot,current):
                name='run_perception' if stage=='observe' else 'evaluate_rule'
                registry=for_job(store,runner,settings,current,run)
                if operation==replay_run:
                    registry.handlers[name]=(NoArguments,lambda args:operation(store,runner,settings,run,current))
                return registry.dispatch(name,{},allowed={'reviewed_cloud'} if stage=='observe' else {'local'})
            job=RunDispatcher(store,runner,settings).submit(run.project_id,run.id,execute,stage=stage)
            project=store.load(run.project_id,'project',Project)
            project.active_run_id=run.id;project.active_job_id=job.id;project.status='processing'
            store.save(project.id,'project',project)
        return {'run_id':run.id,'job_id':job.id,'program':run.program}

    @app.post('/api/programs/{program_id}/run',status_code=202)
    def start(program_id:UUID,body:RunRequest):
        store=app.state.store;project_id=owner('programs',program_id)
        with app.state.runner.lock,store.mutation():
            if app.state.runner.has_active(project_id):raise AppError('PROJECT_BUSY','Project is already processing',409,True)
            program=Repository(store).load(project_id,'programs',program_id,body.version)
            if fingerprint(program)!=body.program_fingerprint:raise AppError('STALE_VERSION','Inspect this exact version before running',409)
            assert_reviewed(store,program)
            parent=None;artifacts=[]
            if body.mode=='reuse':
                if not body.parent_run_id:raise AppError('PARENT_RUN_REQUIRED','Select a source-compatible saved parent run',422)
                parent=Repository(store).load(project_id,'runs',body.parent_run_id)
                check_compatible(store,project_id,program,parent)
                artifacts=parent_artifacts(store,parent)
            if body.mode=='observe_evaluate' and not settings.model_api_key:raise AppError('PROVIDER_UNAVAILABLE','Configure the server key before starting cloud observation',503)
            run=snapshot_run(store,settings,project_id,program_id,body.version,
                muse_adapter_version=COMPLETE_OBSERVATION_ADAPTER_VERSION,sam_adapter_version=ENTITY_ADAPTER_VERSION,evaluator_version=EVALUATOR_VERSION,
                reuse_parent=parent,reuse_artifacts=artifacts)
            if body.mode=='plan_only':
                from .planning import plan_run
                plan=plan_run(store,settings,project_id,run.id)
                return {'run_id':run.id,'plan_id':plan.id,'job_id':None}
            return submit(run,replay_run if parent else observe_run,'evaluate' if parent else 'observe')

    @app.post('/api/jobs/{job_id}/cancel')
    def cancel(job_id:UUID):return app.state.runner.cancel(job_id)

    @app.post('/api/jobs/{job_id}/retry',status_code=202)
    def retry(job_id:UUID):
        job=app.state.runner.find(job_id)
        if job.status not in ('failed','cancelled','interrupted'):raise AppError('RETRY_UNAVAILABLE','Retry applies to failed, cancelled or interrupted jobs',409)
        if job.type=='evaluate' and job.run_id:return submit(run_record(job.run_id),evaluate_run,'evaluate')
        if job.type=='observe' and job.run_id:
            run=run_record(job.run_id)
            return start(run.program.id,RunRequest(version=run.program.version,program_fingerprint=run.program.record_sha256))
        if job.type in ('action','report') and job.action_id:
            plan=Repository(app.state.store).load(job.project_id,'action_plans',job.action_id)
            next_job,reused=request_execution(app.state.store,app.state.runner,settings,plan)
            return {'job_id':next_job.id,'reused':reused}
        raise AppError('RETRY_UNAVAILABLE','Repeat the original request after inspecting its source and version',409)

    @app.get('/api/projects/{project_id}/runs')
    def history(project_id:UUID,offset:int=Query(0,ge=0),limit:int=Query(25,ge=1,le=50)):
        store=app.state.store;repo=Repository(store);store.load(project_id,'project',Project)
        paths=sorted(store.path(project_id,'runs').glob('*.json'),key=lambda p:p.stat().st_mtime_ns,reverse=True)
        return {'items':[repo.load(project_id,'runs',p.stem) for p in paths[offset:offset+limit]],
            'next_offset':offset+limit if offset+limit<len(paths) else None}

    @app.get('/api/runs/{run_id}')
    def inspect(run_id:UUID):return run_record(run_id)

    @app.post('/api/runs/{run_id}/evaluate',status_code=202)
    def evaluate_saved(run_id:UUID):return submit(run_record(run_id),evaluate_run,'evaluate')

    @app.get('/api/runs/{run_id}/events')
    def events(run_id:UUID,offset:int=Query(0,ge=0),limit:int=Query(25,ge=1,le=50)):
        run=run_record(run_id);store=app.state.store;repo=Repository(store)
        traces=[r for r in records(store,run.project_id,'traces',run) if 'event_ids' in r.payload]
        latest=traces[-1] if traces else None
        ids=latest.payload['event_ids'] if latest else []
        return {'run':reference(run),'evaluation':latest.payload if latest else None,
            'items':[{**record.model_dump(mode='json'),'record_sha256':fingerprint(record)}
                for record in [repo.load(run.project_id,'temporal_events',i) for i in ids[offset:offset+limit]]],
            'next_offset':offset+limit if offset+limit<len(ids) else None}

    @app.get('/api/runs/{run_id}/tracks')
    def tracks(run_id:UUID,offset:int=Query(0,ge=0),limit:int=Query(25,ge=1,le=50)):
        run=run_record(run_id);items=records(app.state.store,run.project_id,'entity_tracks',run)
        return {'items':items[offset:offset+limit],'next_offset':offset+limit if offset+limit<len(items) else None}

    @app.get('/api/runs/{run_id}/trace')
    def trace(run_id:UUID):
        run=run_record(run_id)
        return {'items':records(app.state.store,run.project_id,'traces',run)}

    @app.post('/api/runs/{run_id}/actions',status_code=202)
    def actions(run_id:UUID,body:ActionRequest):
        run=run_record(run_id);store=app.state.store
        plan=build_action_plan(store,settings,run.project_id,run.id,body.model_dump(mode='json'),initiated=True)
        job,reused=request_execution(store,app.state.runner,settings,plan)
        return {'action_id':plan.id,'job_id':job.id,'reused':reused}

    @app.get('/api/runs/{run_id}/outputs')
    def outputs(run_id:UUID):
        run=run_record(run_id)
        return {'items':records(app.state.store,run.project_id,'action_results',run)}

    @app.post('/api/runs/{run_id}/events/{event_id}/corrections')
    def correction(run_id:UUID,event_id:UUID,body:CorrectionRequest):
        run=run_record(run_id)
        if app.state.runner.has_active(run.project_id):raise AppError('PROJECT_BUSY','Wait for the current job before editing evidence',409)
        return correct(app.state.store,settings,run,event_id,body)
