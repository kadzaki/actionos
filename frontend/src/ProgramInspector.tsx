import { useEffect, useRef, useState } from 'react'
import { api } from './api'
import type { Asset, Capabilities, Job, Project } from './types'
import type { ProgramInfo, ProgramOutcome } from './programTypes'

const labels = {
  ready_for_review: 'Ready for your review', clarification_required: 'Clarification needed',
  unsupported_operation: 'Unsupported operation', invalid_program: 'Program needs correction',
}
const stages: Record<string, string> = { queued: 'Waiting for the worker', compiling: 'Compiling with Meta Muse', validating_program: 'Validating the proposed program' }
const DEFAULT_VISUAL_RULE = 'Find each moment where the red number 15 car overtakes the yellow car. Require visible evidence that the same red car changes from behind the yellow car to ahead while both travel in the same direction around the track. Being alongside is not a completed pass. Do not include passes involving only other cars. Mark unclear identities, occlusion, or passes obscured by distance or camera movement as needs-review; do not assume a pass happened outside the visible evidence. Use a maximum event duration of 12 seconds and zero overlap tolerance. Select all matching overtakes and create full-frame clips with 2 seconds before and after, plus a timestamped report.'

export function ProgramInspector({ project, asset, capabilities, job, locked, onJob, onProgramChange }: {
  project: Project | null; asset: Asset | null; capabilities: Capabilities | null
  job: Job | null; locked: boolean; onJob: (job: Job) => void
  onProgramChange: (program: ProgramInfo | null) => void
}) {
  const [instruction, setInstruction] = useState(DEFAULT_VISUAL_RULE)
  const [info, setInfo] = useState<ProgramInfo | null>(null)
  const [version, setVersion] = useState<number | null>(null)
  const [sending, setSending] = useState(false)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [refresh, setRefresh] = useState(0)
  const [acceptedAssumptions, setAcceptedAssumptions] = useState<string[]>([])
  const [revisionText, setRevisionText] = useState('')
  const dirty = useRef(false)
  const compileJob = job?.type === 'compile' && job.project_id === project?.id ? job : null
  const resultId = compileJob?.status === 'completed' && typeof compileJob.result.program_id === 'string' ? compileJob.result.program_id : null
  const programId = resultId ?? project?.active_program_id
  const currentOutcome = compileJob?.status === 'completed' && compileJob.result.outcome ? compileJob.result.outcome as ProgramOutcome : null
  const running = !!compileJob && (compileJob.status === 'queued' || compileJob.status === 'running')
  const changed = !!info && instruction !== info.program.ast.instruction
  const canCompile = !!project && asset?.status === 'ready' && !!capabilities?.operations.compile && !!instruction.trim() && !locked && !sending
  const pendingInterpretation = !!currentOutcome && !compileJob?.result.program_id
  const canApprove = !!info && info.outcome.approval_allowed && !info.review_approved && !changed && !pendingInterpretation && !locked && !sending && !loading

  useEffect(() => { onProgramChange(info) }, [info, onProgramChange])

  useEffect(() => {
    if (job?.type === 'revise' && job.status === 'completed' && job.result.program_id === programId && typeof job.result.version === 'number') {
      dirty.current = false; setVersion(job.result.version); setRefresh(n => n + 1); setRevisionText('')
    }
  }, [job?.id, job?.status, programId])

  const revise = async () => {
    if (!info) return
    setSending(true); setError('')
    try {
      const accepted = await api<{ job_id: string }>(`/api/programs/${info.program.program_id}/revise`, { method: 'POST', body: JSON.stringify({
        version: info.program.version, program_fingerprint: info.program_fingerprint, instruction: revisionText,
      }) })
      onJob(await api<Job>(`/api/jobs/${accepted.job_id}`))
    } catch (err) { setError((err as Error).message) }
    finally { setSending(false) }
  }

  useEffect(() => {
    if (!programId || !project || !asset) return
    const controller = new AbortController()
    setLoading(true); setError('')
    api<ProgramInfo>(`/api/programs/${programId}${version ? `?version=${version}` : ''}`, { signal: controller.signal })
      .then(next => {
        if (controller.signal.aborted) return
        if (next.program.project_id !== project.id || next.program.source_asset_id !== asset.id) throw new Error('This program belongs to a different source. Select its original project to inspect it.')
        setInfo(next)
        setAcceptedAssumptions([])
        if (!dirty.current) setInstruction(next.program.ast.instruction)
      }).catch(err => { if (!controller.signal.aborted) setError((err as Error).message) })
      .finally(() => { if (!controller.signal.aborted) setLoading(false) })
    return () => controller.abort()
  }, [programId, project?.id, asset?.id, version, refresh])

  const compile = async () => {
    if (!canCompile || !project || !asset) return
    setSending(true); setError('')
    try {
      const accepted = await api<{ job_id: string }>('/api/programs/compile', { method: 'POST',
        body: JSON.stringify({ project_id: project.id, source_asset_id: asset.id, instruction }) })
      dirty.current = false; setVersion(null)
      onJob(await api<Job>(`/api/jobs/${accepted.job_id}`))
    } catch (err) { setError((err as Error).message) }
    finally { setSending(false) }
  }
  const approve = async () => {
    if (!canApprove || !info) return
    setSending(true); setError('')
    try {
      await api(`/api/programs/${info.program.program_id}/review`, { method: 'POST', body: JSON.stringify({
        version: info.program.version, program_fingerprint: info.program_fingerprint, source_sha256: info.program.source_sha256,
      }) })
      setRefresh(n => n + 1)
    } catch (err) { setError((err as Error).message); setRefresh(n => n + 1) }
    finally { setSending(false) }
  }
  const run = async () => {
    if (!info?.review_approved || changed || pendingInterpretation || locked || sending || !capabilities?.studio.run.available) return
    setSending(true); setError('')
    try {
      const accepted = await api<{ job_id: string }>(`/api/programs/${info.program.program_id}/run`, { method: 'POST', body: JSON.stringify({
        version: info.program.version, program_fingerprint: info.program_fingerprint,
      }) })
      onJob(await api<Job>(`/api/jobs/${accepted.job_id}`))
    } catch (err) { setError((err as Error).message) }
    finally { setSending(false) }
  }
  const confirm = async () => {
    if (!info || !acceptedAssumptions.length || changed || locked || sending) return
    setSending(true); setError('')
    try {
      const saved = await api<{ version: number }>(`/api/programs/${info.program.program_id}/confirm-assumptions`, {
        method: 'POST', body: JSON.stringify({ version: info.program.version, program_fingerprint: info.program_fingerprint,
          source_sha256: info.program.source_sha256, accepted_codes: acceptedAssumptions }),
      })
      setVersion(saved.version); setRefresh(n => n + 1)
    } catch (err) { setError((err as Error).message) }
    finally { setSending(false) }
  }
  const ast = info?.program.ast
  const shownOutcome = currentOutcome && (!compileJob?.result.program_id || running) ? currentOutcome : info?.outcome
  return <section id="program" className="direction-panel program-inspector" aria-labelledby="program-title">
    <div className="panel-heading"><h2 id="program-title"><span className="number">02</span> Visual program</h2><span className="accent-label">DSL {ast?.schema_version ?? '1.0'}</span></div>
    <div className="program-body">
      <label className="field-label" htmlFor="visual-instruction">Describe your visual rule</label>
      <button className="text-button" disabled={locked || sending || asset?.status !== 'ready'} onClick={() => { dirty.current = true; setInstruction(DEFAULT_VISUAL_RULE) }}>Use racing example</button>
      <textarea id="visual-instruction" value={instruction} maxLength={8000} disabled={locked || sending || asset?.status !== 'ready'}
        onChange={event => { dirty.current = true; setInstruction(event.target.value) }}
        placeholder="Find when the red number 15 car overtakes the yellow car." />
      <p className="field-help">Compile the rule, inspect its interpretation, then approve the exact version. Approval does not start evaluation or outputs.</p>
      <button className="primary-button" id="compile-program" disabled={!canCompile} onClick={() => void compile()}>Compile with Meta Muse</button>
      {!project || asset?.status !== 'ready' ? <p className="field-help">Upload and prepare a source before compiling.</p> : !capabilities?.operations.compile && <p className="field-help">Configure the server-side Meta key to compile. Saved programs remain inspectable.</p>}
      {running && <p className="program-status" role="status">{stages[compileJob?.step ?? 'queued'] ?? 'Processing the program'} · elapsed work is tracked by the server.</p>}
      {loading && <p role="status">Loading saved program…</p>}
      {compileJob?.error && <div className="feedback error" role="alert"><strong>Compilation did not complete</strong><p>{compileJob.error.message}</p><p>Previously saved programs and outputs remain available.</p></div>}
      {error && <p className="feedback error" role="alert">{error}</p>}
      {job?.type === 'revise' && <p role="status">{job.status === 'completed' ? (job.result.no_op ? 'The revision made no changes. No new program or run was created.' : 'Revision saved. Inspect the diff and approve the new version.') : job.error ? job.error.message : `Revision: ${job.step.replaceAll('_', ' ')}`}</p>}
      {shownOutcome && <div className={`program-outcome ${shownOutcome.state}`} role="status">
        <strong>{labels[shownOutcome.state]}</strong><p>{shownOutcome.warning}</p>
        {shownOutcome.interpretation_issues.map((issue, index) => <p key={`${issue.code}-${index}`}><strong>{issue.code}</strong>: {issue.message}</p>)}
        {shownOutcome.validation_issues.map((issue, index) => <p key={`${issue.code}-${index}`}><strong>{issue.node_id ?? issue.path.join('.') ?? 'Program'}</strong>: {issue.message} ({issue.code})</p>)}
      </div>}
      {info && ast && <>
        <div className="program-version"><strong>Program version {info.program.version}</strong>
          {info.versions.length > 1 && <label>Inspect version <select aria-label="Program version" value={info.program.version} disabled={locked || sending} onChange={e => { dirty.current = false; setVersion(Number(e.target.value)) }}>{info.versions.map(v => <option key={v} value={v}>{v}</option>)}</select></label>}
          <button className="text-button" onClick={() => setRefresh(n => n + 1)} disabled={loading || sending}>Reload</button>
        </div>
        {(info.program.compiler.origin === 'test_fixture' || info.compilation_origin === 'test_fixture') && <p className="event-warning">OFFLINE TEST FIXTURE · not a live compiler result</p>}
        <details className="program-details"><summary>Compiled instruction</summary><p className="compiled-wording">{ast.instruction}</p></details>
        {changed && <p className="event-warning" id="draft-changed">The text above differs from this saved version. Compile the edited rule before approving or running it.</p>}
        <ol className="step-ribbon" aria-label="Compiled sequence">{ast.steps.map((step, index) => <li key={step.id}><span>Step {index + 1}</span><strong>{step.phrase || step.source_wording || step.id}</strong></li>)}</ol>
        <p className="field-help">Missing evidence stays needs-review. Review this interpretation, then approve and run the version.</p>
        <div className="program-controls">
          <button id="approve-program" className="secondary-button" disabled={!canApprove} onClick={() => void approve()}>{info.review_approved ? `Version ${info.program.version} approved` : `Approve version ${info.program.version}`}</button>
          <button id="run-program" className="primary-button" aria-describedby="program-run-reason" disabled={!info.review_approved || changed || pendingInterpretation || locked || sending || !capabilities?.studio.run.available} onClick={() => void run()}>Run this version</button>
        </div>
        <p id="program-run-reason" className="field-help">{capabilities?.studio.run.reason ?? 'Checking evaluation availability'}</p>
        <details className="program-details"><summary>Inspect entities, timing and output rules</summary>
        <h3>Entities</h3><ul className="program-entities">{ast.entities.map(entity => <li key={entity.id}><strong>{entity.id}</strong> <span>{entity.role}</span><p>{entity.description}{entity.attributes.length ? ` · ${entity.attributes.join(', ')}` : ''}</p></li>)}</ul>
        {ast.zones.length > 0 && <><h3>Image zones</h3>{ast.zones.map(zone => <p key={zone.id}><strong>{zone.id}</strong>: {zone.description} · x={zone.x}, y={zone.y}, width={zone.width}, height={zone.height} · oriented source image.</p>)}</>}
        <h3>Predicates</h3><ol className="predicate-list">{ast.steps.map(step => <li key={step.id} id={`predicate-${step.id}`}><strong>{step.id}: {step.predicate}</strong><p>{step.phrase ?? step.source_wording ?? ''}</p><p>Entities: {step.entities.join(', ')}{step.zone_id ? ` · zone: ${step.zone_id}` : ''} · anchor: {step.anchor}</p>{Object.keys(step.parameters).length > 0 && <pre>{JSON.stringify(step.parameters, null, 2)}</pre>}</li>)}</ol>
        <h3>Temporal conditions</h3>{ast.relations.length ? <ul>{ast.relations.map(relation => <li key={relation.id}><strong>{relation.from}</strong> {relation.op} <strong>{relation.to}</strong>{relation.within_ms !== null ? ` · ${relation.within_ms} ms from the first step's end to the next start` : ''}</li>)}</ul> : <p>No additional temporal relation.</p>}
        <p>Cycle starts at <strong>{ast.cycle.root_step_id}</strong>. Maximum cycle {ast.cycle.policy.max_cycle_ms} ms; maximum step gap {ast.cycle.policy.max_gap_ms} ms; overlap tolerance {ast.cycle.policy.overlap_tolerance_ms} ms.</p>
        <p>Occurrences: <strong>{ast.selection.occurrences}{ast.selection.ordinal ? ` ${ast.selection.ordinal}` : ''}</strong>.</p>
        {ast.exclusions.length > 0 && <><h3>Exclusions</h3>{ast.exclusions.map(exclusion => <details key={exclusion.id}><summary>{exclusion.source_wording}</summary><pre>{JSON.stringify(exclusion.expression, null, 2)}</pre></details>)}</>}
        <h3>When evidence is insufficient</h3><p>{ast.unknown_policy.observation} → {ast.unknown_policy.presentation}. Negative proof requires {ast.unknown_policy.negative_coverage}. Not observed does not mean the operation did not happen.</p>
        <h3>Declared outputs</h3>{ast.actions.length ? <ul>{ast.actions.map(action => <li key={action.id}><strong>{action.type}</strong> · {action.when}<details><summary>Output parameters</summary><pre>{JSON.stringify(action.parameters, null, 2)}</pre></details></li>)}</ul> : <p>No output action declared. Source event inspection remains the rule's result.</p>}
        </details>
        {info.outcome.assumptions.length > 0 && <details className="program-details" open={info.outcome.assumptions.some(note => note.material)}><summary>Assumptions to review</summary><ul className="assumption-list">{info.outcome.assumptions.map((note, index) => <li key={index}>{note.material ? <label><input type="checkbox" checked={acceptedAssumptions.includes(note.code)} disabled={changed || locked || sending} onChange={event => setAcceptedAssumptions(prior => event.target.checked ? [...prior, note.code] : prior.filter(code => code !== note.code))} /> I confirm this interpretation: {note.message}</label> : note.message}</li>)}</ul>
          {info.outcome.assumptions.some(note => note.material) && <button id="confirm-assumptions" className="secondary-button" disabled={!acceptedAssumptions.length || changed || locked || sending} onClick={() => void confirm()}>Confirm selected assumptions as a new version</button>}
        </details>}
        <details className="program-details"><summary>Clause accounting and machine AST</summary>{ast.clauses.map((clause, index) => <div key={index}><p>{clause.text}</p><p>{clause.status}: {clause.represented_by.map(ref => `${ref.kind}:${ref.id}`).join(', ')}</p>{clause.note && <p>{clause.note}</p>}</div>)}<pre>{JSON.stringify(ast, null, 2)}</pre></details>
        {info.program.explanation && <details className="program-details"><summary>Compiler explanation · interpretation, not evidence</summary><p>{info.program.explanation}</p></details>}
        <details className="program-details"><summary>Version binding and provenance</summary><p>Source hash: <code>{info.program.source_sha256}</code></p><p>Program fingerprint: <code id="program-fingerprint">{info.program_fingerprint}</code></p><p>{info.program.compiler.origin} · {info.program.compiler.returned_model ?? 'No model call'} · {info.program.compiler.compiler_version}</p><a href={`/api/compilations/${info.compilation_id}`}>Inspect sanitized compilation record</a></details>
        <details className="program-details"><summary>Revise this rule</summary>
          <label>Describe the change<textarea maxLength={4000} value={revisionText} disabled={locked || sending} onChange={event => setRevisionText(event.target.value)} placeholder="Only include overtakes completed within three seconds." /></label>
          <button className="secondary-button" disabled={!revisionText.trim() || locked || sending || changed || !capabilities?.operations.revise} onClick={() => void revise()}>Propose a version with Meta Muse</button>
          <p>Review the resulting changes and approve the new version before running it. Historical evidence and outputs remain available.</p>
        </details>
        {info.human_edit?.revision && <details className="program-details"><summary>Changes from version {info.human_edit.base_version}</summary>
          <p>{info.human_edit.revision.instruction}</p><p>Required work: {info.human_edit.revision.diff.invalidates}.</p>
          {info.human_edit.revision.diff.changes.map(change => <div key={change.path}><strong>{change.path}</strong><pre>{JSON.stringify(change.before, null, 2)}</pre><p>Changed to</p><pre>{JSON.stringify(change.after, null, 2)}</pre></div>)}
        </details>}
      </>}
    </div>
  </section>
}
