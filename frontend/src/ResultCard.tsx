import { useEffect, useState } from 'react'
import type { RefObject } from 'react'
import { api } from './api'
import type { Export, Job } from './types'

type Plan = {
  id: string; parent_id: string | null; version: number; requested_duration_ms: number
  segments: { source_in_ms: number; source_out_ms: number; speed: number; crop: { mode: string; warnings: string[] } }[]
  reasoning: { operation?: string; explanation?: string; instruction?: string; changed_fields?: string[]; evidence_reanalyzed?: boolean }[]
}
export function ResultCard({ output, disabled, canRevise, onJob, sourceVideo }: {
  output: Export; disabled: boolean; canRevise: boolean; onJob: (job: Job) => void
  sourceVideo: RefObject<HTMLVideoElement | null>
}) {
  const [plan, setPlan] = useState<Plan | null>(null)
  const [instruction, setInstruction] = useState('')
  const [error, setError] = useState('')
  const [sending, setSending] = useState(false)
  useEffect(() => {
    let alive = true
    api<Plan>(`/api/plans/${output.plan_id}`).then(result => { if (alive) setPlan(result) }).catch(err => { if (alive) setError(err.message) })
    return () => { alive = false }
  }, [output.plan_id])
  const revise = async () => {
    setSending(true); setError('')
    try {
      const result = await api<{ job_id: string }>(`/api/plans/${output.plan_id}/revise`, { method: 'POST', body: JSON.stringify({ instruction }) })
      onJob(await api<Job>(`/api/jobs/${result.job_id}`))
      setInstruction('')
    } catch (err) { setError((err as Error).message) }
    finally { setSending(false) }
  }
  const seconds = (ms: number) => `${(ms / 1000).toFixed(2)}s`
  const diff = plan?.reasoning.find(reason => reason.operation === 'revision')
  return <article className="export-card" id={`plan-${output.plan_id}`}>
    {output.actionos_lineage?.fixture_only && <p className="event-warning">MANUAL DEVELOPER FIXTURE · no live detection or temporal evaluation</p>}
    {output.actionos_lineage?.review_only && <p className="event-warning">Review-only output · event verdict: {output.actionos_lineage.verdict}</p>}
    <div className="panel-heading"><h3>Version {output.plan_version} · {seconds(output.duration_ms)}</h3><span className="accent-label">{output.actionos_lineage && !output.actionos_lineage.fixture_only ? 'PROGRAM OUTPUT' : output.origin === 'manual' ? 'MANUAL EDL' : 'ACTION DIRECTED'}</span></div>
    <video controls playsInline preload="metadata" src={output.stream_url} />
    <div className="export-info"><p>{output.actionos_lineage && !output.actionos_lineage.fixture_only ? 'Rendered from selected run evidence.' : output.origin === 'manual' ? 'Developer-authored edit.' : 'Rendered from an evidence-linked action plan.'}</p><a href={output.download_url}>Download MP4 →</a></div>
    {plan && <details className="result-choices"><summary>Source evidence & editing choices</summary>
      <p>Requested {seconds(plan.requested_duration_ms)} · exported {seconds(output.duration_ms)}. Source moments are never repeated to fill the target.</p>
      {plan.segments.map((segment, index) => <div key={index} className="choice-row"><button className="text-button" onClick={() => { if (sourceVideo.current) { sourceVideo.current.currentTime = segment.source_in_ms / 1000; sourceVideo.current.scrollIntoView({ behavior: 'smooth', block: 'center' }) } }}>Source {seconds(segment.source_in_ms)}–{seconds(segment.source_out_ms)}</button><span>{segment.speed}× · {segment.crop.mode === 'track_subject' ? 'Following subject' : segment.crop.mode === 'full_frame' ? 'Full frame with letterboxing' : 'Fixed crop'}</span>{segment.crop.warnings.map((warning, i) => <p className="event-warning" key={i}>{warning}</p>)}</div>)}
      {plan.parent_id && <p>Revises <a href={`#plan-${plan.parent_id}`}>version {plan.version - 1}</a>. Earlier video and source evidence are preserved.</p>}
      {diff && <div className="revision-diff"><p>{diff.explanation}</p><p>Changed: {diff.changed_fields?.join(', ') || 'duration or plan selection'}. {diff.evidence_reanalyzed ? 'Source action was analyzed again.' : 'Existing action evidence was reused.'}</p></div>}
    </details>}
    {output.origin === 'autonomous' && <div className="revision-form"><label htmlFor={`revise-${output.id}`}>Revise this version</label><textarea id={`revise-${output.id}`} value={instruction} maxLength={8000} placeholder="Less slow motion, more context, or extract a different action…" onChange={e => setInstruction(e.target.value)} /><button className="secondary-button" disabled={disabled || sending || !canRevise || !instruction.trim()} onClick={() => void revise()}>Create revised version</button></div>}
    {error && <p className="feedback error" role="alert">{error}</p>}
  </article>
}
