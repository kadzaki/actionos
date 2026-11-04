from pathlib import Path
import json
from uuid import uuid4

import httpx
import pytest

from app.config import Settings
from app.errors import AppError
from app.providers.meta import MetaCloud
from app.providers.sam import parse_sam,track_window
from app.providers.muse import Candidate,IntentDraft
from app.providers.status import read_status
from app.providers.status import note_success
from app.providers.status import note_contract_success, read_contract_status
from app.sampling import VideoWindow

FIXTURES=Path(__file__).parent/'fixtures'/'meta'


def test_access_evidence_is_scoped_to_credential_endpoint_and_model(tmp_path):
    from dataclasses import replace
    settings=Settings(data_dir=tmp_path,model_api_key='test-secret')
    note_success(settings,settings.muse_model)
    assert set(read_status(settings))=={settings.muse_model}
    assert read_status(replace(settings,model_api_key='other-test-secret'))=={}
    path=tmp_path/'provider-status.json'
    record=json.loads(path.read_text())
    record['models'][settings.muse_model]['endpoint']='https://unrelated.invalid/v1'
    path.write_text(json.dumps(record))
    assert read_status(settings)=={}


def test_contract_qualification_is_separate_and_survives_transport_success(tmp_path):
    from dataclasses import replace
    settings=Settings(data_dir=tmp_path,model_api_key='fixture-key')
    note_success(settings,settings.muse_model)
    assert read_contract_status(settings)=={}
    note_contract_success(settings,'compiler','contract-test-1','response-fixture')
    note_success(settings,settings.sam_model)
    assert read_contract_status(settings)['compiler']['version']=='contract-test-1'
    assert read_contract_status(replace(settings,muse_model='another-model'))=={}
    assert read_contract_status(replace(settings,model_api_key='different-fixture-key'))=={}


def recorded_text(name):
    body=json.loads((FIXTURES/name).read_text(encoding='utf-8'))
    return ''.join(p.get('text','') for item in body['output'] for p in item.get('content',[]) if p.get('type')=='output_text')


def test_real_recorded_sam_video_geometry_and_explicit_frame_ids():
    text=recorded_text('sam-video.json')
    objects=parse_sam(text,18,320,180)
    assert len(objects)==1 and len(objects[0])==18
    assert objects[0][0]==pytest.approx((20/320,70/180,40/320,40/180))
    assert objects[0][17][0]==pytest.approx(132/320)
    lines=text.splitlines()
    out_of_order='\n'.join([lines[17],lines[0],lines[8]])
    assert set(parse_sam(out_of_order,18,320,180)[0])=={0,8,17}
    with pytest.raises(AppError):parse_sam(text,17,320,180)
    with pytest.raises(AppError):parse_sam(text,18,321,180)
    assert parse_sam('',18,320,180)=={}


def test_recorded_muse_multimodal_outputs_understand_observed_motion():
    for name in ('muse-native-video.json','muse-timestamped-frames.json'):
        value=json.loads(recorded_text(name))
        assert value['direction']=='left_to_right'
        assert 'red' in value['description'].lower()


def test_cloud_transport_redacts_auth_failures_and_retries_transient_errors(tmp_path,monkeypatch):
    settings=Settings(data_dir=tmp_path,model_api_key='test-secret')
    count=[]
    def transient(request):
        count.append(request)
        return httpx.Response(429 if len(count)<3 else 200,json={'ok':True})
    monkeypatch.setattr('app.providers.meta.time.sleep',lambda _:None)
    with MetaCloud(settings,httpx.MockTransport(transient)) as cloud:
        assert cloud.request('GET','test')=={'ok':True}
    assert len(count)==3
    with MetaCloud(settings,httpx.MockTransport(lambda _:httpx.Response(401,json={'leaked':'test-secret'}))) as cloud:
        with pytest.raises(AppError) as exc:cloud.request('GET','test')
    assert exc.value.code=='PROVIDER_AUTH'
    assert 'test-secret' not in str(exc.value.envelope())


def test_normalized_tracking_uses_recorded_cloud_response_and_cleans_upload(tmp_path):
    body=json.loads((FIXTURES/'sam-video.json').read_text())
    requests=[]
    def transport(request):
        requests.append((request.method,request.url.path))
        if request.method=='DELETE':return httpx.Response(200,json={'deleted':True})
        if request.url.path.endswith('/files'):return httpx.Response(200,json={'id':'file-unit-test'})
        output=''.join(part.get('text','') for item in body.get('output',[]) if item.get('type')=='message' for part in item.get('content',[]) if part.get('type')=='output_text')
        events=[{'type':'response.output_text.delta','delta':output},{'type':'response.completed','response':body}]
        return httpx.Response(200,headers={'Content-Type':'text/event-stream'},content=''.join('data: '+json.dumps(e)+'\n\n' for e in events))
    path=tmp_path/'fixture-input.mp4';path.write_bytes(b'recorded-response test only')
    window=VideoWindow(project_id=uuid4(),source_asset_id=uuid4(),source_sha256='0'*64,proxy_sha256='0'*64,
                       path='proxy/test.mp4',sha256='0'*64,requested_in_ms=0,requested_out_ms=1200,
                       first_working_frame_index=0,first_working_time_ms=0,duration_ms=1200,width=320,height=180,fps=15,
                       source_frame_indices=list(range(18)),source_frame_times_ms=[n*1000/15 for n in range(18)])
    settings=Settings(data_dir=tmp_path/'state',model_api_key='test-secret')
    with MetaCloud(settings,httpx.MockTransport(transport)) as cloud:
        tracks,provenance=track_window(cloud,window,path,'red square')
    assert len(tracks)==1 and len(tracks[0].samples)==18
    assert tracks[0].samples[-1].time_ms==1133
    assert tracks[0].origin=='meta_cloud'
    assert provenance['remote_file_deleted'] is True
    assert requests[-1][0]=='DELETE'
    assert settings.sam_model in read_status(settings)


def test_dotenv_is_loaded_without_interpolation_and_shell_takes_precedence(tmp_path,monkeypatch):
    file=tmp_path/'.env'
    file.write_text('MODEL_API_KEY="literal-${NOT_INTERPOLATED}"\nMAX_UPLOAD_MB=7\n')
    monkeypatch.delenv('MODEL_API_KEY',raising=False)
    monkeypatch.delenv('MAX_UPLOAD_MB',raising=False)
    settings=Settings.from_env(file)
    assert settings.model_api_key=='literal-${NOT_INTERPOLATED}' and settings.max_upload_mb==7
    monkeypatch.setenv('MODEL_API_KEY','shell-test')
    assert Settings.from_env(file).model_api_key=='shell-test'


def test_arbitrary_intent_and_phase_schema_have_no_sport_allowlist():
    draft=IntentDraft(action_description='a robot unfolds a map',subjects=['robot','paper map'])
    assert draft.action_description=='a robot unfolds a map'
    with pytest.raises(ValueError):
        Candidate(action_label='spin',start_ms=500,end_ms=100,primary_subject='dancer',
                  match_reason='visible rotation',evidence_ms=[400])


def test_sse_handles_split_utf8_records_and_requires_confirmed_completion(tmp_path):
    class Chunks(httpx.SyncByteStream):
        def __init__(self,raw):self.raw=raw
        def __iter__(self):
            for index in range(0,len(self.raw),3):yield self.raw[index:index+3]
    settings=Settings(data_dir=tmp_path,model_api_key='test-secret')
    def run(events):
        raw=': heartbeat\r\n\r\n'+''.join('event: delta\r\ndata: '+json.dumps(e,ensure_ascii=False)+'\r\n\r\n' for e in events)
        with MetaCloud(settings,httpx.MockTransport(lambda _:httpx.Response(200,headers={'content-type':'text/event-stream'},stream=Chunks(raw.encode())))) as cloud:
            return cloud.stream_response(settings.sam_model,[])
    done={'type':'response.completed','response':{'status':'completed','id':'stream-unit','model':settings.sam_model,'usage':{}}}
    value,metadata=run([{'type':'response.output_text.delta','delta':'café\n'},done])
    assert value=='café\n' and metadata['transport']=='sse'
    assert run([done])[0]==''  # Real zero-match streams are valid.
    with pytest.raises(AppError) as exc:run([{'type':'response.output_text.delta','delta':'partial'}])
    assert exc.value.code=='PROVIDER_INCOMPLETE'
    with pytest.raises(AppError):run([{'type':'response.failed','error':{'message':'test-secret'}}])
