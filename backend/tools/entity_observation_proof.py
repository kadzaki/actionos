"""Validate one prepared SAM request locally; explicit execution is single-use."""
import argparse
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.config import Settings
from app.engine.entity_repository import publish_entity_batch,load_entity_batch
from app.engine.entity_tracks import observe_entity
from app.engine.muse_observations import prepare_window
from app.errors import AppError
from app.providers.meta import MetaCloud
from app.repository import Repository,encoded,reference
from app.storage import FileStore
from app.validation import WorkBudget


def request_manifest(prepared,entity_id,*,plan_id,program_id):
    if entity_id not in prepared.plan.entity_prompts:
        raise AppError('UNKNOWN_ENTITY','Choose a requested entity from this run plan',422)
    prompt=prepared.plan.entity_prompts[entity_id]
    return {'scope':'Candidate entity tracking only; no identity binding, final event or output action',
        'run_id':str(prepared.plan.run_id),'plan_id':str(plan_id),'program_id':str(program_id),
        'program_fingerprint':prepared.plan.program_fingerprint,'source_sha256':prepared.plan.source_sha256,
        'planned_window_id':prepared.planned_window.id,'entity_id':entity_id,'input_video':prepared.window.model_dump(mode='json'),
        'files_upload':{'method':'POST','endpoint':'https://api.meta.ai/v1/files','purpose':'user_data',
            'filename':'source-window.mp4','content_type':'video/mp4','sha256':prepared.window.sha256},
        'responses_request':{'model':prepared.plan.providers['sam_model'],'stream':True,'input':[{'type':'message','role':'user','content':[
            {'type':'input_text','text':prompt},{'type':'input_video','file_id':'<Files upload result>'}]}]},
        'response_headers':{'Accept':'text/event-stream'},
        'cleanup':'DELETE https://api.meta.ai/v1/files/<Files upload result> in finally','responses_attempt_limit':1}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir',type=Path,required=True)
    parser.add_argument('--project',required=True);parser.add_argument('--run',required=True);parser.add_argument('--plan',required=True)
    parser.add_argument('--window',required=True);parser.add_argument('--entity',required=True)
    parser.add_argument('--request',type=Path,required=True);parser.add_argument('--output',type=Path)
    parser.add_argument('--execute-reviewed-sha256')
    args=parser.parse_args()
    settings=replace(Settings.from_env(),data_dir=args.data_dir.resolve())
    store=FileStore(settings.data_dir);output=(args.output or args.request.with_suffix('')).resolve()
    output.mkdir(parents=True,exist_ok=True);proof=FileStore(output)
    store.acquire_process_lock()
    try:
        prepared=prepare_window(store,settings,args.project,args.run,args.plan,args.window)
        run=Repository(store).load(args.project,'runs',args.run)
        manifest=request_manifest(prepared,args.entity,plan_id=args.plan,program_id=run.program.id)
        digest=hashlib.sha256(encoded(manifest)).hexdigest()
        saved=json.loads(args.request.read_text(encoding='utf-8'))
        if encoded(manifest)!=encoded(saved) or (args.execute_reviewed_sha256 and args.execute_reviewed_sha256!=digest):
            raise AppError('STALE_PROOF_REQUEST','SAM request differs from its prepared source/program/entity payload',409)
        if (output/'attempt.json').exists():
            raise AppError('PROOF_ALREADY_ATTEMPTED','Inspect the saved SAM attempt before another request',409)
        if not args.execute_reviewed_sha256:
            print(json.dumps({'validated':True,'sha256':digest,'cloud_requests_made':0}),flush=True);return 0
        with MetaCloud(settings,budget=WorkBudget(provider_calls=1)) as cloud:
            proof.write_json(output/'attempt.json',{'request_sha256':digest,'responses_attempt_limit':1})
            raw={'text':None}
            def capture(text,provenance):
                raw['text']=text
                proof.write_json(output/'provider-response.json',{'text':text,'provenance':provenance})
            batch=observe_entity(cloud,prepared,args.entity,record_response=capture)
            proof.write_json(output/'entity-tracks.json',batch.model_dump(mode='json'))
            record=publish_entity_batch(store,settings,args.project,args.run,args.window,batch,
                raw_text=raw['text'] if batch.status!='unavailable' else None)
            _,replayed,_=load_entity_batch(store,settings,args.project,args.run,record.id)
            if replayed!=batch:raise AppError('EVIDENCE_MISMATCH','Stored SAM batch differs from the actual reply',409)
            result={'record':reference(record).model_dump(mode='json'),'status':batch.status,'candidates':len(batch.candidates),
                'responses_attempts':cloud.budget.used,'remote_file_deleted':batch.provider_provenance.get('remote_file_deleted',
                    batch.provider_provenance.get('error',{}).get('details',{}).get('remote_file_deleted',False)),
                'identity_bindings_created':False,'output_actions_executed':False}
            proof.write_json(output/'result.json',result);print(json.dumps(result),flush=True)
            return 1 if batch.status=='unavailable' else 0
    except AppError as exc:
        if (output/'attempt.json').exists():proof.write_json(output/'error.json',exc.envelope())
        print(json.dumps(exc.envelope()),flush=True);return 1
    finally:
        store.release_process_lock()


if __name__=='__main__':raise SystemExit(main())
