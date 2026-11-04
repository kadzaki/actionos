"""Reconcile file references without fabricating completion or deleting sources."""
from pathlib import Path
import re
from uuid import UUID

from .editing import EditPlan,Export,Track,fingerprint,fingerprint_data
from .errors import AppError
from .models import Asset,Job,JobStatus,Project
from .storage import FileStore,file_sha256

UUID_TEXT=r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'
TRANSIENT=re.compile(rf'(?:{UUID_TEXT}\.(?:upload|render\.mp4|window\.mp4|filters\.txt)|'
                     rf'{UUID_TEXT}\.[0-9a-f]{{32}}\.frame\.jpg)')
PARTIAL=re.compile(rf'\.{UUID_TEXT}\.(?:working|preview)\.partial\.mp4')
JSON_TEMP=re.compile(r'\..+\.json\.[0-9a-f]{32}\.tmp')
TRACK_TEMP=re.compile(r'\.[0-9a-f]{64}\.json\.gz\.[0-9a-f]{32}\.tmp')


def reconcile_files(store: FileStore):
    issues=[]
    def issue(project_id,code,message,record_id=None,record_kind=None):
        item={'project_id':str(project_id),'code':code,'message':message}
        if record_id is not None: item['record_id']=str(record_id)
        if record_kind is not None: item['record_kind']=record_kind
        issues.append(item)

    def load(project_id,kind,model,record_id=None):
        try:
            return store.load(project_id,kind,model,record_id)
        except AppError as exc:
            issue(project_id,exc.code,exc.message,record_id,kind)
            return None

    for directory in sorted(store.projects.iterdir()):
        if not directory.is_dir(): continue
        try:
            project_id=UUID(directory.name)
            root=store.project_dir(project_id)
        except (ValueError,AppError):
            # Unknown directories are never candidates for automatic deletion.
            continue
        # Remove only recognized generated temporary files within this project.
        for candidate in root.rglob('*'):
            if not candidate.is_file(): continue
            if ((candidate.parent.name=='tmp' and TRANSIENT.fullmatch(candidate.name))
                or (candidate.parent.name=='proxy' and PARTIAL.fullmatch(candidate.name))
                or (candidate.parent.parent.name=='samples' and TRACK_TEMP.fullmatch(candidate.name))
                or JSON_TEMP.fullmatch(candidate.name)):
                try:
                    checked=store.path(project_id,candidate.relative_to(root).as_posix())
                    checked.unlink(missing_ok=True)
                except (AppError,OSError):
                    issue(project_id,'TEMP_CLEANUP_FAILED','A temporary artifact could not be safely removed')
        project=load(project_id,'project',Project)
        if project is None:
            # Leave corrupt and unknown-schema manifests untouched for recovery.
            continue
        assets={}
        for path in (root/'assets').glob('*.json'):
            try: UUID(path.stem)
            except ValueError: continue
            asset=load(project_id,'assets',Asset,path.stem)
            if asset is None: continue
            if asset.status=='ready':
                required=(asset.source_path,asset.working_path,asset.proxy_path,asset.timeline_path)
                try:
                    missing=any(not relative or not store.path(project_id,relative).is_file() for relative in required)
                except AppError:
                    missing=True
                if missing:
                    asset.status='failed'
                    store.save(project_id,'assets',asset)
                    issue(project_id,'ASSET_FILES_MISSING','Prepared source files are missing; upload again to retry',asset.id)
            elif asset.status=='processing':
                asset.status='failed'
                store.save(project_id,'assets',asset)
                issue(project_id,'INGEST_INTERRUPTED','Incomplete source preparation requires a retry',asset.id)
            assets[str(asset.id)]=asset
        active=assets.get(str(project.active_asset_id)) if project.active_asset_id else None
        changed=False
        if project.active_asset_id and active is None:
            # Keep corrupt record/source files, but clear the unusable active
            # pointer so users can still open and recover this project.
            project.active_asset_id=None
            project.status='failed'
            changed=True
            issue(project_id,'ACTIVE_ASSET_UNAVAILABLE','The active source record could not be restored')
        if project.active_job_id:
            job=load(project_id,'jobs',Job,project.active_job_id)
            if job is None:
                project.active_job_id=None
                changed=True
                issue(project_id,'ACTIVE_JOB_UNAVAILABLE','The previous active job record is unavailable')
        if project.status=='processing' or (project.status=='failed' and active and active.status=='ready'):
            project.status='ready' if active and active.status=='ready' else 'failed'
            changed=True
        elif active and active.status=='failed' and project.status=='ready':
            project.status='failed'
            changed=True
        if changed: store.save(project_id,'project',project)

        referenced_samples=set()
        for path in (root/'tracks').glob('*.json'):
            try:UUID(path.stem)
            except ValueError:continue
            track=load(project_id,'tracks',Track,path.stem)
            if track is None:continue
            data=store.read_json(path)
            if isinstance(data,dict) and isinstance(data.get('sample_storage'),dict):
                referenced_samples.add(data['sample_storage'].get('path'))
            source=assets.get(str(track.source_asset_id))
            if source is None or (source.metadata and any(s.time_ms>source.metadata.duration_ms for s in track.samples)):
                issue(project_id,'TRACK_LINEAGE_INVALID','Track samples reference unavailable or out-of-range source evidence',track.id,'tracks')
        for samples in (root/'tracks'/'samples').glob('*/*.json.gz'):
            if samples.relative_to(root).as_posix() not in referenced_samples:
                issue(project_id,'ORPHAN_TRACK_SAMPLES','Unreferenced sample bytes were preserved for inspection',samples.parent.name,'tracks')

        referenced_sources={asset.source_path for asset in assets.values()}
        for source in [*(root/'source').glob('*.media'), *(root/'media'/'source').glob('*.media')]:
            if source.relative_to(root).as_posix() not in referenced_sources:
                issue(project_id,'ORPHAN_SOURCE','An unreferenced original was preserved for inspection',source.stem)
        exports={}
        for path in (root/'exports').glob('*.json'):
            try: UUID(path.stem)
            except ValueError: continue
            export=load(project_id,'exports',Export,path.stem)
            if export is None: continue
            exports[str(export.id)]=export
            try:
                if not store.path(project_id,export.path).is_file() or not store.path(project_id,export.manifest_path).is_file():
                    raise AppError('EXPORT_FILES_MISSING','Export artifacts are missing; this output is unavailable',500)
                manifest=store.read_json(store.path(project_id,export.manifest_path))
                if not isinstance(manifest,dict):
                    raise AppError('CORRUPT_RECORD','Saved export manifest is not an object',500)
                plan_path=store.path(project_id,f'plans/{export.plan_id}/v{export.plan_version}.json')
                plan_data=store.read_json(plan_path)
                plan=EditPlan.model_validate(plan_data)
                source_asset=assets.get(str(plan.source_asset_id))
                if (plan.id!=export.plan_id or plan.project_id!=project_id
                    or plan.version!=export.plan_version or manifest.get('plan_id')!=str(plan.id)
                    or manifest.get('source_asset_id')!=str(plan.source_asset_id)
                    or source_asset is None or manifest.get('source_sha256')!=source_asset.sha256
                    or manifest.get('plan_fingerprint') not in (fingerprint(plan),fingerprint_data(plan_data))
                    or export.origin!=plan.origin or manifest.get('origin')!=plan.origin
                    or manifest.get('output_path')!=export.path or not manifest.get('complete_decode_verified')):
                    raise AppError('EXPORT_LINEAGE_INVALID','Saved export references inconsistent evidence',500)
                if manifest.get('output_sha256') and file_sha256(store.path(project_id,export.path))!=manifest['output_sha256']:
                    raise AppError('EXPORT_CHECKSUM_INVALID','Saved export bytes no longer match verified output',500)
            except (ValueError,AppError) as exc:
                code=exc.code if isinstance(exc,AppError) else 'CORRUPT_RECORD'
                issue(project_id,code,'Export could not be reconciled; original artifacts were preserved',export.id,'exports')
        for output in (root/'exports').glob('*.mp4'):
            if output.stem not in exports:
                issue(project_id,'ORPHAN_EXPORT','An unpublished output was preserved; it is not a completed export',output.stem)
    return issues
