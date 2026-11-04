import { useEffect, useState } from 'react'
import { api } from './api'
import type { BatchInfo, BatchSummary, BindingInfo, EvidenceList, FrameInfo } from './entityTypes'

export function EntityTrackInspector({ projectId, programId, version, programFingerprint, sourceId, locked, refreshToken }: {
  projectId: string; programId: string; version: number; programFingerprint: string; sourceId: string; locked: boolean
  refreshToken?: string
}) {
  const [list, setList] = useState<BatchSummary[]>([])
  const [selected, setSelected] = useState('')
  const [offset, setOffset] = useState(0)
  const [next, setNext] = useState<number | null>(null)
  const [loading, setLoading] = useState(true)
  const [errors, setErrors] = useState<string[]>([])
  useEffect(() => { setOffset(0); setSelected('') }, [refreshToken, programFingerprint])
  useEffect(() => {
    const controller = new AbortController()
    setLoading(true)
    api<EvidenceList<BatchSummary> & { program_fingerprint: string }>(`/api/projects/${projectId}/entity-tracks?program_id=${programId}&version=${version}&offset=${offset}`, { signal: controller.signal })
      .then(result => {
        if (result.program_fingerprint !== programFingerprint) throw new Error('Tracking evidence belongs to a different program version. Reload the program.')
        setList(previous => offset ? [...previous, ...result.items] : result.items)
        setSelected(previous => previous || result.items[0]?.batch_id || '')
        setErrors(previous => [...(offset ? previous : []), ...result.errors.map(e => `${e.code}: ${e.message}`)])
        setNext(result.next_offset)
      }).catch(error => { if (!controller.signal.aborted) setErrors(previous => [...previous, (error as Error).message]) })
      .finally(() => { if (!controller.signal.aborted) setLoading(false) })
    return () => controller.abort()
  }, [projectId, programId, version, programFingerprint, offset, refreshToken])
  const item = list.find(b => b.batch_id === selected)
  return <details className="program-details entity-inspector" id="entity-tracking">
    <summary>Entity tracking evidence and selection</summary>
    <p>Inspect recorded candidates on original source frames. Identity stays unknown until you select a candidate segment. Selection does not start evaluation or create outputs.</p>
    {loading && <p role="status">Loading tracking evidence…</p>}
    {errors.map((message, i) => <p className="feedback error" role="alert" key={i}>{message}</p>)}
    {!loading && !list.length && !errors.length && <p>No tracking evidence is recorded for this program version.</p>}
    {!!list.length && <label>Recorded entity window<select aria-label="Recorded entity window" value={selected} onChange={e => setSelected(e.target.value)}>
      {list.map((b, i) => <option value={b.batch_id} key={b.batch_id}>{i + 1}. {b.entity_id} · {b.status} · {b.candidate_count} candidates</option>)}
    </select></label>}
    {next !== null && <button className="text-button" disabled={loading} onClick={() => setOffset(next)}>Load more tracking windows</button>}
    {item && <BatchView key={item.batch_id} item={item} projectId={projectId} programFingerprint={programFingerprint} sourceId={sourceId} locked={locked} />}
  </details>
}

function BatchView({ item, projectId, programFingerprint, sourceId, locked }: {
  item: BatchSummary; projectId: string; programFingerprint: string; sourceId: string; locked: boolean
}) {
  const base = `/api/projects/${projectId}/runs/${item.run_id}/entity-tracks/${item.batch_id}`
  const [info, setInfo] = useState<BatchInfo | null>(null)
  const [frame, setFrame] = useState<FrameInfo | null>(null)
  const [index, setIndex] = useState(0)
  const [candidateId, setCandidateId] = useState('')
  const [error, setError] = useState('')
  const [saving, setSaving] = useState(false)
  const [bindings, setBindings] = useState<BindingInfo[]>([])
  const [bindingOffset, setBindingOffset] = useState<number | null>(0)
  useEffect(() => {
    const controller = new AbortController()
    api<BatchInfo>(base, { signal: controller.signal }).then(result => {
      if (result.batch.program_fingerprint !== programFingerprint || result.batch.source_asset_id !== sourceId) throw new Error('These candidates belong to another source or program version.')
      setInfo(result); setCandidateId(result.batch.candidates[0]?.id ?? '')
    }).catch(err => { if (!controller.signal.aborted) setError((err as Error).message) })
    return () => controller.abort()
  }, [base, programFingerprint, sourceId])
  useEffect(() => {
    if (!info) return
    const controller = new AbortController()
    setFrame(null)
    const timer = window.setTimeout(() => {
      api<FrameInfo>(`${base}/frame?input_frame_index=${index}`, { signal: controller.signal })
        .then(result => { if (!controller.signal.aborted) setFrame(result) })
        .catch(err => { if (!controller.signal.aborted) setError((err as Error).message) })
    }, 180)
    return () => { window.clearTimeout(timer); controller.abort() }
  }, [base, info, index])
  useEffect(() => {
    const controller = new AbortController()
    api<EvidenceList<BindingInfo>>(`${base}/bindings`, { signal: controller.signal }).then(result => {
      setBindings(result.items); setBindingOffset(result.next_offset)
      if (result.errors.length) setError(result.errors.map(e => `${e.code}: ${e.message}`).join('; '))
    }).catch(err => { if (!controller.signal.aborted) setError((err as Error).message) })
    return () => controller.abort()
  }, [base])
  const candidate = info?.batch.candidates.find(c => c.id === candidateId)
  const sample = candidate?.samples.find(s => s.input_frame_index === index)
  const segment = candidate?.segments.find(s => s.input_frames.start_frame <= index && index < s.input_frames.stop_frame)
  const canSelect = !!info && !!frame && frame.input_frame_index === index && !!sample && !!segment && !locked && !saving
  const save = async () => {
    if (!canSelect || !info || !frame || !segment) return
    setSaving(true); setError('')
    try {
      const saved = await api<BindingInfo>(`${base}/bindings`, { method: 'POST', body: JSON.stringify({
        batch_fingerprint: info.batch_fingerprint, candidate_id: candidateId, segment_id: segment.id,
        input_frame_index: index, evidence_frame_id: frame.frame.evidence_frame_id,
      }) })
      setBindings(previous => [saved, ...previous.filter(b => b.binding_id !== saved.binding_id)])
    } catch (err) { setError((err as Error).message) }
    finally { setSaving(false) }
  }
  const moreBindings = async () => {
    if (bindingOffset === null) return
    try {
      const result = await api<EvidenceList<BindingInfo>>(`${base}/bindings?offset=${bindingOffset}`)
      setBindings(previous => [...previous, ...result.items]); setBindingOffset(result.next_offset)
      if (result.errors.length) setError(result.errors.map(e => e.message).join('; '))
    } catch (err) { setError((err as Error).message) }
  }
  return <div className="entity-batch">
    <p className="event-warning">Evidence scope: {info?.evidence_scope ?? item.evidence_scope}</p>
    {error && <p className="feedback error" role="alert">{error}</p>}
    {info && <>
      <p><strong>{info.batch.entity_id}</strong> ({info.batch.role}) · {info.batch.status}</p>
      {info.batch.uncertainty.map((reason, i) => <p key={i}>{reason}</p>)}
      {info.batch.error_code && <p role="status">Tracking unavailable: {info.batch.error_code}. Presence and identity remain unknown.</p>}
      {!!info.batch.candidates.length && <>
        <label>Candidate<select aria-label="Entity candidate" value={candidateId} disabled={saving} onChange={e => { setCandidateId(e.target.value); const c = info.batch.candidates.find(c => c.id === e.target.value); if (c) setIndex(c.samples[0].input_frame_index) }}>
          {info.batch.candidates.map((c, i) => <option key={c.id} value={c.id}>{i + 1}. Candidate {c.id.slice(0, 8)} · identity unknown</option>)}
        </select></label>
        <label>Input frame <input aria-label="Tracking input frame" type="number" min={0} max={info.batch.input_frame_count - 1} value={index} disabled={saving} onChange={e => setIndex(Math.max(0, Math.min(info.batch.input_frame_count - 1, Number(e.target.value))))} /></label>
        {frame && frame.input_frame_index === index ? <>
          <div className="entity-frame">
            <img src={frame.image_url} alt={`Original source frame ${frame.frame.source_frame_index}; candidate boxes are sampled hypotheses`} />
            <svg viewBox="0 0 1 1" preserveAspectRatio="none" aria-label="Sampled candidate boxes">
              {info.batch.candidates.flatMap(c => c.samples.filter(s => s.input_frame_index === index).map(s => <rect key={c.id} x={s.x} y={s.y} width={s.width} height={s.height} fill="none" stroke={c.id === candidateId ? '#95ff99' : '#ffba66'} strokeWidth={c.id === candidateId ? .006 : .003}><title>Candidate {c.id.slice(0, 8)} · identity unknown</title></rect>))}
            </svg>
          </div>
          <p>Original frame {frame.frame.source_frame_index} · {frame.frame.source_time_ms.toFixed(3)} ms · PTS {frame.frame.source_pts} ({frame.frame.source_time_base}). <a href={frame.source_url}>Open original source</a></p>
        </> : <p role="status">Loading original frame…</p>}
        {candidate && <>
          <p><strong>Identity unknown.</strong> {candidate.lost_at_window_end ? 'No samples reach the end of this window.' : 'Samples reach the end of this window.'}</p>
          <p>Observed segments (input frames, end excluded):</p>
          <div className="entity-segments">{candidate.segments.map(s => <button className="text-button" key={s.id} disabled={saving} onClick={() => setIndex(s.input_frames.start_frame)}>{s.input_frames.start_frame}–{s.input_frames.stop_frame}</button>)}</div>
          <p>Missing sample ranges: {candidate.missing_input_ranges.map(r => `${r.start_frame}–${r.stop_frame}`).join(', ') || 'None in this input'}. Gaps do not prove physical absence.</p>
          {!sample && <p className="event-warning">No SAM sample for this candidate on this input frame. Select an observed segment.</p>}
          <button className="secondary-button" id="bind-entity-segment" disabled={!canSelect} onClick={() => void save()}>Select this candidate segment for {info.batch.entity_id}</button>
          <p>Selection applies to this sampled segment only. Identity across gaps and other windows remains unverified.</p>
        </>}
      </>}
      <div aria-live="polite"><h4>Saved segment selections</h4>
        {!bindings.length && <p>No saved selection for this tracking window.</p>}
        {bindings.map(b => <p key={b.binding_id}>Saved candidate {b.binding.candidate_id.slice(0, 8)}, segment {b.binding.segment_id.slice(0, 8)}, input frame {b.binding.input_frame_index}. Scope: this segment only.</p>)}
        {bindingOffset !== null && <button className="text-button" onClick={() => void moreBindings()}>Load more selections</button>}
      </div>
    </>}
  </div>
}
