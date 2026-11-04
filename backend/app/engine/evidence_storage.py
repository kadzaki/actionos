"""Bounded evidence artifacts and aggregate per-run perception admission."""
import gzip
import hashlib
import json
import zlib

from ..errors import AppError
from ..programs.policy import LIMITS
from ..repository import ArtifactReference,Repository
from ..storage import file_sha256
from ..validation import bounded_json

MAX_JSON_BYTES=4*1024*1024


def digest(value):
    from ..repository import encoded
    return hashlib.sha256(encoded(value)).hexdigest()


def existing_artifact(store,project_id,path,media_type,*,expected_sha256=None):
    target=store.path(project_id,path)
    try:
        checksum=file_sha256(target)
        if expected_sha256 is not None and checksum!=expected_sha256:
            raise AppError('CORRUPT_ARTIFACT','Evidence artifact differs from its pinned checksum',409)
        return ArtifactReference(path=path,sha256=checksum,byte_size=target.stat().st_size,media_type=media_type)
    except OSError:raise AppError('CORRUPT_ARTIFACT','Referenced evidence artifact is missing or unreadable',409) from None


def source_artifacts(store,project_id,prepared):
    asset=prepared.source.asset
    return [existing_artifact(store,project_id,prepared.window.path,'video/mp4',expected_sha256=prepared.window.sha256),
        existing_artifact(store,project_id,asset.timeline_path,'application/json',expected_sha256=prepared.plan.source_timeline_sha256),
        existing_artifact(store,project_id,asset.source_path,'application/octet-stream',expected_sha256=asset.sha256)]


def write_json_artifact(store,project_id,path,value,*,compressed=False,max_bytes=MAX_JSON_BYTES):
    data=bounded_json(value,max_bytes=max_bytes)
    if compressed:data=gzip.compress(data,mtime=0)
    store.write_bytes(store.path(project_id,path),data)
    return existing_artifact(store,project_id,path,'application/gzip' if compressed else 'application/json')


def read_json_artifact(store,project_id,path,*,compressed=False,max_bytes=MAX_JSON_BYTES):
    try:
        opener=gzip.open if compressed else open
        with opener(store.path(project_id,path),'rb') as stream:data=stream.read(max_bytes+1)
        if len(data)>max_bytes:raise ValueError('Oversized evidence artifact')
        result=json.loads(data);bounded_json(result,max_bytes=max_bytes)
        return result
    except (OSError,ValueError,EOFError,zlib.error,RecursionError):
        raise AppError('CORRUPT_ARTIFACT','Evidence JSON is damaged or exceeds its bounded representation',409) from None


def run_perception_usage(store,project_id,run_id):
    """Count normalized node hypotheses/entity candidates from actual artifacts.

    Call while holding store.mutation() before publication. Geometry/fusion are
    derived views and do not consume new provider-observation slots. Dense sample
    arrays retain their own per-candidate/per-batch and byte bounds; video frame
    count is not the normalized-observation quota's unit.
    """
    from .entity_tracks import EntityTrackBatch
    from .observations import ObservationBatch
    from pydantic import ValidationError
    observations=attempts=0;repo=Repository(store)
    for kind in ('entity_tracks','muse_observations'):
        for path in store.path(project_id,kind).glob('*.json'):
            header=repo._read(project_id,kind,path.stem,1)
            if str(header.run.id)!=str(run_id):continue
            record=repo.load(project_id,kind,path.stem)
            attempts+=1
            if attempts>LIMITS['provider_calls']:
                raise AppError('OBSERVATION_LIMIT','Stored perception attempts already exceed this run\'s bounded budget',422)
            try:
                if kind=='entity_tracks':
                    expected=f'entity_tracks/artifacts/{record.id}/batch.json.gz'
                    if not record.artifacts or record.artifacts[0].path!=expected:
                        raise AppError('CORRUPT_RECORD','Track sample artifact has an invalid owner path',409)
                    batch=EntityTrackBatch.model_validate(read_json_artifact(store,project_id,expected,compressed=True))
                    observations+=len(batch.candidates)
                elif record.payload.get('state')=='validated':
                    expected=f'muse_observations/artifacts/{record.id}/batch.json.gz'
                    if not any(a.path==expected for a in record.artifacts):
                        raise AppError('CORRUPT_RECORD','Muse batch artifact is missing from its manifest',409)
                    batch=ObservationBatch.model_validate(read_json_artifact(store,project_id,expected,compressed=True))
                    observations+=len(batch.observations)
                elif record.payload.get('state')!='unavailable':
                    raise AppError('CORRUPT_RECORD','Muse observation state is invalid',409)
            except ValidationError:
                raise AppError('CORRUPT_ARTIFACT','Stored perception samples failed typed budget validation',409) from None
    return observations,attempts


def admit_perception(store,project_id,run_id,additional):
    observations,attempts=run_perception_usage(store,project_id,run_id)
    if observations+additional>LIMITS['observations'] or attempts+1>LIMITS['provider_calls']:
        raise AppError('OBSERVATION_LIMIT','Run perception storage exceeds its observation/attempt budget; nothing was silently truncated',422,
            details={'existing_observations':observations,'additional_observations':additional,'existing_attempts':attempts})
