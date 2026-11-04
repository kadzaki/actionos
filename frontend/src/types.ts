export type Project = {
  id: string; name: string; status: 'new' | 'processing' | 'ready' | 'failed'
  active_asset_id: string | null; active_job_id: string | null
  active_program_id?: string | null; active_compilation_id?: string | null
  active_run_id?: string | null
  action_prompt: string; requested_duration_ms: number; created_at: string
}
export type Metadata = {
  duration_ms: number; fps: number; codec: string; audio_present: boolean
  geometry: { display_width: number; display_height: number; rotation_degrees: number }
}
export type Asset = {
  id: string; project_id: string; status: 'processing' | 'ready' | 'failed'
  original_name: string; byte_size: number; metadata: Metadata | null
  working_metadata: Metadata | null
}
export type Job = {
  id: string; project_id: string; status: 'queued' | 'running' | 'completed' | 'failed' | 'interrupted' | 'cancelled'
  run_id?: string | null
  step: string; progress: number | null; error: ApiError | null
  type: 'ingest' | 'analyze' | 'track' | 'render' | 'revise' | 'compile' | 'observe' | 'evaluate' | 'report' | 'action'; result: Record<string, unknown>
}
export type ApiError = { code: string; message: string; retryable: boolean; details: Record<string, unknown> }
export type Capability = { configured: boolean; available: boolean; verified: boolean; model: string; reason: string }
export type Capabilities = {
  muse: Capability; sam: Capability
  studio: Record<'source' | 'program' | 'run' | 'report', { available: boolean; reason: string }>
  operations: { upload: boolean; analyze: boolean; render: boolean; revise: boolean; compile: boolean }
  limits: { max_upload_mb: number; max_video_seconds: number }
}
export type Export = {
  id: string; origin: 'manual' | 'autonomous'; duration_ms: number
  stream_url: string; download_url: string; plan_id: string; plan_version: number
  actionos_lineage?: { fixture_only: boolean; verdict: string; review_only: boolean; program_id?: string; program_version?: number; run_id?: string } | null
}
