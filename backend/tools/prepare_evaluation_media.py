"""Opt-in licensed evaluation media preparation. Never calls a model."""
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
from uuid import uuid4
from urllib.parse import urlsplit

import httpx

ROOT=Path(__file__).resolve().parents[2]
MANIFEST=ROOT/'backend/evaluation/manifest.json'
MEDIA=ROOT/'.tmp/evaluation/media'
MEDIA.mkdir(parents=True,exist_ok=True)
MAX_BYTES=160*1024*1024


def sha(path):
    with path.open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


def metadata(path):
    result=subprocess.run(['ffprobe','-protocol_whitelist','file,pipe','-v','error','-show_streams',
                           '-show_format','-of','json',str(path)],capture_output=True,check=True,timeout=30)
    value=json.loads(result.stdout)
    video=next(s for s in value['streams'] if s['codec_type']=='video')
    return {'duration_seconds':float(video.get('duration') or value['format']['duration']),
            'width':video['width'],'height':video['height'],'fps':video.get('avg_frame_rate'),
            'audio_present':any(s['codec_type']=='audio' for s in value['streams'])}


manifest=json.loads(MANIFEST.read_text(encoding='utf-8'))
with httpx.Client(timeout=httpx.Timeout(30,connect=15),follow_redirects=False,
                  headers={'User-Agent':'VideoCutEvaluation/0.1 (local licensed media evaluation)'}) as client:
    for entry in manifest['videos']:
        identifier=entry['id']
        if not re.fullmatch(r'[a-z0-9-]+',identifier):raise ValueError('Invalid local evaluation identifier')
        if entry.get('license_verification')!='license_recorded_from_public_file_page':
            raise ValueError('Record the source-page license and attribution before downloading')
        parsed=urlsplit(entry['original_url'])
        if parsed.scheme!='https' or parsed.hostname!='upload.wikimedia.org' or parsed.query:
            raise ValueError('Only the reviewed public Wikimedia media URLs are supported')
        original=MEDIA/f'{identifier}.webm'
        prepared=MEDIA/f'{identifier}.mp4'
        if not original.is_file():
            if shutil.disk_usage(MEDIA).free<512*1024*1024:raise RuntimeError('Insufficient free disk for evaluation download')
            temporary=MEDIA/f'{identifier}.download.part'
            try:
                with client.stream('GET',entry['original_url']) as response:
                    print(json.dumps({'id':identifier,'download_http':response.status_code}),flush=True)
                    if response.status_code in (403,429):
                        manifest['media_download_state']={'id':identifier,'http_status':response.status_code,
                                                          'retry_after':response.headers.get('retry-after'),
                                                          'state':'remote_host_rejected_download; no bypass or automatic retry'}
                        MANIFEST.write_text(json.dumps(manifest,indent=2,ensure_ascii=True),encoding='utf-8')
                        raise SystemExit('Public media download is unavailable; source files and pending evaluation state were preserved')
                    response.raise_for_status()
                    size=0
                    with temporary.open('xb') as output:
                        for chunk in response.iter_bytes(1024*1024):
                            size+=len(chunk)
                            if size>MAX_BYTES:raise RuntimeError('Evaluation source exceeds download bound')
                            output.write(chunk)
                    entry['download_etag']=response.headers.get('etag')
                    entry['download_last_modified']=response.headers.get('last-modified')
                temporary.replace(original)
            finally:temporary.unlink(missing_ok=True)
        source_hash=sha(original)
        if entry.get('source_sha256') and source_hash!=entry['source_sha256']:
            raise RuntimeError('Downloaded source changed from frozen evaluation manifest')
        entry['source_sha256']=source_hash
        entry['source_metadata']=metadata(original)
        MANIFEST.write_text(json.dumps(manifest,indent=2,ensure_ascii=True),encoding='utf-8')
        if not 0<entry['source_metadata']['duration_seconds']<=120:
            raise RuntimeError('Evaluation candidate exceeds the application duration bound')
        if not prepared.is_file() or sha(prepared)!=entry.get('prepared_sha256'):
            temporary=MEDIA/f'.{identifier}.{uuid4().hex}.prepare.mp4'
            try:
                subprocess.run(['ffmpeg','-protocol_whitelist','file,pipe','-v','error','-nostdin','-y','-i',str(original),
                            '-map','0:v:0','-map','0:a:0?','-vf','scale=trunc(iw/2)*2:trunc(ih/2)*2,setsar=1',
                            '-fps_mode','passthrough','-c:v','libx264','-crf','18','-preset','veryfast','-threads','2',
                            '-c:a','aac','-b:a','128k','-map_metadata','-1','-movflags','+faststart',str(temporary)],check=True,timeout=300)
                actual=metadata(temporary)
                if abs(actual['duration_seconds']-entry['source_metadata']['duration_seconds'])>.1:
                    raise RuntimeError('Converted evaluation source changed duration by more than 100 ms')
                subprocess.run(['ffmpeg','-protocol_whitelist','file,pipe','-v','error','-xerror','-nostdin','-i',str(temporary),
                                '-f','null','-'],capture_output=True,check=True,timeout=300)
                temporary.replace(prepared)
            finally:temporary.unlink(missing_ok=True)
        entry['prepared_sha256']=sha(prepared)
        entry['prepared_metadata']=metadata(prepared)
        entry['original_path']=original.relative_to(ROOT).as_posix()
        entry['prepared_path']=prepared.relative_to(ROOT).as_posix()
        entry['preparation']={'trim':'none','speed':'unchanged','fps':'passthrough','video':'H.264 CRF 18',
                              'audio':'AAC if present','geometry':'even dimensions and square pixels; no crop'}
        entry['media_state']='downloaded_and_prepared_not_yet_annotated'
        (MEDIA/f'{identifier}.attribution.txt').write_text(entry['attribution']+'\nLicense: '+entry['license_url']+'\n',encoding='utf-8')
        MANIFEST.write_text(json.dumps(manifest,indent=2,ensure_ascii=True),encoding='utf-8')
        print(json.dumps({'id':identifier,'prepared':entry['prepared_metadata'],'source_sha256':source_hash}),flush=True)
manifest['state']='six_media_sources_prepared_pending_independent_annotations'
MANIFEST.write_text(json.dumps(manifest,indent=2,ensure_ascii=True),encoding='utf-8')
