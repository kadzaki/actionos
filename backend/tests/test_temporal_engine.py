from itertools import product
from uuid import uuid4
import pytest
from app.engine.truth import Truth,conjunction,disjunction,negate
from app.engine.temporal import NodeEvidence,evaluate_program,relation_result
from app.programs.ast import ProgramAST
from app.programs.patches import apply_patch
from app.errors import AppError
from test_program_ast import draft


def node(step,start,end,verdict='true',identity='same'):
    from app.source_evidence import SourceEvidenceReference
    frame=SourceEvidenceReference(project_id=uuid4(),source_asset_id=uuid4(),source_sha256='a'*64,
        evidence_frame_id=uuid4(),source_frame_index=start,source_time_ms=float(start),source_pts=start,
        source_time_base='1/1000',image_sha256='b'*64,transforms=[])
    return NodeEvidence(record_id=uuid4(),step_id=step,verdict=verdict,interval={'start_ms':start,'end_ms':end},
        window={'start_ms':0,'end_ms':10000},frames=[frame],identity_bindings={'component':identity},observed_candidate=True)


def test_exhaustive_strong_kleene_tables():
    for a,b in product(Truth,repeat=2):
        assert conjunction([a,b])==('false' if 'false' in (a,b) else 'unknown' if 'unknown' in (a,b) else 'true')
        assert disjunction([a,b])==('true' if 'true' in (a,b) else 'unknown' if 'unknown' in (a,b) else 'false')
        assert negate(conjunction([a,b]))==disjunction([negate(a),negate(b)])
    assert negate(Truth.UNKNOWN)==Truth.UNKNOWN


def test_interior_anchor_repeats_missing_steps_and_chronological_second():
    raw=draft();raw['cycle']['root_step_id']='use';raw['selection']={'occurrences':'second'}
    evidence=[node('place',100,200),node('use',300,400),node('place',1000,1100),node('use',1200,1300),node('use',5000,5100,verdict='unknown')]
    result=evaluate_program(raw,evidence,run_id=uuid4(),source_duration_ms=10000,coverage_complete=True)
    assert len(result.cycles)==3
    assert [c.selected for c in result.cycles]==[False,True,False]
    assert result.cycles[1].verdict=='true'
    assert result.cycles[2].needs_review
    missing=evaluate_program(raw,[evidence[-1]],run_id=uuid4(),source_duration_ms=10000)
    assert missing.cycles[0].verdict=='unknown'
    assert missing.cycles[0].steps[0].interval is None


def test_wrong_order_and_identity_conflict_are_distinct_from_absence():
    raw=draft();raw['cycle']['root_step_id']='use'
    wrong=evaluate_program(raw,[node('use',100,200),node('place',300,400)],run_id=uuid4(),source_duration_ms=10000,coverage_complete=True)
    assert wrong.cycles[0].edges[0].verdict=='false'
    assert wrong.cycles[0].verdict=='false'
    conflict=evaluate_program(raw,[node('place',100,200,identity='A'),node('use',300,400,identity='B')],run_id=uuid4(),source_duration_ms=10000,coverage_complete=True)
    assert conflict.cycles[0].verdict=='unknown'
    assert 'CROSS_STEP_IDENTITY_AMBIGUOUS' in conflict.cycles[0].reasons


def test_patch_rejects_identity_escape_and_does_not_mutate_base():
    raw=draft();raw['clauses']=[{'start_char':0,'end_char':len(raw['instruction']),'text':raw['instruction'],
        'status':'represented','represented_by':[{'kind':'step','id':'place'},{'kind':'step','id':'use'}]}]
    ast=ProgramAST.model_validate(raw)
    patched,diff=apply_patch(ast,[{'op':'replace','path':'/selection/occurrences','value':'first'}],duration_ms=10000)
    assert ast.selection.occurrences=='all' and patched.selection.occurrences=='first'
    assert diff['invalidates']=='evaluate'
    with pytest.raises(AppError):apply_patch(ast,[{'op':'add','path':'/source_sha256','value':'evil'}],duration_ms=10000)


@pytest.mark.parametrize('op,left,right,bound,expected',[
    ('before',(100,200),(200,300),None,'true'),
    ('after',(200,300),(100,200),None,'true'),
    ('then',(100,200),(200,300),None,'true'),
    ('then',(100,201),(200,300),None,'false'),
    ('within',(100,200),(300,400),100,'true'),
    ('within',(100,200),(301,400),100,'false'),
    ('during',(150,200),(100,300),None,'true'),
    ('during',(150,301),(100,300),None,'false'),
])
def test_half_open_relation_boundaries(op,left,right,bound,expected):
    from app.programs.ast import Relation
    from app.programs.policy import TemporalPolicy
    relation=Relation(id='edge',op=op,from_step='place',to='use',within_ms=bound)
    state,_=relation_result(relation,node('place',*left),node('use',*right),TemporalPolicy())
    assert state==expected
