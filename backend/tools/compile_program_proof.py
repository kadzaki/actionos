"""Opt-in live A19 Muse text-to-AST proof; no video recognition or fallback."""
from dataclasses import replace
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.config import Settings
from app.errors import AppError
from app.models import Asset
from app.programs.compiler import build_compile_request, compile_instruction
from app.providers.meta import MetaCloud
from app.storage import FileStore


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prepare-only',action='store_true',help='Write the exact pending request without a network call')
    args=parser.parse_args()
    root=Path(__file__).resolve().parents[2]
    output=root/'.tmp/compiler-proof'
    settings=replace(Settings.from_env(),data_dir=output/'state')
    store=FileStore(output)
    proof=json.loads((root/'.tmp/manual-action-proof/proof.json').read_text(encoding='utf-8'))
    media=FileStore(root/'.tmp/manual-action-proof/data')
    # Resolve the actual owned asset from the proof's project manifest.
    from app.models import Project
    project=media.load(proof['project_id'],'project',Project)
    asset=media.load(project.id,'assets',Asset,project.active_asset_id)
    source={'duration_ms':asset.metadata.duration_ms,'width':asset.metadata.geometry.display_width,
            'height':asset.metadata.geometry.display_height,'audio_present':asset.metadata.audio_present}
    instruction='Find each cycle where the operator places a component, then uses the tool. Flag any cycle where the tool step cannot be seen.'
    request={'instruction':instruction,'source':source,'source_sha256':asset.sha256,
             'scope':'live text-to-AST only; source is generated permitted media, not tabletop recognition'}
    store.write_json(output/'request.json',request)
    _,schema,content=build_compile_request(instruction,source)
    wire={'model':settings.muse_model,'stream':False,'input':[{'type':'message','role':'user',
          'content':[{'type':'input_text','text':'Return only a JSON object conforming to this application schema: '+json.dumps(schema,separators=(',',':'))},*content]}]}
    store.write_json(output/'pending-request.json',wire)
    if args.prepare_only:
        print(json.dumps({'prepared':str(output/'pending-request.json'),'network_calls':0,'source':source}),flush=True)
        return 0
    try:
        with MetaCloud(settings) as cloud:
            result=compile_instruction(cloud,instruction,source)
        store.write_json(output/'compilation.json',result.model_dump(mode='json',by_alias=True))
        print(json.dumps({'provider':'meta_cloud','ast_returned':result.draft.ast is not None,
            'structurally_valid':result.validation.valid,'issues':[i.model_dump(mode='json') for i in result.validation.issues],
            'clarifications':[i.model_dump(mode='json') for i in result.draft.clarifications],
            'latency_seconds':result.latency_seconds,'output':str(output/'compilation.json')}),flush=True)
        return 0 if result.validation.valid or result.draft.clarifications or result.draft.unsupported_operations else 1
    except AppError as exc:
        store.write_json(output/'error.json',exc.envelope())
        print(json.dumps(exc.envelope()),flush=True)
        return 1


if __name__=='__main__':
    raise SystemExit(main())
