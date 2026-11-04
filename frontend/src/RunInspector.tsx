import { useEffect, useRef, useState } from 'react'
import { api } from './api'
import type { Job } from './types'

type Frame = { evidence_frame_id: string; source_frame_index: number; source_time_ms: number; observed_at_source_ms?: number | null; source_asset_id: string }
type Step = { step_id: string; label: string; verdict: string; interval: { start_ms: number; end_ms: number } | null; reason: string; frames: Frame[]; observation_ids: string[] }
type Event = { id: string; record_sha256: string; payload: { label: string; origin: string; verdict: string; interval: { start_ms: number; end_ms: number }; steps: Step[]; review_reason: string; coverage: { selected?: boolean; estimated?: boolean; edges?: { relation_id: string; from_step: string; to_step: string; verdict: string; reasons: string[] }[] } } }
type Run = { id: string; program: { id: string; version: number }; created_at: string }
type Results = { items: Event[]; next_offset: number | null; evaluation: { outcome: string; reasons: string[]; coverage_complete: boolean; graph: unknown } | null }
type Outputs = { items: { id: string; payload: { status: string; outputs: { type: string; stream_url?: string; download_url?: string; view_url?: string; json_url?: string }[]; failed_events: unknown[] } }[] }

const seconds = (ms: number) => (ms / 1000).toFixed(2)
const outcomeLabels: Record<string, string> = { matches: 'Matches found', no_match: 'No matching cycles', all_unknown: 'Needs review', incomplete: 'Analysis incomplete', human_review: 'Human-reviewed results' }
const verdictLabel = (value: string) => value === 'true' ? 'Supported' : value === 'false' ? 'Not matched' : 'Needs review'
function evidenceSummary(reason: string) {
  const messages: string[] = []
  if (/IDENTITY|ENTITY_BINDING/.test(reason)) messages.push('Objects or people still need confirmation.')
  if (/OCCLUDED|NOT_VISIBLE|NOT_CLEARLY_VISIBLE|VISIBILITY/.test(reason)) messages.push('Part of the interaction is not clearly visible.')
  if (/COVERAGE|SAMPLING|OBSERVED_RANGES/.test(reason)) messages.push('Some steps are not fully covered by the available evidence.')
  if (/QUALITY|RESOLUTION|blur/i.test(reason)) messages.push('Image quality limits confidence.')
  return messages.slice(0, 2).join(' ') || 'The available evidence is not sufficient to confirm every required step.'
}

export function RunInspector({ projectId, programId, version, fingerprint, approved, job, locked, onJob }: {
  projectId: string; programId: string; version: number; job: Job | null; locked: boolean; onJob: (job: Job) => void
  fingerprint: string; approved: boolean
}) {
  const advanced = new URLSearchParams(window.location.search).get('advanced') === '1'
  const [runs, setRuns] = useState<Run[]>([])
  const [parents, setParents] = useState<Run[]>([])
  const [parentId, setParentId] = useState('')
  const [runId, setRunId] = useState('')
  const [results, setResults] = useState<Results | null>(null)
  const [resultsLoading, setResultsLoading] = useState(false)
  const [loadedRunId, setLoadedRunId] = useState('')
  const [sourceCursor, setSourceCursor] = useState<{ assetId: string; atMs: number } | null>(null)
  const sourcePlayer = useRef<HTMLVideoElement>(null)
  const [outputs, setOutputs] = useState<Outputs>({ items: [] })
  const [offset, setOffset] = useState(0)
  const [selected, setSelected] = useState<string[]>([])
  const [reviewOnly, setReviewOnly] = useState(false)
  const [error, setError] = useState('')
  const [refresh, setRefresh] = useState(0)
  const [sending, setSending] = useState(false)
  const [correction, setCorrection] = useState<Event | null>(null)
  const [edit, setEdit] = useState('')
  const [reason, setReason] = useState('')

  useEffect(() => { setSelected([]); setCorrection(null); setOffset(0) }, [runId])

  useEffect(() => {
    const controller = new AbortController()
    api<{ items: Run[] }>(`/api/projects/${projectId}/runs?limit=50`, { signal: controller.signal }).then(value => {
      const owned = value.items.filter(r => r.program.id === programId && r.program.version === version)
      const prior = value.items.filter(r => r.program.id === programId)
      setParents(prior); setParentId(previous => prior.some(r => r.id === previous) ? previous : prior[0]?.id ?? '')
      setRuns(owned)
      setRunId(previous => job?.run_id && owned.some(r => r.id === job.run_id) ? job.run_id : owned.some(r => r.id === previous) ? previous : owned[0]?.id ?? '')
    }).catch(err => { if (!controller.signal.aborted) setError((err as Error).message) })
    return () => controller.abort()
  }, [projectId, programId, version, job?.id, job?.status, refresh])

  useEffect(() => {
    if (!runId) { setResults(null); return }
    const controller = new AbortController()
    setResultsLoading(true)
    Promise.all([
      api<Results>(`/api/runs/${runId}/events?offset=${offset}`, { signal: controller.signal }),
      api<Outputs>(`/api/runs/${runId}/outputs`, { signal: controller.signal }),
    ]).then(([events, saved]) => { setResults(events); setOutputs(saved); setLoadedRunId(runId) })
      .catch(err => { if (!controller.signal.aborted) setError((err as Error).message) })
      .finally(() => { if (!controller.signal.aborted) setResultsLoading(false) })
    return () => controller.abort()
  }, [runId, offset, job?.status, refresh])

  useEffect(() => {
    const element = sourcePlayer.current
    if (element && sourceCursor && element.readyState >= 1) { element.currentTime = sourceCursor.atMs / 1000; element.pause(); element.scrollIntoView({ behavior: 'smooth', block: 'center' }) }
  }, [sourceCursor])

  const dispatch = async (path: string, body?: unknown) => {
    setSending(true); setError('')
    try {
      const accepted = await api<{ job_id: string }>(path, { method: 'POST', ...(body === undefined ? {} : { body: JSON.stringify(body) }) })
      onJob(await api<Job>(`/api/jobs/${accepted.job_id}`))
    } catch (err) { setError((err as Error).message) }
    finally { setSending(false) }
  }
  const correct = async () => {
    if (!correction) return
    setSending(true); setError('')
    try {
      await api(`/api/runs/${runId}/events/${correction.id}/corrections`, { method: 'POST', body: JSON.stringify({
        base_fingerprint: correction.record_sha256, reason, steps: JSON.parse(edit),
      }) })
      setCorrection(null); setSelected([]); setRefresh(n => n + 1)
    } catch (err) { setError((err as Error).message) }
    finally { setSending(false) }
  }
  const busy = locked || sending
  const visibleResults = loadedRunId === runId ? results : null
  const visibleOutputs = loadedRunId === runId ? outputs.items : []
  const needsReview = !!visibleResults?.items.some(event => selected.includes(event.id) && (event.payload.verdict !== 'true' || event.payload.coverage.estimated || event.payload.coverage.selected === false))
  return <section className="run-inspector" aria-label="Run evidence">
    <details className="run-options"><summary>Run history & saved evidence</summary>
    {parents.length > 0 && <div><label>Reuse compatible saved evidence <select value={parentId} disabled={busy} onChange={event => setParentId(event.target.value)}>
      {parents.map(run => <option key={run.id} value={run.id}>Version {run.program.version} · {run.id.slice(0, 8)}</option>)}</select></label>
      <button className="secondary-button" disabled={busy || !approved || !parentId} onClick={() => void dispatch(`/api/programs/${programId}/run`, {
        version, program_fingerprint: fingerprint, mode: 'reuse', parent_run_id: parentId,
      })}>Run locally with compatible saved evidence</button><p className="field-help">Source, entities, predicates, policies and provider versions are checked before reuse. Changed perception requirements need a new cloud run.</p></div>}
    {!!runs.length && <><label>Saved run <select value={runId} disabled={busy} onChange={event => { setRunId(event.target.value); setOffset(0); setSelected([]) }}>
      {runs.map((run, index) => <option key={run.id} value={run.id}>Run {runs.length - index} · {new Date(run.created_at).toLocaleString()}</option>)}</select></label>
      <div className="program-controls"><button className="secondary-button" disabled={busy} onClick={() => void dispatch(`/api/runs/${runId}/evaluate`)}>Update results from saved evidence</button>
      <button className="secondary-button" disabled={sending} onClick={() => setRefresh(n => n + 1)}>Refresh</button></div>
      <p className="field-help">After confirming tracked objects, update the results. This uses saved evidence without a new cloud call.</p></>}
    </details>
    {job && ['observe', 'evaluate', 'action', 'report'].includes(job.type) && <div>
      {['queued', 'running'].includes(job.status) && <button disabled={sending} onClick={() => void api(`/api/jobs/${job.id}/cancel`, { method: 'POST' }).catch(err => setError((err as Error).message))}>Cancel at next processing boundary</button>}
      {job.error && <p className="feedback error">{job.error.message} Completed evidence and outputs remain available.</p>}</div>}
    {job && ['observe', 'evaluate', 'action', 'report'].includes(job.type) && ['failed', 'cancelled', 'interrupted'].includes(job.status) && <button className="secondary-button" disabled={busy} onClick={() => void dispatch(`/api/jobs/${job.id}/retry`)}>{job.type === 'observe' ? 'Retry as a new cloud run' : 'Retry this operation'}</button>}
    {!runs.length ? <p className="results-empty">Run the approved rule to analyze your video.</p> : <>
      {resultsLoading && <p role="status">Loading run results…</p>}
      {visibleResults?.evaluation ? <div className="results-summary" role="status"><strong>{outcomeLabels[visibleResults.evaluation.outcome] ?? 'Results available'}</strong>
        <span>{visibleResults.items.length} candidate{visibleResults.items.length === 1 ? '' : 's'} on this page</span>
        {!visibleResults.evaluation.coverage_complete && <p>Some steps or identities could not be verified. Unseen steps are not treated as absent.</p>}
      </div> : !resultsLoading && <p>No saved evaluation yet. Completed observations remain available if a run failed.</p>}
      {visibleResults?.evaluation && <details className="technical-details"><summary>Technical details & provenance</summary>
        {visibleResults.evaluation.reasons.map(reason => <p key={reason}>{reason}</p>)}
        <pre>{JSON.stringify(visibleResults.evaluation.graph, null, 2)}</pre><a href={`/api/runs/${runId}/trace`} target="_blank" rel="noreferrer">Inspect trace</a>
        <a href={`/api/projects/${projectId}/runs/${runId}/evidence`} target="_blank" rel="noreferrer">Inspect observation provenance</a></details>}
      {sourceCursor && <div className="run-source-player"><h4>Original source evidence · {seconds(sourceCursor.atMs)} s</h4>
        <video ref={sourcePlayer} key={sourceCursor.assetId} controls playsInline preload="metadata" src={`/api/assets/${sourceCursor.assetId}/stream?variant=original`}
          onLoadedMetadata={() => { if (sourcePlayer.current) { sourcePlayer.current.currentTime = sourceCursor.atMs / 1000; sourcePlayer.current.scrollIntoView({ behavior: 'smooth', block: 'center' }) } }} />
        <p className="field-help">The witness image below identifies the exact original frame. Playback seeks in the original source.</p></div>}
      {!!visibleResults?.items.length && <div className="results-toolbar"><button className="secondary-button" disabled={busy || visibleResults.items.length > 32} onClick={() => setSelected(visibleResults.items.map(event => event.id))}>Select visible candidates</button><span>{selected.length} selected</span></div>}
      <div className="cycle-grid">{visibleResults?.items.map(event => <article key={event.id} className={`cycle-card verdict-${event.payload.verdict}`}>
        <label><input type="checkbox" checked={selected.includes(event.id)} disabled={busy} onChange={change => setSelected(ids => change.target.checked ? [...ids, event.id] : ids.filter(id => id !== event.id))} />
          <strong>{event.payload.label}</strong><span className="verdict-badge">{verdictLabel(event.payload.verdict)}</span></label>
        <p className="candidate-time">{seconds(event.payload.interval.start_ms)}–{seconds(event.payload.interval.end_ms)} s{event.payload.coverage.estimated ? ' · estimated timing' : ''}{event.payload.coverage.selected === false ? ' · outside rule selection' : ''}{event.payload.origin === 'user_correction' ? ' · human corrected' : event.payload.origin === 'manual_fixture' ? ' · manual fixture' : ''}</p>
        {event.payload.review_reason && <p>{evidenceSummary(event.payload.review_reason)}</p>}
        <div className="step-verdicts" aria-label="Step verdicts">{event.payload.steps.map((step, index) => <span key={step.step_id} className={`step-verdict verdict-${step.verdict}`}>Step {index + 1}: {verdictLabel(step.verdict)}</span>)}</div>
        <details className="cycle-evidence"><summary>Inspect steps & source frames</summary>
        <ol>{event.payload.steps.map(step => <li key={step.step_id}><strong>{step.label}</strong> · {verdictLabel(step.verdict)}
          {step.interval && <span> · {seconds(step.interval.start_ms)}–{seconds(step.interval.end_ms)} s</span>}
          {step.verdict !== 'true' && <p>{evidenceSummary(step.reason)}</p>}<div className="witness-strip">{step.frames.map(frame => <button className="witness-button" key={`${frame.evidence_frame_id}:${frame.observed_at_source_ms}`} onClick={() => setSourceCursor({ assetId: frame.source_asset_id, atMs: frame.observed_at_source_ms ?? frame.source_time_ms })}>
            <img loading="lazy" src={`/api/evidence/${frame.evidence_frame_id}/image`} alt={`Original source frame ${frame.source_frame_index}, ${seconds(frame.observed_at_source_ms ?? frame.source_time_ms)} seconds`} /><span>{seconds(frame.observed_at_source_ms ?? frame.source_time_ms)} s · frame {frame.source_frame_index}</span></button>)}</div>
          <details className="technical-details"><summary>Evidence details</summary><p>{step.reason}</p>{step.observation_ids.map(id => <a key={id} href={`/api/projects/${projectId}/runs/${runId}/observations/${id}`} target="_blank" rel="noreferrer">Evidence {id.slice(0, 8)} </a>)}</details></li>)}</ol>
        <details className="technical-details"><summary>Full review reasons & timing checks</summary><p>{event.payload.review_reason}</p>
        {event.payload.coverage.edges?.map(edge => <p key={edge.relation_id}>{edge.from_step} → {edge.to_step}: <strong>{edge.verdict}</strong> {edge.reasons.join('; ')}</p>)}</details>
        </details>
        {advanced && <details><summary>Advanced evidence correction</summary><button className="secondary-button" disabled={busy} onClick={() => {
          setCorrection(event); setReason(''); setEdit(JSON.stringify(event.payload.steps.map(step => ({ step_id: step.step_id,
            verdict: step.verdict === 'true' ? 'true' : 'unknown', interval: step.interval,
            source_frame_indices: step.frames.map(f => f.source_frame_index), confirm_identity: false })), null, 2))
        }}>Correct evidence</button></details>}
      </article>)}</div>
      {visibleResults && <div className="program-controls"><button disabled={busy || !offset} onClick={() => { setOffset(Math.max(0, offset - 25)); setSelected([]) }}>Previous</button>
        <button disabled={busy || visibleResults.next_offset === null} onClick={() => { setOffset(visibleResults.next_offset ?? 0); setSelected([]) }}>Next</button></div>}
      {correction && <div className="evidence-editor"><h4>Human correction · new immutable event</h4>
        <p>Use original frame indices and source milliseconds. Positive steps require actual witnesses and explicit identity confirmation. Missing evidence remains unknown.</p>
        <label>Reason <input value={reason} maxLength={1000} onChange={event => setReason(event.target.value)} /></label>
        <label>Step evidence <textarea rows={12} value={edit} onChange={event => setEdit(event.target.value)} /></label>
        <button disabled={busy || !reason.trim()} onClick={() => void correct()}>Save correction</button><button onClick={() => setCorrection(null)}>Cancel edit</button>
      </div>}
      <div className="output-actions"><h3>Create outputs</h3><label><input type="checkbox" checked={reviewOnly} onChange={event => setReviewOnly(event.target.checked)} /> Include uncertain results as review-only outputs</label>
      {needsReview && !reviewOnly && <p className="field-help">Your selection contains uncertain or estimated evidence. Enable review-only outputs above to continue.</p>}
      <div className="program-controls">{(['extract_clip', 'create_report'] as const).map(type => <button className="primary-button" key={type} disabled={busy || !selected.length || selected.length > 32 || (needsReview && !reviewOnly) || loadedRunId !== runId} onClick={() => void dispatch(`/api/runs/${runId}/actions`, {
        type, event_ids: selected, review_only: reviewOnly, parameters: type === 'extract_clip' ? { framing: 'full_frame' } : {},
      })}>{type === 'extract_clip' ? 'Create clips' : 'Create report'}</button>)}</div>
      <p className="field-help">Full-frame clips preserve the source context. Review-only outputs retain their uncertainty labels.</p></div>
      {visibleOutputs.map(result => <article key={result.id}><h4>Output batch · {result.payload.status}</h4>
        {result.payload.outputs.map((output, i) => <div key={i}>{output.stream_url && <a href="#outputs">View clip below ↓</a>}
          {output.view_url && <a href={output.view_url} target="_blank" rel="noreferrer">Open report</a>}{output.json_url && <a href={output.json_url}>Download report JSON</a>}</div>)}
        {result.payload.failed_events.length > 0 && <p>Some outputs failed. Completed files remain available. <a href={`/api/jobs/${result.id}`}>Inspect failures</a></p>}</article>)}
    </>}
    {error && <p className="feedback error" role="alert">{error}</p>}
  </section>
}
