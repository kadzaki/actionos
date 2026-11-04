"""Timestamp reports with owned evidence URLs and explicit unknown semantics."""
from html import escape
import hashlib
from uuid import UUID, uuid4

from pydantic import Field

from .contracts import ActionPlanData
from .registry import validate_event
from ..errors import AppError
from ..models import Model
from ..repository import ArtifactReference, Envelope, Repository, encoded, reference
from ..resources import MediaBudget
from ..source_evidence import SourceEvidenceAdapter


class ReportRow(Model):
    event_id: UUID
    step_id: str
    label: str
    event_verdict: str
    verdict: str
    source_start_ms: int
    source_end_ms: int
    step_start_ms: int | None
    step_end_ms: int | None
    reason: str
    source_url: str
    evidence: list[dict]
    observation_ids: list[UUID]
    track_ids: list[UUID]
    coverage: dict
    provenance: dict


class ReportDocument(Model):
    report_schema_version: int = 1
    report_id: UUID
    title: str
    project_id: UUID
    source_asset_id: UUID
    source_sha256: str
    program_id: UUID
    program_version: int
    program_fingerprint: str
    run_id: UUID
    action_id: UUID
    fixture_only: bool
    review_only: bool
    rows: list[ReportRow] = Field(min_length=1, max_length=1024)


def readable_html(document):
    label = 'MANUAL DEVELOPER FIXTURE - not live detection or temporal evaluation' if document.fixture_only else 'Source-linked timestamp review'
    parts = ['<!doctype html><html lang="en"><head><meta charset="utf-8">',
             '<meta name="viewport" content="width=device-width, initial-scale=1">',
             f'<title>{escape(document.title)}</title>',
             '<style>body{font:15px system-ui;margin:2rem;background:#111115;color:#eee}a{color:#c6afff}table{border-collapse:collapse;width:100%}th,td{border:1px solid #555;padding:.6rem;text-align:left;vertical-align:top}caption{text-align:left;margin:1rem 0}code{overflow-wrap:anywhere}</style>',
             f'</head><body><h1>{escape(document.title)}</h1><p><strong>{label}</strong></p>',
             '<p>Unknown means evidence is insufficient. Not observed does not mean the operation did not happen.</p>',
             f'<p>Program <code>{document.program_id}</code> v{document.program_version}; run <code>{document.run_id}</code>; source <code>{document.source_sha256}</code></p>',
             '<table><caption>Timestamped event and step evidence</caption><thead><tr><th scope="col">Event / step</th><th scope="col">Source interval</th><th scope="col">Verdict</th><th scope="col">Reason / evidence</th></tr></thead><tbody>']
    for row in document.rows:
        links = ' '.join(f'<a href="{escape(e["image_url"], quote=True)}">Frame {e["source_frame_index"]} at {e["source_time_ms"]:.3f} ms</a>' for e in row.evidence)
        parts.append(f'<tr><td><code>{row.event_id}</code><br>{escape(row.step_id)}: {escape(row.label)}</td>'
                     f'<td><a href="{escape(row.source_url, quote=True)}">{row.source_start_ms}-{row.source_end_ms} ms</a></td>'
                     f'<td>{escape(row.verdict)}<br>Cycle: {escape(row.event_verdict)}</td>'
                     f'<td>{escape(row.reason)}<br>{links}</td></tr>')
    parts.append('</tbody></table></body></html>')
    return ''.join(parts).encode('utf-8')


def build_report(store, settings, plan):
    repo = Repository(store)
    checked = repo.load(plan.project_id, 'action_plans', plan.id)
    if checked != plan:
        raise AppError('REFERENCE_MISMATCH', 'Action plan changed before report execution', 409)
    data = ActionPlanData.model_validate(plan.payload)
    if data.request.type != 'create_report':
        raise AppError('INVALID_ACTION', 'Report executor requires a report plan', 422)
    if not data.request.regenerate:
        for path in sorted(store.path(plan.project_id, 'reports').glob('*.json')):
            try:
                previous = repo.load(plan.project_id, 'reports', path.stem)
            except AppError:
                continue
            if (previous.payload.get('request_fingerprint') == data.request_fingerprint
                    and reference(plan) in previous.references):
                return previous
    adapter = SourceEvidenceAdapter(store, settings, plan.project_id, plan.source_asset_id)
    rows = []
    for event_ref in data.event_references:
        record = repo.load(plan.project_id, 'temporal_events', event_ref.id)
        event = validate_event(store, adapter, record)
        for step in event.steps:
            reason = step.reason
            if step.verdict == 'unknown':
                reason += ' Not observed; requires review. This does not establish physical non-occurrence.'
            rows.append(ReportRow(event_id=record.id, step_id=step.step_id, label=step.label,
                event_verdict=event.verdict, verdict=step.verdict,
                source_start_ms=event.interval.start_ms, source_end_ms=event.interval.end_ms,
                step_start_ms=step.interval.start_ms if step.interval else None,
                step_end_ms=step.interval.end_ms if step.interval else None,
                reason=reason, source_url=f'/api/assets/{plan.source_asset_id}/stream?variant=original#t={event.interval.start_ms/1000:.3f}',
                evidence=[{'frame_id': str(f.evidence_frame_id), 'source_frame_index': f.source_frame_index,
                           'source_time_ms': f.source_time_ms, 'source_pts': f.source_pts,
                           'observed_at_source_ms': f.observed_at_source_ms,
                           'source_time_base': f.source_time_base, 'image_sha256': f.image_sha256,
                           'image_url': f.image_url, 'source_url': f.source_url} for f in step.frames],
                observation_ids=step.observation_ids, track_ids=event.track_ids,
                coverage=event.coverage, provenance={'event_origin': event.origin,
                    'event_record_sha256': event_ref.record_sha256, 'fixture_only': data.fixture_only}))
    report_id = uuid4()
    document = ReportDocument(report_id=report_id, title=data.request.parameters.title,
        project_id=plan.project_id, source_asset_id=plan.source_asset_id, source_sha256=plan.source_sha256,
        program_id=plan.program.id, program_version=plan.program.version,
        program_fingerprint=plan.program.record_sha256, run_id=plan.run.id, action_id=plan.id,
        fixture_only=data.fixture_only, review_only=data.request.review_only, rows=rows)
    raw_json = encoded(document.model_dump(mode='json'))
    raw_html = readable_html(document)
    # Validate serialized JSON before any public report manifest is written.
    ReportDocument.model_validate_json(raw_json)
    budget = MediaBudget(store.project_dir(plan.project_id), settings)
    budget.check(len(raw_json) + len(raw_html))
    artifacts = []
    for suffix, raw, media_type in [('json', raw_json, 'application/json'), ('html', raw_html, 'text/html')]:
        relative = f'reports/artifacts/{report_id}.{suffix}'
        store.write_bytes(store.path(plan.project_id, relative), raw)
        artifacts.append(ArtifactReference(path=relative, sha256=hashlib.sha256(raw).hexdigest(),
                                            byte_size=len(raw), media_type=media_type))
    return repo.publish(Envelope(kind='reports', id=report_id, project_id=plan.project_id,
        source_asset_id=plan.source_asset_id, source_sha256=plan.source_sha256,
        program=plan.program, run=plan.run, references=[reference(plan), *data.event_references],
        artifacts=artifacts, payload={'title': document.title, 'row_count': len(rows),
            'fixture_only': data.fixture_only, 'request_fingerprint': data.request_fingerprint}))
