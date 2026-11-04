"""Deterministic DSL types, references, DAG and difference-bound validation."""
from collections import deque

from pydantic import Field, ValidationError

from ..errors import AppError
from ..models import Model
from ..validation import bounded_json
from .ast import ProgramAST
from .predicates import normalize_alias

VALIDATOR_VERSION='program-validation-2'


class ValidationIssue(Model):
    code: str
    message: str
    path: list[str | int] = Field(default_factory=list)
    node_id: str | None = None


class ProgramValidation(Model):
    ast: ProgramAST | None = None
    issues: list[ValidationIssue] = Field(default_factory=list)
    topological_steps: list[str] = Field(default_factory=list)

    @property
    def valid(self):
        return self.ast is not None and not self.issues


def validate_program(value, *, source_duration_ms=None, require_clauses=False):
    if isinstance(value, ProgramAST):
        value = value.model_dump(mode='json', by_alias=True)
    result = ProgramValidation()
    def issue(code, message, path=(), node=None):
        result.issues.append(ValidationIssue(code=code, message=message, path=list(path), node_id=node))
    try:
        bounded_json(value, max_bytes=131072, max_depth=12)
    except AppError:
        issue('AST_LIMIT', 'Program exceeds bounded JSON size, depth or representation limits')
        return result
    if isinstance(value, dict) and isinstance(value.get('steps'), list):
        lowered = []
        for index, step in enumerate(value['steps']):
            try:
                lowered.append(normalize_alias(step))
            except ValueError:
                issue('INVALID_PREDICATE_ARGUMENTS', 'Alias arguments do not match the defined signature', ['steps', index],
                      step.get('id') if isinstance(step, dict) and isinstance(step.get('id'), str) else None)
        value = {**value, 'steps': lowered}
    if result.issues:
        return result
    try:
        ast = ProgramAST.model_validate(value)
    except ValidationError as exc:
        for error in exc.errors(include_input=False, include_url=False):
            path = list(error['loc'])
            code = 'AST_SCHEMA'
            if error['type'] == 'union_tag_invalid':
                code = 'UNSUPPORTED_OPERATOR' if path and path[0] == 'steps' else 'UNSUPPORTED_ACTION'
            issue(code, 'Program field does not match its typed DSL signature', path)
        return result
    result.ast = ast
    tables = {}
    for kind, field in [('entity','entities'),('zone','zones'),('step','steps'),('relation','relations'),
                        ('exclusion','exclusions'),('action','actions')]:
        table = {}
        for index, node in enumerate(getattr(ast, field)):
            if node.id in table:
                issue('DUPLICATE_NODE_ID', 'Node ID is duplicated within its namespace', [field,index,'id'],node.id)
            table[node.id] = node
        tables[kind] = table
    tables.update(selection={'selection':ast.selection}, unknown_policy={'unknown_policy':ast.unknown_policy}, cycle={'cycle':ast.cycle})
    def reference(ref, path):
        if ref.id not in tables[ref.kind]:
            issue('UNKNOWN_REFERENCE', 'Node reference does not exist in the required namespace', path, ref.id)
    for index, step in enumerate(ast.steps):
        for argument, entity_id in enumerate(step.entities):
            if entity_id not in tables['entity']:
                issue('UNKNOWN_ENTITY', 'Predicate references an unknown entity', ['steps',index,'entities',argument],step.id)
        if getattr(step,'zone_id',None) is not None and step.zone_id not in tables['zone']:
            issue('UNKNOWN_ZONE', 'Predicate references an unknown image zone', ['steps',index,'zone_id'],step.id)
    if ast.cycle.root_step_id not in tables['step']:
        issue('UNKNOWN_CYCLE_ROOT', 'Cycle root must reference a positive predicate step', ['cycle','root_step_id'])
    elif tables['step'][ast.cycle.root_step_id].anchor != 'interval':
        issue('INVALID_CYCLE_ROOT', 'Cycle root cannot depend on its own cycle-start anchor', ['cycle','root_step_id'])
    action_types = set()
    for index, action in enumerate(ast.actions):
        if action.type in action_types:
            issue('DUPLICATE_ACTION', 'Only one declaration per P0 action type is allowed', ['actions',index],action.id)
        action_types.add(action.type)
        for entity_id in getattr(action.parameters,'entity_ids',[]):
            if entity_id not in tables['entity']:
                issue('UNKNOWN_ENTITY', 'Output framing references an unknown entity', ['actions',index,'parameters','entity_ids'],action.id)
    for index, exclusion in enumerate(ast.exclusions):
        pending = [(exclusion.expression,['exclusions',index,'expression'])]
        while pending:
            expression, path = pending.pop()
            if expression.reference:
                reference(expression.reference, path+['reference'])
            pending.extend((child,path+['args',child_index]) for child_index,child in enumerate(expression.args))
    for field in ('assumptions','ambiguities','unrepresented_clauses'):
        for index, note in enumerate(getattr(ast,field)):
            if note.node:
                reference(note.node,[field,index,'node'])
    covered = set()
    for index, clause in enumerate(ast.clauses):
        if (not 0 <= clause.start_char < clause.end_char <= len(ast.instruction)
                or ast.instruction[clause.start_char:clause.end_char] != clause.text):
            issue('CLAUSE_SPAN_MISMATCH', 'Clause text/span must preserve the exact original instruction', ['clauses',index])
        else:
            covered.update(range(clause.start_char,clause.end_char))
        if clause.status == 'represented' and not clause.represented_by:
            issue('UNBOUND_CLAUSE', 'Represented clause must identify its typed nodes', ['clauses',index])
        for ref in clause.represented_by:
            reference(ref,['clauses',index,'represented_by'])
    if require_clauses and any(not char.isspace() and index not in covered for index,char in enumerate(ast.instruction)):
        issue('UNACCOUNTED_INSTRUCTION', 'Every non-whitespace part of the instruction needs an exact clause accounting', ['clauses'])
    steps = tables['step']
    edges = {step_id:set() for step_id in steps}
    constraints = []
    # x[v] - x[u] <= bound. Positive step durations prevent zero-duration
    # solutions from hiding impossible containment/order combinations.
    def bound(u,v,limit,node): constraints.append((u,v,limit,node))
    horizon = ast.cycle.policy.max_cycle_ms
    if source_duration_ms is not None:
        if type(source_duration_ms) is not int or not 0 < source_duration_ms <= 120000:
            issue('INVALID_SOURCE_DURATION', 'Source duration must be bounded integer milliseconds')
        else:
            horizon = min(horizon,source_duration_ms)
    root = ast.cycle.root_step_id
    if root in steps:
        origin = ('__cycle_origin__','start')  # Not a legal NodeId.
        for node in ast.steps:
            duration = getattr(node.parameters,'duration_ms',1)
            bound((node.id,'end'),(node.id,'start'),-duration,node.id)
            # The positive root seeds candidates; it need not be the first
            # required step. Bound the entire assignment, including predecessors.
            bound((node.id,'start'),origin,0,node.id)
            bound(origin,(node.id,'end'),horizon,node.id)
            if node.anchor == 'cycle_start':
                bound(origin,(node.id,'start'),0,node.id)
    for index, relation in enumerate(ast.relations):
        a,b = relation.from_step,relation.to
        if a not in steps or b not in steps:
            issue('UNKNOWN_STEP', 'Temporal relation references an unknown step', ['relations',index],relation.id)
            continue
        if a == b:
            issue('TEMPORAL_SELF_REFERENCE', 'Temporal relation cannot reference itself', ['relations',index],relation.id)
            continue
        if relation.op == 'after': a,b = b,a
        if relation.op == 'during':
            edges[b].add(a)
            bound((a,'start'),(b,'start'),0,relation.id)
            bound((b,'end'),(a,'end'),0,relation.id)
        else:
            edges[a].add(b)
            bound((b,'start'),(a,'end'),ast.cycle.policy.overlap_tolerance_ms,relation.id)
            gap = relation.within_ms if relation.op == 'within' else ast.cycle.policy.max_gap_ms
            if relation.op in ('within','then'):
                if gap > ast.cycle.policy.max_cycle_ms:
                    issue('CONTRADICTORY_GAP_POLICY', 'Relation gap exceeds the reviewed cycle bound', ['relations',index],relation.id)
                bound((a,'end'),(b,'start'),gap,relation.id)
    indegree = {node:0 for node in steps}
    for targets in edges.values():
        for node in targets:indegree[node]+=1
    ready = deque(sorted(node for node,n in indegree.items() if n == 0))
    order = []
    while ready:
        node=ready.popleft();order.append(node)
        for target in sorted(edges[node]):
            indegree[target]-=1
            if indegree[target]==0:ready.append(target)
    if len(order) != len(steps):
        issue('TEMPORAL_CYCLE', 'Temporal dependencies must be acyclic', ['relations'])
    else:
        result.topological_steps = order
    variables = {key for u,v,_,_ in constraints for key in (u,v)}
    distances = {key:0 for key in variables}
    changed = None
    for _ in variables:
        changed = None
        for u,v,limit,node in constraints:
            if distances[v] > distances[u]+limit:
                distances[v] = distances[u]+limit
                changed = node
        if changed is None:break
    if changed is not None:
        issue('UNSATISFIABLE_TEMPORAL_BOUNDS', 'Step duration, containment and ordering bounds cannot all be satisfied', ['relations'],changed)
    return result


def require_valid_program(value, **kwargs):
    result = validate_program(value, **kwargs)
    if not result.valid:
        raise AppError('INVALID_PROGRAM', 'Visual program failed validation', 422,
                       details={'issues':[i.model_dump(mode='json') for i in result.issues]})
    return result.ast
