"""Prepare one source-bound A25 request; execution requires its reviewed digest.

Preparation is local. This command neither approves a program nor supplies event
times, and does not declare the tabletop acceptance gate passed. Use a reviewed
program/run in an offline server data directory (the process lock is exclusive).
"""
import argparse
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.config import Settings
from app.engine.muse_observations import build_observation_request, observe_window, prepare_window, observation_text_format
from app.errors import AppError
from app.programs.repository import load_program
from app.providers.meta import MetaCloud
from app.repository import encoded
from app.storage import FileStore
from app.validation import WorkBudget


def request_manifest(prepared,*,permission,compiler_origin):
    content,schema=build_observation_request(prepared)
    manifest={
        'scope':'one_window_hypotheses_only; not temporal evaluation or an acceptance claim',
        'media_permission_statement':permission,'compiler_origin':compiler_origin,
        'source_asset_id':str(prepared.plan.source_asset_id),'source_sha256':prepared.plan.source_sha256,
        'run_id':str(prepared.plan.run_id),'program_fingerprint':prepared.plan.program_fingerprint,
        'plan_fingerprint':prepared.plan.request_fingerprint,'planned_window_id':prepared.planned_window.id,
        'input_video':prepared.window.model_dump(mode='json'),
        'provider_configuration':prepared.plan.providers,
        'files_upload':{'method':'POST','endpoint':'https://api.meta.ai/v1/files',
            'purpose':'user_data','filename':'source-window.mp4','content_type':'video/mp4',
            'sha256':prepared.window.sha256},
        'responses_request':{'model':prepared.plan.providers['muse_model'],'stream':False,
            'input':[{'type':'message','role':'user','content':[
                {'type':'input_text','text':'Return only a JSON object conforming to this application schema: '+json.dumps(schema,separators=(',',':'))},
                *content,{'type':'input_video','file_id':'<Files upload result>'}]}]},
        'cleanup':'DELETE https://api.meta.ai/v1/files/<Files upload result> in finally',
        'responses_attempt_limit':1,
    }
    native=observation_text_format(prepared,schema)
    if native is not None:manifest['responses_request']['text']={'format':native}
    return manifest


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir',type=Path,required=True)
    parser.add_argument('--project',required=True)
    parser.add_argument('--run',required=True)
    parser.add_argument('--plan',required=True)
    parser.add_argument('--window',default='w000')
    parser.add_argument('--permission',required=True,help='Actual media permission/provenance, including fixture status')
    parser.add_argument('--output',type=Path,default=Path('.tmp/step-observation-proof'))
    parser.add_argument('--execute-reviewed-sha256',help='Execute exactly the prepared request with this digest; omit for local preparation')
    args=parser.parse_args()
    settings=replace(Settings.from_env(),data_dir=args.data_dir.resolve())
    store=FileStore(settings.data_dir)
    output=args.output.resolve();output.mkdir(parents=True,exist_ok=True)
    proof_store=FileStore(output)
    store.acquire_process_lock()
    try:
        prepared=prepare_window(store,settings,args.project,args.run,args.plan,args.window)
        from app.repository import Repository
        run=Repository(store).load(args.project,'runs',args.run)
        _,program=load_program(store,args.project,run.program.id,run.program.version)
        manifest=request_manifest(prepared,permission=args.permission,compiler_origin=program.program.compiler.origin)
        digest=hashlib.sha256(encoded(manifest)).hexdigest()
        request_path=output/'pending-request.json'
        if not args.execute_reviewed_sha256:
            proof_store.write_json(request_path,manifest)
            print(json.dumps({'prepared':str(request_path),'sha256':digest,'cloud_requests_made':0}),flush=True)
            return 0
        if (args.execute_reviewed_sha256!=digest or not request_path.is_file()
                or hashlib.sha256(encoded(json.loads(request_path.read_text(encoding='utf-8')))).hexdigest()!=digest):
            raise AppError('STALE_PROOF_REQUEST','Prepare and review this exact source/program/window request before execution',409)
        if (output/'attempt.json').exists():
            raise AppError('PROOF_ALREADY_ATTEMPTED','This proof output already records an attempt; inspect its result before any new request',409)
        with MetaCloud(settings,budget=WorkBudget(provider_calls=1)) as cloud:
            proof_store.write_json(output/'attempt.json',{'request_sha256':digest,'responses_attempt_limit':1})
            def record(raw,provenance):
                proof_store.write_json(output/'provider-response.json',{'output':raw,'provenance':provenance})
            def record_text(text,provenance):
                proof_store.write_json(output/'provider-text-response.json',{'text':text,'provenance':provenance,
                    'capture_stage':'before JSON parsing; cleanup result is recorded in observations.json or error.json'})
            try:
                batch=observe_window(cloud,prepared,record_response=record,record_text_response=record_text)
                proof_store.write_json(output/'observations.json',batch.model_dump(mode='json'))
                print(json.dumps({'normalized':True,'observations':len(batch.observations),
                    'missing_step_ids':batch.missing_step_ids,'responses_attempts':cloud.budget.used,
                    'remote_file_deleted':batch.provider_provenance['remote_file_deleted'],
                    'output':str(output/'observations.json'),'tabletop_gate':'requires human source/evidence inspection'}),flush=True)
                return 0
            except AppError as exc:
                proof_store.write_json(output/'error.json',exc.envelope())
                raise
    except AppError as exc:
        print(json.dumps(exc.envelope()),flush=True)
        return 1
    finally:
        store.release_process_lock()


if __name__=='__main__':raise SystemExit(main())
