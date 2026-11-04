from dataclasses import replace
import hashlib
import json
from pathlib import Path
import struct
import subprocess

from fastapi.testclient import TestClient
import pytest

from app.main import create_app
from app.models import Asset
from app.timing import SourceTimeline,coded_to_display
from test_api import create,wait_job


@pytest.fixture
def marked_media(tmp_path):
    clips={}
    for name,select in [('cfr',''),('vfr',",select='not(mod(n,3))+not(mod(n,5))'")]:
        clip=tmp_path/f'{name}.mp4'
        subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-y','-f','lavfi','-i',
                        "nullsrc=size=64x48:rate=24:duration=1,geq=lum='16+8*N':cb=128:cr=128"+select,
                        '-fps_mode','vfr','-c:v','libx264','-crf','10','-pix_fmt','yuv420p',str(clip)],
                       check=True,timeout=20)
        clips[name]=clip
    return clips


def prepare(client,clip):
    project_id=create(client)
    result=client.post(f'/api/projects/{project_id}/upload',content=clip.read_bytes(),headers={'Content-Type':'video/mp4'})
    assert result.status_code==202,result.text
    body=result.json()
    job=wait_job(client,body['job_id'])
    assert job['status']=='completed',job
    return Asset.model_validate(client.get(f'/api/projects/{project_id}').json()['asset'])


def gray_frames(path,width,height):
    result=subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-i',str(path),'-map','0:v:0',
                           '-fps_mode','passthrough','-pix_fmt','gray','-f','rawvideo','-'],
                          check=True,capture_output=True,timeout=20)
    size=width*height
    assert len(result.stdout)%size==0
    return [result.stdout[i:i+size] for i in range(0,len(result.stdout),size)]


@pytest.mark.parametrize('kind',['cfr','vfr'])
def test_working_map_matches_actual_encoded_frame_identity(client,settings,marked_media,kind):
    asset=prepare(client,marked_media[kind])
    timeline=SourceTimeline.model_validate(client.get(f'/api/assets/{asset.id}/timeline').json())
    root=settings.data_dir/'projects'/str(asset.project_id)
    original=gray_frames(root/asset.source_path,64,48)
    working=gray_frames(root/asset.working_path,64,48)
    assert len(working)==len(timeline.working_frame_source_indices)
    original_levels=[sum(frame)/len(frame) for frame in original]
    for pixels,index in zip(working,timeline.working_frame_source_indices):
        level=sum(pixels)/len(pixels)
        matched=min(range(len(original_levels)),key=lambda i:abs(original_levels[i]-level))
        assert index==matched
    # An arbitrary source-time request reports the actual closest frame, not
    # an invented exact frame at the requested millisecond.
    result=client.get(f'/api/assets/{asset.id}/frames/234')
    assert result.status_code==200,result.text
    descriptor=result.json()
    frame=descriptor['frame']
    index=timeline.nearest_frame(234)
    assert frame['source_frame_index']==index
    assert frame['source_pts']==timeline.source_frame_pts[index]
    assert frame['source_time_ms']==timeline.source_frame_times_ms[index]
    image=client.get(descriptor['image_url'])
    assert image.status_code==200
    assert image.headers['x-source-frame-index']==str(index)
    assert hashlib.sha256(image.content).hexdigest()==frame['image_sha256']
    image_path=root/frame['image_path']
    level=sum(gray_frames(image_path,64,48)[0])/(64*48)
    assert abs(level-original_levels[index])<4
    again=client.get(f'/api/assets/{asset.id}/frames/234').json()
    assert again['frame']['id']==frame['id']
    # Browser inspection must follow the actual CFR sample, not simply choose
    # whichever original timestamp is nearest to the browser's clock.
    import math
    different=[]
    for wi,source_index in enumerate(timeline.working_frame_source_indices):
        time_ms=math.ceil(timeline.working_frame_times_ms[wi])
        if source_index!=timeline.nearest_frame(time_ms):different.append((wi,time_ms,source_index))
    assert different
    wi,time_ms,expected=different[0]
    inspected=client.get(f'/api/assets/{asset.id}/frames/{time_ms}?space=working')
    assert inspected.status_code==200,inspected.text
    assert inspected.json()['working_frame_index']==wi
    assert inspected.json()['frame']['source_frame_index']==expected
    assert client.get(f'/api/assets/{asset.id}/frames/99999?space=working').status_code==422


def test_timestamp_bounds_cache_limit_and_checksum_recovery(settings,marked_media):
    with TestClient(create_app(replace(settings,max_cached_frames=1))) as client:
        asset=prepare(client,marked_media['cfr'])
        url=f'/api/assets/{asset.id}/frames'
        assert client.get(url+'/-1').status_code==422
        assert client.get(url+'/99999').status_code==422
        first=client.get(url+'/0').json()
        assert client.get(url+'/500').status_code==429
        assert client.get(url+'/1').json()['frame']['id']==first['frame']['id']
        root=settings.data_dir/'projects'/str(asset.project_id)
        (root/first['frame']['image_path']).write_bytes(b'corrupt image')
        assert client.get(first['image_url']).json()['code']=='CORRUPT_ARTIFACT'
        restored=client.get(url+'/0')
        assert restored.status_code==200
        assert client.get(restored.json()['image_url']).status_code==200


def test_original_v1_timeline_is_reprobed_and_upgraded(client,settings,media):
    asset=prepare(client,media['vfr'])
    root=settings.data_dir/'projects'/str(asset.project_id)
    path=root/asset.timeline_path
    old=json.loads(path.read_text())
    for key in ('source_frame_pts','source_time_base','source_asset_id','source_sha256',
                'working_frame_source_indices','working_frame_times_ms','transforms'):
        old.pop(key,None)
    path.write_text(json.dumps(old))
    asset_path=root/'assets'/f'{asset.id}.json'
    old_asset=json.loads(asset_path.read_text())
    old_asset['metadata'].pop('video_time_base',None)
    old_asset['metadata'].pop('video_stream_index',None)
    asset_path.write_text(json.dumps(old_asset))
    result=client.get(f'/api/assets/{asset.id}/frames/234')
    assert result.status_code==200,result.text
    upgraded=json.loads(path.read_text())
    assert upgraded['source_frame_pts'] and upgraded['working_frame_source_indices']
    assert upgraded['source_sha256']==asset.sha256
    assert json.loads(asset_path.read_text())['metadata']['video_time_base']!='1/1000'


def test_rotated_non_square_pixels_match_display_transform(client,settings,tmp_path):
    source=tmp_path/'quadrants.mp4'
    subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-y','-f','lavfi','-i',
                    'color=red:size=64x48:rate=24:duration=1,drawbox=x=32:y=0:w=32:h=24:color=lime:t=fill,'
                    'drawbox=x=0:y=24:w=32:h=24:color=blue:t=fill,drawbox=x=32:y=24:w=32:h=24:color=yellow:t=fill,setsar=2/1',
                    '-c:v','libx264','-crf','10',str(source)],check=True,timeout=20)
    data=bytearray(source.read_bytes())
    offset=data.index(b'tkhd')+44
    data[offset:offset+36]=struct.pack('>9i',0,-65536,0,65536,0,0,0,0,1073741824)
    source.write_bytes(data)
    asset=prepare(client,source)
    geometry=asset.working_metadata.geometry
    assert (geometry.display_width,geometry.display_height)==(48,128)
    point=coded_to_display(asset.metadata.geometry).point(16,12)
    assert point==(12,96)
    working=settings.data_dir/'projects'/str(asset.project_id)/asset.working_path
    rgb=subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-i',str(working),'-frames:v','1',
                        '-pix_fmt','rgb24','-f','rawvideo','-'],capture_output=True,check=True,timeout=20).stdout
    index=(int(point[1])*48+int(point[0]))*3
    assert rgb[index]>180 and rgb[index+1]<60 and rgb[index+2]<60
    evidence=client.get(f'/api/assets/{asset.id}/frames/0').json()['frame']
    assert (evidence['width'],evidence['height'])==(48,128)
