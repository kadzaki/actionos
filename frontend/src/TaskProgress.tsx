import type { Job } from './types'

const tasks: Record<Job['type'], string> = {
  ingest: 'Preparing your video', compile: 'Compiling your rule', observe: 'Analyzing your video',
  evaluate: 'Evaluating the rule', action: 'Creating clips', report: 'Creating your report',
  revise: 'Revising your rule', analyze: 'Analyzing your video', track: 'Tracking objects', render: 'Creating clips',
}
const stages: Record<string, string> = {
  queued: 'Waiting for the worker', probing: 'Checking the video', mapping_timestamps: 'Mapping source frames',
  normalizing: 'Preparing the video', creating_preview: 'Creating the preview', compiling: 'Interpreting your instruction',
  validating_program: 'Checking the compiled rule', revising_program: 'Interpreting your changes', validating_revision: 'Checking the revised rule',
  observing: 'Finding evidence with Muse', tracking: 'Tracking objects with SAM', evaluating: 'Checking steps and timing',
  reusing_observations: 'Loading saved evidence', rendering: 'Rendering video', reporting: 'Building the timestamp report',
  verifying: 'Verifying the output', reusing: 'Loading an existing output',
}

export function TaskProgress({ job, upload }: { job: Job | null; upload: number | null }) {
  if (!job && upload === null) return null
  const active = upload !== null || job?.status === 'queued' || job?.status === 'running'
  const failed = upload === null && job && ['failed', 'interrupted', 'cancelled'].includes(job.status)
  const progress = upload ?? (active ? job?.progress : failed ? null : 100)
  const percent = typeof progress === 'number' ? Math.max(0, Math.min(100, progress)) : null
  const title = upload !== null ? 'Uploading your video' : failed ? 'Task needs attention' : active ? tasks[job!.type] : 'Task complete'
  const detail = upload !== null ? 'Transferring the source file' : failed ? job?.error?.message ?? 'The task stopped; your source is preserved.' : active ? stages[job!.step] ?? job!.step.replaceAll('_', ' ') : job?.type === 'ingest' ? 'Your video is ready. Describe the rule to continue.' : job?.type === 'compile' || job?.type === 'revise' ? 'Review the interpreted rule before running it.' : 'Results and available outputs are shown below.'
  return <section className={`task-status ${active ? 'is-active' : failed ? 'is-failed' : 'is-complete'}`} aria-label="Current task">
    <div className="task-status-heading"><div><span className="task-status-kicker">{active ? 'IN PROGRESS' : failed ? 'ATTENTION' : 'READY'}</span><strong>{title}</strong></div>
      <span className="task-percent">{active && percent === null ? 'Working…' : percent !== null && !failed ? `${percent}%` : 'Stopped'}</span></div>
    <div className="task-progress-bar" role="progressbar" aria-label={title} aria-valuemin={0} aria-valuemax={100} aria-valuenow={percent ?? undefined}>
      <div className={active && percent === null ? 'task-indeterminate' : ''} style={percent === null ? { width: failed ? '0%' : undefined } : { width: `${percent}%` }} />
    </div>
    <p role="status">{detail}</p>
  </section>
}
