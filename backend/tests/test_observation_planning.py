from uuid import uuid4

import pytest

from app.engine.planning import build_plan, initial_coverage, begin_window, finish_window, plan_run
from app.errors import AppError
from app.models import Geometry
from app.programs.compiler import Compilation
from app.programs.repository import approve_program, save_compilation
from app.programs.validation import validate_program
from app.repository import Repository, fingerprint
from app.runs import snapshot_run
from app.timing import SourceTimeline
from test_evidence import prepare
from test_program_ast import draft


def timeline(duration_ms=30000,source_fps=30):
    original_count=duration_ms*source_fps//1000
    working_count=duration_ms*30//1000
    return SourceTimeline(origin_pts_seconds=0,source_time_convention='test fixture elapsed source',
        source_frame_times_ms=[i*1000/source_fps for i in range(original_count)],
        source_frame_pts=list(range(original_count)),source_time_base=f'1/{source_fps}',source_duration_ms=duration_ms,
        source_geometry=Geometry(coded_width=320,coded_height=180,display_width=320,display_height=180,rotation_degrees=0,sample_aspect_ratio=1),
        working_to_source='test fixture actual identity map',working_frame_times_ms=[i*1000/30 for i in range(working_count)],
        working_frame_source_indices=[min(original_count-1,i*source_fps//30) for i in range(working_count)],
        source_asset_id=uuid4(),source_sha256='a'*64)


def plan(raw=None,source=None,**changes):
    source=source or timeline()
    values={'run_id':uuid4(),'program_fingerprint':'b'*64,'duration_ms':source.source_duration_ms,
        'working_duration_ms':source.source_duration_ms,'timeline_sha256':'c'*64,'proxy_sha256':'d'*64,
        'proxy_width':320,'proxy_height':180,'providers':{'muse_model':'fixture','sam_model':'fixture'}}
    values.update(changes)
    return build_plan(raw or draft(),source,**values)


def test_full_source_windows_and_predicate_requirements_are_bounded_and_fingerprinted():
    result=plan()
    assert [(w.source_interval.start_ms,w.source_interval.end_ms) for w in result.windows]==[(0,12000),(10000,22000),(20000,30000)]
    assert result.full_source_scan_required
    assert result.muse_requests==3 and result.sam_requests==9 and result.planned_provider_requests==12
    assert result.max_provider_requests==64
    assert all(r.semantic_support_required and r.negative_requirements for r in result.requirements)
    assert result.requirements[0].phrase=='the operator places the component'
    source=timeline()
    same=plan(source=source,run_id=result.run_id)
    repeated=plan(source=source,run_id=result.run_id)
    assert same.request_fingerprint==repeated.request_fingerprint
    assert plan(source=source,program_fingerprint='e'*64).request_fingerprint!=same.request_fingerprint


def test_elapsed_scan_does_not_create_observed_coverage_or_negative_proof():
    result=plan()
    ledger=initial_coverage(result)
    assert not ledger.scan_complete and not ledger.processing_complete
    for identifier in ledger.windows:
        begin_window(ledger,identifier);finish_window(ledger,identifier)
    assert ledger.scan_complete and ledger.processing_complete
    for coverage in ledger.predicates.values():
        assert not coverage.sufficient_negative and coverage.visibility=='unknown'
        assert coverage.observed_intervals==[] and coverage.identity=='unknown'
    with pytest.raises(AppError):begin_window(ledger,'foreign')
    with pytest.raises(AppError):finish_window(ledger,next(iter(ledger.windows)))


def test_failed_windows_are_processing_complete_but_not_a_complete_scan():
    ledger=initial_coverage(plan(source=timeline(3000)))
    begin_window(ledger,'w000');finish_window(ledger,'w000',error_code='PROVIDER_TIMEOUT')
    assert ledger.processing_complete and not ledger.scan_complete
    assert ledger.windows['w000'].error_code=='PROVIDER_TIMEOUT'
    assert ledger.predicates['use'].observed_intervals==[]


def test_high_frame_rate_omissions_and_prepared_tail_are_explicit():
    result=plan(source=timeline(3000,120))
    assert result.windows[0].omitted_original_frames==270
    assert not result.windows[0].sampling_covers_encoded_source
    tail=plan(source=timeline(3000),working_duration_ms=2990)
    assert tail.windows[0].unrepresented_tail_ms==10
    assert not tail.windows[0].sampling_covers_encoded_source


def test_provider_budget_rejection_does_not_truncate_the_source_or_entities():
    raw=draft('vehicle')
    raw['entities']=[{'id':f'entity_{i}','role':'object','description':f'object {i}'} for i in range(16)]
    raw['steps']=[{'id':f'step_{i}','predicate':'visible','entities':[f'entity_{i}']} for i in range(16)]
    raw['cycle']['root_step_id']='step_0';raw['actions']=[]
    with pytest.raises(AppError) as exc:plan(raw,source=timeline(120000))
    assert exc.value.code=='OBSERVATION_BUDGET_EXCEEDED'
    assert exc.value.details=={'muse_requests':12,'sam_requests':192,'limit':64}


def test_real_vfr_source_plan_persists_exact_mapping_and_reuses_snapshot(client,settings,media):
    asset=prepare(client,media['vfr']);store=client.app.state.store
    raw=draft();raw['clauses']=[{'start_char':0,'end_char':len(raw['instruction']),'text':raw['instruction'],
        'status':'represented','represented_by':[{'kind':'step','id':'place'},{'kind':'step','id':'use'}]}]
    validation=validate_program(raw,source_duration_ms=asset.metadata.duration_ms,require_clauses=True)
    compilation=Compilation(original_instruction=raw['instruction'],source={'duration_ms':asset.metadata.duration_ms,
        'width':320,'height':180,'audio_present':False},draft={'instruction':raw['instruction'],'ast':raw},validation=validation,
        compiler={'origin':'test_fixture','compiler_version':'fixture','prompt_version':'fixture'},latency_seconds=0,provider_output={'fixture_only':True})
    _,program=save_compilation(store,asset,uuid4(),compilation)
    approve_program(store,settings,asset.project_id,program.id,{'version':1,'program_fingerprint':fingerprint(program),'source_sha256':asset.sha256})
    run=snapshot_run(store,settings,asset.project_id,program.id,1,muse_adapter_version='fixture',sam_adapter_version='fixture',evaluator_version='fixture-no-evaluation')
    first=plan_run(store,settings,asset.project_id,run.id)
    again=plan_run(store,settings,asset.project_id,run.id)
    assert first==again
    saved=Repository(store).load(asset.project_id,'observation_plans',first.id)
    assert saved.payload['plan']['windows'][0]['input_source_frame_indices']
    assert all(w['state']=='pending' for w in saved.payload['coverage']['windows'].values())
    assert not list(store.path(asset.project_id,'observations').glob('*.json'))
