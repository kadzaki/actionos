import hashlib
import json
from uuid import uuid4

import pytest

from app.errors import AppError
from app.models import Asset, Project
from app.repository import ArtifactReference, Envelope, Repository, reference
from app.storage import FileStore


@pytest.fixture
def owned(tmp_path):
    store = FileStore(tmp_path)
    project = Project(name='ActionOS storage fixture')
    store.create_project(project)
    asset = Asset(project_id=project.id, status='ready', source_path='source/fixture.media',
                  original_name='generated fixture', byte_size=3,
                  sha256=hashlib.sha256(b'abc').hexdigest())
    store.write_bytes(store.path(project.id, asset.source_path), b'abc')
    store.save(project.id, 'assets', asset)
    return store, Repository(store), {'project_id': project.id, 'source_asset_id': asset.id,
                                     'source_sha256': asset.sha256}


def chain(repo, base):
    program = repo.publish(Envelope(kind='programs', payload={'schema_version': '1.0'}, **base))
    run = repo.publish(Envelope(kind='runs', program=reference(program), payload={'fixture': True}, **base))
    observations = repo.publish(Envelope(kind='observations', program=reference(program), run=reference(run), **base))
    event = repo.publish(Envelope(kind='temporal_events', program=reference(program), run=reference(run),
                                  references=[reference(observations)], **base))
    action = repo.publish(Envelope(kind='action_plans', program=reference(program), run=reference(run),
                                   references=[reference(event)], **base))
    return program, run, observations, event, action


def test_restart_reads_all_envelope_kinds_without_changing_legacy(owned):
    store, repo, base = owned
    old = (store.project_dir(base['project_id']) / 'project.json').read_bytes()
    records = list(chain(repo, base))
    report = repo.publish(Envelope(kind='reports', program=reference(records[0]), run=reference(records[1]),
                                    references=[reference(records[-1])], **base))
    trace = repo.publish(Envelope(kind='traces', program=reference(records[0]), run=reference(records[1]),
                                   references=[reference(report)], **base))
    restarted = Repository(FileStore(store.root))
    for record in records + [report, trace]:
        assert restarted.load(record.project_id, record.kind, record.id) == record
    assert (store.project_dir(base['project_id']) / 'project.json').read_bytes() == old
    assert restarted.load(base['project_id'], 'programs', records[0].id).payload['schema_version'] == '1.0'


def test_immutable_versions_and_idempotent_publication(owned):
    _, repo, base = owned
    program = repo.publish(Envelope(kind='programs', **base))
    assert repo.publish(program) == program
    with pytest.raises(AppError) as exc:
        repo.publish(program.model_copy(update={'payload': {'changed': True}}))
    assert exc.value.code == 'IMMUTABLE_RECORD'
    repo.publish(program.model_copy(update={'version': 2, 'payload': {'new': True}}))
    assert repo.load(program.project_id, 'programs', program.id, 1).payload == {}
    assert repo.load(program.project_id, 'programs', program.id, 2).payload == {'new': True}
    with pytest.raises(AppError):
        repo.publish(program.model_copy(update={'version': 4}))


def test_rejects_foreign_project_source_hash_version_and_cross_run(owned):
    store, repo, base = owned
    program, run, obs, event, action = chain(repo, base)
    other = Project(name='other')
    store.create_project(other)
    bad_records = [
        Envelope(kind='programs', **{**base, 'project_id': other.id}),
        Envelope(kind='programs', **{**base, 'source_sha256': '0' * 64}),
        Envelope(kind='runs', program=reference(program).model_copy(update={'version': 2}), **base),
        Envelope(kind='runs', program=reference(program).model_copy(update={'record_sha256': '0' * 64}), **base),
    ]
    for record in bad_records:
        with pytest.raises(AppError):
            repo.publish(record)
        assert not repo.path(record.project_id, record.kind, record.id, record.version).exists()
    second = repo.publish(Envelope(kind='runs', program=reference(program), **base))
    with pytest.raises(AppError) as exc:
        repo.publish(Envelope(kind='action_plans', program=reference(program), run=reference(second),
                              references=[reference(event)], **base))
    assert exc.value.code == 'REFERENCE_MISMATCH'


def test_metadata_last_missing_corrupt_and_unsafe_artifacts(owned):
    store, repo, base = owned
    program, run, _, _, action = chain(repo, base)
    content = b'<html>verified fixture only</html>'
    relative = 'reports/fixture.html'
    artifact = ArtifactReference(path=relative, sha256=hashlib.sha256(content).hexdigest(),
                                  byte_size=len(content), media_type='text/html')
    record = Envelope(kind='reports', program=reference(program), run=reference(run),
                        references=[reference(action)], artifacts=[artifact], **base)
    path = repo.path(record.project_id, record.kind, record.id)
    with pytest.raises(AppError):
        repo.publish(record)
    assert not path.exists()
    store.write_bytes(store.path(record.project_id, relative), content)
    repo.publish(record)
    store.write_bytes(store.path(record.project_id, relative), b'damaged')
    with pytest.raises(AppError) as exc:
        repo.load(record.project_id, record.kind, record.id)
    assert exc.value.code == 'CORRUPT_ARTIFACT'
    for relative in ['../outside.html', '/absolute.html', 'reports/../outside.html', 'C:/absolute.html']:
        bad = record.model_copy(update={'id': uuid4(), 'artifacts': [artifact.model_copy(update={'path': relative})]})
        with pytest.raises(AppError) as exc:
            repo.publish(bad)
        assert exc.value.code == 'INVALID_PATH'


def test_corrupt_upstream_prevents_publication_and_read(owned):
    store, repo, base = owned
    program, run, obs, event, action = chain(repo, base)
    obs_path = repo.path(obs.project_id, obs.kind, obs.id)
    obs_path.unlink()
    with pytest.raises(AppError):
        repo.load(action.project_id, action.kind, action.id)
    report = Envelope(kind='reports', program=reference(program), run=reference(run),
                        references=[reference(action)], **base)
    with pytest.raises(AppError):
        repo.publish(report)
    assert not repo.path(report.project_id, report.kind, report.id).exists()


def test_schema_identity_and_json_bounds_fail_explicitly(owned):
    store, repo, base = owned
    program = repo.publish(Envelope(kind='programs', **base))
    path = repo.path(program.project_id, program.kind, program.id)
    data = program.model_dump(mode='json')
    for changed, code in [({'storage_schema_version': 2}, 'UNSUPPORTED_STORAGE_SCHEMA'),
                           ({'project_id': str(uuid4())}, 'OWNERSHIP')]:
        store.write_json(path, {**data, **changed})
        with pytest.raises(AppError) as exc:
            repo.load(program.project_id, program.kind, program.id)
        assert exc.value.code == code
    path.write_text('{bad')
    with pytest.raises(AppError) as exc:
        repo.load(program.project_id, program.kind, program.id)
    assert exc.value.code == 'CORRUPT_RECORD'
    with pytest.raises(AppError) as exc:
        repo.publish(Envelope(kind='programs', payload={'large': 'x' * (4 * 1024 * 1024)}, **base))
    assert exc.value.code == 'RECORD_LIMIT'
    nested = {}
    for _ in range(13):
        nested = {'nested': nested}
    with pytest.raises(AppError) as exc:
        repo.publish(Envelope(kind='programs', payload=nested, **base))
    assert exc.value.code == 'RECORD_LIMIT'
