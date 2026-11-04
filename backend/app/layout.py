"""Manifest-owned media paths and a read-only, non-destructive migration audit."""
from uuid import UUID

from .editing import Export, Track
from .errors import AppError
from .models import Asset, Project
from .repository import LEVELS, Repository
from .storage import canonical_id, file_sha256


def source_path(asset_id, layout='actionos'):
    prefix = 'media/source' if layout == 'actionos' else 'source'
    if layout not in ('actionos', 'legacy'):
        raise AppError('INVALID_LAYOUT', 'Unknown media layout', 422)
    return f'{prefix}/{canonical_id(asset_id)}.media'


def derivative_path(asset, kind):
    if kind not in ('working', 'preview'):
        raise AppError('INVALID_VARIANT', 'Unknown derivative kind', 422)
    prefix = 'media/proxy' if asset.media_layout == 'actionos' else 'proxy'
    return f'{prefix}/{asset.id}.{kind}.mp4'


def owned_media(store, project_id, asset_id, role):
    fields = {'source': 'source_path', 'working': 'working_path', 'proxy': 'proxy_path',
              'timeline': 'timeline_path', 'thumbnail': 'thumbnail_path'}
    if role not in fields:
        raise AppError('INVALID_VARIANT', 'Unknown source artifact role', 422)
    asset = store.load(project_id, 'assets', Asset, asset_id)
    relative = getattr(asset, fields[role])
    if not relative:
        raise AppError('MEDIA_NOT_READY', 'Media manifest has no artifact for this role', 409)
    # The owned manifest is authoritative: never guess a path by extension,
    # never rename uploaded bytes, and never search another project's files.
    path = store.path(project_id, relative)
    if not path.is_file():
        raise AppError('MEDIA_MISSING', 'Manifest-owned artifact is missing', 404)
    expected = asset.sha256 if role == 'source' else asset.artifact_sha256.get(relative)
    if expected is not None and file_sha256(path) != expected:
        raise AppError('CORRUPT_ARTIFACT', 'Manifest-owned bytes failed checksum validation', 409)
    return path


def inventory_project(store, project_id):
    """No moves, record rewrites, inference, source decoding or recovery side effects."""
    project = store.load(project_id, 'project', Project)
    result = {'project_id': str(project.id), 'dry_run': True, 'moves': [],
              'sources': [], 'exports': [], 'actionos_records': [], 'issues': []}
    def issue(exc, kind, record_id):
        result['issues'].append({**exc.envelope(), 'kind': kind, 'record_id': str(record_id)})
    assets, errors = store.scan_records(project_id, 'assets', Asset)
    result['issues'].extend(errors)
    for asset in assets:
        entry = {'asset_id': str(asset.id), 'layout': asset.media_layout,
                 'source_path': asset.source_path, 'source_sha256': asset.sha256,
                 'derivative_integrity': 'qualified' if asset.artifact_sha256 else 'legacy_unqualified'}
        for role in ('source', 'working', 'proxy', 'timeline', 'thumbnail'):
            try:
                owned_media(store, project_id, asset.id, role)
            except AppError as exc:
                issue(exc, 'assets', asset.id)
        result['sources'].append(entry)
    _, errors = store.scan_records(project_id, 'tracks', Track)
    result['issues'].extend(errors)  # Includes bounded gzip-json-v1 validation.
    exports, errors = store.scan_records(project_id, 'exports', Export)
    result['issues'].extend(errors)
    for export in exports:
        try:
            manifest = store.read_json(store.path(project_id, export.manifest_path))
            if not isinstance(manifest, dict):
                raise AppError('CORRUPT_RECORD', 'Export manifest is not an object', 409)
            source = store.load(project_id, 'assets', Asset, manifest.get('source_asset_id'))
            output = store.path(project_id, export.path)
            if (manifest.get('source_sha256') != source.sha256
                    or manifest.get('plan_id') != str(export.plan_id)
                    or manifest.get('plan_version') != export.plan_version
                    or manifest.get('output_path') != export.path
                    or not output.is_file() or file_sha256(output) != manifest.get('output_sha256')
                    or manifest.get('complete_decode_verified') is not True):
                raise AppError('REFERENCE_MISMATCH', 'Export source/output lineage failed validation', 409)
            result['exports'].append({'export_id': str(export.id), 'path': export.path,
                                      'sha256': manifest['output_sha256'], 'semantics': 'legacy_edit'})
        except AppError as exc:
            issue(exc, 'exports', export.id)
    repo = Repository(store)
    for kind in LEVELS:
        folder = store.path(project_id, kind)
        paths = folder.glob('*/v*.json') if kind == 'programs' else folder.glob('*.json')
        for path in sorted(paths):
            identifier = path.parent.name if kind == 'programs' else path.stem
            try:
                version = int(path.stem[1:]) if kind == 'programs' else 1
                record = repo.load(project_id, kind, identifier, version)
                result['actionos_records'].append({'kind': kind, 'id': str(record.id), 'version': record.version})
            except (ValueError, AppError) as exc:
                issue(exc if isinstance(exc, AppError) else AppError('CORRUPT_RECORD', 'Invalid version filename', 409), kind, identifier)
    result['valid'] = not result['issues']
    return result
