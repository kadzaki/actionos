"""Complete status reporting may be entirely unknown; it never requires positives."""
from copy import deepcopy
from dataclasses import replace
import json

import httpx
import pytest

from app.engine.muse_observations import COMPLETE_OBSERVATION_ADAPTER_VERSION,build_observation_request,normalize_response,observation_text_format,observe_window
from app.engine.observations import ObservationResponse
from app.engine.observation_repository import request_snapshot
from app.errors import AppError
from app.providers.meta import MetaCloud
from test_step_observations import context,PROVENANCE


@pytest.fixture
def complete(context):
    plan=context.plan.model_copy(update={'providers':{**context.plan.providers,'muse_adapter_version':COMPLETE_OBSERVATION_ADAPTER_VERSION}})
    prepared=replace(context,plan=plan)
    count=len(prepared.window.source_frame_indices)
    raw={'schema_version':'predicate-observations-1','window_id':str(prepared.window.id),
        'entities':[{'entity_id':e.id,'role':e.role,'description':'Explicit unknown test entity',
            'visibility':'unknown','identity':'unknown','uncertainty':['Labeled fixture has no identity evidence']} for e in prepared.ast.entities],
        'hypotheses':[{'step_id':s.id,'entity_ids':s.entities,'assessment':'not_observed',
            'description':'Explicit test unknown, not a physical absence claim','quality':'low',
            'uncertainty':['No visual support supplied by this fixture']} for s in prepared.ast.steps],
        'coverage':[{'step_id':s.id,'attempted_frames':{'start_frame':0,'stop_frame':count},'complete_inspection_claim':False,
            'visibility':'unknown','identity':'unknown','resolution':'unknown','uncertainty':['No sufficient coverage']} for s in prepared.ast.steps]}
    return prepared,ObservationResponse.model_validate(raw).model_dump(mode='json')


def test_complete_provider_schema_requires_fields_ids_and_actual_window_bounds_without_mutating_app_schema(complete):
    prepared,_=complete
    _,schema=build_observation_request(prepared);original=deepcopy(schema)
    native=observation_text_format(prepared,schema)
    assert schema==original and native['strict'] is True
    transport=native['schema'];assert set(transport['required'])==set(transport['properties'])
    assert transport['properties']['hypotheses']['minItems']==len(prepared.ast.steps)
    assert transport['properties']['coverage']['minItems']==len(prepared.ast.steps)
    assert transport['properties']['window_id']['const']==str(prepared.window.id)
    assert transport['$defs']['FrameRange']['properties']['stop_frame']['maximum']==len(prepared.window.source_frame_indices)
    pending=[transport]
    while pending:
        node=pending.pop()
        if isinstance(node,dict):
            assert 'oneOf' not in node and 'allOf' not in node
            if node.get('type')=='object':
                assert node['additionalProperties'] is False and set(node['required'])==set(node.get('properties',{}))
            pending.extend(node.values())
        elif isinstance(node,list):pending.extend(node)


def test_identifier_only_reply_stays_valid_empty_in_v3_but_is_rejected_in_complete_contract(complete):
    prepared,_=complete
    bare={'schema_version':'predicate-observations-1','window_id':str(prepared.window.id)}
    legacy=replace(prepared,plan=prepared.plan.model_copy(update={'providers':{**prepared.plan.providers,'muse_adapter_version':'muse-step-observations-3'}}))
    assert normalize_response(legacy,bare,PROVENANCE).observations==[]
    with pytest.raises(AppError) as exc:normalize_response(prepared,bare,PROVENANCE)
    assert exc.value.code=='PROVIDER_SCHEMA'


def test_complete_unknown_statuses_are_accepted_and_missing_node_is_rejected(complete):
    prepared,raw=complete
    batch=normalize_response(prepared,raw,PROVENANCE)
    assert not batch.missing_step_ids and all(o.hypothesis=='not_observed' for o in batch.observations)
    assert all(not claim.negative_coverage_verified for claim in batch.coverage)
    raw['hypotheses'].pop()
    with pytest.raises(AppError) as exc:normalize_response(prepared,raw,PROVENANCE)
    assert exc.value.code=='PROVIDER_COVERAGE'


def test_complete_wire_matches_stored_schema_and_cleanup_still_runs(complete,settings):
    prepared,raw=complete;requests=[]
    def transport(request):
        if request.method=='DELETE':return httpx.Response(200,json={'deleted':True})
        if request.url.path.endswith('/files'):return httpx.Response(200,json={'id':'file-complete-status'})
        requests.append(json.loads(request.content))
        return httpx.Response(200,json={'status':'completed','id':'fixture-complete-status','model':settings.muse_model,
            'output':[{'type':'message','content':[{'type':'output_text','text':json.dumps(raw)}]}]})
    with MetaCloud(replace(settings,model_api_key='fixture-key'),httpx.MockTransport(transport)) as cloud:
        batch=observe_window(cloud,prepared)
    assert batch.provider_provenance['remote_file_deleted'] is True
    assert requests[0]['text']==request_snapshot(prepared)['request']['text']
    assert requests[0]['text']['format']['strict'] is True and 'response_format' not in requests[0]
