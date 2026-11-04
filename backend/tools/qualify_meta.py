"""Opt-in A03 transport qualification, never imported by production or pytest.

Makes two live media requests using a locally generated, nonpersonal clip.
Stores only application output/provenance, not secrets or internal reasoning.
Does not qualify VisualProgram compilation or predicate observations.
"""
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import Settings
from app.errors import AppError
from app.providers.meta import MetaCloud
from app.providers.sam import parse_sam
from app.storage import FileStore, file_sha256


def main():
    root = Path(__file__).resolve().parents[2]
    output = root / '.tmp/meta-qualification'
    settings = replace(Settings.from_env(), data_dir=output / 'state')
    store = FileStore(output)
    report = {'recorded_at': datetime.now(timezone.utc).isoformat(),
              'evidence_kind': 'live_generated_media_transport_only',
              'endpoint': settings.meta_api_base_url,
              'compiler_contract': 'not_tested', 'observation_contract': 'not_tested',
              'checks': []}
    clip = output / 'motion.mp4'
    subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y',
                    '-f', 'lavfi', '-i',
                    "color=c=black:s=320x180:r=15:d=2[bg];color=c=red:s=40x40:r=15:d=2[box];[bg][box]overlay=x='20+50*t':y=70:shortest=1",
                    '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(clip)],
                   check=True, timeout=20)
    report['input'] = {'sha256': file_sha256(clip), 'width': 320, 'height': 180,
                       'fps': 15, 'frames': 30, 'duration_ms': 2000,
                       'permission': 'application-generated geometric transport input'}
    for kind in ('muse_video', 'sam_video'):
        started = time.monotonic()
        check = {'capability': kind, 'passed': False}
        cleanup = {}
        try:
            with MetaCloud(settings) as cloud:
                with cloud.video_file(clip) as (file_id, cleanup):
                    media = {'type': 'input_video', 'file_id': file_id}
                    if kind == 'muse_video':
                        schema = {'type': 'object', 'required': ['direction', 'description'],
                                  'properties': {'direction': {'type': 'string'},
                                                 'description': {'type': 'string'}},
                                  'additionalProperties': False}
                        value, provenance = cloud.json_response(settings.muse_model,
                            [{'type': 'input_text', 'text': 'Describe the red square motion. Use direction left_to_right, right_to_left, stationary, or unknown.'}, media], schema)
                        if (not isinstance(value, dict) or set(value) != {'direction', 'description'}
                                or value['direction'] != 'left_to_right'
                                or not isinstance(value['description'], str)):
                            raise AppError('QUALIFICATION_FAILED', 'Video response did not establish the expected motion', 502)
                        check['output'] = value
                    else:
                        text, provenance = cloud.stream_response(settings.sam_model,
                            [{'type': 'input_text', 'text': 'red square'}, media])
                        objects = parse_sam(text, 30, 320, 180)
                        check['output'] = text
                        check['object_frame_counts'] = {str(k): len(v) for k, v in objects.items()}
                        if not any(len(frames) >= 2 and frames[max(frames)][0] > frames[min(frames)][0]
                                   for frames in objects.values()):
                            raise AppError('QUALIFICATION_FAILED', 'SAM did not establish timestamped moving-object geometry', 502)
                    check.update(passed=True, provenance=provenance)
        except AppError as exc:
            check['error'] = exc.envelope()
        finally:
            check['latency_seconds'] = round(time.monotonic() - started, 3)
            check['remote_file_deleted'] = cleanup.get('deleted', False)
            report['checks'].append(check)
            store.write_json(output / 'report.json', report)
            print(json.dumps({k: v for k, v in check.items() if k not in ('output', 'provenance')}), flush=True)
    return 0 if all(c['passed'] and c['remote_file_deleted'] for c in report['checks']) else 1


if __name__ == '__main__':
    raise SystemExit(main())
