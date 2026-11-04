"""A22 local browser QA with a conspicuously labeled recorded-response replay.

Never a production fallback. Replays the approved A19 recording in this test
process only; provider request methods raise instead of contacting Meta.
Exercises the real React UI, compiler job routes, immutable edits and review API.
"""
import asyncio
from dataclasses import replace
from html import unescape
import json
from pathlib import Path
import re
import socket
import subprocess
import sys
from threading import Thread
import time
from uuid import uuid4

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient
import uvicorn
from app.config import Settings
from app.errors import AppError
from app.main import create_app
from app.programs.compiler import Compilation
from app.providers.meta import MetaCloud
import app.programs.routes as program_routes
from browser_action_proof import browser_binary


def wait_job(client,job_id):
    deadline=time.monotonic()+60
    while time.monotonic()<deadline:
        job=client.get(f'/api/jobs/{job_id}').json()
        if job['status'] in ('completed','failed','interrupted','cancelled'):
            assert job['status']=='completed',job
            return job
        time.sleep(.05)
    raise RuntimeError('Source preparation did not finish')


def script(project_id,instruction,readonly=False,view='program'):
    return '''<script>
localStorage.setItem('videocut.project',PROJECT_ID);
window.addEventListener('DOMContentLoaded',async()=>{
const panel=document.createElement('div');panel.id='qa-results';panel.style='position:fixed;left:0;top:0;right:0;background:#38234f;color:white;padding:9px;z-index:99999;font:12px system-ui';
panel.textContent='OFFLINE INSPECTOR QA: recorded A19 response replay; no new Meta requests.';document.body.prepend(panel);
const evidence={scope:'recorded_response_replay_ui_only',live_meta_requests:0,browser_user_agent:navigator.userAgent};
const pause=()=>new Promise(resolve=>setTimeout(resolve,50));
async function until(test){const deadline=Date.now()+22000;while(Date.now()<deadline){const value=test();if(value)return value;await pause();}throw new Error('UI condition timed out');}
function assert(value,message){if(!value)throw new Error(message);}
try{
 const input=await until(()=>{const el=document.querySelector('#visual-instruction');return el&&!el.disabled&&el;});
 assert(document.querySelector('label[for="visual-instruction"]').control===input,'Instruction label is not associated');
 if(!READ_ONLY){
  const setter=Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set;
  setter.call(input,INSTRUCTION);input.dispatchEvent(new Event('input',{bubbles:true}));
  const compile=await until(()=>{const el=document.querySelector('#compile-program');return el&&!el.disabled&&el;});
  compile.click();
  await until(()=>document.querySelector('#approve-program'));
  assert(document.querySelector('.program-outcome').textContent.includes('Clarification needed'),'Material assumptions did not block review');
  assert(document.querySelector('#approve-program').disabled,'Unresolved assumptions were approvable');
  assert(document.querySelectorAll('.predicate-list li').length===2,'Typed predicate cards missing');
  assert(document.querySelector('.program-inspector').textContent.includes('Not observed does not mean'),'Unknown semantics missing');
  const checks=[...document.querySelectorAll('.assumption-list input[type=checkbox]')];
  assert(checks.length===2,'Recorded material assumptions missing');
  checks.forEach(el=>el.click());
  const confirm=await until(()=>{const el=document.querySelector('#confirm-assumptions');return el&&!el.disabled&&el;});
  confirm.click();
  await until(()=>document.querySelector('.program-version')?.textContent.includes('version 2'));
  const approve=await until(()=>{const el=document.querySelector('#approve-program');return el&&!el.disabled&&el;});
  approve.focus();assert(document.activeElement===approve,'Review button could not receive focus');approve.click();
  await until(()=>document.querySelector('#approve-program')?.textContent.includes('Version 2 approved'));
  assert(document.querySelector('#run-program').disabled,'Unimplemented evaluation was enabled');
  const project=await (await fetch('/api/projects/'+PROJECT_ID)).json();
  const id=project.project.active_program_id;
  const old=await (await fetch('/api/programs/'+id+'?version=1')).json();
  const next=await (await fetch('/api/programs/'+id+'?version=2')).json();
  assert(old.outcome.state==='clarification_required'&&!old.review_approved,'Historical program was changed');
  assert(next.review_approved&&next.human_edit.base_fingerprint===old.program_fingerprint,'Review lineage missing');
  evidence.program_id=id;evidence.original_version=1;evidence.confirmed_version=2;evidence.original_unchanged=true;evidence.review_bound=true;
  setter.call(input,INSTRUCTION+' Only count cycles where the component starts on the left.');input.dispatchEvent(new Event('input',{bubbles:true}));
  await until(()=>document.querySelector('#draft-changed'));
  assert(document.querySelector('#approve-program').disabled&&document.querySelector('#run-program').disabled,'Edited text reused old review');
  evidence.edited_text_invalidates_current_controls=true;
 }else{
  await until(()=>document.querySelector('#approve-program')?.textContent.includes('Version 2 approved'));
  if(VIEW_MODE==='original'){
   const versions=document.querySelector('select[aria-label="Program version"]');versions.value='1';versions.dispatchEvent(new Event('change',{bubbles:true}));
   await until(()=>document.querySelector('.program-version')?.textContent.includes('version 1'));
   assert(document.querySelector('#approve-program').disabled,'Original unresolved version became approved');
  }
  evidence.approved_version_visible=true;
 }
 evidence.passed=true;
}catch(error){evidence.passed=false;evidence.error=String(error);}
const result=document.createElement('pre');result.id='inspector-test-result';result.hidden=true;result.textContent=JSON.stringify(evidence);document.body.append(result);
panel.textContent+=' Test '+(evidence.passed?'PASSED':'FAILED: '+evidence.error);
});
</script>'''.replace('PROJECT_ID',json.dumps(project_id)).replace('INSTRUCTION',json.dumps(instruction)).replace('READ_ONLY','true' if readonly else 'false').replace('VIEW_MODE',json.dumps(view))


def main():
    root=Path(__file__).resolve().parents[2]
    output=root/'.tmp/inspector-proof';output.mkdir(parents=True,exist_ok=True)
    recorded=Compilation.model_validate_json((root/'.tmp/compiler-proof/compilation.json').read_text(encoding='utf-8'))
    settings=replace(Settings.from_env(),data_dir=output/'data',model_api_key='offline-ui-fixture')
    counts={'replays':0,'live_requests':0}
    def replay(cloud,instruction,source,*,record_response=None):
        if instruction!=recorded.original_instruction or any(source.get(key)!=getattr(recorded.source,key) for key in ('duration_ms','width','height','audio_present')):
            raise AppError('FIXTURE_INPUT_MISMATCH','Offline QA replays only its labeled recorded instruction',422)
        counts['replays']+=1
        result=recorded.model_copy(deep=True)
        if record_response:
            record_response(result.provider_output,{'provider':'meta_cloud','recorded_replay':True,
                'response_id':result.compiler.response_id,'model':result.compiler.returned_model,
                'requested_model':result.compiler.requested_model,'usage':result.compiler.usage,'endpoint':result.compiler.endpoint})
        return result
    def reject_network(*args,**kwargs):
        counts['live_requests']+=1
        raise AppError('OFFLINE_NETWORK_FORBIDDEN','Live Meta requests are disabled in this browser QA process',503)
    program_routes.compile_instruction=replay
    MetaCloud.request=reject_network
    MetaCloud.stream_response=reject_network
    with TestClient(create_app(settings)) as client:
        project_id=client.post('/api/projects',json={'name':'OFFLINE INSPECTOR QA - recorded A19 replay'}).json()['project_id']
        source=(root/'.tmp/manual-action-proof/generated-source.mp4').read_bytes()
        accepted=client.post(f'/api/projects/{project_id}/upload',content=source,headers={'Content-Type':'video/mp4'})
        accepted.raise_for_status();wait_job(client,accepted.json()['job_id'])
    app=create_app(settings)
    html=(settings.static_dir/'index.html').read_text(encoding='utf-8')
    @app.get('/__qa__/inspector',include_in_schema=False)
    def qa(readonly:bool=False,view:str='program'):
        page=html.replace('</head>',script(project_id,recorded.original_instruction,readonly,view)+'</head>')
        page=page.replace('</body>','<img hidden src="/__qa__/hold"></body>')
        return HTMLResponse(page)
    app.router.routes.insert(0,app.router.routes.pop())
    @app.get('/__qa__/hold',include_in_schema=False)
    async def hold():
        await asyncio.sleep(3)
        return HTMLResponse('')
    app.router.routes.insert(0,app.router.routes.pop())
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,log_level='warning'))
    thread=Thread(target=server.run,daemon=True);thread.start()
    deadline=time.monotonic()+10
    while not server.started and thread.is_alive() and time.monotonic()<deadline:time.sleep(.05)
    if not server.started:raise RuntimeError('QA server did not start')
    profile=output/f'browser-profile-{uuid4().hex}'
    common=[browser_binary(),'--headless=new',f'--user-data-dir={profile}','--no-first-run',
        '--no-default-browser-check','--disable-background-networking','--disable-component-update',
        '--disable-sync','--disable-default-apps','--disable-background-timer-throttling','--disable-renderer-backgrounding',
        '--window-size=1600,3200','--run-all-compositor-stages-before-draw']
    try:
        for view in ('workflow','review'):
            url=f'http://127.0.0.1:{port}/__qa__/inspector'+('?readonly=true' if view=='review' else '')
            process=subprocess.run([*common,'--dump-dom','--virtual-time-budget=30000',url],
                capture_output=True,timeout=45,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            dom=process.stdout.decode('utf-8',errors='replace')
            (output/(view+'.html')).write_text(dom,encoding='utf-8')
            match=re.search(r'<pre id="inspector-test-result"[^>]*>(.*?)</pre>',dom,re.S)
            if not match:raise RuntimeError('Browser QA did not produce a result; inspect '+str(output/(view+'.html')))
            result=json.loads(unescape(match.group(1)))
            result.update(counts)
            (output/(view+'.json')).write_text(json.dumps(result,indent=2),encoding='utf-8')
            if not result.get('passed'):raise RuntimeError(str(result))
        for view in ('program','original'):
            subprocess.run([*common,f'--screenshot={(output/(view+".png")).resolve()}',
                f'http://127.0.0.1:{port}/__qa__/inspector?readonly=true&view={view}'],
                capture_output=True,check=True,timeout=30,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        assert counts['live_requests']==0 and counts['replays']==1,counts
        print(json.dumps({'passed':True,'scope':'recorded_response_replay_ui_only','counts':counts,
                          'evidence_dir':str(output),'visual_review':'pending'}),flush=True)
    finally:
        server.should_exit=True;thread.join(timeout=10)


if __name__=='__main__':main()
