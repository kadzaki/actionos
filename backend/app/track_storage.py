"""Versioned compressed sample files; JSON track manifests publish references last."""
import gzip
import hashlib
import io
import json
import re
import zlib

from .editing import Track
from .errors import AppError

MAX_SAMPLE_BYTES=16*1024*1024
ENCODING='gzip-json-v1'


def pack_track(store,project_id,record):
    if not isinstance(record,Track):raise AppError('INVALID_TRACK','Track storage requires validated observations',422)
    data=record.model_dump(mode='json')
    raw=json.dumps(data.pop('samples'),allow_nan=False,separators=(',',':')).encode('utf-8')
    if len(raw)>MAX_SAMPLE_BYTES:raise AppError('TRACK_STORAGE_LIMIT','Track samples exceed the bounded storage representation',422)
    compressed=gzip.compress(raw,compresslevel=6,mtime=0)
    checksum=hashlib.sha256(compressed).hexdigest()
    relative=f'tracks/samples/{record.id}/{checksum}.json.gz'
    path=store.path(project_id,relative)
    if not path.is_file() or path.stat().st_size!=len(compressed):
        store.write_bytes(path,compressed)
    else:
        with path.open('rb') as stream:
            if hashlib.file_digest(stream,'sha256').hexdigest()!=checksum:
                raise AppError('CORRUPT_ARTIFACT','Existing track sample bytes failed checksum validation',500)
    data['sample_storage']={'encoding':ENCODING,'path':relative,'sha256':checksum,
                            'count':len(record.samples),'uncompressed_bytes':len(raw)}
    return data


def unpack_track(store,project_id,record_id,data):
    if not isinstance(data,dict) or 'sample_storage' not in data:return data  # Read-only compatibility with inline records.
    metadata=data.get('sample_storage')
    if (not isinstance(metadata,dict) or metadata.get('encoding')!=ENCODING or 'samples' in data
        or data.get('schema_version')!=1 or data.get('id')!=str(record_id) or data.get('project_id')!=str(project_id)):
        raise AppError('CORRUPT_RECORD','Compressed track manifest has invalid identity or encoding',500)
    checksum=metadata.get('sha256')
    if not isinstance(checksum,str) or not re.fullmatch('[0-9a-f]{64}',checksum):
        raise AppError('CORRUPT_RECORD','Compressed track checksum is invalid',500)
    expected=f'tracks/samples/{record_id}/{checksum}.json.gz'
    if metadata.get('path')!=expected:
        raise AppError('CORRUPT_RECORD','Compressed track reference is outside its expected sample artifact',500)
    if (type(metadata.get('count')) is not int or not 1<=metadata['count']<=30000
        or type(metadata.get('uncompressed_bytes')) is not int or not 1<=metadata['uncompressed_bytes']<=MAX_SAMPLE_BYTES):
        raise AppError('CORRUPT_RECORD','Compressed track declares invalid sample bounds',500)
    try:
        with store.path(project_id,expected).open('rb') as stream:compressed=stream.read(MAX_SAMPLE_BYTES+1)
        if len(compressed)>MAX_SAMPLE_BYTES or hashlib.sha256(compressed).hexdigest()!=checksum:
            raise ValueError('Invalid compressed sample checksum or size')
        with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as stream:raw=stream.read(MAX_SAMPLE_BYTES+1)
        if len(raw)>MAX_SAMPLE_BYTES or len(raw)!=metadata.get('uncompressed_bytes'):
            raise ValueError('Invalid decompressed sample size')
        samples=json.loads(raw)
        if not isinstance(samples,list) or not 1<=len(samples)<=30000 or len(samples)!=metadata.get('count'):
            raise ValueError('Invalid sample count')
    except (OSError,ValueError,EOFError,zlib.error):
        raise AppError('CORRUPT_ARTIFACT','Stored track samples are missing, damaged or exceed their declared bounds',500) from None
    return {**{key:value for key,value in data.items() if key!='sample_storage'},'samples':samples}
