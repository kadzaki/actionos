import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import ts from 'typescript'

const source = await readFile(new URL('../src/api.ts', import.meta.url), 'utf8')
const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2022 } }).outputText
const { api, RequestError } = await import(`data:text/javascript;base64,${Buffer.from(compiled).toString('base64')}`)

for (const endpoint of ['/api/projects/unit/reels', '/api/programs/compile', '/api/programs/unit/run']) test(`queued ${endpoint} retries a lost response with the same key and body`, async () => {
  const original = globalThis.fetch
  const requests = []
  globalThis.fetch = async (url, options) => {
    requests.push({ url, key: options.headers.get('Idempotency-Key'), body: options.body })
    if (requests.length === 1) throw new TypeError('simulated lost response after server acceptance')
    return Response.json({ job_id: 'same-persisted-job' })
  }
  try {
    const body = JSON.stringify({ requested_duration_ms: 15000 })
    assert.deepEqual(await api(endpoint, { method: 'POST', body }), { job_id: 'same-persisted-job' })
    assert.equal(requests.length, 2)
    assert.ok(requests[0].key)
    assert.deepEqual(requests[0], requests[1])
  } finally { globalThis.fetch = original }
})

test('project creation and explicit server errors are not automatically resubmitted', async () => {
  const original = globalThis.fetch
  let calls = 0
  globalThis.fetch = async () => { calls++; throw new TypeError('offline') }
  try {
    await assert.rejects(api('/api/projects', { method: 'POST', body: '{}' }))
    assert.equal(calls, 1)
    calls = 0
    globalThis.fetch = async () => { calls++; return Response.json({ code: 'PROJECT_BUSY', message: 'Wait for processing', details: {}, retryable: true }, { status: 409 }) }
    await assert.rejects(api('/api/projects/unit/reels', { method: 'POST', body: '{}' }), error => error instanceof RequestError && error.data.code === 'PROJECT_BUSY')
    assert.equal(calls, 1)
  } finally { globalThis.fetch = original }
})

test('a caller-provided request key is preserved', async () => {
  const original = globalThis.fetch
  globalThis.fetch = async (_url, options) => {
    assert.equal(options.headers.get('Idempotency-Key'), 'caller-key')
    return Response.json({ job_id: 'unit' })
  }
  try { await api('/api/plans/unit/revise', { method: 'POST', body: '{}', headers: new Headers({ 'Idempotency-Key': 'caller-key' }) }) }
  finally { globalThis.fetch = original }
})
