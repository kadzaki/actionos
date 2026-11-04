import { useEffect, useRef, useState } from 'react'
import type { RefObject } from 'react'
import { api } from './api'

type FrameEvidence = {
  requested_time_ms: number
  working_frame_index: number
  image_url: string
  frame: { id: string; source_frame_index: number; source_time_ms: number; width: number; height: number }
}

export function SourceEvidence({ assetId, durationMs, video }: {
  assetId: string; durationMs: number; video: RefObject<HTMLVideoElement | null>
}) {
  const [frame, setFrame] = useState<FrameEvidence | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const request = useRef<AbortController | null>(null)

  useEffect(() => {
    setFrame(null); setError(''); setLoading(false)
    return () => { request.current?.abort() }
  }, [assetId])

  const inspect = async () => {
    request.current?.abort()
    const controller = new AbortController()
    request.current = controller
    video.current?.pause()
    const time = Math.max(0, Math.min(durationMs - 1, Math.floor((video.current?.currentTime ?? 0) * 1000)))
    setLoading(true); setError('')
    try {
      const result = await api<FrameEvidence>(`/api/assets/${assetId}/frames/${time}?space=working`, { signal: controller.signal })
      if (!controller.signal.aborted) setFrame(result)
    } catch (err) {
      if (!controller.signal.aborted) setError((err as Error).message)
    } finally {
      if (!controller.signal.aborted) setLoading(false)
    }
  }

  return <div className="source-evidence">
    <div className="evidence-control"><div><strong>Inspect source evidence</strong><p>Pause on a moment to see its original source frame.</p></div><button className="secondary-button" disabled={loading} onClick={() => void inspect()}>{loading ? 'Extracting frame…' : 'Inspect this frame'}</button></div>
    {error && <p className="evidence-error" role="alert">{error}</p>}
    {frame && <figure><img src={frame.image_url} width={frame.frame.width} height={frame.frame.height} alt={`Original source frame ${frame.frame.source_frame_index} at ${(frame.frame.source_time_ms / 1000).toFixed(3)} seconds`} onError={() => setError('This source frame could not be loaded. Retry inspection.')} /><figcaption><span>Original frame #{frame.frame.source_frame_index}</span><span>Source {(frame.frame.source_time_ms / 1000).toFixed(3)}s · preview {(frame.requested_time_ms / 1000).toFixed(3)}s</span></figcaption></figure>}
  </div>
}
