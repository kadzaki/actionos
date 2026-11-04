from uuid import uuid4

import pytest

from app.errors import AppError
from app.models import Job,Project
from app.requests import identify_request
from app.storage import FileStore


def test_request_reuse_survives_restart_and_conflicts_are_explicit(settings):
    store=FileStore(settings.data_dir)
    project=Project(name='request test');store.create_project(project)
    payload={'action_prompt':'the second map unfolding','source_sha256':'0'*64}
    request,cached=identify_request(store,project.id,'analyze',payload,'safe-request-1',settings)
    assert cached is None and len(request['request_fingerprint'])==64
    job=Job(project_id=project.id,type='analyze',request=request)
    store.save(project.id,'jobs',job)
    restarted=FileStore(settings.data_dir)
    _,cached=identify_request(restarted,project.id,'analyze',payload,'safe-request-1',settings)
    assert cached.id==job.id
    assert 'safe-request-1' not in store.path(project.id,f'jobs/{job.id}.json').read_text()
    with pytest.raises(AppError) as exc:
        identify_request(restarted,project.id,'analyze',{**payload,'action_prompt':'different action'},'safe-request-1',settings)
    assert exc.value.code=='IDEMPOTENCY_CONFLICT'
    _,new=identify_request(restarted,project.id,'analyze',payload,None,settings)
    assert new is None
    with pytest.raises(AppError):identify_request(restarted,project.id,'analyze',payload,'bad key',settings)
