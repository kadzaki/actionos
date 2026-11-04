from copy import deepcopy

import pytest
from pydantic import ValidationError

from app.programs.ast import BooleanExpression,ProgramAST
from app.programs.compiler import build_compile_request
from test_program_ast import draft


def test_compiler_schema_does_not_offer_entities_as_boolean_operands():
    _,schema,content=build_compile_request('Find the red car overtaking the yellow car.',
        {'duration_ms':29000,'width':1280,'height':720,'audio_present':True})
    assert schema['$defs']['BooleanReference']['properties']['kind']['enum']==['step','relation']
    # Entities still legitimately appear in clause-accounting references.
    assert 'entity' in schema['$defs']['NodeReference']['properties']['kind']['enum']
    assert schema['$defs']['BooleanExpression']['allOf']
    assert 'bind those exact entities to the positive event' in content[0]['text']


def test_rejected_entity_exclusion_is_not_silently_changed_to_visibility_or_presence():
    raw=draft()
    entity=raw['entities'][0]['id']
    raw['exclusions']=[{'id':'other_cars_only','source_wording':'Do not include passes involving only other cars.',
        'expression':{'op':'not','args':[{'op':'ref','reference':{'kind':'entity','id':entity}}]}}]
    original=deepcopy(raw)
    with pytest.raises(ValidationError):ProgramAST.model_validate(raw)
    assert raw==original


@pytest.mark.parametrize('kind,identifier',[('step','place'),('relation','place_then_use')])
def test_valid_existing_boolean_reference_wire_format_remains_compatible(kind,identifier):
    expression={'op':'not','args':[{'op':'ref','reference':{'kind':kind,'id':identifier}}]}
    parsed=BooleanExpression.model_validate(expression)
    assert parsed.args[0].reference.kind==kind
    assert BooleanExpression.model_validate(parsed.model_dump(mode='json'))==parsed
