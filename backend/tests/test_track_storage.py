import gzip
import hashlib
import json
import os
from uuid import uuid4

import pytest

from app.editing import Track,TrackSample
from app.errors import AppError
from app.models import Project
from app.storage import FileStore
from app.track_storage import MAX_SAMPLE_BYTES


def track_fixture(tmp_path):
    store=FileStore(tmp_path);project=Project(name='compressed geometry test');store.create_project(project)
    track=Track(project_id=project.id,source_asset_id=uuid4(),label='synthetic storage geometry',origin='synthetic_test',
                samples=[TrackSample(time_ms=t,x=.1,y=.2,width=.2,height=.3) for t in range(1000)])
    return store,project,track


def test_compressed_track_round_trip_is_lossless_and_metadata_is_published_separately(tmp_path):
    store,project,track=track_fixture(tmp_path)
    store.save(project.id,'tracks',track)
    path=store.path(project.id,f'tracks/{track.id}.json');raw=json.loads(path.read_text())
    assert 'samples' not in raw and raw['sample_storage']['count']==1000
    payload=store.path(project.id,raw['sample_storage']['path']).read_bytes()
    assert len(payload)<raw['sample_storage']['uncompressed_bytes']/10
    assert hashlib.sha256(payload).hexdigest()==raw['sample_storage']['sha256']
    assert json.loads(gzip.decompress(payload))==[s.model_dump() for s in track.samples]
    assert FileStore(tmp_path).load(project.id,'tracks',Track,track.id)==track
    assert store.list_records(project.id,'tracks',Track)==[track]


def test_failed_metadata_publication_keeps_previous_samples_and_record(tmp_path,monkeypatch):
    store,project,track=track_fixture(tmp_path);store.save(project.id,'tracks',track)
    old=store.load(project.id,'tracks',Track,track.id)
    original_replace=os.replace
    def fail_manifest(source,destination):
        if destination.name==f'{track.id}.json':raise OSError('simulated metadata disk failure')
        return original_replace(source,destination)
    monkeypatch.setattr(os,'replace',fail_manifest)
    changed=track.model_copy(deep=True);changed.samples[0].x=.11
    with pytest.raises(OSError):store.save(project.id,'tracks',changed)
    assert store.load(project.id,'tracks',Track,track.id)==old
    assert not list(store.project_dir(project.id).rglob('*.tmp'))


@pytest.mark.parametrize('damage',['bytes','missing','path','encoding','future_schema'])
def test_compressed_track_corruption_and_foreign_paths_are_not_hidden(tmp_path,damage):
    store,project,track=track_fixture(tmp_path);store.save(project.id,'tracks',track)
    path=store.path(project.id,f'tracks/{track.id}.json');raw=json.loads(path.read_text())
    payload=store.path(project.id,raw['sample_storage']['path'])
    if damage=='bytes':payload.write_bytes(b'damaged gzip')
    if damage=='missing':payload.unlink()
    if damage=='path':raw['sample_storage']['path']='../../outside.json.gz'
    if damage=='encoding':raw['sample_storage']['encoding']='future-codec'
    if damage=='future_schema':raw['schema_version']=999
    path.write_text(json.dumps(raw))
    with pytest.raises(AppError) as exc:store.load(project.id,'tracks',Track,track.id)
    assert exc.value.code in ('CORRUPT_RECORD','CORRUPT_ARTIFACT')
    assert path.is_file()


def test_decompression_is_bounded_even_with_a_matching_compressed_checksum(tmp_path):
    store,project,track=track_fixture(tmp_path);store.save(project.id,'tracks',track)
    path=store.path(project.id,f'tracks/{track.id}.json');raw=json.loads(path.read_text())
    compressed=gzip.compress(b' '* (MAX_SAMPLE_BYTES+1),mtime=0)
    checksum=hashlib.sha256(compressed).hexdigest()
    relative=f'tracks/samples/{track.id}/{checksum}.json.gz'
    store.write_bytes(store.path(project.id,relative),compressed)
    raw['sample_storage'].update(path=relative,sha256=checksum,uncompressed_bytes=10)
    path.write_text(json.dumps(raw))
    with pytest.raises(AppError) as exc:store.load(project.id,'tracks',Track,track.id)
    assert exc.value.code=='CORRUPT_ARTIFACT'


def test_legacy_inline_track_remains_readable_without_silent_rewrite(tmp_path):
    store,project,track=track_fixture(tmp_path)
    path=store.path(project.id,f'tracks/{track.id}.json');store.write_json(path,track.model_dump(mode='json'))
    previous=path.read_bytes()
    assert store.load(project.id,'tracks',Track,track.id)==track
    assert path.read_bytes()==previous


def test_explicit_record_scan_returns_healthy_tracks_and_reports_damaged_ones(tmp_path):
    store,project,track=track_fixture(tmp_path);store.save(project.id,'tracks',track)
    bad=track.model_copy(update={'id':uuid4()});store.save(project.id,'tracks',bad)
    path=store.path(project.id,f'tracks/{bad.id}.json');raw=json.loads(path.read_text())
    blob=store.path(project.id,raw['sample_storage']['path']);blob.write_bytes(b'corrupt')
    good,errors=store.scan_records(project.id,'tracks',Track)
    assert good==[track] and [error['record_id'] for error in errors]==[str(bad.id)]
    with pytest.raises(AppError):store.list_records(project.id,'tracks',Track)
    assert blob.is_file() and path.is_file()
