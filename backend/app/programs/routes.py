"""Program compilation, inspection and review routes; no implicit run/action."""
from functools import partial
from uuid import UUID

from fastapi import Request, Query
from pydantic import Field

from ..errors import AppError
from ..models import Asset, Model, Project
from ..models import Job
from ..providers.meta import MetaCloud
from ..repository import Envelope, Repository, fingerprint
from ..requests import identify_request
from ..source_evidence import SourceEvidenceAdapter
from .compiler import compile_instruction
from .repository import ReviewRequest, approve_program, assert_reviewed, load_program, provider_recording, save_compilation
from .repository import ConfirmAssumptionsRequest, confirm_assumptions


class CompileRequest(Model):
    project_id: UUID
    source_asset_id: UUID
    instruction: str = Field(min_length=1,max_length=8000)


def compile_job(store, runner, settings, job):
    adapter=SourceEvidenceAdapter(store,settings,job.project_id,job.asset_id)
    asset=adapter.asset
    if asset.sha256 != job.request['source_sha256']:
        raise AppError('SOURCE_MISMATCH','Source changed after compilation was queued',409)
    metadata={'duration_ms':asset.metadata.duration_ms,'width':asset.metadata.geometry.display_width,
              'height':asset.metadata.geometry.display_height,'audio_present':asset.metadata.audio_present}
    artifact=None
    def record_response(output, provenance):
        nonlocal artifact
        artifact=provider_recording(store,asset,job.id,output,provenance,settings=settings)
    runner.stage(job,'compiling',None)
    try:
        with MetaCloud(settings) as cloud:
            compilation=compile_instruction(cloud,job.request['instruction'],metadata,record_response=record_response)
        runner.stage(job,'validating_program',None)
        record,program=save_compilation(store,asset,job.id,compilation,artifact)
        with store.mutation():
            project=store.load(job.project_id,'project',Project)
            project.active_compilation_id=record.id
            if program:project.active_program_id=program.id
            store.save(project.id,'project',project)
        return {'compilation_id':str(record.id),'program_id':str(program.id) if program else None,
                'version':program.version if program else None,'outcome':record.payload['outcome'],
                'program_fingerprint':fingerprint(program) if program else None}
    except AppError as exc:
        repo=Repository(store)
        if not repo.path(job.project_id,'compilations',job.id).exists():
            repo.publish(Envelope(kind='compilations',id=job.id,project_id=job.project_id,
                source_asset_id=asset.id,source_sha256=asset.sha256,artifacts=[artifact] if artifact else [],
                payload={'original_instruction':job.request['instruction'],'error':exc.envelope(),
                         'state':'provider_failure' if exc.code.startswith('PROVIDER_') else 'invalid_program'}))
        runner.checkpoint(job,{'compilation_id':str(job.id),'program_id':None})
        raise


def register_program_routes(app, settings):
    def owner(program_id):
        matches=list(app.state.store.projects.glob(f'*/programs/{program_id}/v*.json'))
        projects={p.parents[2].name for p in matches}
        if not projects:raise AppError('NOT_FOUND','Program not found',404)
        if len(projects)!=1:raise AppError('OWNERSHIP','Program identity occurs in conflicting projects',409)
        return next(iter(projects)),matches

    @app.post('/api/programs/compile',status_code=202)
    def compile_program(body:CompileRequest,request:Request):
        if not settings.model_api_key:
            raise AppError('PROVIDER_UNAVAILABLE','Meta API key is not configured on the server',503)
        if not body.instruction.strip():
            raise AppError('INVALID_INSTRUCTION','Instruction cannot be blank',422)
        store,runner=app.state.store,app.state.runner
        with runner.lock,store.mutation():
            project=store.load(body.project_id,'project',Project)
            asset=store.load(body.project_id,'assets',Asset,body.source_asset_id)
            if asset.status!='ready':raise AppError('MEDIA_NOT_READY','Prepare the source before compiling',409)
            payload={**body.model_dump(mode='json'),'source_sha256':asset.sha256}
            payload,cached=identify_request(store,body.project_id,'compile',payload,request.headers.get('idempotency-key'),settings)
            if cached:return {'job_id':cached.id,'compilation_id':cached.id,'reused':True}
            job=Job(project_id=body.project_id,asset_id=asset.id,type='compile',request=payload)
            runner.submit(job,partial(compile_job,store,runner,settings))
            project.active_job_id=job.id;project.status='processing'
            store.save(project.id,'project',project)
        return {'job_id':job.id,'compilation_id':job.id,'reused':False}

    @app.get('/api/programs/{program_id}')
    def inspect_program(program_id:UUID,version:int|None=Query(default=None,ge=1)):
        project_id,paths=owner(program_id)
        try:versions=sorted({int(p.stem[1:]) for p in paths})
        except ValueError:raise AppError('CORRUPT_PROGRAM','Program version filename is invalid',500) from None
        selected=version or versions[-1]
        envelope,data=load_program(app.state.store,project_id,program_id,selected)
        approved=False
        try:
            assert_reviewed(app.state.store,envelope);approved=True
        except AppError as exc:
            if exc.code not in ('PROGRAM_REVIEW_REQUIRED','PROGRAM_NOT_REVIEWABLE'):raise
        return {'program':data.program,'outcome':data.outcome,'versions':versions,
                'compilation_id':data.compilation_id,'program_fingerprint':fingerprint(envelope),
                'review_approved':approved,'human_edit':data.human_edit,
                'compilation_origin':Repository(app.state.store).load(project_id,'compilations',data.compilation_id).payload['compilation']['compiler']['origin']}

    @app.post('/api/programs/{program_id}/confirm-assumptions',status_code=201)
    def resolve_assumptions(program_id:UUID,body:ConfirmAssumptionsRequest):
        project_id,_=owner(program_id)
        program,data=confirm_assumptions(app.state.store,settings,project_id,program_id,body)
        return {'program_id':program_id,'version':program.version,'program_fingerprint':fingerprint(program),
                'outcome':data.outcome,'review_approved':False}

    @app.post('/api/programs/{program_id}/review',status_code=201)
    def review_program(program_id:UUID,body:ReviewRequest):
        project_id,_=owner(program_id)
        review=approve_program(app.state.store,settings,project_id,program_id,body)
        return {'review_id':review.id,'program_id':program_id,'version':body.version,
                'program_fingerprint':body.program_fingerprint,'decision':'approved'}

    @app.get('/api/compilations/{compilation_id}')
    def inspect_compilation(compilation_id:UUID):
        paths=list(app.state.store.projects.glob(f'*/compilations/{compilation_id}.json'))
        if not paths:raise AppError('NOT_FOUND','Compilation not found',404)
        if len(paths)!=1:raise AppError('OWNERSHIP','Compilation has conflicting owners',409)
        return Repository(app.state.store).load(paths[0].parent.parent.name,'compilations',compilation_id)
