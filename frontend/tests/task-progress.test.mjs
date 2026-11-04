import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import vm from 'node:vm'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import ts from 'typescript'

const source = await readFile(new URL('../src/TaskProgress.tsx', import.meta.url), 'utf8')
const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.React, target: ts.ScriptTarget.ES2022 } }).outputText
const exports = {}
vm.runInNewContext(compiled, { exports, React })
const render = (job, upload = null) => renderToStaticMarkup(React.createElement(exports.TaskProgress, { job, upload }))

test('known progress uses the actual server percentage and task label', () => {
  const html = render({ type: 'ingest', status: 'running', step: 'creating_preview', progress: 65 })
  assert.match(html, /Preparing your video/)
  assert.match(html, /Creating the preview/)
  assert.match(html, /aria-valuenow="65"/)
})

test('cloud tasks with unknown progress show an indeterminate bar without an invented percentage', () => {
  const html = render({ type: 'compile', status: 'running', step: 'compiling', progress: null })
  assert.match(html, /Compiling your rule/)
  assert.match(html, /task-indeterminate/)
  assert.doesNotMatch(html, /aria-valuenow|[0-9]+%/)
})

test('a failed task prominently shows its error rather than a successful progress value', () => {
  const html = render({ type: 'observe', status: 'failed', progress: 30, error: { message: 'Connection interrupted' } })
  assert.match(html, /Connection interrupted/)
  assert.match(html, /Task needs attention/)
  assert.doesNotMatch(html, /100%|task-indeterminate/)
})
