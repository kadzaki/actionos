from copy import deepcopy

import pytest

from app.programs.validation import validate_program
from test_program_ast import draft


@pytest.mark.parametrize('name',['tabletop','handoff','vehicle'])
def test_domain_fixtures_have_valid_typed_graphs(name):
    result=validate_program(draft(name))
    assert result.valid, result.issues
    assert len(result.topological_steps)==len(result.ast.steps)


def test_foreign_references_duplicates_and_action_entity_bindings_are_local_issues():
    raw=draft()
    raw['steps'][0]['entities']=['missing']
    raw['steps'][1]['id']=raw['steps'][0]['id']
    raw['cycle']['root_step_id']='absent'
    result=validate_program(raw)
    assert {'UNKNOWN_ENTITY','DUPLICATE_NODE_ID','UNKNOWN_CYCLE_ROOT','UNKNOWN_STEP'} <= {i.code for i in result.issues}
    assert any(i.path==['steps',0,'entities',0] and i.node_id=='place' for i in result.issues)
    raw=draft('vehicle');raw['actions'][0]['parameters']['entity_ids'].append('foreign')
    assert 'UNKNOWN_ENTITY' in {i.code for i in validate_program(raw).issues}


def test_after_edges_temporal_cycles_and_impossible_containment_are_detected():
    raw=draft()
    raw['relations'].append({'id':'reverse','op':'after','from':'place','to':'use'})
    assert 'TEMPORAL_CYCLE' in {i.code for i in validate_program(raw).issues}
    raw=draft()
    raw['relations']=[{'id':'order','op':'before','from':'place','to':'use'},
                      {'id':'inside','op':'during','from':'use','to':'place'}]
    # Same dependency direction, no DAG cycle: positive-duration difference
    # constraints must still reject the impossible nested-after interval.
    result=validate_program(raw)
    assert 'TEMPORAL_CYCLE' not in {i.code for i in result.issues}
    assert 'UNSATISFIABLE_TEMPORAL_BOUNDS' in {i.code for i in result.issues}


def test_duration_and_cycle_limits_include_anchored_spatial_steps():
    raw=draft('vehicle')
    raw['steps']=[{'id':'move','predicate':'moves','entities':['red_car'],
                   'parameters':{'min_displacement_ratio':.1,'duration_ms':10000}}]
    raw['cycle']['root_step_id']='move'
    assert validate_program(raw,source_duration_ms=12000).valid
    assert 'UNSATISFIABLE_TEMPORAL_BOUNDS' in {i.code for i in validate_program(raw,source_duration_ms=3000).issues}
    raw['steps'][0]['anchor']='cycle_start'
    assert 'INVALID_CYCLE_ROOT' in {i.code for i in validate_program(raw).issues}


@pytest.mark.parametrize('anchor_id',['use','cycle'])
def test_observed_middle_root_allows_predecessors_but_bounds_the_whole_cycle(anchor_id):
    raw=draft()
    raw['steps'][1]['id']=anchor_id;raw['relations'][0]['to']=anchor_id
    raw['cycle']={'root_step_id':anchor_id,'policy':{'max_cycle_ms':5,'max_gap_ms':5}}
    assert validate_program(raw).valid
    # Both one-millisecond steps must fit the same span, not separate horizons
    # before and after the observed anchor.
    raw['cycle']['policy']={'max_cycle_ms':1,'max_gap_ms':1}
    assert 'UNSATISFIABLE_TEMPORAL_BOUNDS' in {i.code for i in validate_program(raw).issues}


def test_exact_clause_spans_and_node_references_are_accounted_without_claiming_semantic_proof():
    raw=draft()
    assert 'UNACCOUNTED_INSTRUCTION' in {i.code for i in validate_program(raw,require_clauses=True).issues}
    raw['clauses']=[{'start_char':0,'end_char':len(raw['instruction']),'text':raw['instruction'],
                    'status':'represented','represented_by':[{'kind':'step','id':'place'},{'kind':'step','id':'use'}]}]
    assert validate_program(raw,require_clauses=True).valid
    raw['clauses'][0]['text']='a paraphrase'
    assert 'CLAUSE_SPAN_MISMATCH' in {i.code for i in validate_program(raw,require_clauses=True).issues}


def test_unsupported_operators_alias_errors_and_depth_limits_are_bounded():
    raw=draft();raw['steps'][0]['predicate']='execute_shell'
    assert 'UNSUPPORTED_OPERATOR' in {i.code for i in validate_program(raw).issues}
    raw=draft();raw['steps'][0]={'id':'place','predicate':'placed','arguments':['only_entity']}
    result=validate_program(raw)
    assert result.issues[0].code=='INVALID_PREDICATE_ARGUMENTS' and result.issues[0].node_id=='place'
    raw=draft();raw['actions']=[{'id':'message','type':'send_email'}]
    assert 'UNSUPPORTED_ACTION' in {i.code for i in validate_program(raw).issues}
    nested={}
    for _ in range(20):nested={'next':nested}
    raw=draft();raw['extra']=nested
    assert validate_program(raw).issues[0].code=='AST_LIMIT'
