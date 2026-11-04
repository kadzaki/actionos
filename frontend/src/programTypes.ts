export type InterpretationIssue = { code: string; message: string; material: boolean; node?: { kind: string; id: string } | null }
export type ValidationIssue = { code: string; message: string; path: (string | number)[]; node_id: string | null }
export type ProgramOutcome = {
  state: 'ready_for_review' | 'clarification_required' | 'unsupported_operation' | 'invalid_program'
  approval_allowed: boolean; interpretation_issues: InterpretationIssue[]; validation_issues: ValidationIssue[]
  assumptions: InterpretationIssue[]; warning: string
}
export type ProgramAST = {
  schema_version: string; instruction: string
  entities: { id: string; description: string; role: string; attributes: string[] }[]
  zones: { id: string; description: string; x: number; y: number; width: number; height: number; coordinate_space: string }[]
  steps: { id: string; predicate: string; entities: string[]; phrase?: string; zone_id?: string | null; parameters: Record<string, unknown>; anchor: string; source_wording: string; original_operator?: string | null }[]
  relations: { id: string; op: string; from: string; to: string; within_ms: number | null }[]
  cycle: { root_step_id: string; policy: { max_cycle_ms: number; max_gap_ms: number; overlap_tolerance_ms: number; endpoint_convention: string; version: string } }
  selection: { occurrences: string; ordinal: number | null }
  exclusions: { id: string; source_wording: string; expression: unknown }[]
  unknown_policy: { observation: string; presentation: string; negative_coverage: string }
  actions: { id: string; type: string; when: string; parameters: Record<string, unknown> }[]
  clauses: { text: string; status: string; represented_by: { kind: string; id: string }[]; note: string }[]
}
export type ProgramInfo = {
  program: {
    program_id: string; version: number; project_id: string; source_asset_id: string; source_sha256: string
    ast: ProgramAST; explanation: string
    compiler: { origin: string; requested_model: string | null; returned_model: string | null; compiler_version: string; prompt_version: string }
  }
  outcome: ProgramOutcome; versions: number[]; compilation_id: string
  program_fingerprint: string; review_approved: boolean
  compilation_origin: string
  human_edit?: { kind: string; base_version: number; revision?: { instruction: string; explanation: string; diff: { invalidates: string; changes: { path: string; before: unknown; after: unknown }[] } } | null } | null
}
