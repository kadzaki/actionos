import { useEffect, useState } from 'react'
import type { RefObject } from 'react'
import { api } from './api'
import type { SourceTrack } from './AnalysisBoard'

type Timeline = { working_frame_times_ms: number[]; working_frame_source_indices: number[]; source_frame_times_ms: number[] }
type Box = { id: string; label: string; x: number; y: number; width: number; height: number }
export function TrackOverlay({ assetId, tracks, video }: { assetId: string; tracks: SourceTrack[]; video: RefObject<HTMLVideoElement | null> }) {
  const [enabled, setEnabled] = useState(false)
  const [boxes, setBoxes] = useState<Box[]>([])
  useEffect(() => {
    let cancelled = false
    let timeline: Timeline | null = null
    const element = video.current
    if (!element || !enabled || !tracks.length) { setBoxes([]); return }
    const draw = () => {
      if (cancelled || !timeline || !element.videoWidth || !element.videoHeight) return
      const ms = element.currentTime * 1000
      let index = 0
      while (index + 1 < timeline.working_frame_times_ms.length && timeline.working_frame_times_ms[index + 1] <= ms) index++
      const sourceMs = timeline.source_frame_times_ms[timeline.working_frame_source_indices[index]]
      const scale = Math.min(element.clientWidth / element.videoWidth, element.clientHeight / element.videoHeight)
      const width = element.videoWidth * scale, height = element.videoHeight * scale
      const offsetX = (element.clientWidth - width) / 2, offsetY = (element.clientHeight - height) / 2
      const found: Box[] = []
      for (const track of tracks) {
        const samples = track.samples.filter(sample => sample.visible)
        let at = 0
        while (at + 1 < samples.length && samples[at + 1].time_ms <= sourceMs) at++
        const a = samples[at], b = samples[at + 1]
        if (!a || sourceMs < a.time_ms || sourceMs - a.time_ms > 150) continue
        if (b && b.time_ms - a.time_ms > 400) continue
        const ratio = b ? Math.max(0, Math.min(1, (sourceMs - a.time_ms) / (b.time_ms - a.time_ms))) : 0
        const box = (key: 'x' | 'y' | 'width' | 'height') => a[key] + (b ? (b[key] - a[key]) * ratio : 0)
        found.push({ id: track.id, label: track.label, x: offsetX + box('x') * width, y: offsetY + box('y') * height, width: box('width') * width, height: box('height') * height })
      }
      setBoxes(found)
    }
    api<Timeline>(`/api/assets/${assetId}/timeline`).then(result => { timeline = result; draw() }).catch(() => setBoxes([]))
    for (const event of ['timeupdate', 'seeked', 'loadedmetadata']) element.addEventListener(event, draw)
    const observer = new ResizeObserver(draw); observer.observe(element)
    return () => { cancelled = true; observer.disconnect(); for (const event of ['timeupdate', 'seeked', 'loadedmetadata']) element.removeEventListener(event, draw) }
  }, [assetId, tracks, enabled])
  if (!tracks.length) return null
  return <><button className="overlay-toggle" aria-pressed={enabled} onClick={() => setEnabled(!enabled)}>{enabled ? 'Hide' : 'Show'} SAM tracks</button>{enabled && <div className="track-overlay" aria-hidden="true">{boxes.map(box => <div key={box.id} className="track-box" style={{ left: box.x, top: box.y, width: box.width, height: box.height }}><span>{box.label}</span></div>)}</div>}</>
}
