from copy import deepcopy
import json
from pathlib import Path

import httpx
import pytest

from app.config import Settings
from app.errors import AppError
from app.programs.compiler import build_compile_request, compile_instruction, instruction_clauses
from app.programs.outcomes import classify_compilation
from app.providers.meta import MetaCloud
from test_program_ast import draft

SOURCE={'duration_ms':30000,'width':320,'height':180,'audio_present':True}


def provider_proposal():
    ast=draft()
    ast['clauses']=[{'start_char':0,'end_char':len(ast['instruction']),'text':ast['instruction'],
        'status':'represented','represented_by':[{'kind':'step','id':'place'},{'kind':'step','id':'use'},
            {'kind':'relation','id':'place_then_use'},{'kind':'unknown_policy','id':'unknown_policy'},
            {'kind':'selection','id':'selection'}]}]
    return {'instruction':ast['instruction'],'ast':ast,'explanation':'TEST FIXTURE: not actual cloud compilation'}


def run_recorded_contract(settings, output, requests=None):
    def transport(request):
        if requests is not None:requests.append(json.loads(request.content))
        return httpx.Response(200,json={'id':'fixture-response','status':'completed','model':settings.muse_model,
            'usage':{'input_tokens':10,'output_tokens':20},
            'output':[{'type':'message','content':[{'type':'output_text','text':json.dumps(output)}]}]})
    with MetaCloud(settings,httpx.MockTransport(transport)) as cloud:
        return compile_instruction(cloud,output['instruction'],SOURCE)


def test_actual_adapter_path_uses_schema_and_open_instruction_with_separate_explanation(tmp_path):
    settings=Settings(data_dir=tmp_path,model_api_key='fixture-test-key')
    requests=[]
    compilation=run_recorded_contract(settings,provider_proposal(),requests)
    assert compilation.validation.valid
    assert classify_compilation(compilation).state=='ready_for_review'
    content=requests[0]['input'][0]['content']
    assert len(content)==2 and 'application schema' in content[0]['text']
    assert 'no domain or verb allowlist' in content[1]['text']
    assert requests[0]['model']==settings.muse_model and requests[0]['stream'] is False
    assert compilation.compiler.response_id=='fixture-response'
    assert compilation.compiler.usage['input_tokens']==10
    assert 'fixture-test-key' not in compilation.model_dump_json()
    assert compilation.draft.ast.instruction==provider_proposal()['instruction']


def test_no_silent_instruction_change_invalid_graph_or_missing_clause_acceptance(tmp_path):
    settings=Settings(data_dir=tmp_path,model_api_key='fixture-key')
    output=provider_proposal();output['ast']['steps'][0]['entities']=['unknown_entity']
    compilation=run_recorded_contract(settings,output)
    assert classify_compilation(compilation).state=='invalid_program'
    output=provider_proposal();output['ast']['clauses']=[]
    compilation=run_recorded_contract(settings,output)
    assert classify_compilation(compilation).state=='invalid_program'
    output=provider_proposal();output['ast']['instruction']='A silently shortened paraphrase'
    with pytest.raises(AppError) as exc:run_recorded_contract(settings,output)
    assert exc.value.code=='INSTRUCTION_CHANGED'


def test_material_ambiguity_unrepresented_and_unsupported_are_distinct(tmp_path):
    settings=Settings(data_dir=tmp_path,model_api_key='fixture-key')
    for field,state in [('clarifications','clarification_required'),('unrepresented_clauses','clarification_required'),
                        ('unsupported_operations','unsupported_operation')]:
        output=provider_proposal()
        output[field]=[{'code':'FIXTURE_ISSUE','message':'Specify the unresolved clause','material':True}]
        outcome=classify_compilation(run_recorded_contract(settings,output))
        assert outcome.state==state and outcome.approval_allowed is False
    output=provider_proposal();output['ast']=None
    output['clarifications']=[{'code':'ENTITY_AMBIGUOUS','message':'Which object?'}]
    assert classify_compilation(run_recorded_contract(settings,output)).state=='clarification_required'
    output=provider_proposal();output['ast']=None
    with pytest.raises(AppError) as exc:run_recorded_contract(settings,output)
    assert exc.value.code=='PROVIDER_SCHEMA'


def test_request_keeps_exact_wording_and_spans_without_claiming_semantic_parsing():
    for instruction in ['  Find a robot unfolding a map. Then report it.  ', 'Within 1.5 seconds; exclude unknowns!', 'Only count cycles where the component starts on the left.']:
        source,schema,content=build_compile_request(instruction,SOURCE)
        spans=instruction_clauses(instruction)
        assert all(instruction[s['start_char']:s['end_char']]==s['text'] for s in spans)
        data=json.loads(content[0]['text'].split('\nInput data: ',1)[1])
        assert data['instruction']==instruction and data['exact_clause_spans']==spans
        assert 'VisualProgram' not in schema.get('required',[])


def test_provider_failure_remains_failure_with_no_fixture_or_local_compiler_fallback(tmp_path):
    settings=Settings(data_dir=tmp_path,model_api_key='fixture-secret')
    with MetaCloud(settings,httpx.MockTransport(lambda _:httpx.Response(401,json={'message':'fixture-secret'}))) as cloud:
        with pytest.raises(AppError) as exc:compile_instruction(cloud,provider_proposal()['instruction'],SOURCE)
    assert exc.value.code=='PROVIDER_AUTH' and 'fixture-secret' not in str(exc.value.envelope())


def test_compiler_schema_exposes_framing_entity_selection_constraint():
    _,schema,content=build_compile_request(provider_proposal()['instruction'],SOURCE)
    clip=schema['$defs']['ClipDeclarationParameters']
    rule=clip['allOf'][0]
    assert rule['if']=={'properties':{'framing':{'const':'track_relevant_entities'}},'required':['framing']}
    assert rule['then']=={'properties':{'entity_ids':{'minItems':1}},'required':['entity_ids']}
    assert rule['else']=={'properties':{'entity_ids':{'maxItems':0}}}
    assert clip['additionalProperties'] is False
    assert 'full_frame or fixed framing requires empty entity_ids' in content[0]['text']


def test_actual_rejected_framing_response_stays_rejected_without_silent_repair(tmp_path):
    recorded=json.loads((Path(__file__).parent/'fixtures/meta/muse-program-invalid-framing.json').read_text(encoding='utf-8'))
    output=recorded['output']
    assert output['ast']['actions'][0]['parameters']['framing']=='full_frame'
    assert output['ast']['actions'][0]['parameters']['entity_ids']
    original=deepcopy(output)
    with pytest.raises(AppError) as exc:
        run_recorded_contract(Settings(data_dir=tmp_path,model_api_key='fixture-key'),output)
    assert exc.value.code=='PROVIDER_SCHEMA'
    assert 'ast.actions.0.extract_clip.parameters' in exc.value.details['fields']
    assert output==original


def test_actual_v2_compiler_response_is_valid_but_material_assumptions_block_review(tmp_path):
    recorded=json.loads((Path(__file__).parent/'fixtures/meta/muse-program-compile-v2.json').read_text(encoding='utf-8'))
    compilation=run_recorded_contract(Settings(data_dir=tmp_path,model_api_key='fixture-key'),recorded['output'])
    assert compilation.validation.valid
    outcome=classify_compilation(compilation)
    assert outcome.state=='clarification_required' and not outcome.approval_allowed
    assert {a.code for a in outcome.assumptions if a.material}=={'DEFAULT_TIMING','GENERIC_REFERENTS'}
    assert [s.id for s in compilation.draft.ast.steps]==['s_place','s_use_tool']
    assert compilation.draft.ast.actions[0].parameters.framing=='full_frame'
    assert compilation.draft.ast.actions[0].parameters.entity_ids==[]
