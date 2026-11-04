import asyncio
import json

import httpx
import pytest

from app.config import Settings
from app.errors import AppError
from app.providers.meta import MetaCloud
from app.request_limits import JsonBodyLimit, MAX_JSON_BODY
from app.validation import WorkBudget, bounded_json, payload_limits


def test_unbounded_dsl_and_external_operators_fail_but_action_phrases_are_open():
    payload_limits('programs', {'steps':[{'predicate':'semantic_event', 'phrase':'a robot unfolds a map'}]})
    for kind, value, code in [
        ('programs', {'steps':[{'predicate':'shell', 'phrase':'echo untrusted'}]}, 'UNSUPPORTED_OPERATOR'),
        ('programs', {'actions':[{'type':'send_email'}]}, 'UNSUPPORTED_ACTION'),
        ('programs', {'entities':[{}]*17}, 'RESOURCE_LIMIT'),
        ('observations', {'observations':[{}]*4097}, 'RESOURCE_LIMIT'),
        ('action_plans', {'event_ids':['fixture']*33}, 'RESOURCE_LIMIT')]:
        with pytest.raises(AppError) as exc:
            payload_limits(kind, value)
        assert exc.value.code == code


def test_oversize_json_is_rejected_before_project_creation(client):
    response = client.post('/api/projects', content=json.dumps({'name': 'x' * MAX_JSON_BODY}),
                           headers={'Content-Type': 'application/json'})
    assert response.status_code == 413
    assert response.json()['code'] == 'REQUEST_TOO_LARGE'
    assert client.get('/api/projects').json()['projects'] == []


def test_chunked_body_without_content_length_is_bounded_before_app():
    sent, called = [], []
    messages = iter([{'type': 'http.request', 'body': b'x' * 600000, 'more_body': True},
                     {'type': 'http.request', 'body': b'x' * 600000, 'more_body': False}])
    async def receive(): return next(messages)
    async def send(message): sent.append(message)
    async def app(*_): called.append(True)
    asyncio.run(JsonBodyLimit(app)({'type':'http','method':'POST','path':'/api/projects','headers':[]}, receive, send))
    assert not called
    assert sent[0]['status'] == 413


def test_schema_size_depth_and_shared_provider_budget_fail_before_extra_calls(tmp_path):
    budget = WorkBudget(provider_calls=1)
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(200, json={'status':'completed', 'output':[]})
    settings = Settings(data_dir=tmp_path, model_api_key='test-secret')
    for _ in range(2):
        with MetaCloud(settings, httpx.MockTransport(respond), budget=budget) as cloud:
            if not calls:
                cloud.response(settings.muse_model, [])
            else:
                with pytest.raises(AppError) as exc:
                    cloud.response(settings.muse_model, [])
                assert exc.value.code == 'PROVIDER_CALL_LIMIT'
    assert len(calls) == 1
    with pytest.raises(AppError):
        bounded_json({'value': 'x' * 100}, max_bytes=10)
    nested = {}
    for _ in range(14): nested = {'next': nested}
    with pytest.raises(AppError):
        bounded_json(nested, max_bytes=10000)


def test_model_json_is_an_object_and_nesting_is_typed_failure(tmp_path):
    settings = Settings(data_dir=tmp_path, model_api_key='test-secret')
    for text in ('[]', '['*1500 + '0' + ']'*1500):
        body = {'status': 'completed', 'output': [{'type':'message', 'content':[{'type':'output_text', 'text':text}]}]}
        with MetaCloud(settings, httpx.MockTransport(lambda _:httpx.Response(200,json=body))) as cloud:
            with pytest.raises(AppError) as exc:
                cloud.json_response(settings.muse_model, [], {'type':'object'})
            assert exc.value.code == 'PROVIDER_SCHEMA'
