"""Validate a prepared source-bound compiler request; execution is explicit and single-use.

Default operation is local, including derivative integrity checks. Execution never
uploads media, confirms assumptions, approves a program or starts a run.
"""
import argparse
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sys
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import Settings
from app.errors import AppError
from app.programs.compiler import build_compile_request, compile_instruction
from app.programs.repository import provider_recording, save_compilation
from app.providers.meta import MetaCloud
from app.repository import encoded
from app.source_evidence import SourceEvidenceAdapter
from app.storage import FileStore
from app.validation import WorkBudget


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--preparation', type=Path, required=True)
    parser.add_argument('--execute-reviewed-sha256', help='Only after authorization of this exact request')
    args = parser.parse_args()
    prepared = json.loads(args.preparation.read_text(encoding='utf-8'))
    output = args.preparation.resolve().parent
    proof = FileStore(output)
    settings = replace(Settings.from_env(), data_dir=Path(prepared['data_dir']))
    store = FileStore(settings.data_dir)
    store.acquire_process_lock()
    try:
        source = SourceEvidenceAdapter(store, settings, prepared['project_id'], prepared['asset_id'])
        asset = source.asset
        metadata = {'duration_ms': asset.metadata.duration_ms,
                    'width': asset.metadata.geometry.display_width,
                    'height': asset.metadata.geometry.display_height,
                    'audio_present': asset.metadata.audio_present}
        _, schema, content = build_compile_request(prepared['instruction'], metadata)
        wire = {'model': settings.muse_model, 'stream': False, 'input': [{'type': 'message', 'role': 'user',
                'content': [{'type': 'input_text', 'text': 'Return only a JSON object conforming to this application schema: ' + json.dumps(schema, separators=(',', ':'))}, *content]}]}
        digest = hashlib.sha256(encoded(wire)).hexdigest()
        saved = json.loads(Path(prepared['request_path']).read_text(encoding='utf-8'))
        if (asset.sha256 != prepared['source_sha256'] or metadata != prepared['source_metadata']
                or asset.artifact_sha256 != prepared['source_artifact_sha256']
                or settings.meta_api_base_url.rstrip('/') + '/responses' != prepared['compiler_endpoint']
                or digest != prepared['request_sha256'] or encoded(saved) != encoded(wire)):
            raise AppError('STALE_PROOF_REQUEST', 'Source, provider or prepared request changed; prepare and review it again', 409)
        if not args.execute_reviewed_sha256:
            print(json.dumps({'validated': True, 'request_sha256': digest, 'cloud_requests_made': 0,
                              'video_transmitted': False, 'execution': 'requires explicit authorization and matching digest'}), flush=True)
            return 0
        if args.execute_reviewed_sha256 != digest:
            raise AppError('STALE_PROOF_REQUEST', 'Execution digest does not match the prepared request', 409)
        if (output / 'compiler-attempt.json').exists():
            raise AppError('PROOF_ALREADY_ATTEMPTED', 'Inspect the previous attempt before preparing a new request', 409)

        class ExactTextRequest(MetaCloud):
            def request(self, method, path, **kwargs):
                if method != 'POST' or path != 'responses' or encoded(kwargs.get('json')) != encoded(wire):
                    raise AppError('STALE_PROOF_REQUEST', 'Only the reviewed text request may be transmitted', 409)
                return super().request(method, path, **kwargs)

        compilation_id = uuid4()
        with ExactTextRequest(settings, budget=WorkBudget(provider_calls=1)) as cloud:
            proof.write_json(output / 'compiler-attempt.json', {'request_sha256': digest,
                             'compilation_id': str(compilation_id), 'responses_attempt_limit': 1})
            artifact = None

            def record(raw, provenance):
                nonlocal artifact
                proof.write_json(output / 'compiler-provider-response.json', {'output': raw, 'provenance': provenance})
                artifact = provider_recording(store, asset, compilation_id, raw, provenance, settings=settings)

            try:
                compilation = compile_instruction(cloud, prepared['instruction'], metadata, record_response=record)
                proof.write_json(output / 'source-compilation.json', compilation.model_dump(mode='json', by_alias=True))
                record, program = save_compilation(store, asset, compilation_id, compilation, artifact)
                result = {'compilation_id': str(record.id), 'program_id': str(program.id) if program else None,
                          'validation_valid': compilation.validation.valid, 'responses_attempts': cloud.budget.used,
                          'program_review_created': False, 'run_created': False, 'video_transmitted': False}
                proof.write_json(output / 'compiler-result.json', result)
                print(json.dumps(result), flush=True)
                return 0
            except AppError as exc:
                proof.write_json(output / 'compiler-error.json', exc.envelope())
                raise
    except AppError as exc:
        print(json.dumps(exc.envelope()), flush=True)
        return 1
    finally:
        store.release_process_lock()


if __name__ == '__main__':
    raise SystemExit(main())
