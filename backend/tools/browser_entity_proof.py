"""A26 isolated Studio/API QA. Hand-authored candidates, zero Meta requests.

Clones the reviewed A22 QA project into a new data directory. This never installs
a production perception fallback or changes the original proof data/user profile.
"""
import asyncio
from dataclasses import replace
from html import unescape
import json
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
from threading import Thread
import time
from uuid import uuid4

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from fastapi.responses import HTMLResponse
import uvicorn
from app.config import Settings
from app.engine.entity_repository import publish_entity_batch
from app.engine.entity_tracks import ENTITY_ADAPTER_VERSION,normalize_entity_tracks
from app.engine.muse_observations import OBSERVATION_ADAPTER_VERSION,prepare_window
from app.engine.planning import plan_run
from app.errors import AppError
from app.main import create_app
from app.models import Project
from app.programs.repository import load_program
from app.providers.meta import MetaCloud
from app.runs import snapshot_run
from app.storage import FileStore
from browser_action_proof import browser_binary


def script(project_id,readonly):
    return '''<style>.program-body > :not(.entity-inspector){display:none!important}.legacy-workflow{display:none!important}</style>
<script>localStorage.setItem('videocut.project',PROJECT_ID);
window.addEventListener('DOMContentLoaded',async()=>{
const banner=document.createElement('div');banner.style='position:fixed;inset:0 0 auto;background:#43295b;color:white;padding:10px;z-index:99999;font:13px system-ui';
banner.textContent='A26 OFFLINE UI QA: hand-authored SAM candidates on generated media; no live recognition or Meta requests.';document.body.prepend(banner);
const result={scope:'hand_authored_entity_candidates_real_studio_api',cloud_requests_made:0,user_agent:navigator.userAgent};
const pause=()=>new Promise(resolve=>setTimeout(resolve,50));
async function until(test){const deadline=Date.now()+25000;while(Date.now()<deadline){const value=test();if(value)return value;await pause();}throw new Error('UI condition timed out');}
function assert(value,message){if(!value)throw new Error(message);}
function change(el,value){const setter=Object.getOwnPropertyDescriptor(el instanceof HTMLTextAreaElement?HTMLTextAreaElement.prototype:HTMLInputElement.prototype,'value').set;setter.call(el,String(value));el.dispatchEvent(new Event('input',{bubbles:true}));}
try{
 const details=await until(()=>document.querySelector('#entity-tracking'));details.open=true;
 const candidates=await until(()=>{const e=document.querySelector('select[aria-label="Entity candidate"]');return e&&e.options.length===2&&e;});
 await until(()=>document.querySelector('.entity-frame img')?.complete&&document.querySelector('.entity-frame img')?.naturalWidth);
 assert(document.querySelector('.entity-batch').textContent.includes('HAND-AUTHORED'),'Fixture scope is hidden');
 assert(candidates.options[0].text.includes('identity unknown')&&candidates.options[1].text.includes('identity unknown'),'Candidates imply identity');
 const bind=document.querySelector('#bind-entity-segment');
 if(!READ_ONLY){
  candidates.value=candidates.options[1].value;candidates.dispatchEvent(new Event('change',{bubbles:true}));
  await until(()=>!bind.disabled);bind.focus();assert(document.activeElement===bind,'Selection cannot receive keyboard focus');bind.click();
  await until(()=>document.querySelector('.entity-batch').textContent.includes('Saved candidate'));
  result.selection_saved=true;
  const frame=document.querySelector('input[aria-label="Tracking input frame"]');change(frame,2);
  await until(()=>document.querySelector('.entity-batch').textContent.includes('No SAM sample for this candidate'));
  assert(bind.disabled,'Missing sample allowed binding');result.gap_disables_selection=true;
  change(frame,4);await until(()=>!bind.disabled);
  const instruction=document.querySelector('#visual-instruction'),original=instruction.value;
  change(instruction,original+' Only count cycles starting on the left.');await until(()=>bind.disabled);
  result.edited_instruction_disables_binding=true;
  change(instruction,original);await until(()=>!bind.disabled);
 }else{
  await until(()=>document.querySelector('.entity-batch').textContent.includes('Saved candidate'));
  result.selection_survives_reload=true;
 }
 assert(document.querySelector('.entity-batch').textContent.includes('Identity unknown'),'Selection rewrote raw identity');
 assert(document.querySelector('#run-program').disabled,'Unimplemented evaluation is enabled');
 result.raw_identity_remains_unknown=true;result.passed=true;
}catch(error){result.passed=false;result.error=String(error);}
const output=document.createElement('pre');output.id='entity-test-result';output.hidden=true;output.textContent=JSON.stringify(result);document.body.append(output);
banner.textContent+=' Test '+(result.passed?'PASSED':'FAILED: '+result.error);
});</script>'''.replace('PROJECT_ID',json.dumps(str(project_id))).replace('READ_ONLY','true' if readonly else 'false')


def main():
    root=Path(__file__).resolve().parents[2];output=root/'.tmp/entity-inspector-proof';output.mkdir(parents=True,exist_ok=True)
    old=json.loads((root/'.tmp/inspector-proof/workflow.json').read_text(encoding='utf-8'))
    source_data=root/'.tmp/inspector-proof/data'
    paths=list((source_data/'projects').glob(f'*/programs/{old["program_id"]}/v2.json'))
    assert len(paths)==1
    project_id=paths[0].parents[2].name
    data=output/f'data-{uuid4().hex}'
    shutil.copytree(paths[0].parents[2],data/'projects'/project_id)
    settings=replace(Settings.from_env(),data_dir=data,model_api_key='')
    store=FileStore(data);store.acquire_process_lock()
    try:
        project=store.load(project_id,'project',Project)
        project.name='A26 OFFLINE QA - hand-authored tracking candidates';store.save(project_id,'project',project)
        _,program=load_program(store,project_id,old['program_id'],2)
        run=snapshot_run(store,settings,project_id,old['program_id'],2,muse_adapter_version=OBSERVATION_ADAPTER_VERSION,
            sam_adapter_version=ENTITY_ADAPTER_VERSION,evaluator_version='not-implemented')
        plan=plan_run(store,settings,project_id,run.id)
        prepared=prepare_window(store,settings,project_id,run.id,plan.id,'w000')
        w,h=prepared.window.width,prepared.window.height
        lines=[]
        for index in (0,1,4,5):
            boxes=[]
            for object_id,x in ((1,w//5),(2,w*3//5)):
                boxes.append(f'{object_id}<|box;x1={x};y1={h//3};x2={x+w//6};y2={h//3+h//4};w={w};h={h}|><|mask;x=0;y=0;data={w},{h},~fixture|>')
            lines.append(f'<{index}f>'+','.join(boxes))
        text='\n'.join(lines)
        entity=next(e.id for e in program.program.ast.entities if e.role=='object')
        batch=normalize_entity_tracks(prepared,entity,text,{'provider':'test_fixture','response_id':'offline-browser-candidates',
            'recording_scope':'HAND-AUTHORED SAM QA fixture on generated media; no live action recognition'})
        record=publish_entity_batch(store,settings,project_id,run.id,'w000',batch,raw_text=text)
    finally:store.release_process_lock()
    calls={'live_requests':0}
    def reject_network(*args,**kwargs):
        calls['live_requests']+=1
        raise AppError('OFFLINE_NETWORK_FORBIDDEN','Meta requests are forbidden in this QA process',503)
    MetaCloud.request=reject_network;MetaCloud.stream_response=reject_network
    app=create_app(settings);html=(settings.static_dir/'index.html').read_text(encoding='utf-8')
    @app.get('/__qa__/entities',include_in_schema=False)
    def qa(readonly:bool=False):
        return HTMLResponse(html.replace('</head>',script(project_id,readonly)+'</head>').replace('</body>','<img hidden src="/__qa__/hold"></body>'))
    app.router.routes.insert(0,app.router.routes.pop())
    @app.get('/__qa__/hold',include_in_schema=False)
    async def hold():
        await asyncio.sleep(4)
        return HTMLResponse('')
    app.router.routes.insert(0,app.router.routes.pop())
    with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,log_level='warning'))
    thread=Thread(target=server.run,daemon=True);thread.start()
    deadline=time.monotonic()+10
    while not server.started and thread.is_alive() and time.monotonic()<deadline:time.sleep(.05)
    if not server.started:raise RuntimeError('QA server failed to start')
    profile=output/f'browser-profile-{uuid4().hex}'
    common=[browser_binary(),'--headless=new',f'--user-data-dir={profile}','--no-first-run','--no-default-browser-check',
        '--disable-background-networking','--disable-component-update','--disable-sync','--disable-default-apps',
        '--disable-background-timer-throttling','--disable-renderer-backgrounding','--window-size=1600,2200',
        '--run-all-compositor-stages-before-draw']
    try:
        for mode in ('workflow','reload'):
            url=f'http://127.0.0.1:{port}/__qa__/entities'+('?readonly=true' if mode=='reload' else '')
            process=subprocess.run([*common,'--dump-dom','--virtual-time-budget=35000',url],capture_output=True,timeout=55,
                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            dom=process.stdout.decode('utf-8',errors='replace');(output/(mode+'.html')).write_text(dom,encoding='utf-8')
            match=re.search(r'<pre id="entity-test-result"[^>]*>(.*?)</pre>',dom,re.S)
            if not match:raise RuntimeError('No browser test result; inspect '+str(output/(mode+'.html')))
            result=json.loads(unescape(match.group(1)));result.update(calls)
            result.update(data_dir=str(data),project_id=project_id,batch_id=str(record.id),run_id=str(run.id))
            (output/(mode+'.json')).write_text(json.dumps(result,indent=2),encoding='utf-8')
            assert result.get('passed'),result
        subprocess.run([*common,f'--screenshot={output/"entities.png"}',f'http://127.0.0.1:{port}/__qa__/entities?readonly=true'],
            capture_output=True,check=True,timeout=30,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        assert calls['live_requests']==0
        print(json.dumps({'passed':True,'scope':'hand_authored_entity_candidates_real_studio_api','cloud_requests_made':0,
            'data_dir':str(data),'evidence_dir':str(output),'visual_review':'pending'}),flush=True)
    finally:server.should_exit=True;thread.join(timeout=10)


if __name__=='__main__':main()
