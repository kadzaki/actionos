"""Offline A26/A27 geometry/storage proof replaying A03 against its exact source.

No network, compiler approval, tabletop recognition or temporal verdict. Uses an
explicit developer AST/context for the historical 15fps cloud input; production
prepare_window still requires a reviewed run and its exact 30fps proxy mapping.
"""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient
from app.config import Settings
from app.engine.entity_tracks import batch_fingerprint,compatibility_track,normalize_entity_tracks,select_candidate
from app.engine.muse_observations import PreparedObservation
from app.engine.planning import build_plan
from app.engine.geometry import derive_geometry
from app.main import create_app
from app.models import Asset
from app.programs.ast import ProgramAST
from app.sampling import VideoWindow
from app.scenes import inspect_scenes
from app.source_evidence import SourceEvidenceAdapter
from app.storage import FileStore,file_sha256
from manual_action_proof import wait


def main():
    root=Path(__file__).resolve().parents[2]
    original=root/'.tmp/meta-qualification/motion.mp4'
    recording=json.loads((root/'.tmp/meta-qualification/report.json').read_text(encoding='utf-8'))
    assert file_sha256(original)==recording['input']['sha256'],'A03 source changed; cannot replay onto another video'
    check=next(c for c in recording['checks'] if c['capability']=='sam_video')
    assert check['passed'] and check['remote_file_deleted']
    output=root/'.tmp/entity-track-proof';output.mkdir(parents=True,exist_ok=True)
    proof_store=FileStore(output)
    settings=replace(Settings.from_env(),data_dir=output/'data',model_api_key='')
    with TestClient(create_app(settings)) as client:
        project=client.post('/api/projects',json={'name':'A26 OFFLINE A03 SAM REPLAY, developer AST only'}).json()['project_id']
        accepted=client.post(f'/api/projects/{project}/upload',content=original.read_bytes(),headers={'Content-Type':'video/mp4'})
        accepted.raise_for_status();wait(client,accepted.json()['job_id'])
        asset=Asset.model_validate(client.get(f'/api/projects/{project}').json()['asset'])
        source=SourceEvidenceAdapter(client.app.state.store,settings,project,asset.id)
        assert asset.sha256==recording['input']['sha256']
        ast=ProgramAST(instruction='DEVELOPER FIXTURE: inspect the red square',
            entities=[{'id':'square','role':'object','description':'red square'}],
            steps=[{'id':'seen','predicate':'visible','entities':['square']},
                {'id':'shift','predicate':'moves','entities':['square'],'parameters':{'min_displacement_ratio':.02,'duration_ms':500}}],
            cycle={'root_step_id':'seen'})
        plan=build_plan(ast,source.timeline,run_id=uuid4(),program_fingerprint=hashlib.sha256(ast.model_dump_json().encode()).hexdigest(),
            duration_ms=asset.metadata.duration_ms,working_duration_ms=asset.working_metadata.duration_ms,
            timeline_sha256=asset.artifact_sha256[asset.timeline_path],proxy_sha256=asset.artifact_sha256[asset.proxy_path],
            proxy_width=320,proxy_height=180,providers={'sam_model':check['provenance']['model']})
        window=VideoWindow(project_id=asset.project_id,source_asset_id=asset.id,source_sha256=asset.sha256,
            proxy_sha256=asset.sha256,path=asset.source_path,sha256=asset.sha256,requested_in_ms=0,requested_out_ms=2000,
            first_working_frame_index=0,first_working_time_ms=0,duration_ms=2000,width=320,height=180,fps=15,
            source_frame_indices=list(range(30)),source_frame_times_ms=source.timeline.source_frame_times_ms,
            input_kind='historical_A03_original_video_replay')
        prepared=PreparedObservation(plan,plan.windows[0],window,ast,source)
        batch=normalize_entity_tracks(prepared,'square',check['output'],{**check['provenance'],
            'remote_file_deleted':check['remote_file_deleted'],'scope':'offline replay of A03 original video; developer AST'})
        assert len(batch.candidates)==1
        candidate=batch.candidates[0];segment=candidate.segments[0]
        assert len(candidate.samples)==30 and len(candidate.segments)==1 and candidate.identity=='unknown'
        scene_evidence=inspect_scenes(source.store,settings,window)
        geometry=derive_geometry(prepared,'shift',{'square':(batch,candidate.id)},scenes=scene_evidence)
        assert geometry.measurements and all(m.comparison_result is True for m in geometry.measurements)
        assert all('CAMERA_MOTION_UNCOMPENSATED' in m.limitations for m in geometry.measurements)
        proof_store.write_json(output/'geometry.json',geometry.model_dump(mode='json'))
        track=compatibility_track(prepared,batch,candidate.id,segment.id)
        source.store.save(project,'tracks',track)
        from app.editing import Track
        assert source.store.load(project,'tracks',Track,track.id)==track
        comparisons=[]
        for index in (0,15,29):
            sample=next(s for s in candidate.samples if s.input_frame_index==index)
            decoded=subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-i',str(original),
                '-vf',f'select=eq(n\\,{index})','-frames:v','1','-f','rawvideo','-pix_fmt','rgb24','pipe:1'],
                capture_output=True,check=True,timeout=20).stdout
            assert len(decoded)==320*180*3
            red=[(i%320,i//320) for i in range(320*180)
                if decoded[3*i]>150 and decoded[3*i+1]<80 and decoded[3*i+2]<80]
            actual=(min(x for x,y in red),min(y for x,y in red),max(x for x,y in red)+1,max(y for x,y in red)+1)
            box=(round(sample.x*320),round(sample.y*180),round((sample.x+sample.width)*320),round((sample.y+sample.height)*180))
            area=lambda b:(b[2]-b[0])*(b[3]-b[1])
            intersection=max(0,min(actual[2],box[2])-max(actual[0],box[0]))*max(0,min(actual[3],box[3])-max(actual[1],box[1]))
            iou=intersection/(area(actual)+area(box)-intersection)
            assert iou>=.90,f'SAM box is misaligned at actual source frame {index}: {iou}'
            frame=source.frame(0,source_frame_index=index)
            comparisons.append({'source_frame_index':index,'source_pts':frame.source_pts,'source_time_ms':frame.source_time_ms,
                'sam_box_pixels':box,'decoded_red_box_pixels':actual,'iou':iou,'evidence_frame_id':str(frame.evidence_frame_id)})
            target=output/f'frame-{index}.png'
            subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-y','-i',str(original),
                '-vf',f'select=eq(n\\,{index}),drawbox=x={box[0]}:y={box[1]}:w={box[2]-box[0]}:h={box[3]-box[1]}:color=lime:t=1',
                '-frames:v','1',str(target)],check=True,timeout=20)
        anchor=source.frame(0,source_frame_index=15)
        binding=select_candidate(prepared,batch,{'batch_fingerprint':batch_fingerprint(batch),'candidate_id':candidate.id,
            'segment_id':segment.id,'input_frame_index':15,'evidence_frame_id':anchor.evidence_frame_id})
        proof={'scope':'offline_A03_exact_source_replay_and_geometry_storage_only','cloud_requests_made':0,
            'compiler':'developer fixture, not live compilation','review':'simulated explicit segment selection, not user approval',
            'source_sha256':asset.sha256,'permission':recording['input']['permission'],
            'a03_response_id':check['provenance']['response_id'],'candidate_identity':candidate.identity,
            'negative_coverage_verified':False,'compressed_track_id':str(track.id),'comparisons':comparisons,
            'geometry_measurements':len(geometry.measurements),'scene_cuts':len(scene_evidence.cuts),
            'geometry_scope':'image-space candidate measurements only; identity/physical action/rule truth unverified',
            'simulated_binding':binding.model_dump(mode='json')}
        proof_store.write_json(output/'proof.json',proof)
        proof_store.write_json(output/'batch.json',batch.model_dump(mode='json'))
        print(json.dumps({'passed':True,'cloud_requests_made':0,'samples':len(candidate.samples),
            'source_box_iou':[v['iou'] for v in comparisons],'geometry_measurements':len(geometry.measurements),
            'output':str(output/'proof.json')}),flush=True)


if __name__=='__main__':main()
