from contextlib import asynccontextmanager
from functools import partial
import hashlib
import logging
import os
from pathlib import Path
from uuid import UUID, uuid4
from urllib.parse import unquote
from typing import Literal

from fastapi import FastAPI, Request, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.requests import ClientDisconnect

from .config import Settings
from .errors import AppError
from .ingest import ingest
from .jobs import JobRunner
from .media import media_tools
from .models import Asset, EvidenceFrame, Job, Project, ProjectCreate, ProjectPreferences,ActionEvent,ActionIntent,Model
from .editing import Track
from pydantic import Field
from .storage import FileStore,file_sha256
from .streaming import media_response,lease_response
from .editing import Export
from .rendering import load_plan
from .evidence import evidence_frame,evidence_at_working_time,read_timeline
from .resources import MediaBudget
from .analysis import analyze as analyze_source, choose_events
from .director import make_reels
from .providers.status import read_status, read_contract_status
from .programs.compiler import COMPILER_CONTRACT_VERSION
from .corrections import EventCorrection, correct_event, latest_events
from .revisions import RevisionRequest, revise
from .requests import identify_request
from .targeting import TargetRequest, select_target
from .request_limits import JsonBodyLimit
from .validation import owned_record
from .layout import source_path
from .repository import Repository
from .programs.routes import register_program_routes
from .engine.entity_routes import register_entity_routes
from .engine.evidence_routes import register_evidence_routes
from .engine.runtime_routes import register_runtime_routes
from .programs.revision_routes import register_revision_routes


class ReelRequest(Model):
    event_id: UUID | None = None
    track_id: UUID | None = None
    requested_duration_ms: int = Field(default=20000,ge=1000,le=120000)
    preview: bool = False
    allow_fixed_crop: bool = False
    regenerate: bool = False

log = logging.getLogger(__name__)


def require_media_tools():
    if not all(media_tools().values()):
        raise AppError('MEDIA_TOOLS_UNAVAILABLE','Install FFmpeg and FFprobe on the server before processing video',503)


def create_app(settings: Settings | None = None):
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app):
        store = FileStore(settings.data_dir)
        store.acquire_process_lock()
        runner = JobRunner(store, settings.max_queued_jobs)
        app.state.store = store
        app.state.runner = runner
        app.state.uploading = set()
        try:
            runner.recover()
            yield
        finally:
            await run_in_threadpool(runner.close)
            store.release_process_lock()

    app = FastAPI(title="ActionOS", version="0.1.0", lifespan=lifespan)
    app.add_middleware(JsonBodyLimit)

    @app.exception_handler(AppError)
    async def app_error(request, exc):
        return JSONResponse(exc.envelope(), status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # Validation inputs can contain prompts or credentials. Return field names only.
        return JSONResponse(AppError("VALIDATION_ERROR", "Request contains invalid fields", 422,
                                     details={"fields": [".".join(map(str, e["loc"])) for e in exc.errors()]}).envelope(), status_code=422)

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        return JSONResponse(AppError("NOT_FOUND" if exc.status_code == 404 else "HTTP_ERROR",
                                     "Resource not found" if exc.status_code == 404 else "Request could not be processed",
                                     exc.status_code).envelope(), status_code=exc.status_code)

    @app.get("/api/health")
    def health():
        tools = media_tools()
        return {"status": "ok" if all(tools.values()) and not app.state.runner.recovery_errors else "degraded", "media_tools": tools,
                "storage": "json_files", "recovery_errors": app.state.runner.recovery_errors}

    @app.get("/api/capabilities")
    def capabilities():
        configured = bool(settings.model_api_key)
        media_ready=all(media_tools().values())
        status=read_status(settings) if configured else {}
        contracts=read_contract_status(settings) if configured else {}
        def provider(model):
            verified=model in status
            return {'model':model,'configured':configured,'verified':verified,'available':verified,
                    'reason':'Meta cloud access verified' if verified else ('Configured; access is checked on the next cloud request' if configured else 'Meta API key is not configured')}
        return {"muse":provider(settings.muse_model),"sam":provider(settings.sam_model),
                "studio": {
                    "source": {"available": media_ready, "reason": "Local upload and source evidence" if media_ready else "FFmpeg and FFprobe are required"},
                    "program": {"available": True, "reason": "Compile and inspect a program; cloud access is checked on each request" if configured else "Inspect saved programs; configure the server-side Meta key to compile"},
                    "run": {"available": configured and media_ready, "reason": "Run sends source windows to Meta; outputs require a separate explicit action" if configured else "Configure the server key to observe; saved runs can be evaluated locally"},
                    "report": {"available": media_ready, "reason": "Select evaluated events to create clips or a timestamp report"}},
                "provider_contracts": {"compiler": "verified" if contracts.get('compiler',{}).get('version')==COMPILER_CONTRACT_VERSION else "not_tested", "predicate_observations": "not_tested"},
                "operations": {"upload": media_ready, "analyze": configured and media_ready, "render": media_ready, "revise": configured and media_ready, "compile": configured},
                "program_operations": {"compile": configured, "observe": configured and media_ready, "evaluate_saved": media_ready,
                    "extract_clip": media_ready, "create_report": media_ready, "compare_events": False, "isolate_subject": False},
                "limits": {"max_upload_mb": settings.max_upload_mb, "max_video_seconds": settings.max_video_seconds},
                "action_scope": "any_action_prompt", "storage": "json_files"}

    @app.post("/api/projects", status_code=201)
    def create_project(body: ProjectCreate):
        project = Project(**body.model_dump())
        app.state.store.create_project(project)
        return {"project_id": project.id, "project": project}

    @app.get("/api/projects")
    def list_projects():
        projects,errors=app.state.store.scan_projects()
        return {'projects':projects,'errors':errors}

    @app.get("/api/projects/{project_id}")
    def get_project(project_id: UUID):
        store = app.state.store
        project = store.load(project_id, "project", Project)
        asset = store.load(project_id, "assets", Asset, project.active_asset_id) if project.active_asset_id else None
        return {"project": project, "asset": asset}

    @app.patch("/api/projects/{project_id}")
    def preferences(project_id: UUID, body: ProjectPreferences):
        store = app.state.store
        with store.mutation():
            project = store.load(project_id, "project", Project)
            project.action_prompt = body.action_prompt
            project.requested_duration_ms = body.requested_duration_ms
            store.save(project_id, "project", project)
        return project

    @app.delete("/api/projects/{project_id}", status_code=204)
    def delete_project(project_id: UUID):
        runner = app.state.runner
        with runner.lock, app.state.store.mutation():
            if str(project_id) in app.state.uploading or runner.has_active(project_id):
                raise AppError("PROJECT_BUSY", "Wait for upload or processing to finish before deleting", 409, True)
            app.state.store.delete_project(project_id)

    @app.post("/api/projects/{project_id}/upload", status_code=202)
    async def upload(project_id: UUID, request: Request):
        store, runner = app.state.store, app.state.runner
        if request.headers.get("content-type", "").split(";")[0].lower() not in ("video/mp4", "video/quicktime", "application/octet-stream"):
            raise AppError("UNSUPPORTED_INPUT", "Send an MP4/MOV file as the raw request body", 415)
        declared = request.headers.get("content-length")
        if declared:
            try:
                declared_size = int(declared)
            except ValueError:
                raise AppError("INVALID_LENGTH", "Invalid upload length", 400) from None
            if declared_size < 0 or declared_size > settings.max_upload_bytes:
                raise AppError("UPLOAD_TOO_LARGE", f"Upload must be at most {settings.max_upload_mb} MB", 413)
        with runner.lock, store.mutation():
            project = store.load(project_id, "project", Project)
            if str(project_id) in app.state.uploading or runner.has_active(project_id):
                raise AppError("PROJECT_BUSY", "This project is already processing", 409, True)
            if project.active_asset_id:
                asset = store.load(project_id, "assets", Asset, project.active_asset_id)
                if asset.status != "failed":
                    raise AppError("SOURCE_EXISTS", "Create a new project for another source", 409)
            app.state.uploading.add(str(project_id))
        asset_id = uuid4()
        temporary = store.path(project_id, f"tmp/{asset_id}.upload")
        source_relative = source_path(asset_id)
        source = store.path(project_id, source_relative)
        size, digest = 0, hashlib.sha256()
        published = False
        try:
            budget=MediaBudget(store.project_dir(project_id),settings)
            await run_in_threadpool(budget.check)
            temporary.parent.mkdir(parents=True, exist_ok=True)
            with temporary.open("xb") as output:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > settings.max_upload_bytes:
                        raise AppError("UPLOAD_TOO_LARGE", f"Upload must be at most {settings.max_upload_mb} MB", 413)
                    await run_in_threadpool(budget.check,len(chunk))
                    digest.update(chunk)
                    await run_in_threadpool(output.write, chunk)
                output.flush()
                os.fsync(output.fileno())
            if not size:
                raise AppError("EMPTY_UPLOAD", "Choose a nonempty MP4/MOV file", 422)
            if declared is not None and size != int(declared):
                raise AppError("INVALID_LENGTH", "Upload was incomplete", 400, True)
            source.parent.mkdir(parents=True, exist_ok=True)
            os.replace(temporary, source)
            asset = Asset(id=asset_id, project_id=project_id, source_path=source_relative, media_layout='actionos',
                          original_name=unquote(request.headers.get("x-file-name", "source"))[:200],
                          byte_size=size, sha256=digest.hexdigest())
            job = Job(project_id=project_id, type="ingest", asset_id=asset.id)
            # Records precede dispatch. Hold runner lock so completion cannot race setup/deletion.
            with runner.lock, store.mutation():
                store.save(project_id, "assets", asset)
                project = store.load(project_id, "project", Project)
                project.active_asset_id = asset.id
                project.active_intent_id = None
                project.active_job_id = job.id
                project.status = "processing"
                store.save(project_id, "project", project)
                try:
                    runner.submit(job, partial(ingest, store, runner, settings))
                except AppError:
                    asset.status = "failed"
                    store.save(project_id, "assets", asset)
                    project.status = "failed"
                    project.active_job_id = None
                    store.save(project_id, "project", project)
                    raise
                published = True
            return {"asset_id": asset.id, "job_id": job.id}
        except ClientDisconnect:
            raise AppError("UPLOAD_INTERRUPTED", "Upload interrupted; try again", 400, True) from None
        finally:
            temporary.unlink(missing_ok=True)
            if not published:
                source.unlink(missing_ok=True)
            with runner.lock:
                app.state.uploading.discard(str(project_id))

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: UUID):
        return app.state.runner.find(job_id)

    @app.get("/api/plans/{plan_id}")
    def get_plan(plan_id: UUID):
        return load_plan(app.state.store, plan_id)

    @app.post('/api/plans/{plan_id}/revise', status_code=202, deprecated=True, tags=['Legacy VideoCut'])
    def revise_plan(plan_id: UUID, body: RevisionRequest, request: Request):
        require_media_tools()
        if not settings.model_api_key:
            raise AppError('PROVIDER_UNAVAILABLE','Meta API key is not configured on the server',503)
        store, runner = app.state.store, app.state.runner
        with runner.lock, store.mutation():
            parent = load_plan(store, plan_id)
            if not parent.intent_id:
                raise AppError('ANALYSIS_REQUIRED','Choose an action-directed plan to revise',409)
            project = store.load(parent.project_id,'project',Project)
            payload={**body.model_dump(mode='json'),'parent_plan_id':str(parent.id),'source_asset_id':str(parent.source_asset_id)}
            payload,cached=identify_request(store,project.id,'revise',payload,request.headers.get('idempotency-key'),settings)
            if cached:return {'job_id':cached.id,'reused':True}
            job = Job(project_id=project.id,asset_id=parent.source_asset_id,type='revise',
                      request=payload)
            runner.submit(job,partial(revise,store,runner,settings))
            project.active_job_id=job.id;project.status='processing'
            store.save(project.id,'project',project)
        return {'job_id':job.id}

    @app.get('/api/projects/{project_id}/plans')
    def project_plans(project_id: UUID):
        store=app.state.store
        store.load(project_id,'project',Project)
        return {'plans': [load_plan(store,path.name) for path in store.project_dir(project_id).joinpath('plans').glob('*') if path.is_dir()]}

    @app.get("/api/projects/{project_id}/exports")
    def get_exports(project_id: UUID):
        exports = app.state.store.list_records(project_id, "exports", Export)
        reports, report_errors = [], []
        repo = Repository(app.state.store)
        for path in sorted(app.state.store.path(project_id, 'reports').glob('*.json')):
            try:
                report = repo.load(project_id, 'reports', path.stem)
            except AppError as exc:
                report_errors.append({**exc.envelope(), 'report_id': path.stem})
                continue
            reports.append({'report_id': str(report.id), 'title': report.payload.get('title'),
                'fixture_only': report.payload.get('fixture_only'),
                'view_url': f'/api/reports/{report.id}/view', 'download_url': f'/api/reports/{report.id}/download'})
        return {"reports": reports, "report_errors": report_errors, "exports": [{**item.model_dump(mode="json"),
                              "stream_url": f"/api/exports/{item.id}/stream",
                              "download_url": f"/api/exports/{item.id}/download"}
                             for item in sorted(exports,key=lambda item:item.created_at,reverse=True)
                             if str(item.id) not in app.state.runner.unavailable_exports
                             if app.state.store.path(project_id,item.path).is_file()
                             and app.state.store.path(project_id,item.manifest_path).is_file()]}

    def find_report(report_id, media_type):
        store = app.state.store
        for path in store.projects.glob(f'*/reports/{report_id}.json'):
            report = Repository(store).load(path.parent.parent.name, 'reports', report_id)
            artifact = next((a for a in report.artifacts if a.media_type == media_type), None)
            if artifact is None:
                raise AppError('CORRUPT_ARTIFACT', 'Report is missing its required representation', 409)
            return report, store.path(report.project_id, artifact.path)
        raise AppError('NOT_FOUND', 'Report not found', 404)

    @app.get('/api/reports/{report_id}/view')
    def view_report(report_id: UUID):
        report, path = find_report(report_id, 'text/html')
        return lease_response(app.state.store, report.project_id, lambda: FileResponse(path, media_type='text/html',
            headers={'Content-Security-Policy': "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'",
                     'X-Content-Type-Options': 'nosniff'}))

    @app.get('/api/reports/{report_id}/download')
    def download_report(report_id: UUID):
        report, path = find_report(report_id, 'application/json')
        return lease_response(app.state.store, report.project_id, lambda: FileResponse(path, media_type='application/json',
            filename=f'actionos-report-{report.id}.json', headers={'X-Content-Type-Options': 'nosniff'}))

    def find_export(export_id: UUID):
        store = app.state.store
        if str(export_id) in app.state.runner.unavailable_exports:
            raise AppError("EXPORT_UNAVAILABLE","Saved export failed startup reconciliation; rerender from source",409,True)
        for path in store.projects.glob(f"*/exports/{export_id}.json"):
            export = store.load(path.parent.parent.name,"exports",Export,export_id)
            output = store.path(export.project_id,export.path)
            if not output.is_file() or not store.path(export.project_id,export.manifest_path).is_file():
                raise AppError("MEDIA_MISSING","Verified export artifacts are missing",404)
            return export,output
        raise AppError("NOT_FOUND","Export not found",404)

    @app.get("/api/exports/{export_id}/stream")
    @app.head("/api/exports/{export_id}/stream")
    def stream_export(export_id: UUID,request: Request):
        export,path = find_export(export_id)
        return lease_response(app.state.store,export.project_id,
                              lambda:media_response(path,request.headers.get("range"),request.method=="HEAD"))

    @app.get("/api/exports/{export_id}/download")
    def download_export(export_id: UUID):
        export,path = find_export(export_id)
        return lease_response(app.state.store,export.project_id,
                              lambda:FileResponse(path,media_type="video/mp4",filename=f"videocut-{export.id}.mp4"))

    @app.get("/api/projects/{project_id}/assets/{asset_id}")
    def get_asset(project_id: UUID, asset_id: UUID):
        return app.state.store.load(project_id, "assets", Asset, asset_id)

    def find_asset(asset_id: UUID):
        store = app.state.store
        for path in store.projects.glob(f"*/assets/{asset_id}.json"):
            return store.load(path.parent.parent.name, "assets", Asset, asset_id)
        raise AppError("NOT_FOUND", "Asset not found", 404)

    @app.get("/api/assets/{asset_id}/stream")
    @app.head("/api/assets/{asset_id}/stream")
    def stream_asset(asset_id: UUID, request: Request, variant: str = "preview"):
        asset = find_asset(asset_id)
        if variant not in ("preview", "working", "original"):
            raise AppError("INVALID_VARIANT", "Choose preview, working, or original", 422)
        relative = {"preview": asset.proxy_path, "working": asset.working_path, "original": asset.source_path}[variant]
        if asset.status != "ready" or not relative:
            raise AppError("MEDIA_NOT_READY", "Source preview is not ready", 409, True)
        path = app.state.store.path(asset.project_id, relative)
        if not path.is_file():
            raise AppError("MEDIA_MISSING", "Stored media is missing", 404)
        return lease_response(app.state.store,asset.project_id,
                              lambda:media_response(path,request.headers.get("range"),request.method=="HEAD"))

    @app.get("/api/assets/{asset_id}/thumbnail")
    def get_thumbnail(asset_id: UUID):
        asset = find_asset(asset_id)
        if asset.status != "ready" or not asset.thumbnail_path:
            raise AppError("MEDIA_NOT_READY", "Thumbnail is not ready", 409, True)
        return lease_response(app.state.store,asset.project_id,
                              lambda:FileResponse(app.state.store.path(asset.project_id,asset.thumbnail_path),media_type="image/jpeg"))

    @app.get("/api/assets/{asset_id}/timeline")
    def get_timeline(asset_id: UUID):
        asset=find_asset(asset_id)
        if asset.status!="ready":
            raise AppError("MEDIA_NOT_READY","Source timeline is not ready",409,True)
        with app.state.store.media_lease(asset.project_id),app.state.store.artifact_lock:
            return read_timeline(app.state.store,asset,settings)

    @app.get("/api/assets/{asset_id}/frames/{time_ms}")
    def get_frame(asset_id: UUID,time_ms: int,space: Literal['source','working']='source'):
        asset=find_asset(asset_id)
        working_index=None
        if space=='working':
            frame,working_index=evidence_at_working_time(app.state.store,settings,asset,time_ms)
        else:
            frame=evidence_frame(app.state.store,settings,asset,time_ms)
        return {"requested_time_ms":time_ms,"requested_space":space,
                "working_frame_index":working_index,"frame":frame,
                "image_url":f"/api/evidence/{frame.id}/image"}

    @app.get("/api/evidence/{frame_id}/image")
    def get_frame_image(frame_id: UUID):
        store=app.state.store
        for path in store.projects.glob(f"*/evidence/{frame_id}.json"):
            frame=store.load(path.parent.parent.name,"evidence",EvidenceFrame,frame_id)
            asset=store.load(frame.project_id,"assets",Asset,frame.source_asset_id)
            if asset.sha256!=frame.source_sha256:
                raise AppError("CORRUPT_RECORD","Frame evidence does not match source lineage",500)
            image_path=store.path(frame.project_id,frame.image_path)
            if not image_path.is_file():
                raise AppError("MEDIA_MISSING","Source evidence image is missing",404)
            if file_sha256(image_path)!=frame.image_sha256:
                raise AppError("CORRUPT_ARTIFACT","Source evidence image failed its checksum",500)
            return lease_response(store,frame.project_id,
                                  lambda:FileResponse(image_path,media_type="image/jpeg",headers={
                                      "X-Source-Time-Ms":str(frame.source_time_ms),
                                      "X-Source-Frame-Index":str(frame.source_frame_index),
                                      "Cache-Control":"private, max-age=0, must-revalidate"}))
        raise AppError("NOT_FOUND","Frame evidence not found",404)

    @app.post("/api/projects/{project_id}/analyze", deprecated=True, tags=['Legacy VideoCut'])
    def analyze(project_id: UUID, body: ProjectPreferences, request: Request):
        require_media_tools()
        if not settings.model_api_key:
            raise AppError('PROVIDER_UNAVAILABLE','Meta API key is not configured on the server',503)
        store,runner=app.state.store,app.state.runner
        with runner.lock,store.mutation():
            project=store.load(project_id,'project',Project)
            if not project.active_asset_id:raise AppError('MEDIA_NOT_READY','Upload source footage first',409)
            asset=store.load(project_id,'assets',Asset,project.active_asset_id)
            if asset.status!='ready':raise AppError('MEDIA_NOT_READY','Prepare source footage first',409)
            payload={**body.model_dump(mode='json'),'source_sha256':asset.sha256,'source_asset_id':str(asset.id)}
            payload,cached=identify_request(store,project.id,'analyze',payload,request.headers.get('idempotency-key'),settings)
            if cached:return {'job_id':cached.id,'reused':True}
            if runner.has_active(project_id):raise AppError('PROJECT_BUSY','This project is already processing',409,True)
            project.action_prompt=body.action_prompt
            project.requested_duration_ms=body.requested_duration_ms
            job=Job(project_id=project_id,asset_id=asset.id,type='analyze',request=payload)
            runner.submit(job,partial(analyze_source,store,runner,settings))
            project.active_job_id=job.id;project.status='processing'
            store.save(project_id,'project',project)
        return {'job_id':job.id}

    @app.get('/api/projects/{project_id}/events', deprecated=True, tags=['Legacy VideoCut'])
    def get_events(project_id: UUID,offset: int | None = Query(default=None,ge=0),limit: int = Query(default=12,ge=1,le=100)):
        store=app.state.store
        project=store.load(project_id,'project',Project)
        intent=store.load(project_id,'intents',ActionIntent,project.active_intent_id) if project.active_intent_id else None
        events=latest_events([e for e in store.list_records(project_id,'events',ActionEvent) if intent and e.intent_id==intent.id])
        events=sorted(events,key=lambda e:(e.start_ms,str(e.id)))
        selected=choose_events(intent,events) if intent else []
        page_offset=offset
        if page_offset is None:
            page_offset=(events.index(selected[0])//limit)*limit if selected else 0
        page_offset=min(page_offset,((len(events)-1)//limit)*limit) if events else 0
        next_offset=page_offset+limit if page_offset+limit<len(events) else None
        return {'events':events[page_offset:page_offset+limit],'intent':intent,
                'total_count':len(events),'offset':page_offset,'limit':limit,'next_offset':next_offset,
                'selected_event_ids':[str(e.id) for e in selected],
                'scan_complete':bool(intent and intent.provider_metadata.get('scan_complete')),
                'review_required':any(e.review_required for e in events)}

    @app.get('/api/projects/{project_id}/tracks', deprecated=True, tags=['Legacy VideoCut'])
    def get_tracks(project_id: UUID):
        tracks,errors=app.state.store.scan_records(project_id,'tracks',Track)
        return {'tracks':tracks,'errors':errors}

    @app.post('/api/projects/{project_id}/events/{event_id}/corrections', status_code=201)
    def save_correction(project_id: UUID, event_id: UUID, body: EventCorrection):
        store, runner = app.state.store, app.state.runner
        with runner.lock, store.mutation():
            if runner.has_active(project_id):
                raise AppError('PROJECT_BUSY', 'Wait for processing before correcting evidence', 409, True)
            return correct_event(store, settings, project_id, event_id, body)

    @app.post('/api/projects/{project_id}/events/{event_id}/track',status_code=202)
    def track_target(project_id: UUID,event_id: UUID,body: TargetRequest,request: Request):
        require_media_tools()
        if not settings.model_api_key:
            raise AppError('PROVIDER_UNAVAILABLE','Meta API key is not configured on the server',503)
        store,runner=app.state.store,app.state.runner
        with runner.lock,store.mutation():
            project=store.load(project_id,'project',Project)
            event=store.load(project_id,'events',ActionEvent,event_id)
            payload={'event_id':str(event_id),'selection':body.model_dump(mode='json')}
            payload,cached=identify_request(store,project.id,'track',payload,request.headers.get('idempotency-key'),settings)
            if cached:return {'job_id':cached.id,'reused':True}
            job=Job(project_id=project.id,asset_id=event.source_asset_id,type='track',request=payload)
            runner.submit(job,partial(select_target,store,runner,settings))
            project.active_job_id=job.id;project.status='processing'
            store.save(project.id,'project',project)
        return {'job_id':job.id}

    @app.post('/api/projects/{project_id}/reels',status_code=202, deprecated=True, tags=['Legacy VideoCut'])
    def generate_reels(project_id: UUID,body: ReelRequest,request: Request):
        require_media_tools()
        store,runner=app.state.store,app.state.runner
        with runner.lock,store.mutation():
            project=store.load(project_id,'project',Project)
            if not project.active_asset_id or not project.active_intent_id:
                raise AppError('ANALYSIS_REQUIRED','Analyze the action prompt first',409)
            if body.event_id:
                event = owned_record(store, project_id, project.active_asset_id, 'events', ActionEvent, body.event_id)
                if event.intent_id != project.active_intent_id:
                    raise AppError('OWNERSHIP', 'Selected event belongs to another action request', 422)
            if body.track_id:
                track = owned_record(store, project_id, project.active_asset_id, 'tracks', Track, body.track_id)
                if body.event_id and track.id not in event.track_ids:
                    raise AppError('OWNERSHIP', 'Selected track does not belong to the selected event', 422)
            payload=body.model_dump(mode='json')
            payload['intent_id']=str(project.active_intent_id)
            payload['source_asset_id']=str(project.active_asset_id)
            payload,cached=identify_request(store,project.id,'render',payload,request.headers.get('idempotency-key'),settings)
            if cached:return {'job_id':cached.id,'reused':True}
            job=Job(project_id=project_id,asset_id=project.active_asset_id,type='render',request=payload)
            runner.submit(job,partial(make_reels,store,runner,settings))
            project.active_job_id=job.id;project.status='processing'
            store.save(project_id,'project',project)
        return {'job_id':job.id}

    register_program_routes(app, settings)
    register_entity_routes(app, settings)
    register_evidence_routes(app, settings)
    register_runtime_routes(app, settings)
    register_revision_routes(app, settings)

    # Static build shares the API port. Unknown API/media paths never fall through to HTML.
    static = settings.static_dir.resolve()
    if (static / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=static / "assets"), name="frontend-assets")

    @app.get("/{path:path}", include_in_schema=False)
    def frontend(path: str):
        if path == "api" or path.startswith("api/") or path.startswith("assets/"):
            raise AppError("NOT_FOUND", "Resource not found", 404)
        index = static / "index.html"
        if not index.is_file():
            raise AppError("FRONTEND_NOT_BUILT", "Run the frontend build or use the Vite development server", 503)
        return FileResponse(index, media_type="text/html")

    return app


app = create_app()
