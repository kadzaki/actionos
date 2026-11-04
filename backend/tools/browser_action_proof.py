"""Local headless Chrome integration test for the A15 manual-action artifacts.

Uses an isolated temporary browser profile, never the user's tabs/profile. The
test-only playback page is registered only in this command, not production.
Run manual_action_proof.py first. No provider calls or external pages are used.
"""
from dataclasses import replace
import argparse
import asyncio
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

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.responses import HTMLResponse
import uvicorn
from app.config import Settings
from app.main import create_app


def browser_binary(explicit=None):
    candidates = [explicit] if explicit else [shutil.which('google-chrome'), shutil.which('chromium'),
        'C:/Program Files/Google/Chrome/Application/chrome.exe',
        'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe']
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(Path(candidate).resolve())
    raise RuntimeError('No supported Chromium browser found; pass --browser EXE')


def playback_page(video_url, report_url):
    # Values are server-generated owned URLs, encoded as JS string literals.
    return '''<!doctype html><html><head><meta charset="utf-8"><title>A15 browser playback test</title>
<style>body{font:14px system-ui;display:grid;grid-template-columns:400px 1fr;gap:20px}h1{grid-column:1/-1}pre{white-space:pre-wrap;overflow-wrap:anywhere}</style></head>
<body><h1>MANUAL DEVELOPER FIXTURE — browser integration test</h1><video id="video" controls width="360" height="640"></video>
<pre id="results">pending</pre><img hidden src="/__qa__/hold"><script>
const video = document.getElementById('video'), results = document.getElementById('results');
const evidence = {fixture_only:true, provider_calls:0,browser_user_agent:navigator.userAgent};
function finish(error) { evidence.passed=!error; if(error)evidence.error=String(error); results.textContent=JSON.stringify(evidence); }
(async()=>{
  video.src=VIDEO_URL;
  video.addEventListener('error',()=>finish(video.error?.message || 'media error'),{once:true});
  await new Promise((resolve,reject)=>{video.onloadedmetadata=resolve;video.onerror=()=>reject('metadata failed');});
  evidence.duration=video.duration; evidence.width=video.videoWidth;evidence.height=video.videoHeight;
  results.textContent=JSON.stringify({...evidence,stage:'metadata_loaded'});
  evidence.play_started=false;
  const ended=new Promise(resolve=>video.addEventListener('ended',resolve,{once:true}));
  await video.play();evidence.play_started=true;
  results.textContent=JSON.stringify({...evidence,stage:'playing'});
  await ended;
  evidence.ended=video.ended;evidence.current_time=video.currentTime;
  evidence.played_ranges=Array.from({length:video.played.length},(_,i)=>[video.played.start(i),video.played.end(i)]);
  const bytes=await (await fetch(VIDEO_URL)).arrayBuffer();
  const context=new OfflineAudioContext(1,48000,48000);
  const audio=await context.decodeAudioData(bytes);
  const samples=audio.getChannelData(0);
  let power=0,crossings=0;
  for(let i=1;i<samples.length;i++){power+=samples[i]*samples[i];if(samples[i-1]<=0&&samples[i]>0)crossings++;}
  evidence.audio={sample_rate:audio.sampleRate,duration:audio.duration,channels:audio.numberOfChannels,
    rms:Math.sqrt(power/samples.length),estimated_hz:crossings/audio.duration};
  const response=await fetch(REPORT_URL), html=await response.text();
  const doc=new DOMParser().parseFromString(html,'text/html');
  evidence.report={status:response.status,rows:doc.querySelectorAll('tbody tr').length,
    manual_label:doc.body.textContent.includes('MANUAL DEVELOPER FIXTURE'),unknown:doc.body.textContent.includes('unknown'),
    nonoccurrence_caveat:doc.body.textContent.includes('does not mean the operation did not happen'),links:[]};
  for(const link of doc.querySelectorAll('a')){
    const r=await fetch(link.getAttribute('href').split('#')[0]);
    evidence.report.links.push({url:link.getAttribute('href'),status:r.status});
  }
  if(!evidence.ended||Math.abs(evidence.current_time-evidence.duration)>.05)throw 'playback did not reach the end';
  if(evidence.width!==1080||evidence.height!==1920)throw 'unexpected output dimensions';
  if(evidence.audio.rms<.01||Math.abs(evidence.audio.estimated_hz-440)>10)throw 'expected audio tone not decoded';
  if(evidence.report.rows!==2||!evidence.report.manual_label||!evidence.report.unknown||!evidence.report.nonoccurrence_caveat)throw 'report semantics missing';
  if(evidence.report.links.some(x=>x.status!==200))throw 'broken evidence link';
  finish();
})().catch(finish);
</script></body></html>'''.replace('VIDEO_URL', json.dumps(video_url)).replace('REPORT_URL', json.dumps(report_url))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--browser')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    output = root/'.tmp/manual-action-proof'
    proof = json.loads((output/'proof.json').read_text(encoding='utf-8'))
    settings = replace(Settings.from_env(), data_dir=output/'data', model_api_key='')
    app = create_app(settings)
    video = proof['outputs']['exports'][0]['stream_url']
    report = proof['outputs']['reports'][0]['view_url']
    @app.get('/__qa__/playback', include_in_schema=False)
    def qa():
        return HTMLResponse(playback_page(video, report))
    # create_app's SPA catch-all is last; keep this test-only route before it.
    app.router.routes.insert(0, app.router.routes.pop())
    @app.get('/__qa__/hold', include_in_schema=False)
    async def hold():
        # --dump-dom waits for page load. Media uses a real playback clock;
        # a delayed test resource permits it to finish before DOM capture.
        await asyncio.sleep(6)
        return HTMLResponse('')
    app.router.routes.insert(0, app.router.routes.pop())
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,log_level='warning'))
    thread = Thread(target=server.run, daemon=True)
    thread.start()
    deadline=time.monotonic()+10
    while not server.started and thread.is_alive() and time.monotonic()<deadline:
        time.sleep(.05)
    if not server.started:
        raise RuntimeError('Proof test server did not start; stop any process holding its DATA_DIR')
    profile=output/f'browser-profile-{uuid4().hex}'
    common=[browser_binary(args.browser),'--headless=new',f'--user-data-dir={profile}',
            '--no-first-run','--no-default-browser-check','--disable-background-networking',
            '--disable-component-update','--disable-sync','--disable-default-apps',
            '--autoplay-policy=no-user-gesture-required','--disable-background-timer-throttling',
            '--disable-renderer-backgrounding','--window-size=1600,1000']
    try:
        result=subprocess.run([*common,'--dump-dom',f'--screenshot={(output/"playback-browser.png").resolve()}',
                               f'http://127.0.0.1:{port}/__qa__/playback'],
                              capture_output=True,timeout=45,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        dom=result.stdout.decode('utf-8',errors='replace')
        (output/'browser-process.json').write_text(json.dumps({'exit_code':result.returncode,
            'stdout_bytes':len(result.stdout),'stderr':result.stderr.decode('utf-8',errors='replace')[-3000:]},indent=2),encoding='utf-8')
        (output/'browser-playback.html').write_text(dom,encoding='utf-8')
        match=re.search(r'<pre id="results">(.*?)</pre>',dom,re.S)
        value=unescape(match.group(1)) if match else ''
        if not value.startswith('{'):
            raise RuntimeError('Browser test did not finish: '+value[:200])
        evidence=json.loads(value)
        (output/'browser-playback.json').write_text(json.dumps(evidence,indent=2),encoding='utf-8')
        if not evidence.get('passed'):
            raise RuntimeError('Browser playback failed: '+str(evidence.get('error')))
        screenshot=output/'report-browser.png'
        subprocess.run([*common,f'--screenshot={screenshot.resolve()}', '--virtual-time-budget=1000',
                        f'http://127.0.0.1:{port}{report}'],capture_output=True,check=True,timeout=30,
                       creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        print(json.dumps({'passed':True,'evidence':str(output/'browser-playback.json'),
                          'report_screenshot':str(screenshot),'visual_review':'pending'}),flush=True)
    finally:
        server.should_exit=True
        thread.join(timeout=10)


if __name__=='__main__':
    main()
