from copy import deepcopy
import json
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import TypeAdapter, ValidationError

from app.programs.ast import ProgramAST, VisualProgram
from app.programs.predicates import PredicateStep, normalize_alias

FIXTURES=Path(__file__).parent/'fixtures/programs'


def draft(name='tabletop'):
    return json.loads((FIXTURES/(name+'.json')).read_text(encoding='utf-8'))


@pytest.mark.parametrize('name',['tabletop','handoff','vehicle'])
def test_representative_asts_roundtrip_without_event_times_or_rationale_substitution(name):
    raw=draft(name)
    ast=ProgramAST.model_validate(raw)
    assert ast.instruction == raw['instruction']
    assert ProgramAST.model_validate(ast.model_dump(mode='json',by_alias=True)) == ast
    assert all(not hasattr(s,'start_ms') and not hasattr(s,'end_ms') for s in ast.steps)
    program=VisualProgram(project_id=uuid4(),source_asset_id=uuid4(),source_sha256='a'*64,ast=ast,
        compiler={'origin':'test_fixture','compiler_version':'test','prompt_version':'test'},explanation='Separate unverified explanation')
    assert program.ast == ast and program.explanation not in ast.model_dump_json()
    assert program.program_id != program.source_asset_id


def test_left_start_revision_uses_image_zone_and_cycle_start_without_rebinding_ids():
    original=draft()
    changed=deepcopy(original)
    changed['instruction']+=' Only count cycles where the component starts on the left.'
    changed['zones']=[{'id':'left_zone','description':'Reviewed left image half','x':0.,'y':0.,'width':.5,'height':1.}]
    changed['steps'].append({'id':'left_start','predicate':'inside','entities':['component'],'zone_id':'left_zone','anchor':'cycle_start'})
    ast=ProgramAST.model_validate(changed)
    assert [s.id for s in ast.steps[:2]] == [s['id'] for s in original['steps']]
    assert ast.steps[-1].anchor == 'cycle_start' and ast.zones[0].width == .5


def test_aliases_lower_to_typed_meaning_and_keep_contact_distinct_from_overlap():
    adapter=TypeAdapter(PredicateStep)
    cases=[('placed',['component','assembly_zone'],'semantic_event'),('picked_up',['component'],'semantic_event'),
           ('located',['component','left_zone'],'inside'),('tool_contact',['tool','component'],'touches')]
    for op,args,expected in cases:
        raw={'id':'step','predicate':op,'arguments':args,'source_wording':'original user clause'}
        before=deepcopy(raw)
        node=adapter.validate_python(normalize_alias(raw))
        assert raw == before and node.predicate == expected and node.original_operator == op
        assert node.source_wording == 'original user clause'
    assert adapter.validate_python({'id':'step','predicate':'semantic_event','entities':['robot','map'],
        'phrase':'a robot unfolds a map with its antenna'}).phrase.endswith('antenna')


@pytest.mark.parametrize('step',[
    {'id':'step','predicate':'touches','entities':['a']},
    {'id':'step','predicate':'overlap','entities':['a','b'],'parameters':{'min_iou':0}},
    {'id':'step','predicate':'near','entities':['a','b'],'parameters':{'distance_ratio':True}},
    {'id':'step','predicate':'moves','entities':['a'],'parameters':{'min_displacement_ratio':.1,'duration_ms':-1}},
    {'id':'step','predicate':'inside','entities':['a'],'zone_id':'../../zone'},
    {'id':'step','predicate':'semantic_event','entities':['a','a'],'phrase':'turns'},
    {'id':'step','predicate':'semantic_event','entities':['a'],'phrase':'turns','shell':'echo invalid'},
    {'id':'step','predicate':'run_python','entities':['a']},
    {'id':'step','predicate':'touches','entities':['a','b'],'parameters':{'min_iou':.1}},
])
def test_signature_errors_cannot_bypass_types_or_add_executable_parameters(step):
    with pytest.raises(ValidationError):TypeAdapter(PredicateStep).validate_python(step)


def test_schema_is_bounded_and_boolean_shapes_and_zone_bounds_are_checked():
    assert len(json.dumps(ProgramAST.model_json_schema()).encode()) < 131072
    for change in [{'instruction':'   '},{'zones':[{'id':'zone','description':'outside','x':.8,'y':0.,'width':.5,'height':1.}]},
                   {'selection':{'occurrences':'all','ordinal':2}}]:
        with pytest.raises(ValidationError):ProgramAST.model_validate({**draft(),**change})
    bound=draft();bound['relations']=[{'id':'order','op':'before','from':'place','to':'use','within_ms':12000}]
    ast=ProgramAST.model_validate(bound)
    assert [(r.id,r.op) for r in ast.relations] == [('order','before'),('order_within','within')]
