from bisect import bisect_left, bisect_right
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
import math
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from app.engine.muse_observations import OBSERVATION_ADAPTER_VERSION, PreparedObservation, build_observation_request, normalize_response, observe_window
from app.engine.observation_repository import request_snapshot
from app.engine.observations import ObservationResponse
from app.engine.planning import ObservationWindow, build_plan
from app.errors import AppError
from app.programs.policy import SourceInterval
from app.providers.meta import MetaCloud
from app.sampling import extract_window
from app.source_evidence import SourceEvidenceAdapter
from app.repository import encoded
from test_evidence import prepare
from test_program_ast import draft


@pytest.fixture
def context(client,settings,media):
    asset=prepare(client,media['vfr'])
    store=client.app.state.store
    adapter=SourceEvidenceAdapter(store,settings,asset.project_id,asset.id)
    plan=build_plan(draft(),adapter.timeline,run_id=uuid4(),program_fingerprint='b'*64,
        duration_ms=asset.metadata.duration_ms,working_duration_ms=asset.working_metadata.duration_ms,
        timeline_sha256=asset.artifact_sha256[asset.timeline_path],proxy_sha256=asset.artifact_sha256[asset.proxy_path],
        proxy_width=asset.proxy_metadata.geometry.display_width,proxy_height=asset.proxy_metadata.geometry.display_height,
        providers={'muse_model':settings.muse_model,'endpoint':settings.meta_api_base_url.rstrip('/'),
            'muse_adapter_version':OBSERVATION_ADAPTER_VERSION,'sam_model':settings.sam_model})
    window=extract_window(store,settings,asset,300,900)
    times=adapter.timeline.source_frame_times_ms
    planned=ObservationWindow(id='w000',source_interval=SourceInterval(start_ms=300,end_ms=900),
        working_interval=SourceInterval(start_ms=300,end_ms=900),first_working_frame=window.first_working_frame_index,
        stop_working_frame=window.first_working_frame_index+len(window.source_frame_indices),
        input_source_frame_indices=window.source_frame_indices,required_source_first_frame=max(0,bisect_right(times,300)-1),
        required_source_stop_frame=bisect_left(times,900),omitted_original_frames=0,unrepresented_tail_ms=0,
        input_map_sha256=hashlib.sha256(encoded(window.source_frame_indices)).hexdigest(),sampling_covers_encoded_source=True)
    from app.programs.ast import ProgramAST
    return PreparedObservation(plan,planned,window,ProgramAST.model_validate(draft()),adapter)


def fixture_response(context):
    count=len(context.window.source_frame_indices)
    return {'schema_version':'predicate-observations-1','window_id':str(context.window.id),
        'entities':[{'entity_id':'operator','role':'actor','description':'Mock entity appearance, not a real detection',
            'visibility':'clear','identity':'unambiguous','evidence_frame_indices':[1]}],
        'hypotheses':[{'step_id':'place','entity_ids':['operator','component'],'assessment':'supported',
            'interval':{'start_frame':0,'stop_frame':6},'evidence_frame_indices':[1,3],
            'description':'Hand-authored mock hypothesis on generated media; not tabletop recognition',
            'quality':'high','uncertainty':[],'boundary_uncertainty_ms':50}],
        'coverage':[{'step_id':'place','attempted_frames':{'start_frame':0,'stop_frame':count},
            'observed_frame_ranges':[{'start_frame':0,'stop_frame':count}],
            'complete_inspection_claim':True,'visibility':'clear','identity':'unambiguous','resolution':'adequate','uncertainty':[]}],
        'notes':['Offline normalization fixture, no actual action detection']}


PROVENANCE={'provider':'meta_cloud','model':'fixture-muse','requested_model':'fixture-muse','response_id':'fixture-response',
            'endpoint':'https://api.meta.ai/v1','status':'completed','usage':{}}


def test_window_offset_and_vfr_use_original_pts_and_contextual_frame_witnesses(context):
    raw=fixture_response(context);original=deepcopy(raw)
    batch=normalize_response(context,raw,PROVENANCE)
    observation=batch.observations[0]
    times=context.source.timeline.source_frame_times_ms
    assert observation.source_interval.start_ms==max(300,math.ceil(context.window.source_frame_times_ms[0]))
    assert observation.provider_assessment=='supported' and observation.estimated is True
    for local,evidence in zip([1,3],observation.frames):
        index=context.window.source_frame_indices[local]
        assert evidence.source_frame_index==index
        assert evidence.source_pts==context.source.timeline.source_frame_pts[index]
        assert evidence.source_time_ms==times[index]
        context.source.validate_witness(index,evidence.observed_at_source_ms)
        assert observation.source_interval.start_ms<=evidence.observed_at_source_ms<observation.source_interval.end_ms
    assert batch.raw_provider_response==original and raw==original
    assert batch.missing_step_ids==['use']
    assert all(c.negative_coverage_verified is False for c in batch.coverage)
    assert batch.coverage[0].provider_claim.complete_inspection_claim is True
    assert batch.coverage[1].provider_claim is None and 'PROVIDER_COVERAGE_MISSING' in batch.coverage[1].reasons
    assert batch.coverage[1].attempted_source_interval is None
    assert 'verdict' not in observation.model_fields


def test_missing_occluded_and_conflicting_steps_cannot_become_false_or_success(context):
    raw=fixture_response(context)
    missing=normalize_response(context,{**raw,'hypotheses':[],'coverage':[]},PROVENANCE)
    assert missing.observations==[] and missing.missing_step_ids==['place','use']
    raw['hypotheses'].append({'step_id':'use','entity_ids':[],'assessment':'not_observed','description':'Tool step occluded',
        'quality':'low','uncertainty':['tool is occluded']})
    batch=normalize_response(context,raw,PROVENANCE)
    assert batch.observations[1].provider_assessment=='not_observed'
    assert batch.observations[1].source_interval is None and not batch.observations[1].frames
    raw=fixture_response(context)
    opposite=deepcopy(raw['hypotheses'][0]);opposite['assessment']='disconfirmed'
    raw['hypotheses'].append(opposite)
    batch=normalize_response(context,raw,PROVENANCE)
    assert all(o.hypothesis=='uncertain' and 'CONTRADICTORY_PROVIDER_HYPOTHESES' in o.uncertainty for o in batch.observations)


@pytest.mark.parametrize('kind,code',[
    ('window','PROVIDER_WINDOW'),('step','PROVIDER_STEP'),('role','PROVIDER_ENTITY'),
    ('frame','PROVIDER_TIMESTAMP'),('range','PROVIDER_TIMESTAMP'),('coverage','PROVIDER_COVERAGE'),
    ('verdict','PROVIDER_SCHEMA'),('evidence_without_interval','PROVIDER_SCHEMA')])
def test_invalid_foreign_or_out_of_window_fields_fail_explicitly(context,kind,code):
    raw=fixture_response(context);count=len(context.window.source_frame_indices)
    if kind=='window':raw['window_id']=str(uuid4())
    elif kind=='step':raw['hypotheses'][0]['step_id']='foreign'
    elif kind=='role':raw['entities'][0]['role']='instrument'
    elif kind=='frame':
        raw['hypotheses'][0]['interval']['stop_frame']=count+2
        raw['hypotheses'][0]['evidence_frame_indices']=[count+1]
    elif kind=='range':raw['coverage'][0]['attempted_frames']['stop_frame']=count+1
    elif kind=='coverage':raw['coverage'][0]['attempted_frames']['stop_frame']=2
    elif kind=='verdict':raw['verdict']='true'
    elif kind=='evidence_without_interval':raw['hypotheses'][0]['interval']=None
    with pytest.raises(AppError) as exc:normalize_response(context,raw,PROVENANCE)
    assert exc.value.code==code


def test_observation_limit_and_frame_display_interval_are_not_silently_repaired(context):
    with pytest.raises(AppError) as exc:normalize_response(context,fixture_response(context),PROVENANCE,remaining_observations=0)
    assert exc.value.code=='OBSERVATION_LIMIT'
    times=context.source.timeline.source_frame_times_ms
    with pytest.raises(AppError) as exc:context.source.frame_witness(0,times[1])
    assert exc.value.code=='EVIDENCE_TIME_MISMATCH'
    witness=context.source.frame_witness(0,(times[0]+times[1])/2)
    assert witness.source_time_ms==times[0] and witness.observed_at_source_ms>times[0]


def test_actual_adapter_transport_path_is_mocked_only_in_tests_and_always_cleans_media(context,settings):
    calls=[];raw=fixture_response(context)
    def transport(request):
        calls.append((request.method,request.url.path))
        if request.method=='DELETE':return httpx.Response(200,json={'deleted':True})
        if request.url.path.endswith('/files'):return httpx.Response(200,json={'id':'file-fixture-observation'})
        body=json.loads(request.content)
        assert body['input'][0]['content'][-1]=={'type':'input_video','file_id':'file-fixture-observation'}
        return httpx.Response(200,json={'status':'completed','id':'fixture-observation','model':settings.muse_model,
            'usage':{'input_tokens':10},'output':[{'type':'message','content':[{'type':'output_text','text':json.dumps(raw)}]}]})
    configured=replace(settings,model_api_key='fixture-only-key')
    recordings=[]
    with MetaCloud(configured,httpx.MockTransport(transport)) as cloud:
        batch=observe_window(cloud,context,record_response=lambda output,provenance:recordings.append((output,provenance)))
    assert calls[-1][0]=='DELETE'
    assert batch.provider_provenance['remote_file_deleted'] is True
    assert recordings[0][0]==raw and recordings[0][1]['remote_file_deleted'] is True
    assert 'fixture-only-key' not in batch.model_dump_json()
    content,schema=build_observation_request(context)
    assert 'never final rule truth' in content[0]['text']
    assert set(schema['properties'])=={'schema_version','window_id','entities','hypotheses','coverage','notes'}


def test_schema_failure_still_deletes_uploaded_media_and_preserves_typed_error(context,settings):
    raw=fixture_response(context);raw['hypotheses'][0]['step_id']='foreign'
    calls=[]
    def transport(request):
        calls.append(request.method)
        if request.method=='DELETE':return httpx.Response(200,json={'deleted':True})
        if request.url.path.endswith('/files'):return httpx.Response(200,json={'id':'file-fixture-observation'})
        return httpx.Response(200,json={'status':'completed','id':'fixture','output':[
            {'type':'message','content':[{'type':'output_text','text':json.dumps(raw)}]}]})
    with MetaCloud(replace(settings,model_api_key='fixture-key'),httpx.MockTransport(transport)) as cloud:
        with pytest.raises(AppError) as exc:observe_window(cloud,context)
    assert exc.value.code=='PROVIDER_STEP' and exc.value.details['remote_file_deleted'] is True
    assert calls[-1]=='DELETE'


def test_run_provider_snapshot_is_checked_before_any_upload(context,settings):
    def no_network(request):
        pytest.fail('A mismatched run must not transmit media')
    with MetaCloud(replace(settings,model_api_key='fixture-key',muse_model='different-model'),httpx.MockTransport(no_network)) as cloud:
        with pytest.raises(AppError) as exc:observe_window(cloud,context)
    assert exc.value.code=='PROVIDER_CONFIGURATION_MISMATCH'


@pytest.mark.parametrize('reply',['not JSON <untrusted reply>','```json\n{"incomplete":\n```',
    json.loads((Path(__file__).parent/'fixtures/meta/muse-adapted-invalid-json-w002.json').read_text(encoding='utf-8'))['text']])
def test_unparseable_provider_text_is_captured_before_failure_and_media_is_deleted(context,settings,reply):
    calls=[];captured=[]
    def transport(request):
        calls.append(request.method)
        if request.method=='DELETE':return httpx.Response(200,json={'deleted':True})
        if request.url.path.endswith('/files'):return httpx.Response(200,json={'id':'file-invalid-json'})
        return httpx.Response(200,json={'status':'completed','id':'fixture-invalid-json','model':settings.muse_model,
            'output':[{'type':'message','content':[{'type':'output_text','text':reply}]}]})
    with MetaCloud(replace(settings,model_api_key='fixture-private-key'),httpx.MockTransport(transport)) as cloud:
        with pytest.raises(AppError) as exc:
            observe_window(cloud,context,record_text_response=lambda text,metadata:captured.append((text,metadata)))
    assert exc.value.code=='PROVIDER_SCHEMA' and exc.value.details['remote_file_deleted'] is True
    assert calls==['POST','POST','DELETE']
    assert captured[0][0]==reply and captured[0][1]['response_id']=='fixture-invalid-json'
    assert 'fixture-private-key' not in json.dumps(captured)
    assert reply not in json.dumps(exc.value.envelope())


def test_run_pinned_prompt_versions_preserve_legacy_text_and_normalized_provenance(context):
    legacy_plan=context.plan.model_copy(update={'providers':{**context.plan.providers,'muse_adapter_version':'muse-step-observations-1'}})
    legacy=replace(context,plan=legacy_plan)
    old_content,old_schema=build_observation_request(legacy)
    new_content,new_schema=build_observation_request(context)
    frozen=(Path(__file__).parent/'fixtures/meta/observation-prompt-v1.txt').read_text(encoding='utf-8')
    assert old_content[0]['text'].split('\nInput data: ',1)[0]==frozen
    assert old_schema==new_schema
    assert 'already-held state is not its onset' in new_content[0]['text']
    assert 'timing-only precision' in new_content[0]['text']
    old=normalize_response(legacy,fixture_response(legacy),PROVENANCE)
    new=normalize_response(context,fixture_response(context),PROVENANCE)
    assert old.provider_provenance['adapter_version']=='muse-step-observations-1'
    assert old.provider_provenance['prompt_version']=='program-window-evidence-1'
    assert new.provider_provenance['adapter_version']==OBSERVATION_ADAPTER_VERSION
    assert new.provider_provenance['prompt_version']=='program-window-evidence-2'


@pytest.mark.parametrize('version',[1,2,3])
def test_native_schema_wire_and_saved_request_follow_the_run_pin(context,settings,version):
    plan=context.plan.model_copy(update={'providers':{**context.plan.providers,'muse_adapter_version':f'muse-step-observations-{version}'}})
    prepared=replace(context,plan=plan);requests=[]
    def transport(request):
        if request.method=='DELETE':return httpx.Response(200,json={'deleted':True})
        if request.url.path.endswith('/files'):return httpx.Response(200,json={'id':'file-version-test'})
        body=json.loads(request.content);requests.append(body)
        return httpx.Response(200,json={'status':'completed','id':'fixture-version-test','model':settings.muse_model,
            'output':[{'type':'message','content':[{'type':'output_text','text':json.dumps(fixture_response(prepared))}]}]})
    with MetaCloud(replace(settings,model_api_key='fixture-key'),httpx.MockTransport(transport)) as cloud:
        observe_window(cloud,prepared)
    wire=requests[0];saved=request_snapshot(prepared)['request']
    assert 'response_format' not in wire
    if version==3:
        assert wire['text']==saved['text']=={'format':{'type':'json_schema','name':'actionos_response',
            'schema':ObservationResponse.model_json_schema(),'strict':False}}
    else:
        assert 'text' not in wire and 'text' not in saved
