"""ActionOS immutable envelopes alongside (never inferred from) legacy records.

An envelope publishes only after all dependencies and artifact bytes validate.
This is metadata-last publication, not a multi-file transaction. Domain payloads
are validated by their owning adapters before this internal repository is used.
"""
from datetime import datetime
import hashlib
import json
from typing import Literal
from uuid import UUID, uuid4

from pydantic import Field, ValidationError, model_validator

from .errors import AppError
from .models import Asset, Model, Project, now
from .storage import FileStore, canonical_id, file_sha256
from .validation import payload_limits

Kind = Literal['compilations', 'programs', 'program_reviews', 'runs', 'observation_plans', 'muse_observations', 'entity_tracks', 'entity_bindings', 'geometry_evidence', 'observations', 'temporal_events', 'action_plans', 'reports', 'action_results', 'traces']
LEVELS = {'compilations': -1, 'programs': 0, 'program_reviews': 1, 'runs': 2, 'observation_plans': 3,
          'muse_observations': 4, 'entity_tracks': 4, 'entity_bindings': 5, 'geometry_evidence': 6,
          'observations': 7, 'temporal_events': 8,
          'action_plans': 9, 'reports': 10, 'action_results': 11, 'traces': 12}
MAX_RECORD_BYTES = 4 * 1024 * 1024
MAX_DEPTH = 12


def encoded(value):
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False,
                          sort_keys=True, separators=(',', ':')).encode('utf-8')
    except (ValueError, TypeError, RecursionError):
        raise AppError('INVALID_RECORD', 'Record must contain bounded JSON data', 422) from None


def fingerprint(record):
    return hashlib.sha256(encoded(record.model_dump(mode='json'))).hexdigest()


def check_depth(value):
    pending = [(value, 0)]
    while pending:
        node, depth = pending.pop()
        if depth > MAX_DEPTH:
            raise AppError('RECORD_LIMIT', 'Record exceeds the JSON nesting limit', 422)
        if isinstance(node, dict):
            pending.extend((v, depth + 1) for v in node.values())
        elif isinstance(node, list):
            pending.extend((v, depth + 1) for v in node)


class RecordReference(Model):
    kind: Kind
    id: UUID
    version: int = Field(default=1, ge=1, strict=True)
    record_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')


class ArtifactReference(Model):
    path: str = Field(min_length=1, max_length=240)
    sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    byte_size: int = Field(gt=0, le=150 * 1024 * 1024, strict=True)
    media_type: Literal['video/mp4', 'application/json', 'text/html', 'image/jpeg',
                        'application/gzip', 'text/csv', 'application/octet-stream']


class Envelope(Model):
    storage_schema_version: Literal[1] = 1
    kind: Kind
    id: UUID = Field(default_factory=uuid4)
    version: int = Field(default=1, ge=1, strict=True)
    created_at: datetime = Field(default_factory=now)
    project_id: UUID
    source_asset_id: UUID
    source_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    program: RecordReference | None = None
    run: RecordReference | None = None
    references: list[RecordReference] = Field(default_factory=list, max_length=512)
    artifacts: list[ArtifactReference] = Field(default_factory=list, max_length=2048)
    payload: dict = Field(default_factory=dict)

    @model_validator(mode='after')
    def lineage_shape(self):
        if self.created_at.tzinfo is None or self.created_at.utcoffset().total_seconds() != 0:
            raise ValueError('Record timestamps must be UTC')
        if self.kind in ('compilations', 'programs'):
            if self.program is not None or self.run is not None:
                raise ValueError('Program envelope cannot claim a run')
        else:
            if self.program is None or self.program.kind != 'programs':
                raise ValueError('Program version reference is required')
            if self.kind in ('runs', 'program_reviews'):
                if self.run is not None:
                    raise ValueError('Run cannot reference itself as a parent')
            elif self.run is None or self.run.kind != 'runs':
                raise ValueError('Run reference is required')
        if self.kind != 'programs' and self.version != 1:
            raise ValueError('Only program envelopes have numbered versions; derived records use new IDs')
        return self


def reference(record: Envelope):
    return RecordReference(kind=record.kind, id=record.id, version=record.version,
                           record_sha256=fingerprint(record))


class Repository:
    def __init__(self, store: FileStore):
        self.store = store

    def path(self, project_id, kind, record_id, version=1):
        if kind not in LEVELS or type(version) is not int or version < 1:
            raise AppError('INVALID_REFERENCE', 'Unknown record kind or version', 422)
        identifier = canonical_id(record_id)
        relative = (f'programs/{identifier}/v{version}.json' if kind == 'programs'
                    else f'{kind}/{identifier}.json')
        if kind != 'programs' and version != 1:
            raise AppError('INVALID_REFERENCE', 'Derived records use new IDs, not versions', 422)
        return self.store.path(project_id, relative)

    def _read(self, project_id, kind, record_id, version):
        path = self.path(project_id, kind, record_id, version)
        try:
            with path.open('rb') as stream:
                raw = stream.read(MAX_RECORD_BYTES + 1)
            if len(raw) > MAX_RECORD_BYTES:
                raise AppError('RECORD_LIMIT', 'Saved record exceeds size limit', 500)
            data = json.loads(raw)
            check_depth(data)
        except FileNotFoundError:
            raise AppError('NOT_FOUND', 'Referenced record is missing', 404) from None
        except (ValueError, OSError, RecursionError):
            raise AppError('CORRUPT_RECORD', 'Saved record could not be read', 500) from None
        if not isinstance(data, dict):
            raise AppError('CORRUPT_RECORD', 'Saved envelope is not an object', 500)
        if data.get('storage_schema_version') != 1 or type(data.get('storage_schema_version')) is not int:
            raise AppError('UNSUPPORTED_STORAGE_SCHEMA', 'Saved envelope uses an unsupported storage schema', 500)
        try:
            record = Envelope.model_validate(data)
        except ValidationError:
            raise AppError('CORRUPT_RECORD', 'Saved envelope failed validation', 500) from None
        if (str(record.project_id) != canonical_id(project_id) or record.kind != kind
                or str(record.id) != canonical_id(record_id) or record.version != version):
            raise AppError('OWNERSHIP', 'Saved envelope identity does not match its owner/path', 409)
        return record

    def _validate(self, record):
        payload=record.payload
        if (record.kind=='programs' and isinstance(payload.get('program'),dict)
                and isinstance(payload['program'].get('ast'),dict)):
            payload=payload['program']['ast']
        payload_limits(record.kind, payload)
        self.store.load(record.project_id, 'project', Project)
        asset = self.store.load(record.project_id, 'assets', Asset, record.source_asset_id)
        if asset.status != 'ready' or asset.sha256 != record.source_sha256:
            raise AppError('SOURCE_MISMATCH', 'Record source is not the owned ready source/hash', 409)
        if record.kind == 'programs' and record.version > 1:
            previous = self._read(record.project_id, 'programs', record.id, record.version - 1)
            if (previous.source_asset_id != record.source_asset_id
                    or previous.source_sha256 != record.source_sha256):
                raise AppError('SOURCE_MISMATCH', 'Program versions cannot change source identity', 409)
        refs = [r for r in (record.program, record.run) if r is not None] + record.references
        for ref in refs:
            if LEVELS[ref.kind] >= LEVELS[record.kind]:
                raise AppError('INVALID_REFERENCE', 'Record dependency order is invalid', 422)
            parent = self._read(record.project_id, ref.kind, ref.id, ref.version)
            if (parent.source_asset_id != record.source_asset_id or parent.source_sha256 != record.source_sha256
                    or fingerprint(parent) != ref.record_sha256):
                raise AppError('REFERENCE_MISMATCH', 'Referenced source/version/fingerprint does not match', 409)
            if parent.kind not in ('compilations','programs') and parent.program != record.program:
                raise AppError('REFERENCE_MISMATCH', 'Reference belongs to another program version', 409)
            if parent.kind not in ('compilations','programs', 'program_reviews', 'runs') and parent.run != record.run:
                raise AppError('REFERENCE_MISMATCH', 'Reference belongs to another run', 409)
            if ref.kind == 'runs' and ref != record.run:
                raise AppError('REFERENCE_MISMATCH', 'Run dependency does not match pinned run', 409)
        for artifact in record.artifacts:
            # Only project-relative paths are accepted, even for absolute paths
            # that happen to point into this project on the current machine.
            if ('\\' in artifact.path or ':' in artifact.path or artifact.path.startswith('/')
                    or any(part in ('', '.', '..') for part in artifact.path.split('/'))):
                raise AppError('INVALID_PATH', 'Artifact requires a canonical project-relative path', 422)
            path = self.store.path(record.project_id, artifact.path)
            try:
                valid = path.is_file() and path.stat().st_size == artifact.byte_size and file_sha256(path) == artifact.sha256
            except OSError:
                valid = False
            if not valid:
                raise AppError('CORRUPT_ARTIFACT', 'Referenced artifact is missing or failed checksum validation', 409)

    def publish(self, record: Envelope):
        # Revalidate mutated/nested models rather than relying on caller types.
        record = Envelope.model_validate(record.model_dump(mode='json'))
        data = record.model_dump(mode='json')
        raw = encoded(data)
        if len(raw) > MAX_RECORD_BYTES:
            raise AppError('RECORD_LIMIT', 'Record exceeds the JSON size limit', 422)
        check_depth(data)
        with self.store.mutation():
            self._validate_tree(record)
            path = self.path(record.project_id, record.kind, record.id, record.version)
            if path.exists():
                previous = self._read(record.project_id, record.kind, record.id, record.version)
                if fingerprint(previous) == fingerprint(record):
                    return previous
                raise AppError('IMMUTABLE_RECORD', 'Published versions cannot be overwritten', 409)
            self.store.write_json(path, data)
        return record

    def load(self, project_id, kind, record_id, version=1):
        with self.store.mutation():
            record = self._read(project_id, kind, record_id, version)
            self._validate_tree(record)
            return record

    def _validate_tree(self, record):
        # Complete bounded dependency closure; upstream damage cannot publish
        # new success or turn a prior result into a fabricated recovered output.
        visited = set()
        pending = [record]
        while pending:
            item = pending.pop()
            key = (item.kind, item.id, item.version)
            if key in visited:
                continue
            if len(visited) >= 4096:
                raise AppError('RECORD_LIMIT', 'Dependency closure exceeds the application limit', 500)
            visited.add(key)
            self._validate(item)
            refs = [r for r in (item.program, item.run) if r is not None] + item.references
            pending.extend(self._read(item.project_id, r.kind, r.id, r.version) for r in refs)
