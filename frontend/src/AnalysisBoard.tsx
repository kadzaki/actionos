import { useEffect, useState } from 'react'
import type { RefObject } from 'react'
import { api } from './api'
import type { Job } from './types'
import { AnchorEditor } from './AnchorEditor'
import { TargetPicker } from './TargetPicker'

type Phase = { id: string; label: string; role: string; time_ms: number; status: string }
type Event = { id: string; action_label: string; start_ms: number; end_ms: number; phases: Phase[]; primary_track_id: string | null; track_ids: string[]; evidence_frame_ids: string[]; match_reason: string; warnings: string[]; confidence: Record<string, string> }
export type SourceTrack = { id: string; label: string; samples: { time_ms: number; x: number; y: number; width: number; height: number; visible: boolean }[] }
type Board = { events: Event[]; total_count: number; offset: number; limit: number; next_offset: number | null; selected_event_ids: string[]; intent: { original_prompt: string; clarification: string | null; occurrence: { mode: string } } | null; scan_complete: boolean }
const terminal = new Set(['completed', 'failed', 'interrupted', 'cancelled'])
const labels: Record<string, string> = { queued: 'Queued for processing', sampling: 'Preparing source windows', understanding: 'Understanding the requested action', reconciling: 'Resolving occurrences across source windows', tracking: 'Tracking action subjects', inspecting: 'Checking source continuity and interaction geometry', localizing: 'Refining action phases', planning: 'Compiling the edit plan', reusing: 'Using the saved verified export', rendering: 'Rendering from source', verifying: 'Verifying output' }

export function AnalysisBoard({ projectId, job, video, durationMs, onJob, onTracks }: {
  projectId: string; job: Job | null; video: RefObject<HTMLVideoElement | null>; durationMs: number
  onJob: (job: Job) => void; onTracks: (tracks: SourceTrack[]) => void
}) {
  const [board, setBoard] = useState<Board | null>(null)
  const [tracks, setTracks] = useState<SourceTrack[]>([])
  const [selected, setSelected] = useState('')
  const [subject, setSubject] = useState('')
  const [fixed, setFixed] = useState(false)
  const [error, setError] = useState('')
  const [sending, setSending] = useState(false)
  const [revision, setRevision] = useState(0)
  const [offset, setOffset] = useState<number | null>(null)
  const active = !!job && !terminal.has(job.status)

  useEffect(() => {
    let cancelled = false
    if ((job?.type === 'analyze' || job?.type === 'revise') && active) { setBoard(null); onTracks([]); setOffset(null); setSelected(''); return }
    Promise.all([api<Board>(`/api/projects/${projectId}/events${offset === null ? '' : `?offset=${offset}`}`), api<{ tracks: SourceTrack[]; errors?: unknown[] }>(`/api/projects/${projectId}/tracks`)])
      .then(([result, found]) => {
        if (cancelled) return
        setBoard(result); setTracks(found.tracks)
        if (found.errors?.length) setError('Some saved tracks are damaged. They are hidden; choose the subject on source evidence to track it again.')
        setSelected(previous => result.events.some(event => event.id === previous) ? previous : result.events.find(event => result.selected_event_ids.includes(event.id))?.id ?? result.events[0]?.id ?? '')
      }).catch(err => { if (!cancelled) setError(err.message) })
    return () => { cancelled = true }
  }, [projectId, job?.id, job?.status, revision, offset])

  const current = board?.events.find(event => event.id === selected)
  useEffect(() => { onTracks(tracks.filter(track => current?.track_ids.includes(track.id))) }, [current, tracks, onTracks])
  useEffect(() => { setSubject(current?.primary_track_id ?? ''); setFixed(false) }, [current?.id, current?.primary_track_id])
  const generate = async (all = false) => {
    setSending(true); setError('')
    try {
      const result = await api<{ job_id: string }>(`/api/projects/${projectId}/reels`, { method: 'POST', body: JSON.stringify({
        event_id: all ? null : selected || null, track_id: all ? null : subject || null,
        requested_duration_ms: durationMs, allow_fixed_crop: fixed,
      }) })
      onJob(await api<Job>(`/api/jobs/${result.job_id}`))
    } catch (err) { setError((err as Error).message) }
    finally { setSending(false) }
  }
  if (!board?.intent && !active && !error) return null
  const timestamp = (ms: number) => `${(ms / 1000).toFixed(2)}s`
  return <section className="analysis-board" aria-label="Action evidence and directing">
    <div className="analysis-heading"><h2>Action evidence</h2><span>{board?.scan_complete ? 'FULL SOURCE SCAN' : active ? 'PROCESSING' : 'REVIEW'}</span></div>
    {active && <div className="analysis-progress" role="status"><span className="activity-dot" />{labels[job.step] ?? job.step}<span>{job.progress === null ? 'Working…' : `${job.progress}%`}</span></div>}
    {job && typeof job.result.requested_count === 'number' && typeof job.result.completed_count === 'number' && job.result.requested_count > 1 && <p className={job.status === 'failed' ? 'feedback error' : 'analysis-prompt'} role="status">{job.result.completed_count} of {job.result.requested_count} matching moments verified.{job.status === 'failed' && job.result.completed_count > 0 ? ' Processing stopped; the verified exports remain available below.' : ''}</p>}
    {board?.intent?.clarification && <p className="feedback">Clarification needed: {board.intent.clarification} Update your action request above and analyze again.</p>}
    {board?.scan_complete && board.total_count === 0 && <p className="no-match">No matching action could be verified in this source. Refine the request or inspect the original footage.</p>}
    {!!board?.events.length && <>
      <p className="analysis-prompt">{board.intent?.original_prompt}</p>
      <div className="candidate-strip">{board.events.map((event, index) => <button key={event.id} className={`candidate-card ${selected === event.id ? 'chosen' : ''}`} onClick={() => { setSelected(event.id); if (video.current) video.current.currentTime = event.start_ms / 1000 }}>
        {event.evidence_frame_ids[0] && <img src={`/api/evidence/${event.evidence_frame_ids[0]}/image`} alt={`Source evidence for match ${index + 1}`} />}
        <span className="candidate-number">MATCH {board.offset + index + 1}{board.selected_event_ids.includes(event.id) && board.intent?.occurrence.mode !== 'all' ? ' · PROMPT SELECTION' : ''}</span><strong>{event.action_label}</strong><span>{timestamp(event.start_ms)} — {timestamp(event.end_ms)}</span><small>Match relevance: {event.confidence.match_relevance ?? 'unassessed'} · estimated timing</small>
      </button>)}</div>
      {board.total_count > board.limit && <nav className="candidate-pagination" aria-label="Matching event pages"><button className="text-button" disabled={active || board.offset === 0} onClick={() => setOffset(Math.max(0, board.offset - board.limit))}>Previous matches</button><span>{board.offset + 1}–{Math.min(board.offset + board.limit, board.total_count)} of {board.total_count} matches</span><button className="text-button" disabled={active || board.next_offset === null} onClick={() => setOffset(board.next_offset)}>Next matches</button></nav>}
      {current && <div className="event-detail"><p>{current.match_reason}</p><div className="phase-ribbon">{current.phases.map(phase => <button key={phase.id} onClick={() => { if (video.current) video.current.currentTime = phase.time_ms / 1000 }}><strong>{phase.label}</strong><span>{timestamp(phase.time_ms)} · {phase.status}</span></button>)}</div>
        {current.warnings.map((warning, index) => <p className="event-warning" key={index}>{warning}</p>)}
        <AnchorEditor key={current.id} projectId={projectId} event={current} disabled={active} onSaved={() => setRevision(value => value + 1)} />
        {current.evidence_frame_ids[0] && <TargetPicker key={`target-${current.id}`} projectId={projectId} eventId={current.id} frameId={current.evidence_frame_ids[0]} disabled={active} onJob={onJob} />}
        {!!current.track_ids.length && <label className="subject-select">Crop subject<select value={subject} onChange={event => setSubject(event.target.value)}><option value="">Choose a tracked subject</option>{tracks.filter(track => current.track_ids.includes(track.id)).map(track => <option value={track.id} key={track.id}>{track.label} · {track.id.slice(0, 6)}</option>)}</select></label>}
        <label className="fixed-crop-choice"><input type="checkbox" checked={fixed} onChange={event => setFixed(event.target.checked)} /> Use fixed framing instead of following the subject</label>
        <div className="direct-buttons"><button className="secondary-button" disabled={active || sending} onClick={() => void generate()}>Auto-direct this moment</button>{board.intent?.occurrence.mode === 'all' && <button className="secondary-button" disabled={active || sending} onClick={() => void generate(true)}>Extract all matching moments</button>}</div>
      </div>}
    </>}
    {error && <p className="feedback error" role="alert">{error}</p>}
  </section>
}
