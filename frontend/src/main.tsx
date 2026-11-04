import { ResultCard } from './ResultCard'
import { ProgramInspector } from './ProgramInspector'
import { RunInspector } from './RunInspector'
import { EntityTrackInspector } from './EntityTrackInspector'
import { TaskProgress } from './TaskProgress'
import type { ProgramInfo } from './programTypes'
import React, { useEffect, useRef, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { api, uploadFile } from './api'
import type { Asset, Capabilities, Export, Job, Project } from './types'
import './styles.css'
import { SourceEvidence } from './SourceEvidence'
import { AnalysisBoard } from './AnalysisBoard'
import type { SourceTrack } from './AnalysisBoard'
import { TrackOverlay } from './TrackOverlay'

const terminal = new Set(['completed', 'failed', 'interrupted', 'cancelled'])
const stages: Record<string, string> = {
  queued: 'Waiting for an available worker', probing: 'Checking your footage',
  mapping_timestamps: 'Mapping source frames', normalizing: 'Preparing working video',
  creating_preview: 'Creating a lighter preview', verifying: 'Verifying your media',
  completed: 'Source is ready', failed: 'Preparation failed', interrupted: 'Preparation was interrupted',
}
const examples = ['The red car overtaking the blue car', 'Every time the dog catches the ball', 'The second dance spin', 'When the chef flips the pancake']

function Icon({ kind, size = 20 }: { kind: 'cut' | 'upload' | 'film' | 'arrow' | 'check' | 'plus' | 'trash'; size?: number }) {
  const paths = {
    cut: <><path d="M8 3v18M16 3v18M3 8h18M3 16h18" /><path d="m9 8 6 4-6 4z" fill="currentColor" stroke="none" /></>,
    upload: <><path d="M12 16V3m-5 5 5-5 5 5M4 15v5h16v-5" /></>,
    film: <><rect x="3" y="4" width="18" height="16" rx="2" /><path d="M7 4v16M17 4v16M3 9h4m-4 6h4m10-6h4m-4 6h4" /></>,
    arrow: <path d="M4 12h16m-6-6 6 6-6 6" />,
    check: <path d="m5 12 4 4L19 6" />,
    plus: <path d="M12 5v14M5 12h14" />,
    trash: <><path d="M4 6h16M9 6V3h6v3M6 6l1 15h10l1-15M10 10v7m4-7v7" /></>,
  }
  return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{paths[kind]}</svg>
}

function App() {
  const showLegacy = new URLSearchParams(window.location.search).get('legacy') === '1'
  const [projects, setProjects] = useState<Project[]>([])
  const [project, setProject] = useState<Project | null>(null)
  const [asset, setAsset] = useState<Asset | null>(null)
  const [exports, setExports] = useState<Export[]>([])
  const [tracks, setTracks] = useState<SourceTrack[]>([])
  const [job, setJob] = useState<Job | null>(null)
  const [capabilities, setCapabilities] = useState<Capabilities | null>(null)
  const [displayedProgram, setDisplayedProgram] = useState<ProgramInfo | null>(null)
  const [prompt, setPrompt] = useState('')
  const [duration, setDuration] = useState(20000)
  const [busy, setBusy] = useState(false)
  const [upload, setUpload] = useState<number | null>(null)
  const [dragging, setDragging] = useState(false)
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')
  const [deleting, setDeleting] = useState(false)
  const fileInput = useRef<HTMLInputElement>(null)
  const sourceVideo = useRef<HTMLVideoElement>(null)
  const activeId = useRef<string | null>(null)

  const refreshProjects = async () => {
    const result = await api<{ projects: Project[]; errors?: unknown[] }>('/api/projects')
    setProjects(result.projects)
    if (result.errors?.length) setError(previous => previous || 'Some saved projects need file recovery. Healthy projects remain available; damaged files have been preserved.')
  }
  const load = async (id: string) => {
    setError(''); setMessage(''); setDeleting(false)
    activeId.current = id
    setBusy(true)
    try {
    const result = await api<{ project: Project; asset: Asset | null }>(`/api/projects/${id}`)
    if (activeId.current !== id) return
    setProject(result.project); setAsset(result.asset)
    setTracks([])
    const output = await api<{ exports: Export[] }>(`/api/projects/${id}/exports`)
    if (activeId.current !== id) return
    setExports(output.exports)
    setPrompt(result.project.action_prompt); setDuration(result.project.requested_duration_ms)
    const savedJob = result.project.active_job_id ? await api<Job>(`/api/jobs/${result.project.active_job_id}`) : null
    if (activeId.current !== id) return
    setJob(savedJob)
    if (savedJob?.error) setError(savedJob.error.message)
    localStorage.setItem('videocut.project', id)
    } finally {
      if (activeId.current === id) setBusy(false)
    }
  }

  useEffect(() => {
    let alive = true
    Promise.all([api<Capabilities>('/api/capabilities'), api<{ projects: Project[]; errors?: unknown[] }>('/api/projects')])
      .then(async ([caps, list]) => {
        if (!alive) return
        setCapabilities(caps); setProjects(list.projects)
        const saved = new URLSearchParams(window.location.search).get('project') || localStorage.getItem('videocut.project')
        if (saved && list.projects.some(p => p.id === saved)) await load(saved)
        if (list.errors?.length) setError(previous => previous || 'Some saved projects need file recovery. Healthy projects remain available; damaged files have been preserved.')
      }).catch(err => { if (alive) setError(err.message) })
    return () => { alive = false }
  }, [])

  useEffect(() => {
    if (!job || terminal.has(job.status)) return
    const jobId = job.id, projectId = job.project_id
    let cancelled = false
    let polling = false
    const poll = async () => {
      if (polling || cancelled) return
      polling = true
      try {
        const next = await api<Job>(`/api/jobs/${jobId}`)
        if (cancelled || activeId.current !== projectId) return
        if (terminal.has(next.status)) {
          // Publish terminal job + refreshed source together. Setting the job
          // first cleans up this effect while its source refresh is in flight.
          const [result, output, caps, list] = await Promise.all([
            api<{ project: Project; asset: Asset | null }>(`/api/projects/${projectId}`),
            api<{ exports: Export[] }>(`/api/projects/${projectId}/exports`),
            api<Capabilities>('/api/capabilities'),
            api<{ projects: Project[]; errors?: unknown[] }>('/api/projects'),
          ])
          if (cancelled || activeId.current !== projectId) return
          setProject(result.project); setAsset(result.asset)
          setExports(output.exports); setCapabilities(caps); setProjects(list.projects)
          if (next.error) setError(next.error.message)
          else if (list.errors?.length) setError('Some saved projects need file recovery. Healthy projects remain available.')
          window.clearInterval(timer)
        }
        setJob(next)
      } catch (err) { if (!cancelled) setError((err as Error).message) }
      finally { polling = false }
    }
    const timer = window.setInterval(poll, 1500)
    return () => { cancelled = true; window.clearInterval(timer) }
  }, [job?.id, job?.status])

  const create = async (name = 'Untitled action') => {
    const result = await api<{ project: Project }>('/api/projects', {
      method: 'POST', body: JSON.stringify({ name, action_prompt: prompt, requested_duration_ms: duration }),
    })
    activeId.current = result.project.id
    setProject(result.project); setAsset(null); setJob(null); setExports([])
    localStorage.setItem('videocut.project', result.project.id)
    await refreshProjects()
    return result.project
  }

  const chooseFile = async (file: File) => {
    setError(''); setMessage(''); setDeleting(false)
    const max = capabilities?.limits.max_upload_mb ?? 150
    if (!/\.(mp4|mov)$/i.test(file.name)) { setError('Choose an MP4 or MOV video.'); return }
    if (file.size > max * 1024 * 1024) { setError(`Choose a file smaller than ${max} MB.`); return }
    if (file.size === 0) { setError('This file is empty. Choose another video.'); return }
    setBusy(true)
    try {
      const current = project && (!asset || asset.status === 'failed') ? project : await create(file.name.replace(/\.[^.]+$/, '').slice(0, 120))
      setUpload(0)
      const result = await uploadFile(current.id, file, setUpload)
      setJob(await api<Job>(`/api/jobs/${result.job_id}`))
      const loaded = await api<{ project: Project; asset: Asset | null }>(`/api/projects/${current.id}`)
      setProject(loaded.project); setAsset(loaded.asset)
      await refreshProjects()
    } catch (err) { setError((err as Error).message) }
    finally { setUpload(null); setBusy(false); if (fileInput.current) fileInput.current.value = '' }
  }

  const savePrompt = async () => {
    if (!prompt.trim()) { setError('Describe the action or moment you want to extract.'); return }
    setBusy(true); setError(''); setMessage('')
    try {
      const current = project ?? await create()
      const saved = await api<Project>(`/api/projects/${current.id}`, {
        method: 'PATCH', body: JSON.stringify({ action_prompt: prompt, requested_duration_ms: duration }),
      })
      setProject(saved); await refreshProjects()
      setMessage('Action request saved.')
    } catch (err) { setError((err as Error).message) }
    finally { setBusy(false) }
  }

  const analyze = async () => {
    if (!project || !prompt.trim()) return
    setBusy(true); setError(''); setMessage('')
    try {
      const request = await api<{ job_id: string }>(`/api/projects/${project.id}/analyze`, { method: 'POST', body: JSON.stringify({ action_prompt: prompt, requested_duration_ms: duration }) })
      setJob(await api<Job>(`/api/jobs/${request.job_id}`))
    } catch (err) { setError((err as Error).message) }
    finally { setBusy(false) }
  }

  const remove = async () => {
    if (!project) return
    setBusy(true); setError('')
    try {
      await api<void>(`/api/projects/${project.id}`, { method: 'DELETE' })
      activeId.current = null; setProject(null); setAsset(null); setJob(null); setDeleting(false); setExports([])
      localStorage.removeItem('videocut.project'); await refreshProjects()
    } catch (err) { setError((err as Error).message) }
    finally { setBusy(false) }
  }

  const processing = !!job && !terminal.has(job.status)
  const locked = busy || processing
  const ready = asset?.status === 'ready'
  const metadata = asset?.metadata
  const resultsProgram = displayedProgram?.program.project_id === project?.id && displayedProgram?.program.source_asset_id === asset?.id ? displayedProgram : null
  const visibleExports = resultsProgram ? exports.filter(output => !output.actionos_lineage || (output.actionos_lineage.program_id === resultsProgram.program.program_id && output.actionos_lineage.program_version === resultsProgram.program.version)) : exports
  const formatTime = (ms: number) => `${Math.floor(ms / 60000)}:${String(Math.floor(ms / 1000) % 60).padStart(2, '0')}`

  return <div className="app-shell">
    <aside className="sidebar">
      <a className="brand" href="/" aria-label="ActionOS Studio home"><span className="brand-mark"><Icon kind="cut" size={23} /></span>ActionOS<span className="brand-dot">.</span></a>
      <button className="new-project" disabled={locked} onClick={() => { activeId.current = null; setProject(null); setAsset(null); setJob(null); setExports([]); setPrompt(''); setError(''); setMessage(''); localStorage.removeItem('videocut.project') }}><Icon kind="plus" /> New project</button>
      <p className="section-label">YOUR WORKSPACE</p>
      <div className="project-list">
        {projects.length === 0 ? <p className="empty-projects">Your footage starts here.<br />Projects stay on this server.</p> : projects.map(p => <button disabled={locked} key={p.id} className={`project-item ${project?.id === p.id ? 'selected' : ''}`} onClick={() => load(p.id).catch(err => setError(err.message))}><Icon kind="film" size={17} /><span>{p.name}</span><i className={`status-dot ${p.status}`} /></button>)}
      </div>
      <div className="sidebar-footer"><span className="status-dot" /> STUDIO <span className="version">v0.1</span></div>
    </aside>

    <div className="workspace">
      <header className="topbar"><div><span className="breadcrumb">Workspace</span><span className="slash">/</span><span>{project?.name ?? 'New project'}</span></div><span className="local-badge">LOCAL WORKSPACE</span></header>
      <main>
        <div className="page-heading"><div><p className="eyebrow">ACTIONOS STUDIO</p><h1>Program what you see.</h1><p className="intro">Describe a visual rule, inspect its program,<br />and review the exact version before evaluation.</p></div><span className="step-label">01 <span>/ SOURCE & INTENT</span></span></div>
        <nav className="studio-nav" aria-label="Studio workflow">
          <a href="#source-title">Source</a>
          <a href="#program" aria-describedby="program-availability">Program</a>
          <a href="#results">Results</a>
          <a href="#outputs">Clips</a>
        </nav>
        <details className="capability-note"><summary>Availability & processing details</summary>
          <p id="program-availability">{capabilities?.studio.program.reason ?? 'Checking program availability'}</p>
          <p id="run-availability">{capabilities?.studio.run.reason ?? 'Checking evaluation availability'}</p>
          <p id="report-availability">{capabilities?.studio.report.reason ?? 'Checking report availability'}</p>
        </details>
        <TaskProgress job={job?.project_id === project?.id ? job : null} upload={upload} />

        <div className="editor-grid">
          <section className="source-panel" aria-labelledby="source-title">
            <div className="panel-heading"><h2 id="source-title"><span className="number">01</span> Your footage</h2>{ready ? <button className="text-button muted" disabled={locked} onClick={() => fileInput.current?.click()}>New source <Icon kind="plus" size={12} /></button> : <span className="muted">MP4 / MOV</span>}</div>
            {ready ? <div className="player-wrap"><video ref={sourceVideo} key={asset.id} controls preload="metadata" playsInline src={`/api/assets/${asset.id}/stream`} poster={`/api/assets/${asset.id}/thumbnail`} /><TrackOverlay assetId={asset.id} tracks={tracks} video={sourceVideo} /><div className="source-caption"><span className="source-chip">SOURCE</span><span>Normalized preview · original preserved</span></div></div> : <div className={`dropzone ${dragging ? 'dragging' : ''} ${locked ? 'processing' : ''}`} onDragOver={event => { event.preventDefault(); if (!locked) setDragging(true) }} onDragLeave={() => setDragging(false)} onDrop={event => { event.preventDefault(); setDragging(false); if (!locked && event.dataTransfer.files[0]) void chooseFile(event.dataTransfer.files[0]) }}>
              <div className="upload-symbol"><Icon kind={processing ? 'film' : 'upload'} size={30} /></div>
              <h3>{upload !== null ? 'Uploading your footage' : processing ? 'Preparing your source' : 'Drop your footage here'}</h3>
              <p>{upload !== null ? `${upload}% transferred` : processing ? stages[job?.step ?? 'queued'] ?? 'Processing media' : 'One video. Any action. Your direction.'}</p>
              {!locked && <button className="secondary-button" onClick={() => fileInput.current?.click()}>Choose a video <Icon kind="arrow" size={16} /></button>}
              {locked && <div className="progress-track"><div className={upload === null && job?.progress === null ? 'indeterminate' : ''} style={{ width: `${upload ?? job?.progress ?? 30}%` }} /></div>}
              <span className="file-limits">MP4 or MOV · up to {capabilities?.limits.max_upload_mb ?? 150} MB · {capabilities?.limits.max_video_seconds ?? 120} seconds</span>
            </div>}
            <input ref={fileInput} type="file" accept=".mp4,.mov,video/mp4,video/quicktime" className="sr-only" aria-label="Choose source video" onChange={event => { if (event.target.files?.[0]) void chooseFile(event.target.files[0]) }} />
            {metadata ? <div className="metadata-strip"><div><span>DURATION</span><strong>{formatTime(metadata.duration_ms)}</strong></div><div><span>RESOLUTION</span><strong>{metadata.geometry.display_width} × {metadata.geometry.display_height}</strong></div><div><span>SOURCE RATE</span><strong>{metadata.fps.toFixed(2)} fps</strong></div><div><span>AUDIO</span><strong>{metadata.audio_present ? 'Included' : 'Silent'}</strong></div></div> : <div className="source-footnote"><Icon kind="check" size={15} /> Your original footage stays intact.</div>}
            {ready && metadata && <SourceEvidence assetId={asset.id} durationMs={asset.working_metadata?.duration_ms ?? metadata.duration_ms} video={sourceVideo} />}
          </section>

          <ProgramInspector key={`${project?.id ?? 'new'}:${asset?.id ?? 'empty'}`} project={project} asset={asset} capabilities={capabilities} job={job} locked={locked} onJob={setJob} onProgramChange={setDisplayedProgram} />
        </div>
        <section id="results" className="results-panel" aria-labelledby="results-title">
          <div className="panel-heading"><h2 id="results-title"><span className="number">03</span> Results</h2><span className="accent-label">{resultsProgram ? `PROGRAM VERSION ${resultsProgram.program.version}` : 'EVIDENCE · CLIPS · REPORTS'}</span></div>
          <div className="results-body">
          {resultsProgram ? <>
            <RunInspector key={`${resultsProgram.program_fingerprint}:runs`} projectId={resultsProgram.program.project_id} programId={resultsProgram.program.program_id}
              version={resultsProgram.program.version} fingerprint={resultsProgram.program_fingerprint} approved={resultsProgram.review_approved} job={job} locked={locked} onJob={setJob} />
            <EntityTrackInspector key={resultsProgram.program_fingerprint} projectId={resultsProgram.program.project_id} programId={resultsProgram.program.program_id}
              version={resultsProgram.program.version} programFingerprint={resultsProgram.program_fingerprint} sourceId={resultsProgram.program.source_asset_id}
              refreshToken={`${job?.id ?? ''}:${job?.status ?? ''}`} locked={locked} />
          </> : <p className="results-empty">Compile and run a visual rule to see matching moments, evidence, clips and reports here.</p>}
          <section id="outputs" className="export-section" aria-label="Clips"><h3>Clips</h3>{visibleExports.length > 0 ? <div className="export-grid">{visibleExports.map(output => <ResultCard key={output.id} output={output} disabled={locked} canRevise={!!capabilities?.operations.revise} onJob={setJob} sourceVideo={sourceVideo} />)}</div> : <p className="results-empty">Select results above, then create a clip.</p>}</section>
          </div>
        </section>
        {showLegacy && <details className="legacy-workflow"><summary>Legacy action finder - model hypotheses</summary>
          <section className="direction-panel" aria-labelledby="direction-title">
            <div className="panel-heading"><h2 id="direction-title"><span className="number">02</span> Your direction</h2><span className="accent-label">ANY ACTION</span></div>
            <label htmlFor="action-prompt" className="field-label">What moment are we looking for?</label>
            <textarea id="action-prompt" value={prompt} maxLength={8000} onChange={event => { setPrompt(event.target.value); setMessage('') }} placeholder="Extract the moment the red car overtakes the blue car. Keep both cars in frame." />
            <p className="field-help">Be specific about the action, the subject, and which occurrence you want.</p>
            <div className="example-prompts"><span>TRY A DIRECTION</span>{examples.map(example => <button key={example} onClick={() => { setPrompt(example); setMessage('') }}>{example} <span>↗</span></button>)}</div>
            <div className="duration-field"><span className="field-label">Target length</span><div className="segmented" aria-label="Target duration">{[15000, 20000, 30000].map(ms => <button aria-pressed={duration === ms} key={ms} onClick={() => setDuration(ms)} className={duration === ms ? 'active' : ''}>{ms / 1000}s</button>)}</div></div>
            <p className="duration-note">A shorter action may make a shorter, more natural edit.</p>
            <button className="primary-button" disabled={locked || !prompt.trim()} onClick={() => void savePrompt()}>Save action request <Icon kind="arrow" size={18} /></button>
            <button className="analysis-button" disabled={locked || !ready || !prompt.trim() || !capabilities?.operations.analyze} onClick={() => void analyze()}>Find action moments <span>{capabilities?.muse.configured ? 'META CLOUD' : 'CLOUD KEY REQUIRED'}</span></button>
          </section>

        {ready && project && <AnalysisBoard key={project.id} projectId={project.id} job={job?.type === 'compile' ? null : job} video={sourceVideo} durationMs={duration} onJob={setJob} onTracks={setTracks} />}
        </details>}
        {job?.status === 'completed' && typeof job.result.clarification === 'string' && <p className="feedback" role="status">Clarification needed: {job.result.clarification}</p>}
        {error && <div className="feedback error" role="alert"><strong>Something needs attention</strong><p>{error}</p><button onClick={() => setError('')}>Dismiss</button></div>}
        {message && <div className="feedback success" role="status">{message}</div>}
        <section className="workflow-note"><div className="small-mark"><Icon kind="cut" size={19} /></div><div><h2>Every edit starts with evidence.</h2><p>Your action request will guide matching moments, subject tracking, and cuts tied to source frames. No matching action? You’ll see an honest result.</p></div><span className="evidence-label">SOURCE → EVIDENCE → EDIT</span></section>
        <div className="bottom-row"><div className="provider-notice"><span className="status-dot pending" /><span>{capabilities ? capabilities.muse.reason : 'Checking server capabilities…'}<small>Analysis sends video or sampled frames to Meta cloud APIs. Upload preparation runs locally.</small></span></div>{project && <div className="delete-area">{deleting ? <><span>Delete all project media?</span><button className="text-button" disabled={locked} onClick={() => void remove()}>Delete project</button><button className="text-button" onClick={() => setDeleting(false)}>Keep</button></> : <button className="text-button muted" disabled={locked} onClick={() => setDeleting(true)}><Icon kind="trash" size={14} /> Delete project</button>}</div>}</div>
      </main>
    </div>
  </div>
}

createRoot(document.getElementById('root')!).render(<React.StrictMode><App /></React.StrictMode>)
