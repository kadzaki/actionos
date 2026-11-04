from contextlib import contextmanager
import hashlib,json,time
from pathlib import Path

import httpx

from ..config import Settings
from ..errors import AppError
from .status import note_success
from ..validation import WorkBudget, bounded_json


def structured_text_format(schema,*,strict=False):
    bounded_json(schema,max_bytes=131072)
    if strict:
        schema=json.loads(json.dumps(schema))
        pending=[schema]
        while pending:
            node=pending.pop()
            if isinstance(node,dict):
                if node.get('type')=='object':
                    node['required']=list(node.get('properties',{}))
                    node['additionalProperties']=False
                pending.extend(node.values())
            elif isinstance(node,list):pending.extend(node)
    return {'type':'json_schema','name':'actionos_response','schema':schema,'strict':strict}


class MetaCloud:
    def __init__(self,settings: Settings,transport=None,*,budget=None):
        if not settings.model_api_key:
            raise AppError('PROVIDER_UNAVAILABLE','Meta API key is not configured on the server',503)
        self.settings=settings
        self.budget=budget or WorkBudget()
        self.client=httpx.Client(base_url=settings.meta_api_base_url.rstrip('/')+'/',
                                 headers={'Authorization':'Bearer '+settings.model_api_key},
                                 timeout=httpx.Timeout(180,connect=20),follow_redirects=False,transport=transport)

    def close(self):self.client.close()

    def __enter__(self):return self
    def __exit__(self,*_):self.close()

    def request(self,method,path,**kwargs):
        for attempt in range(3):
            if method.upper()=='POST' and path=='responses':
                self.budget.consume_provider_call()
            try:
                with self.client.stream(method,path,**kwargs) as response:
                    if response.status_code in (429,502,503,504) and attempt<2:
                        time.sleep(.25*2**attempt)
                        continue
                    if response.status_code>=400:
                        code={401:'PROVIDER_AUTH',403:'PROVIDER_ACCESS_DENIED',429:'PROVIDER_QUOTA'}.get(response.status_code,'PROVIDER_REQUEST_FAILED')
                        raise AppError(code,'Meta cloud request was rejected; check server access or retry',503,
                                       response.status_code in (429,500,502,503,504),
                                       {'provider_status':response.status_code})
                    chunks=[];size=0
                    for chunk in response.iter_bytes():
                        size+=len(chunk)
                        if size>16*1024*1024:
                            raise AppError('PROVIDER_OUTPUT_LIMIT','Meta response exceeded the application size limit',502)
                        chunks.append(chunk)
                    try:
                        decoded=json.loads(b''.join(chunks))
                        if not isinstance(decoded,dict):raise ValueError('Expected object')
                        return decoded
                    except (ValueError,UnicodeDecodeError):
                        raise AppError('PROVIDER_INVALID_RESPONSE','Meta returned an invalid JSON envelope',502) from None
            except httpx.TimeoutException:
                raise AppError('PROVIDER_TIMEOUT','Meta cloud request timed out; retry without losing your source',504,True) from None
            except httpx.HTTPError:
                raise AppError('PROVIDER_CONNECTION','Could not reach Meta cloud API',503,True) from None

    @contextmanager
    def video_file(self,path: Path):
        with path.open('rb') as stream:
            uploaded=self.request('POST','files',data={'purpose':'user_data'},
                                  files={'file':('source-window.mp4',stream,'video/mp4')})
        file_id=uploaded.get('id')
        if not isinstance(file_id,str) or not file_id.startswith('file-'):
            raise AppError('PROVIDER_INVALID_RESPONSE','Meta did not return a valid uploaded file ID',502)
        cleanup={'deleted':False}
        try:yield file_id,cleanup
        finally:
            try:
                result=self.request('DELETE','files/'+file_id)
                cleanup['deleted']=result.get('deleted',False) is True
            except AppError:
                cleanup['deleted']=False

    def response(self,model,content,*,text_format=None):
        bounded_json(content,max_bytes=2*1024*1024)
        payload={'model':model,'stream':False,'input':[{'type':'message','role':'user','content':content}]}
        if text_format is not None:payload['text']={'format':text_format}
        body=self.request('POST','responses',json=payload)
        if body.get('status')!='completed' or body.get('error'):
            raise AppError('PROVIDER_INCOMPLETE','Meta response did not complete successfully',502,True)
        text=''.join(part.get('text','') for item in body.get('output',[]) if item.get('type')=='message'
                     for part in item.get('content',[]) if part.get('type')=='output_text')
        provenance={'provider':'meta_cloud','model':body.get('model',model),'requested_model':model,
                    'response_id':body.get('id'),'usage':body.get('usage'),
                    'endpoint':self.settings.meta_api_base_url,'status':'completed'}
        note_success(self.settings,model)
        return text,provenance

    def stream_response(self,model,content):
        """Consume bounded SSE records; never accept a truncated stream as done."""
        payload={'model':model,'stream':True,'input':[{'type':'message','role':'user','content':content}]}
        bounded_json(payload,max_bytes=2*1024*1024)
        for attempt in range(3):
            self.budget.consume_provider_call()
            try:
                with self.client.stream('POST','responses',json=payload,headers={'Accept':'text/event-stream'}) as response:
                    if response.status_code in (429,502,503,504) and attempt<2:
                        time.sleep(.25*2**attempt);continue
                    if response.status_code>=400:
                        code={401:'PROVIDER_AUTH',403:'PROVIDER_ACCESS_DENIED',429:'PROVIDER_QUOTA'}.get(response.status_code,'PROVIDER_REQUEST_FAILED')
                        raise AppError(code,'Meta cloud stream was rejected; check server access or retry',503,
                                       response.status_code in (429,500,502,503,504),{'provider_status':response.status_code})
                    if 'text/event-stream' not in response.headers.get('content-type',''):
                        raise AppError('PROVIDER_INVALID_RESPONSE','Meta did not return the requested event stream',502)
                    buffer=b'';data=[];text=[];size=0;completed=None
                    def consume(lines):
                        nonlocal completed
                        if not lines:return
                        raw=b'\n'.join(lines)
                        if raw==b'[DONE]':return
                        try:event=json.loads(raw)
                        except (ValueError,UnicodeDecodeError):
                            raise AppError('PROVIDER_INVALID_RESPONSE','Meta stream contained invalid JSON',502) from None
                        if not isinstance(event,dict):
                            raise AppError('PROVIDER_INVALID_RESPONSE','Meta stream contained an invalid event',502)
                        kind=event.get('type')
                        if kind=='response.output_text.delta':
                            delta=event.get('delta')
                            if not isinstance(delta,str):raise AppError('PROVIDER_SCHEMA','Meta text delta was invalid',502)
                            text.append(delta)
                        elif kind=='response.completed':completed=event.get('response')
                        elif kind in ('error','response.failed','response.incomplete'):
                            raise AppError('PROVIDER_INCOMPLETE','Meta stream did not complete successfully',502,True)
                    for chunk in response.iter_bytes():
                        size+=len(chunk)
                        if size>16*1024*1024:
                            raise AppError('PROVIDER_OUTPUT_LIMIT','Meta response exceeded the application size limit',502)
                        buffer+=chunk
                        while b'\n' in buffer:
                            line,buffer=buffer.split(b'\n',1);line=line.rstrip(b'\r')
                            if not line:consume(data);data=[]
                            elif line.startswith(b'data:'):data.append(line[5:].lstrip(b' '))
                    if buffer.startswith(b'data:'):data.append(buffer[5:].lstrip(b' ').rstrip(b'\r'))
                    consume(data)
                    if not isinstance(completed,dict) or completed.get('status')!='completed' or completed.get('error'):
                        raise AppError('PROVIDER_INCOMPLETE','Meta stream ended before confirmed completion',502,True)
                    provenance={'provider':'meta_cloud','model':completed.get('model',model),'requested_model':model,
                                'response_id':completed.get('id'),'usage':completed.get('usage'),
                                'endpoint':self.settings.meta_api_base_url,'status':'completed','transport':'sse'}
                    note_success(self.settings,model)
                    return ''.join(text),provenance
            except httpx.TimeoutException:
                raise AppError('PROVIDER_TIMEOUT','Meta cloud stream timed out; source remains available',504,True) from None
            except httpx.HTTPError:
                raise AppError('PROVIDER_CONNECTION','Meta cloud stream disconnected; source remains available',503,True) from None

    def json_response(self,model,content,schema,*,record_text_response=None,native_schema=False,text_format=None):
        bounded_json(schema,max_bytes=131072)
        instruction={'type':'input_text','text':'Return only a JSON object conforming to this application schema: '+json.dumps(schema,separators=(',',':'))}
        options={'text_format':text_format} if text_format is not None else ({'text_format':structured_text_format(schema)} if native_schema else {})
        text,provenance=self.response(model,[instruction,*content],**options)
        if record_text_response is not None:
            record_text_response(text,provenance)
        raw=text.strip()
        if raw.startswith('```'):
            lines=raw.splitlines();raw='\n'.join(lines[1:-1])
        try:
            value=json.loads(raw)
            bounded_json(value,max_bytes=4*1024*1024)
            if not isinstance(value,dict):raise ValueError('Expected object')
            return value,provenance
        except (ValueError,RecursionError,AppError):
            raise AppError('PROVIDER_SCHEMA','Muse did not return a valid structured result',502,True) from None
