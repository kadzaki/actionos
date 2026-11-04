"""Validate a full-source Muse request bundle locally; explicit execution is single-use.

Reuses step_observation_proof's exact-request guard and one-attempt allowance for
each window. Stops at the first failure. No SAM requests or actions are issued.
"""
import argparse
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import Settings
from app.engine.muse_observations import prepare_window
from app.engine.observation_repository import load_muse_record, publish_muse_record
from app.engine.planning import ObservationPlan, plan_run
from app.errors import AppError
from app.programs.repository import load_program
from app.repository import encoded, reference
from app.storage import FileStore, file_sha256
from step_observation_proof import request_manifest


def preflight(bundle, settings, store):
    windows = bundle['windows']
    if (not 1 <= len(windows) <= 24 or bundle['responses_attempt_limit_total'] != len(windows)
            or bundle['responses_attempt_limit_per_window'] != 1 or bundle['stop_on_first_failure'] is not True
            or bundle['sam_requests_authorized_by_this_bundle'] != 0 or bundle['actions_to_execute'] != []):
        raise AppError('INVALID_PROOF_BUNDLE', 'Only bounded single-attempt Muse windows without SAM/actions are supported', 409)
    record = plan_run(store, settings, bundle['project_id'], bundle['run']['id'])
    plan = ObservationPlan.model_validate(record.payload['plan'])
    if (reference(record).model_dump(mode='json') != bundle['plan'] or record.run.model_dump(mode='json') != bundle['run']
            or plan.source_sha256 != bundle['source_sha256'] or plan.program_fingerprint != bundle['program_fingerprint']
            or [w.id for w in plan.windows] != [w['window_id'] for w in windows]):
        raise AppError('STALE_PROOF_REQUEST', 'Bundle does not cover the exact reviewed run and full-source plan', 409)
    _, program = load_program(store, bundle['project_id'], bundle['program_id'], bundle['program_version'])
    for row in windows:
        prepared = prepare_window(store, settings, bundle['project_id'], bundle['run']['id'], record.id, row['window_id'])
        manifest = request_manifest(prepared, permission=bundle['media_permission_statement'], compiler_origin=program.program.compiler.origin)
        saved = json.loads(Path(row['request_path']).read_text(encoding='utf-8'))
        media = store.path(bundle['project_id'], prepared.window.path)
        if (encoded(saved) != encoded(manifest) or hashlib.sha256(encoded(manifest)).hexdigest() != row['request_sha256']
                or media.resolve() != Path(row['video_path']).resolve() or file_sha256(media) != row['video_sha256']
                or prepared.window.sha256 != row['video_sha256'] or media.stat().st_size != row['video_bytes']
                or prepared.planned_window.source_interval.model_dump(mode='json') != row['source_interval']):
            raise AppError('STALE_PROOF_REQUEST', 'Prepared media or request differs from the reviewed bundle', 409)
        if (Path(row['request_path']).parent / 'attempt.json').exists():
            raise AppError('PROOF_ALREADY_ATTEMPTED', 'A bundle window already records an attempt; inspect it before further calls', 409)


def persist_unavailable(store, settings, bundle, row, directory, error):
    """Preserve a failed attempted window without inventing a provider response."""
    raw_path = directory / 'provider-response.json'
    if raw_path.exists():
        raw = json.loads(raw_path.read_text(encoding='utf-8'))
    elif (directory / 'provider-text-response.json').exists():
        captured = json.loads((directory / 'provider-text-response.json').read_text(encoding='utf-8'))
        raw = {'output': {'unparsed_text': captured['text']}, 'provenance': {
            **captured['provenance'], 'structured_output_received': False,
            'remote_file_deleted': error.get('details', {}).get('remote_file_deleted', False)}}
    else:
        raw = {'output': None, 'provenance': {'provider': 'meta_cloud',
            'requested_model': settings.muse_model, 'endpoint': settings.meta_api_base_url.rstrip('/'),
            'response_received': False, 'remote_file_deleted': error.get('details', {}).get('remote_file_deleted', False)}}
    store.acquire_process_lock()
    try:
        record = publish_muse_record(store, settings, bundle['project_id'], bundle['run']['id'], row['window_id'],
            raw['output'], raw['provenance'], origin='meta_cloud', error=error,
            evidence_scope='Actual provider attempt failed; unavailable observation, not a successful empty result')
        _, batch, _ = load_muse_record(store, settings, bundle['project_id'], bundle['run']['id'], record.id)
        if batch is not None:
            raise AppError('EVIDENCE_MISMATCH', 'Failed provider attempt unexpectedly supplied a validated observation batch', 409)
        return reference(record).model_dump(mode='json')
    finally:
        store.release_process_lock()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--execute-reviewed-sha256', help='Only after explicit approval of this video bundle')
    args = parser.parse_args()
    output = args.bundle.resolve().parent
    proof = FileStore(output)
    bundle = json.loads(args.bundle.read_text(encoding='utf-8'))
    digest = hashlib.sha256(encoded(bundle)).hexdigest()
    settings = replace(Settings.from_env(), data_dir=Path(bundle['data_dir']))
    store = FileStore(settings.data_dir)
    try:
        if args.execute_reviewed_sha256 and args.execute_reviewed_sha256 != digest:
            raise AppError('STALE_PROOF_REQUEST', 'Execution digest does not match this exact bundle', 409)
        store.acquire_process_lock()
        try:
            preflight(bundle, settings, store)
        finally:
            store.release_process_lock()
        if not args.execute_reviewed_sha256:
            print(json.dumps({'validated': True, 'bundle_sha256': digest, 'muse_windows': len(bundle['windows']), 'cloud_requests_made': 0}), flush=True)
            return 0
        if (output / 'bundle-attempt.json').exists():
            raise AppError('PROOF_ALREADY_ATTEMPTED', 'Inspect the saved bundle attempt before any further request', 409)
        proof.write_json(output / 'bundle-attempt.json', {'bundle_sha256': digest, 'window_attempt_limit': len(bundle['windows'])})
        results = []
        for row in bundle['windows']:
            directory = Path(row['request_path']).parent
            print(json.dumps({'starting_window': row['window_id']}), flush=True)
            child = subprocess.run([sys.executable, str(Path(__file__).with_name('step_observation_proof.py')),
                '--data-dir', str(settings.data_dir), '--project', bundle['project_id'], '--run', bundle['run']['id'],
                '--plan', bundle['plan']['id'], '--window', row['window_id'], '--permission', bundle['media_permission_statement'],
                '--output', str(directory), '--execute-reviewed-sha256', row['request_sha256']], capture_output=True, text=True)
            result = {'window_id': row['window_id'], 'exit_code': child.returncode, 'state': 'normalized' if child.returncode == 0 else 'failed'}
            if child.returncode == 0:
                raw = json.loads((directory / 'provider-response.json').read_text(encoding='utf-8'))
                store.acquire_process_lock()
                try:
                    record = publish_muse_record(store, settings, bundle['project_id'], bundle['run']['id'], row['window_id'],
                        raw['output'], raw['provenance'], origin='meta_cloud',
                        evidence_scope='Actual program-conditioned source window; hypotheses only, not full primary demo or final event truth')
                    _, batch, _ = load_muse_record(store, settings, bundle['project_id'], bundle['run']['id'], record.id)
                    if batch is None:
                        raise AppError('PROVIDER_SCHEMA', 'Normalized result could not be replayed as validated evidence', 502)
                    result.update(record=reference(record).model_dump(mode='json'), observations=len(batch.observations),
                        remote_file_deleted=raw['provenance'].get('remote_file_deleted', False))
                finally:
                    store.release_process_lock()
            elif (directory / 'error.json').exists():
                result['error'] = json.loads((directory / 'error.json').read_text(encoding='utf-8'))
                if str(result['error'].get('code', '')).startswith('PROVIDER_') and (directory / 'attempt.json').exists():
                    result['record'] = persist_unavailable(store, settings, bundle, row, directory, result['error'])
            results.append(result)
            proof.write_json(output / 'bundle-results.json', {'bundle_sha256': digest, 'windows': results,
                'stopped_on_failure': child.returncode != 0, 'full_source_processing_completed': len(results) == len(bundle['windows']) and child.returncode == 0,
                'final_event_evaluation': 'not performed'})
            print(json.dumps(result), flush=True)
            if child.returncode != 0:
                return 1
        return 0
    except AppError as exc:
        if (output / 'bundle-attempt.json').exists():
            proof.write_json(output / 'bundle-error.json', exc.envelope())
        print(json.dumps(exc.envelope()), flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
