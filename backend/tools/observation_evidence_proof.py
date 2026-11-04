"""A29 local persistence/API/restart proof with conspicuous mock perception.

Clones the A22 generated-source QA project. No provider requests, live tabletop
recognition, temporal evaluation or output action is performed.
"""
from dataclasses import replace
import json
from pathlib import Path
import shutil
import sys
from uuid import uuid4

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient
from app.config import Settings
from app.engine.entity_repository import publish_binding,publish_entity_batch
from app.engine.entity_tracks import ENTITY_ADAPTER_VERSION,batch_fingerprint,normalize_entity_tracks
from app.engine.fusion_repository import publish_fused_observation
from app.engine.geometry_repository import publish_geometry
from app.engine.muse_observations import OBSERVATION_ADAPTER_VERSION,prepare_window
from app.engine.observation_repository import publish_muse_record
from app.engine.planning import plan_run
from app.errors import AppError
from app.main import create_app
from app.models import Project
from app.programs.repository import load_program
from app.providers.meta import MetaCloud
from app.repository import reference
from app.runs import snapshot_run
from app.storage import FileStore


def main():
    root=Path(__file__).resolve().parents[2];output=root/'.tmp/observation-evidence-proof';output.mkdir(parents=True,exist_ok=True)
    qa=json.loads((root/'.tmp/inspector-proof/workflow.json').read_text(encoding='utf-8'))
    source=root/'.tmp/inspector-proof/data'
    paths=list((source/'projects').glob(f'*/programs/{qa["program_id"]}/v2.json'))
    assert len(paths)==1,'Run A22 QA first; original reviewed fixture owner is missing/ambiguous'
    project_id=paths[0].parents[2].name
    data=output/f'data-{uuid4().hex}'
    shutil.copytree(paths[0].parents[2],data/'projects'/project_id)
    settings=replace(Settings.from_env(),data_dir=data,model_api_key='')
    calls={'live_requests':0}
    def no_network(*args,**kwargs):
        calls['live_requests']+=1
        raise AppError('OFFLINE_NETWORK_FORBIDDEN','Meta requests are forbidden in this proof process',503)
    MetaCloud.request=no_network;MetaCloud.stream_response=no_network
    with TestClient(create_app(settings)) as client:
        store=client.app.state.store
        project=store.load(project_id,'project',Project)
        project.name='A29 OFFLINE QA - hand-authored perception only';store.save(project_id,'project',project)
        _,program=load_program(store,project_id,qa['program_id'],2)
        ast=program.program.ast;positive,unseen=ast.steps[:2]
        run=snapshot_run(store,settings,project_id,program.program.program_id,2,muse_adapter_version=OBSERVATION_ADAPTER_VERSION,
            sam_adapter_version=ENTITY_ADAPTER_VERSION,evaluator_version='not-implemented')
        plan=plan_run(store,settings,project_id,run.id);prepared=prepare_window(store,settings,project_id,run.id,plan.id,'w000')
        count=len(prepared.window.source_frame_indices);w,h=prepared.window.width,prepared.window.height
        assert count>=24
        entities={e.id:e for e in ast.entities}
        raw={'schema_version':'predicate-observations-1','window_id':str(prepared.window.id),'entities':[],
            'hypotheses':[{'step_id':positive.id,'entity_ids':positive.entities,'assessment':'supported',
                'interval':{'start_frame':0,'stop_frame':24},'evidence_frame_indices':[0,15],
                'description':'HAND-AUTHORED semantic QA hypothesis, not recognized from this source',
                'quality':'high','uncertainty':[],'boundary_uncertainty_ms':100},
                {'step_id':unseen.id,'entity_ids':unseen.entities,'assessment':'not_observed',
                'description':'HAND-AUTHORED mock occluded tool step','quality':'low',
                'uncertainty':['No source evidence in this fixture establishes tool use']}],
            'coverage':[],'notes':['Developer fixture on generated test pattern; no actual action recognition']}
        for entity in set(positive.entities+unseen.entities):
            clear=entity in positive.entities
            raw['entities'].append({'entity_id':entity,'role':entities[entity].role,'description':'Explicit mock entity appearance',
                'visibility':'clear' if clear else 'occluded','identity':'unambiguous' if clear else 'unknown',
                'evidence_frame_indices':[0,15] if clear else [],'uncertainty':[] if clear else ['Mock occlusion']})
        for step,clear in ((positive,True),(unseen,False)):
            raw['coverage'].append({'step_id':step.id,'attempted_frames':{'start_frame':0,'stop_frame':count},
                'observed_frame_ranges':[{'start_frame':0,'stop_frame':count}] if clear else [],
                'complete_inspection_claim':clear,'visibility':'clear' if clear else 'occluded',
                'identity':'unambiguous' if clear else 'unknown','resolution':'adequate' if clear else 'insufficient','uncertainty':[]})
        muse=publish_muse_record(store,settings,project_id,run.id,'w000',raw,{'provider':'test_fixture','response_id':'A29-offline-muse'},
            origin='developer_fixture',evidence_scope='HAND-AUTHORED perception on generated source; no live tabletop recognition')
        bindings={};assignments={}
        text='\n'.join(f'<{i}f>1<|box;x1={w//5};y1={h//3};x2={w//5+w//5};y2={h//3+h//4};w={w};h={h}|><|mask;x=0;y=0;data={w},{h},~fixture|>' for i in range(count))
        for entity in positive.entities:
            batch=normalize_entity_tracks(prepared,entity,text,{'provider':'test_fixture','response_id':'A29-offline-'+entity,
                'recording_scope':'HAND-AUTHORED SAM geometry fixture, not a source recognition result'})
            record=publish_entity_batch(store,settings,project_id,run.id,'w000',batch,raw_text=text)
            candidate=batch.candidates[0];segment=candidate.segments[0];frame=prepared.source.frame(0,source_frame_index=prepared.window.source_frame_indices[0])
            binding=publish_binding(store,settings,project_id,run.id,record.id,{'batch_fingerprint':batch_fingerprint(batch),
                'candidate_id':candidate.id,'segment_id':segment.id,'input_frame_index':0,'evidence_frame_id':frame.evidence_frame_id})
            bindings[entity]=binding.id;assignments[entity]=(record.id,candidate.id)
        geometry=publish_geometry(store,settings,project_id,run.id,positive.id,assignments)
        supported=publish_fused_observation(store,settings,project_id,run.id,'w000',positive.id,prepared.planned_window.source_interval,
            muse_id=muse.id,geometry_id=geometry.id,binding_ids=bindings)
        unknown=publish_fused_observation(store,settings,project_id,run.id,'w000',unseen.id,prepared.planned_window.source_interval,
            muse_id=muse.id,binding_ids={e:bindings[e] for e in unseen.entities if e in bindings})
        api_root=f'/api/projects/{project_id}/runs/{run.id}'
        views={}
        link_count=0;image_count=0
        for expected,record in [('true',supported),('unknown',unknown)]:
            response=client.get(api_root+'/observations/'+str(record.id));response.raise_for_status();body=response.json()
            assert body['result']['verdict']==expected and body['result']['needs_review']
            assert body['result']['interpretation']=='bounded_visual_evidence_only'
            for link in body['evidence_links']:
                checked=client.get(link['url']);checked.raise_for_status();link_count+=1
                for frame in checked.json().get('frames',[]):
                    client.get(frame['image_url']).raise_for_status()
                    client.get(frame['source_url'].split('#')[0]).raise_for_status();image_count+=1
            views[expected]=body['result']
        listing=client.get(api_root+'/evidence').json()
        assert len(listing['items'])==4 and listing['errors']==[]
        assert calls['live_requests']==0
        proof={'scope':'developer_mock_perception_real_storage_and_api_only','cloud_requests_made':0,
            'compiler_scope':'cached A19 program with A22 simulated QA review, not a new live compile',
            'source_permission':'application-generated source pattern/audio','source_sha256':prepared.plan.source_sha256,
            'data_dir':str(data),'project_id':project_id,'run':reference(run).model_dump(mode='json'),
            'muse':reference(muse).model_dump(mode='json'),'geometry':reference(geometry).model_dump(mode='json'),
            'supported':reference(supported).model_dump(mode='json'),'unknown':reference(unknown).model_dump(mode='json'),
            'evidence_links_checked':link_count,'frame_image_source_links_checked':image_count,
            'index_records':len(listing['items']),'unknown_reason_codes':views['unknown']['reasons']}
    # A completely new app/store instance proves on-disk dependencies, not an
    # object still held by the first process. Corruption is restored in finally.
    with TestClient(create_app(settings)) as client:
        response=client.get(api_root+'/observations/'+str(supported.id));response.raise_for_status()
        assert response.json()['result']==views['true'];proof['restart_replay_identical']=True
        store=client.app.state.store;path=store.path(project_id,muse.artifacts[1].path);backup=path.read_bytes()
        try:
            store.write_bytes(path,b'intentional QA corruption')
            damaged=client.get(api_root+'/observations/'+str(supported.id))
            assert damaged.status_code==409 and damaged.json()['code']=='CORRUPT_ARTIFACT'
            listing=client.get(api_root+'/evidence').json()
            assert listing['errors'] and len(listing['items'])==1  # Healthy geometry remains inspectable.
            proof['corruption_error']=damaged.json()['code'];proof['healthy_records_preserved']=len(listing['items'])
        finally:store.write_bytes(path,backup)
        client.get(api_root+'/observations/'+str(supported.id)).raise_for_status();proof['restored_replay_valid']=True
    assert calls['live_requests']==0
    FileStore(output).write_json(output/'proof.json',proof)
    print(json.dumps({'passed':True,'scope':proof['scope'],'cloud_requests_made':0,
        'evidence_links_checked':link_count,'output':str(output/'proof.json')}),flush=True)


if __name__=='__main__':main()
