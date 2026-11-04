"""Interpretation states are distinct from provider/job status and event truth."""
from typing import Literal

from pydantic import Field

from ..models import Model
from .ast import InterpretationIssue
from .compiler import Compilation
from .validation import ValidationIssue


class CompileOutcome(Model):
    state: Literal['ready_for_review', 'clarification_required', 'unsupported_operation', 'invalid_program']
    approval_allowed: bool
    interpretation_issues: list[InterpretationIssue] = Field(default_factory=list)
    validation_issues: list[ValidationIssue] = Field(default_factory=list)
    assumptions: list[InterpretationIssue] = Field(default_factory=list)
    warning: str = 'A compiled program is an interpretation, not visual or physical proof. Review its clauses before running.'


def classify_compilation(compilation: Compilation):
    draft=compilation.draft
    ast=draft.ast
    unsupported=list(draft.unsupported_operations)
    unresolved=[*draft.clarifications,*draft.unrepresented_clauses]
    assumptions=[]
    if ast:
        unresolved.extend(ast.ambiguities)
        unresolved.extend(ast.unrepresented_clauses)
        assumptions=ast.assumptions
        unresolved.extend(note for note in assumptions if note.material)
        for clause in ast.clauses:
            if clause.status != 'represented':
                issue=InterpretationIssue(code='CLAUSE_'+clause.status.upper(),
                    message=clause.note or f'Instruction clause requires review: {clause.text}',material=True)
                (unsupported if clause.status=='unsupported' else unresolved).append(issue)
    if unsupported:
        state='unsupported_operation'
    elif compilation.validation.issues:
        state='invalid_program'
    elif ast is None or any(note.material for note in unresolved):
        state='clarification_required'
    elif not compilation.validation.valid:
        state='invalid_program'
    else:
        state='ready_for_review'
    return CompileOutcome(state=state,approval_allowed=state=='ready_for_review',
        interpretation_issues=[*unsupported,*unresolved],validation_issues=compilation.validation.issues,
        assumptions=assumptions)
