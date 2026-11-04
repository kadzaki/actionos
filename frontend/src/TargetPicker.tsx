import { useState } from 'react'
import type { PointerEvent } from 'react'
import { api } from './api'
import type { Job } from './types'

type Selection = { x: number; y: number; width?: number; height?: number }
export function TargetPicker({ projectId, eventId, frameId, disabled, onJob }: {
  projectId: string; eventId: string; frameId: string; disabled: boolean; onJob: (job: Job) => void
}) {
  const [mode, setMode] = useState<'point' | 'box'>('point')
  const [selection, setSelection] = useState<Selection | null>(null)
  const [anchor, setAnchor] = useState<Selection | null>(null)
  const [error, setError] = useState('')
  const [sending, setSending] = useState(false)
  const position = (event: PointerEvent<HTMLDivElement>) => {
    const rect = event.currentTarget.getBoundingClientRect()
    return { x: Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width)), y: Math.max(0, Math.min(1, (event.clientY - rect.top) / rect.height)) }
  }
  const down = (event: PointerEvent<HTMLDivElement>) => {
    if (disabled || sending) return
    event.preventDefault(); event.currentTarget.setPointerCapture(event.pointerId)
    const point = position(event)
    setAnchor(point); setSelection(point)
  }
  const move = (event: PointerEvent<HTMLDivElement>) => {
    if (!anchor || mode !== 'box') return
    const point = position(event)
    setSelection({ x: Math.min(point.x, anchor.x), y: Math.min(point.y, anchor.y), width: Math.abs(point.x - anchor.x), height: Math.abs(point.y - anchor.y) })
  }
  const send = async () => {
    if (!selection) return
    setSending(true); setError('')
    try {
      const result = await api<{ job_id: string }>(`/api/projects/${projectId}/events/${eventId}/track`, { method: 'POST', body: JSON.stringify({ evidence_frame_id: frameId, mode, ...selection }) })
      onJob(await api<Job>(`/api/jobs/${result.job_id}`))
    } catch (err) { setError((err as Error).message) }
    finally { setSending(false) }
  }
  return <details className="target-picker"><summary>Choose a subject on source evidence</summary>
    <p>Click a point inside the subject, or drag a box around it. SAM will track this selection through the action.</p>
    <label>Selection type<select disabled={disabled || sending} value={mode} onChange={e => { setMode(e.target.value as 'point' | 'box'); setSelection(null); setAnchor(null) }}><option value="point">Point</option><option value="box">Box</option></select></label>
    <div className="target-image" onPointerDown={down} onPointerMove={move} onPointerUp={event => { move(event); setAnchor(null) }} onPointerCancel={() => setAnchor(null)}>
      <img src={`/api/evidence/${frameId}/image`} alt="Original source evidence for choosing the tracked subject" draggable={false} />
      {selection && <span className={mode === 'box' ? 'target-box' : 'target-point'} style={{ left: `${selection.x * 100}%`, top: `${selection.y * 100}%`, ...(mode === 'box' ? { width: `${(selection.width ?? 0) * 100}%`, height: `${(selection.height ?? 0) * 100}%` } : {}) }} />}
    </div>
    <fieldset disabled={disabled || sending}><legend>Selection coordinates (percent of evidence image)</legend>
      {(['x', 'y', ...(mode === 'box' ? ['width', 'height'] : [])] as (keyof Selection)[]).map(field => <label key={field}>{field}<input type="number" min="0" max="100" step="0.1" value={((selection?.[field] ?? 0) * 100).toFixed(1)} onChange={e => setSelection({ x: selection?.x ?? 0, y: selection?.y ?? 0, ...(mode === 'box' ? { width: selection?.width ?? .1, height: selection?.height ?? .1 } : {}), [field]: Number(e.target.value) / 100 })} /></label>)}
      <button className="secondary-button" disabled={!selection || (mode === 'box' && (!selection.width || !selection.height))} onClick={() => void send()}>Track selected subject</button>
    </fieldset>
    {error && <p className="feedback error" role="alert">{error}</p>}
  </details>
}
