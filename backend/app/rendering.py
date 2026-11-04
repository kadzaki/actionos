"""Deterministic render execution and verified, atomic export publication."""
import json
import os
from pathlib import Path
from uuid import uuid4

from .config import Settings
from .editing import Crop, EditPlan, Export, Track, fingerprint, timeline, validate_plan
from .errors import AppError
from .media import probe, run_media,media_tool_versions
from .models import Asset
from .storage import FileStore,atomic_replace,file_sha256
from .resources import MediaBudget

RENDER_CONTRACT_VERSION=1


def crop_expression(crop: Crop, axis: str):
    # Only validated numbers become expressions. No model text or arbitrary filters.
    expression = f"{getattr(crop.points[-1],axis):.6f}"
    for a,b in reversed(list(zip(crop.points,crop.points[1:]))):
        av, bv = getattr(a,axis), getattr(b,axis)
        value = f"({av:.6f}+({bv-av:.6f})*clip((t-{a.time_ms/1000:.6f})/{(b.time_ms-a.time_ms)/1000:.6f},0,1))"
        expression = f"if(lt(t,{b.time_ms/1000:.6f}),{value},{expression})"
    return expression


def tempo_filters(speed: float):
    factors = []
    while speed < .5:
        factors.append(.5); speed /= .5
    while speed > 2:
        factors.append(2.); speed /= 2
    factors.append(speed)
    return ",".join(f"atempo={factor:.10f}" for factor in factors)


def filtergraph(plan: EditPlan, audio_present: bool):
    filters, video_labels, audio_labels = [], [], []
    for index, segment in enumerate(plan.segments):
        start,end = segment.source_in_ms/1000, segment.source_out_ms/1000
        duration = (end-start)/segment.speed
        crop = segment.crop
        framing = (f'scale={plan.output.width}:{plan.output.height}:force_original_aspect_ratio=decrease:force_divisible_by=2,'
                   f'pad={plan.output.width}:{plan.output.height}:(ow-iw)/2:(oh-ih)/2:color=black'
                   if crop.mode == 'full_frame' else f'scale={plan.output.width}:{plan.output.height}:flags=lanczos')
        filters.append(f"[0:v:0]trim=start={start:.6f}:end={end:.6f},"
                       f"crop=w={crop.width}:h={crop.height}:x='{crop_expression(crop,'x')}':y='{crop_expression(crop,'y')}',"
                       f"setpts=(PTS-STARTPTS)/{segment.speed:.10f},fps=30,"
                       f"{framing},setsar=1[v{index}]")
        video_labels.append(f"[v{index}]")
        if audio_present:
            filters.append(f"[0:a:0]atrim=start={start:.6f}:end={end:.6f},asetpts=PTS-STARTPTS,"
                           f"{tempo_filters(segment.speed)},aresample=48000,apad,atrim=duration={duration:.8f},"
                           f"asetpts=PTS-STARTPTS[a{index}]")
            audio_labels.append(f"[a{index}]")
    n = len(plan.segments)
    if audio_present:
        filters.append("".join(v+a for v,a in zip(video_labels,audio_labels))+f"concat=n={n}:v=1:a=1[vout][aout]")
    else:
        filters.append("".join(video_labels)+f"concat=n={n}:v=1:a=0[vout]")
    return ";\n".join(filters)


def save_plan(store: FileStore, plan: EditPlan):
    path = store.path(plan.project_id, f"plans/{plan.id}/v{plan.version}.json")
    with store.mutation():
        if path.exists():
            existing = load_plan(store, plan.id)
            if existing != plan:
                raise AppError("IMMUTABLE_PLAN", "Existing plan versions cannot be overwritten", 409)
            return
        store.write_json(path, plan.model_dump(mode="json"))


def load_plan(store: FileStore, plan_id):
    from .storage import canonical_id
    plan_id = canonical_id(plan_id)
    matches = sorted(store.projects.glob(f"*/plans/{plan_id}/v*.json"))
    if not matches:
        raise AppError("NOT_FOUND", "Plan not found", 404)
    try:
        plans = [EditPlan.model_validate_json(path.read_text(encoding="utf-8")) for path in matches]
        if any(str(p.id)!=plan_id or str(p.project_id)!=path.parents[2].name for p,path in zip(plans,matches)):
            raise ValueError("Invalid ownership")
        return max(plans,key=lambda p:p.version)
    except (ValueError,OSError):
        raise AppError("CORRUPT_RECORD", "Saved plan failed validation", 500) from None


def reusable_export(store,asset,plan,tools=None):
    tools=tools or media_tool_versions()
    expected=fingerprint(plan)
    for export in sorted(store.list_records(plan.project_id,'exports',Export),key=lambda e:e.created_at,reverse=True):
        output=store.path(plan.project_id,export.path)
        try:manifest=store.read_json(store.path(plan.project_id,export.manifest_path))
        except AppError:continue
        if not isinstance(manifest,dict):continue
        if (manifest.get('plan_fingerprint')!=expected or manifest.get('source_sha256')!=asset.sha256
            or manifest.get('source_asset_id')!=str(asset.id) or manifest.get('plan_id')!=str(export.plan_id)
            or manifest.get('plan_version')!=export.plan_version or export.origin!=plan.origin
            or manifest.get('render_contract_version')!=RENDER_CONTRACT_VERSION or manifest.get('tools')!=tools
            or manifest.get('output_path')!=export.path or not manifest.get('complete_decode_verified')):continue
        sha=manifest.get('output_sha256')
        if not isinstance(sha,str) or not output.is_file() or file_sha256(output)!=sha:continue
        try:saved=load_plan(store,export.plan_id)
        except AppError:continue
        if saved.project_id==plan.project_id and saved.source_asset_id==asset.id and fingerprint(saved)==expected:
            return export


def render(store: FileStore, settings: Settings, plan: EditPlan, stage=lambda *_: None, *,regenerate=False):
    asset = store.load(plan.project_id,"assets",Asset,plan.source_asset_id)
    validate_plan(plan,asset)
    for segment in plan.segments:
        for context_id in segment.crop.context_track_ids:
            context=store.load(plan.project_id,'tracks',Track,context_id)
            if context.source_asset_id!=asset.id or (plan.origin=='autonomous' and context.origin!='meta_cloud'):
                raise AppError('INVALID_EVIDENCE','Context track does not belong to this source and provider',422)
        if segment.crop.track_id:
            track = store.load(plan.project_id,"tracks",Track,segment.crop.track_id)
            if track.source_asset_id != asset.id:
                raise AppError("OWNERSHIP", "Crop track belongs to a different source",422)
            if plan.origin == "autonomous" and track.origin != "meta_cloud":
                raise AppError("INVALID_EVIDENCE", "Autonomous rendering requires actual cloud tracking",422)
    if store.path(plan.project_id,f'plans/{plan.id}/v{plan.version}.json').exists():
        save_plan(store,plan)  # Validate immutable identity even on a cache hit.
    tools=media_tool_versions()
    if not regenerate:
        cached=reusable_export(store,asset,plan,tools)
        if cached:
            stage('reusing',100)
            return cached
    budget=MediaBudget(store.project_dir(plan.project_id),settings)
    budget.check()
    save_plan(store,plan)
    export_id = uuid4()
    temporary = store.path(plan.project_id,f"tmp/{export_id}.render.mp4")
    script = store.path(plan.project_id,f"tmp/{export_id}.filters.txt")
    temporary.parent.mkdir(parents=True,exist_ok=True)
    source = store.path(plan.project_id,asset.working_path)
    has_audio = asset.working_metadata.audio_present
    try:
        stage("rendering",None)
        script.write_text(filtergraph(plan,has_audio),encoding="utf-8")
        args = ["ffmpeg","-hide_banner","-loglevel","error","-xerror","-nostdin","-y",
                "-i",str(source),"-filter_complex_threads","1","-filter_complex_script",str(script),"-map","[vout]"]
        if has_audio:
            args += ["-map","[aout]","-c:a","aac","-b:a","128k"]
        args += ["-c:v","libx264","-preset","veryfast","-crf","19","-pix_fmt","yuv420p","-r","30",
                 "-threads","2","-map_metadata","-1","-movflags","+faststart",str(temporary)]
        run_media(args,settings.media_timeout_seconds,budget=budget)
        stage("verifying",90)
        metadata = probe(temporary,settings,enforce_limits=False)
        expected = timeline(plan)[-1]["output_out_ms"]
        duration_tolerance_ms = 100
        if (metadata.codec != "h264" or metadata.geometry.display_width != plan.output.width
            or metadata.geometry.display_height != plan.output.height or abs(metadata.fps-30)>.01
            or abs(metadata.duration_ms-expected)>duration_tolerance_ms or metadata.audio_present != has_audio
            or (has_audio and metadata.audio_codec != "aac")):
            raise AppError("EXPORT_VERIFICATION_FAILED","Rendered output did not satisfy the plan",422)
        # Decode the whole bounded output, including the final frame; do not accept a probe-only file.
        run_media(["ffmpeg","-hide_banner","-loglevel","error","-xerror","-nostdin","-i",str(temporary),
                   "-f","null","-"],settings.media_timeout_seconds,budget=budget)
        output_relative = f"exports/{export_id}.mp4"
        manifest_relative = f"exports/{export_id}.manifest.json"
        final = store.path(plan.project_id,output_relative)
        final.parent.mkdir(parents=True,exist_ok=True)
        manifest = {"schema_version":1,"source_asset_id":str(asset.id),"source_sha256":asset.sha256,
                    "plan_id":str(plan.id),"plan_version":plan.version,"plan_fingerprint":fingerprint(plan),
                    "origin":plan.origin,"selected_event_ids":[str(e) for e in plan.selected_event_ids],
                    "source_to_output":timeline(plan),"source_geometry":asset.metadata.geometry.model_dump(),
                    "working_geometry":asset.working_metadata.geometry.model_dump(),"tools":tools,
                    "render_contract_version":RENDER_CONTRACT_VERSION,
                    "output":metadata.model_dump(),"planned_duration_ms":expected,
                    "duration_tolerance_ms":duration_tolerance_ms,"complete_decode_verified":True,
                    "output_sha256":file_sha256(temporary),
                    "crop_modes":[s.crop.mode for s in plan.segments],"output_path":output_relative}
        if plan.actionos_lineage is not None:
            manifest['actionos_lineage'] = plan.actionos_lineage.model_dump(mode='json')
        store.write_json(store.path(plan.project_id,manifest_relative),manifest)
        atomic_replace(temporary,final)
        export = Export(id=export_id,project_id=plan.project_id,plan_id=plan.id,plan_version=plan.version,
                        path=output_relative,manifest_path=manifest_relative,duration_ms=metadata.duration_ms,origin=plan.origin,
                        actionos_lineage=plan.actionos_lineage)
        # Publish discoverable export record only after manifest + verified media exist.
        store.save(plan.project_id,"exports",export)
        return export
    finally:
        temporary.unlink(missing_ok=True)
        script.unlink(missing_ok=True)
