"""Reconcile window hypotheses without inventing evidence or occurrence times."""
from uuid import UUID,uuid4

from pydantic import Field

from .errors import AppError
from .models import ActionEvent,Model,Phase,Record


class PhaseReference(Model):
    candidate_id: UUID
    phase_id: str


class CandidateGroup(Model):
    candidate_ids: list[UUID] = Field(min_length=1,max_length=24)
    matches_intent: bool
    action_label: str = Field(min_length=1,max_length=1000)
    match_reason: str = Field(min_length=1,max_length=2000)
    phase_references: list[PhaseReference] = Field(default_factory=list,max_length=24)
    uncertainty: list[str] = Field(default_factory=list,max_length=12)


class Reconciliation(Model):
    groups: list[CandidateGroup] = Field(max_length=400)
    notes: list[str] = Field(default_factory=list,max_length=24)


class AnalysisRecord(Record):
    project_id: UUID
    source_asset_id: UUID
    intent_id: UUID
    status: str = 'discovered'
    windows: list[dict]
    candidates: list[dict]
    reconciliation: dict = Field(default_factory=dict)
    provider_metadata: dict = Field(default_factory=dict)


def merge_groups(events: list[ActionEvent],result: Reconciliation,analysis_id,provenance):
    by_id={e.id:e for e in events}
    supplied=[identifier for group in result.groups for identifier in group.candidate_ids]
    if len(supplied)!=len(set(supplied)) or set(supplied)!=set(by_id):
        raise AppError('PROVIDER_SCHEMA','Occurrence reconciliation must account for every candidate exactly once',502,True)
    merged=[]
    for group in result.groups:
        members=[by_id[identifier] for identifier in group.candidate_ids]
        source=members[0]
        if any((e.project_id,e.source_asset_id,e.intent_id)!=(source.project_id,source.source_asset_id,source.intent_id) for e in members):
            raise AppError('OWNERSHIP','Occurrence candidates belong to different analysis sources',422)
        if len(members)>1:
            windows=[e.provider_metadata.get('window_id') for e in members]
            if None in windows or len(windows)!=len(set(windows)):
                raise AppError('PROVIDER_SCHEMA','Distinct occurrences from one source window cannot be collapsed',502,True)
            ordered=sorted(members,key=lambda e:e.start_ms)
            covered=ordered[0].end_ms
            for event in ordered[1:]:
                if event.start_ms>covered:
                    raise AppError('PROVIDER_SCHEMA','Occurrence merge crosses an unobserved time gap',502,True)
                covered=max(covered,event.end_ms)
        phases=[]
        seen=set()
        for reference in group.phase_references:
            key=(reference.candidate_id,reference.phase_id)
            if key in seen or reference.candidate_id not in group.candidate_ids:
                raise AppError('PROVIDER_SCHEMA','Reconciled phase has invalid candidate lineage',502)
            seen.add(key)
            original=next((p for p in by_id[reference.candidate_id].phases if p.id==reference.phase_id),None)
            if original is None:raise AppError('PROVIDER_SCHEMA','Reconciliation referenced an absent phase',502)
            phases.append(original)
        if not group.matches_intent:continue
        phases=[Phase.model_validate({**p.model_dump(),'id':f'phase_{i+1}'})
                for i,p in enumerate(sorted(phases,key=lambda p:p.time_ms))]
        data=source.model_dump()
        data.update(id=uuid4(),action_label=group.action_label,start_ms=min(e.start_ms for e in members),
                    end_ms=max(e.end_ms for e in members),phases=phases,match_reason=group.match_reason,
                    evidence_ms=sorted({t for e in members for t in e.evidence_ms}),
                    context_subjects=list(dict.fromkeys(s for e in members for s in e.context_subjects)),
                    warnings=sorted({w for e in members for w in e.warnings}|set(group.uncertainty)),
                    provider_metadata={**provenance,'analysis_id':str(analysis_id),
                                       'candidate_ids':[str(e.id) for e in members],
                                       'window_ids':[e.provider_metadata['window_id'] for e in members],
                                       'phase_sources':[r.model_dump(mode='json') for r in group.phase_references]})
        merged.append(ActionEvent.model_validate(data))
    return sorted(merged,key=lambda e:(e.start_ms,e.end_ms))
