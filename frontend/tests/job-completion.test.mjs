import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import vm from 'node:vm'
import ts from 'typescript'

// Run the actual polling effect with controlled network timing and React's
// cleanup-on-terminal-state behavior. No browser, upload or cloud call needed.
const source = await readFile(new URL('../src/main.tsx', import.meta.url), 'utf8')
const file = ts.createSourceFile('main.tsx', source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX)
let callback
function visit(node) {
  if (ts.isCallExpression(node) && node.expression.getText(file) === 'useEffect' &&
      node.arguments[1]?.getText(file).replaceAll(/\s/g, '') === '[job?.id,job?.status]') callback = node.arguments[0].getText(file)
  ts.forEachChild(node, visit)
}
visit(file)
assert.ok(callback, 'The job polling effect must exist')
const compiled = ts.transpileModule(`const effect = ${callback}; effect;`, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.None },
}).outputText

function scenario() {
  let resolveProject
  const projectResponse = new Promise(resolve => { resolveProject = resolve })
  const state = { asset: null, job: { id: 'upload', project_id: 'project', status: 'running' }, requests: 0, errors: [] }
  const activeId = { current: 'project' }
  let poll, cleanup
  const context = {
    job: state.job, activeId, terminal: new Set(['completed', 'failed', 'cancelled', 'interrupted']),
    window: { setInterval: fn => { poll = fn; return 1 }, clearInterval: () => {} },
    api: async path => {
      if (path === '/api/jobs/upload') { state.requests++; return { ...state.job, status: 'completed', error: null } }
      if (path === '/api/projects/project') return projectResponse
      if (path.endsWith('/exports')) return { exports: [] }
      if (path === '/api/projects') return { projects: [] }
      if (path === '/api/capabilities') return {}
      throw new Error(`Unexpected request: ${path}`)
    },
    setAsset: asset => { state.asset = asset }, setProject: () => {}, setExports: () => {},
    setCapabilities: () => {}, setProjects: () => {}, setError: error => state.errors.push(error),
    setJob: job => { state.job = job; cleanup() },
  }
  cleanup = vm.runInNewContext(compiled, context)()
  return { state, activeId, poll: () => poll(), cleanup: () => cleanup(), resolveProject }
}

test('completed upload publishes the ready asset before terminal cleanup and avoids overlapping polls', async () => {
  const run = scenario()
  const pending = run.poll()
  await Promise.resolve()
  await run.poll()
  assert.equal(run.state.requests, 1)
  assert.equal(run.state.job.status, 'running')
  run.resolveProject({ project: { id: 'project', status: 'ready' }, asset: { id: 'video', status: 'ready' } })
  await pending
  assert.equal(run.state.asset?.status, 'ready')
  assert.equal(run.state.job.status, 'completed')
  assert.deepEqual(run.state.errors, [])
})

test('switching projects while completion refresh is pending cannot replace the new project asset', async () => {
  const run = scenario()
  const pending = run.poll()
  await Promise.resolve()
  run.activeId.current = 'another-project'
  run.cleanup()
  run.resolveProject({ project: { id: 'project' }, asset: { id: 'old-video', status: 'ready' } })
  await pending
  assert.equal(run.state.asset, null)
  assert.equal(run.state.job.status, 'running')
})
