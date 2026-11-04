from uuid import uuid4

import pytest

from app.analysis import choose_events
from app.errors import AppError
from app.models import ActionEvent,ActionIntent,Occurrence,Phase
from app.reconciliation import CandidateGroup,PhaseReference,Reconciliation,merge_groups


def candidates():
    project,source,intent=uuid4(),uuid4(),uuid4()
    def event(start,end,window,label):
        return ActionEvent(project_id=project,source_asset_id=source,intent_id=intent,
                           action_label=label,primary_subject='robot arm',start_ms=start,end_ms=end,
                           match_reason='visible source rotation',evidence_ms=[start+100],
                           phases=[Phase(id='phase_1',label='arm turns',time_ms=start+100,
                                         evidence_ms=[start+100],role='onset',status='estimated')],
                           provider_metadata={'window_id':window})
    return [event(9500,12000,'window_a','arm turns'),event(10000,13500,'window_b','robot rotates'),
            event(15000,17000,'window_b','arm turns again')]


def group(events,match=True):
    return CandidateGroup(candidate_ids=[e.id for e in events],matches_intent=match,
                          action_label='robot arm rotates',match_reason='The supplied overlapping source frames show one continuous turn',
                          phase_references=[PhaseReference(candidate_id=events[0].id,phase_id='phase_1')])


def test_synonymous_window_duplicates_merge_before_global_ordinal_selection():
    original=candidates();snapshots=[e.model_dump(mode='json') for e in original]
    result=Reconciliation(groups=[group(original[:2]),group(original[2:])])
    analysis=uuid4();merged=merge_groups(original,result,analysis,{'provider':'meta_cloud'})
    assert [(e.start_ms,e.end_ms) for e in merged]==[(9500,13500),(15000,17000)]
    assert merged[0].evidence_ms==[9600,10100]
    assert merged[0].provider_metadata['candidate_ids']==[str(e.id) for e in original[:2]]
    assert merged[0].provider_metadata['analysis_id']==str(analysis)
    intent=ActionIntent(id=original[0].intent_id,project_id=original[0].project_id,source_asset_id=original[0].source_asset_id,
                        original_prompt='second arm turn',action_description='robot arm turns',occurrence=Occurrence(mode='ordinal',ordinal=2))
    assert choose_events(intent,merged)==merged[1:]
    assert [e.model_dump(mode='json') for e in original]==snapshots


@pytest.mark.parametrize('kind',['missing','duplicate','unknown','same_window','time_gap','foreign_phase'])
def test_model_cannot_drop_candidates_collapse_repetitions_or_invent_lineage(kind):
    original=candidates();groups=[group(original[:2]),group(original[2:])]
    if kind=='missing':groups=groups[:1]
    if kind=='duplicate':groups.append(group(original[2:]))
    if kind=='unknown':groups[0].candidate_ids=[uuid4()]
    if kind=='same_window':original[1].provider_metadata['window_id']='window_a'
    if kind=='time_gap':groups=[group([original[0],original[2]]),group([original[1]])]
    if kind=='foreign_phase':groups[0].phase_references=[PhaseReference(candidate_id=original[2].id,phase_id='phase_1')]
    with pytest.raises(AppError):merge_groups(original,Reconciliation(groups=groups),uuid4(),{})


def test_exclusion_decisions_remain_explicit_and_unknown_actions_are_not_gated():
    original=candidates()
    result=Reconciliation(groups=[group(original[:2],match=False),group(original[2:])])
    accepted=merge_groups(original,result,uuid4(),{})
    assert len(accepted)==1 and accepted[0].start_ms==15000
    assert result.groups[0].candidate_ids==[e.id for e in original[:2]]
    assert merge_groups([],Reconciliation(groups=[]),uuid4(),{})==[]


def test_recorded_live_reconciliation_accounts_for_overlap_and_selects_second_actual_episode():
    import json
    from pathlib import Path
    recording=json.loads((Path(__file__).parent/'fixtures/meta/muse-occurrence-reconciliation.json').read_text())
    original=[ActionEvent.model_validate(e) for e in recording['candidates']]
    result=Reconciliation.model_validate(recording['reconciliation'])
    accepted=merge_groups(original,result,uuid4(),recording['provider_metadata'])
    assert len(original)==5 and len(accepted)==4
    assert sum(not g.matches_intent for g in result.groups)==1
    intent=ActionIntent(project_id=original[0].project_id,source_asset_id=original[0].source_asset_id,
                        original_prompt=recording['original_prompt'],action_description='red square travels right',
                        occurrence=Occurrence(mode='ordinal',ordinal=2))
    second=choose_events(intent,accepted)[0]
    assert 3500<=second.start_ms<=4500 and 6500<=second.end_ms<=7500
