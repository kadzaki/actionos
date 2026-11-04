import { useState } from 'react'
import { api } from './api'

type Anchor = { id: string; label: string; role: string; time_ms: number }
export function AnchorEditor({ projectId, event, disabled, onSaved }: {
  projectId: string; event: { id: string; start_ms: number; end_ms: number; phases: Anchor[] }
  disabled: boolean; onSaved: () => void
}) {
  const [start, setStart] = useState(event.start_ms)
  const [end, setEnd] = useState(event.end_ms)
  const [anchors, setAnchors] = useState<Anchor[]>(event.phases.map(({ id, label, role, time_ms }) => ({ id, label, role, time_ms })))
  const [note, setNote] = useState('')
  const [error, setError] = useState('')
  const [saving, setSaving] = useState(false)
  const save = async () => {
    setSaving(true); setError('')
    try {
      await api(`/api/projects/${projectId}/events/${event.id}/corrections`, { method: 'POST', body: JSON.stringify({ start_ms: start, end_ms: end, phases: anchors, note }) })
      onSaved()
    } catch (err) { setError((err as Error).message) }
    finally { setSaving(false) }
  }
  return <details className="anchor-editor"><summary>Correct boundaries or action anchors</summary>
    <p>Inspect the source frames above, then enter source times in milliseconds. Saving creates a new evidence revision. Earlier exports remain available.</p>
    <fieldset disabled={disabled || saving}><legend>Action boundaries</legend>
      <label>Start (ms)<input type="number" min="0" step="1" value={start} onChange={e => setStart(Number(e.target.value))} /></label>
      <label>End (ms)<input type="number" min="1" step="1" value={end} onChange={e => setEnd(Number(e.target.value))} /></label>
      {anchors.map((anchor, index) => <div className="anchor-row" key={anchor.id}>
        <label>Anchor label<input value={anchor.label} onChange={e => setAnchors(anchors.map((p, i) => i === index ? { ...p, label: e.target.value } : p))} /></label>
        <label>Role<select value={anchor.role} onChange={e => setAnchors(anchors.map((p, i) => i === index ? { ...p, role: e.target.value } : p))}>{['onset', 'key_moment', 'completion', 'context', 'other'].map(role => <option key={role}>{role}</option>)}</select></label>
        <label>Source time (ms)<input type="number" min={start} max={end} value={anchor.time_ms} onChange={e => setAnchors(anchors.map((p, i) => i === index ? { ...p, time_ms: Number(e.target.value) } : p))} /></label>
        <button className="text-button" onClick={() => setAnchors(anchors.filter((_, i) => i !== index))}>Remove anchor</button>
      </div>)}
      <button className="text-button" disabled={anchors.length >= 12} onClick={() => setAnchors([...anchors, { id: `user_${crypto.randomUUID()}`, label: 'New anchor', role: 'other', time_ms: Math.round((start + end) / 2) }])}>Add missing anchor</button>
      <label>Correction note<input value={note} maxLength={1000} onChange={e => setNote(e.target.value)} /></label>
      <button className="secondary-button" onClick={() => void save()}>Save evidence revision</button>
    </fieldset>
    {error && <p className="feedback error" role="alert">{error}</p>}
  </details>
}
