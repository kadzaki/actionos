import type { ApiError } from './types'

export class RequestError extends Error {
  constructor(public data: ApiError) { super(data.message) }
}

export async function api<T>(url: string, options: RequestInit = {}): Promise<T> {
  const headers = new Headers(options.headers)
  if (options.body && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json')
  const queuedRequest = options.method?.toUpperCase() === 'POST' && /\/(analyze|reels|revise|track|compile|run)$/.test(url) && typeof options.body === 'string'
  if (queuedRequest && !headers.has('Idempotency-Key')) headers.set('Idempotency-Key', crypto.randomUUID())
  const request = { ...options, headers }
  let response: Response
  try { response = await fetch(url, request) }
  catch (error) {
    if (!queuedRequest || options.signal?.aborted) throw error
    // A lost response can leave a real job running. Retry once with exactly
    // the same persisted request key, rather than submit duplicate work.
    await new Promise(resolve => setTimeout(resolve, 400))
    if (options.signal?.aborted) throw error
    response = await fetch(url, request)
  }
  if (!response.ok) {
    const data = await response.json().catch(() => ({ code: 'NETWORK_ERROR', message: 'Request failed. Please try again.', retryable: true, details: {} }))
    throw new RequestError(data)
  }
  return response.status === 204 ? undefined as T : response.json()
}

export function uploadFile(projectId: string, file: File, onProgress: (value: number) => void): Promise<{ asset_id: string; job_id: string }> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest()
    xhr.open('POST', `/api/projects/${projectId}/upload`)
    xhr.setRequestHeader('Content-Type', file.type === 'video/quicktime' ? 'video/quicktime' : 'video/mp4')
    xhr.setRequestHeader('X-File-Name', encodeURIComponent(file.name))
    xhr.upload.onprogress = event => {
      if (event.lengthComputable) onProgress(Math.round(100 * event.loaded / event.total))
    }
    xhr.onload = () => {
      let result
      try { result = JSON.parse(xhr.responseText) } catch { result = { message: 'Upload failed. Please retry.' } }
      if (xhr.status >= 200 && xhr.status < 300) resolve(result)
      else reject(new RequestError(result))
    }
    xhr.onerror = () => reject(new Error('Connection lost during upload. Please try again.'))
    xhr.onabort = () => reject(new Error('Upload interrupted.'))
    xhr.send(file)
  })
}
