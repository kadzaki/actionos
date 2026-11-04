"""Pure bounded interval/sequence evaluation over explicit, source-owned evidence."""
from itertools import product
from typing import Literal
from uuid import UUID,uuid5

from pydantic import Field,model_validator

from .truth import Truth,conjunction,disjunction,negate
from ..errors import AppError
from ..models import Model
from ..programs.policy import SourceInterval,LIMITS
from ..programs.validation import require_valid_program
from ..source_evidence import SourceEvidenceReference

EVALUATOR_VERSION='temporal-engine-2'
MAX_ASSIGNMENTS=4096


class NodeEvidence(Model):
    record_id: UUID
    step_id: str
    verdict: Truth
    interval: SourceInterval | None = None
    window: SourceInterval
    frames: list[SourceEvidenceReference] = Field(default_factory=list,max_length=32)
    identity_key: str | None = None
    identity_bindings: dict[str,str] = Field(default_factory=dict)
    lineage: list[UUID] = Field(default_factory=list)
    estimated: bool = False
    observed_candidate: bool = False
    negative_complete: bool = False
    reasons: list[str] = Field(default_factory=list,max_length=128)

    @model_validator(mode='after')
    def grounded(self):
        if self.interval and not self.window.start_ms<=self.interval.start_ms<self.interval.end_ms<=self.window.end_ms:
            raise ValueError('Evidence interval must lie inside its observed window')
        for frame in self.frames:
            at=frame.source_time_ms if frame.observed_at_source_ms is None else frame.observed_at_source_ms
            if not self.interval or not self.interval.start_ms<=at<self.interval.end_ms:
                raise ValueError('Witness must lie inside its evidence interval')
        if self.verdict==Truth.TRUE and (self.interval is None or not self.frames):
            raise ValueError('Positive interval evidence needs actual source witnesses')
        if self.verdict==Truth.FALSE and not self.negative_complete:
            raise ValueError('False predicates require verified bounded negative coverage')
        if self.observed_candidate and (self.interval is None or not self.frames):
            raise ValueError('Review candidates require actual observed intervals/witnesses')
        return self


class StepResult(Model):
    step_id: str
    verdict: Truth
    interval: SourceInterval | None = None
    evidence_ids: list[UUID] = Field(default_factory=list)
    frames: list[SourceEvidenceReference] = Field(default_factory=list)
    estimated: bool = False
    reasons: list[str] = Field(default_factory=list)


class EdgeResult(Model):
    relation_id: str
    op: str
    from_step: str
    to_step: str
    verdict: Truth
    reasons: list[str]
    evidence_ids: list[UUID]


class EvaluatedCycle(Model):
    id: UUID
    interval: SourceInterval
    root_evidence_ids: list[UUID]
    verdict: Truth
    rule_verdict: Truth | None = None
    needs_review: bool
    steps: list[StepResult]
    edges: list[EdgeResult]
    exclusions: dict[str,Truth]
    reasons: list[str]
    selected: bool = True
    selection_certain: bool = True


class Evaluation(Model):
    evaluator_version: Literal['temporal-engine-2'] = EVALUATOR_VERSION
    cycles: list[EvaluatedCycle]
    coverage_complete: bool
    outcome: Literal['matches','no_match','all_unknown','incomplete']
    reasons: list[str]
    candidate_count: int


def relation_result(relation,left,right,policy):
    if left.verdict!=Truth.TRUE or right.verdict!=Truth.TRUE or not left.interval or not right.interval:
        return Truth.UNKNOWN,['RELATION_ENDPOINT_UNVERIFIED']
    ok=_interval_relation(relation,left.interval,right.interval,policy)
    return (Truth.TRUE,[]) if ok else (Truth.FALSE,['OBSERVED_TEMPORAL_BOUND_VIOLATION'])


def _interval_relation(relation,a,b,policy):
    if relation.op=='after':a,b=b,a
    if relation.op=='during':ok=b.start_ms<=a.start_ms and a.end_ms<=b.end_ms
    else:
        ordered=a.end_ms<=b.start_ms+policy.overlap_tolerance_ms
        bound=relation.within_ms if relation.op=='within' else policy.max_gap_ms
        ok=ordered and (relation.op not in ('then','within') or max(0,b.start_ms-a.end_ms)<=bound)
    return ok


def _boolean(expression,steps,edges):
    if expression.op=='ref':
        source=steps if expression.reference.kind=='step' else edges
        return source.get(expression.reference.id,Truth.UNKNOWN)
    children=[_boolean(arg,steps,edges) for arg in expression.args]
    if expression.op=='not':return negate(children[0])
    return conjunction(children) if expression.op=='and' else disjunction(children)


def _merge_roots(roots):
    """Only merge duplicate observations with explicit identity AND shared witnesses."""
    result=[]
    for item in sorted(roots,key=lambda e:(e.interval.start_ms,e.interval.end_ms,str(e.record_id))):
        previous=result[-1] if result else None
        shared=previous and {f.evidence_frame_id for f in previous.frames}&{f.evidence_frame_id for f in item.frames}
        if (previous and shared and previous.identity_key and previous.identity_key==item.identity_key
                and previous.identity_bindings==item.identity_bindings
                and len({f.evidence_frame_id for f in [*previous.frames,*item.frames]})<=32
                and item.interval.start_ms<previous.interval.end_ms):
            frames={f.evidence_frame_id:f for f in [*previous.frames,*item.frames]}
            result[-1]=previous.model_copy(update={'interval':SourceInterval(start_ms=min(previous.interval.start_ms,item.interval.start_ms),
                end_ms=max(previous.interval.end_ms,item.interval.end_ms)),'frames':list(frames.values()),
                'window':SourceInterval(start_ms=min(previous.window.start_ms,item.window.start_ms),end_ms=max(previous.window.end_ms,item.window.end_ms)),
                'lineage':list(dict.fromkeys([previous.record_id,*previous.lineage,item.record_id,*item.lineage])),
                'verdict':conjunction([previous.verdict,item.verdict]),'estimated':previous.estimated or item.estimated})
        else:result.append(item)
    return result


SELECTION_REASON='OCCURRENCE_CANNOT_BE_CERTIFIED_WITH_INCOMPLETE_OR_UNKNOWN_PRECEDING_CANDIDATES'


def select_cycles(cycles,selection,*,coverage_complete):
    """Re-select a complete immutable result set, including after human edits."""
    cycles=sorted([c.model_copy(deep=True) for c in cycles],key=lambda c:(c.interval.start_ms,c.interval.end_ms,str(c.id)))
    for cycle in cycles:
        if cycle.rule_verdict is not None:cycle.verdict=cycle.rule_verdict
        cycle.reasons=[r for r in cycle.reasons if r!=SELECTION_REASON]
        cycle.selection_certain=True
        cycle.needs_review=cycle.verdict!=Truth.TRUE or any(s.estimated for s in cycle.steps)
        cycle.selected=False
    eligible=[c for c in cycles if c.verdict!=Truth.FALSE and not any(v==Truth.TRUE for v in c.exclusions.values())]
    if selection.occurrences=='all':
        for cycle in eligible:cycle.selected=True
        return cycles
    wanted={'first':1,'second':2,'last':len(eligible),'ordinal':selection.ordinal}.get(selection.occurrences)
    if wanted and wanted<=len(eligible):
        target=eligible[wanted-1];target.selected=True
        uncertain_prefix=selection.occurrences!='last' and any(c.verdict==Truth.UNKNOWN for c in eligible[:wanted-1])
        if not coverage_complete or uncertain_prefix:
            target.selection_certain=False;target.verdict=Truth.UNKNOWN;target.needs_review=True
            target.reasons.append(SELECTION_REASON)
    return cycles


def evaluate_program(ast,evidence,*,run_id,source_duration_ms,coverage_complete=False):
    ast=require_valid_program(ast,source_duration_ms=source_duration_ms)
    evidence=[NodeEvidence.model_validate(e) for e in evidence]
    if len(evidence)>LIMITS['observations']:
        raise AppError('OBSERVATION_LIMIT','Evaluation input exceeds the reviewed observation quota',422)
    step_map={s.id:s for s in ast.steps}
    if any(e.step_id not in step_map or e.window.end_ms>source_duration_ms for e in evidence):
        raise AppError('EVIDENCE_MISMATCH','Evaluation contains foreign nodes or out-of-source windows',409)
    grouped={step:[e for e in evidence if e.step_id==step] for step in step_map}
    root_id=ast.cycle.root_step_id
    roots=_merge_roots([e for e in grouped[root_id] if e.interval and e.frames and (e.verdict==Truth.TRUE or e.observed_candidate)])
    if len(roots)>LIMITS['events']:
        raise AppError('EVENT_LIMIT','Distinct observed roots exceed the event quota; no cycles were silently dropped',422)
    cycles=[];global_reasons=[]
    for number,root in enumerate(roots):
        # Interior anchors can have predecessors. Never search beyond source or
        # the bounded horizon; the complete assignment span is checked below.
        lo=max(0,root.interval.start_ms-ast.cycle.policy.max_cycle_ms)
        hi=min(source_duration_ms,root.interval.end_ms+ast.cycle.policy.max_cycle_ms)
        previous_root=roots[number-1] if number else None
        if (previous_root and previous_root.identity_bindings and previous_root.identity_bindings==root.identity_bindings
                and previous_root.interval.end_ms<=root.interval.start_ms):
            lo=max(lo,previous_root.interval.end_ms)
        next_root=roots[number+1] if number+1<len(roots) else None
        if next_root and next_root.interval.start_ms>=root.interval.end_ms:hi=min(hi,next_root.interval.start_ms)
        window=SourceInterval(start_ms=lo,end_ms=max(lo+1,hi))
        choices=[];ordered=list(ast.steps)
        for step in ordered:
            if step.id==root_id:options=[root]
            else:
                options=[e for e in grouped[step.id] if e.interval and lo<=e.interval.start_ms<e.interval.end_ms<=hi]
                negatives=sorted([e for e in grouped[step.id] if e.verdict==Truth.FALSE and e.negative_complete],key=lambda e:e.window.start_ms)
                cursor=lo;cover=[]
                for negative in negatives:
                    if negative.window.end_ms<=cursor:continue
                    if negative.window.start_ms>cursor:break
                    cursor=negative.window.end_ms;cover.append(negative)
                    if cursor>=hi:break
                if cursor>=hi and cover:
                    options.append(NodeEvidence(record_id=cover[0].record_id,step_id=step.id,verdict=Truth.FALSE,window=window,
                        negative_complete=True,lineage=list(dict.fromkeys(i for e in cover for i in [e.record_id,*e.lineage])),
                        reasons=['VERIFIED_NEGATIVE_COVERAGE_OF_COMPLETE_BOUNDED_CYCLE']))
                options.sort(key=lambda e:(0 if e.verdict==Truth.TRUE else 1,e.interval.start_ms if e.interval else lo,str(e.record_id)))
            # Missing evidence is an explicit unknown choice, not false.
            choices.append(options or [None])
        total=1
        for options in choices:total*=len(options)
        limited=total>MAX_ASSIGNMENTS
        if limited:global_reasons.append('ASSIGNMENT_SEARCH_LIMIT')
        evaluated=[]
        for index,assignment in enumerate(product(*choices)):
            if index>=MAX_ASSIGNMENTS:break
            steps=[];identities={}
            for step,item in zip(ordered,assignment):
                if item is None:
                    steps.append(StepResult(step_id=step.id,verdict=Truth.UNKNOWN,reasons=['STEP_NOT_OBSERVED_IN_BOUNDED_CYCLE']));continue
                state=item.verdict;reasons=list(item.reasons)
                for entity,key in (item.identity_bindings or ({'_legacy':item.identity_key} if item.identity_key else {})).items():
                    identities.setdefault(entity,set()).add(key)
                if state==Truth.TRUE and not (item.identity_key or item.identity_bindings):
                    state=Truth.UNKNOWN;reasons.append('ENTITY_IDENTITY_UNVERIFIED')
                duration=getattr(step.parameters,'duration_ms',None)
                if item.interval and duration and item.interval.end_ms-item.interval.start_ms<duration:
                    state=Truth.FALSE if state==Truth.TRUE else Truth.UNKNOWN;reasons.append('MINIMUM_DURATION_NOT_MET')
                steps.append(StepResult(step_id=step.id,verdict=state,interval=item.interval,evidence_ids=list(dict.fromkeys([item.record_id,*item.lineage])),
                    frames=item.frames,estimated=item.estimated,reasons=reasons))
            by_id={s.step_id:s for s in steps};edges=[]
            for relation in ast.relations:
                left,right=by_id[relation.from_step],by_id[relation.to]
                state,reasons=relation_result(relation,left,right,ast.cycle.policy)
                edges.append(EdgeResult(relation_id=relation.id,op=relation.op,from_step=relation.from_step,to_step=relation.to,
                    verdict=state,reasons=reasons,evidence_ids=sorted(set(left.evidence_ids+right.evidence_ids),key=str)))
            intervals=[s.interval for s in steps if s.interval]
            start=min(i.start_ms for i in intervals);end=max(i.end_ms for i in intervals)
            reasons=[];bounds=Truth.TRUE
            if end-start>ast.cycle.policy.max_cycle_ms:bounds=Truth.FALSE;reasons.append('MAX_CYCLE_SPAN_EXCEEDED')
            if any(len(keys)>1 for keys in identities.values()):bounds=Truth.UNKNOWN;reasons.append('CROSS_STEP_IDENTITY_AMBIGUOUS')
            if any(s.anchor=='cycle_start' and by_id[s.id].interval and by_id[s.id].interval.start_ms!=start for s in ast.steps):
                bounds=Truth.UNKNOWN;reasons.append('CYCLE_START_ANCHOR_UNVERIFIED')
            exclusions={e.id:_boolean(e.expression,{s.step_id:s.verdict for s in steps},{e.relation_id:e.verdict for e in edges}) for e in ast.exclusions}
            state=conjunction([*(s.verdict for s in steps),*(e.verdict for e in edges),bounds,negate(disjunction(exclusions.values()))])
            if state==Truth.FALSE and not coverage_complete:
                # A disproved pairing is retained on the edge, but incomplete
                # association cannot rule out another unseen compatible step.
                state=Truth.UNKNOWN;reasons.append('ASSOCIATION_COVERAGE_INCOMPLETE')
            if root.verdict!=Truth.TRUE or limited:
                state=Truth.UNKNOWN;reasons.append('UNVERIFIED_ROOT' if root.verdict!=Truth.TRUE else 'ASSIGNMENT_SEARCH_LIMIT')
            if state!=Truth.TRUE:reasons.extend(r for s in steps for r in s.reasons)
            rank={Truth.TRUE:2,Truth.UNKNOWN:1,Truth.FALSE:0}[state]
            # Prefer source-time-compatible, compact provisional associations.
            # This ranks review hypotheses; it never upgrades their truth.
            temporal_fit=sum(bool(by_id[r.from_step].interval and by_id[r.to].interval and
                _interval_relation(r,by_id[r.from_step].interval,by_id[r.to].interval,ast.cycle.policy)) for r in ast.relations)
            distance=sum(abs((i.start_ms+i.end_ms)-(root.interval.start_ms+root.interval.end_ms)) for i in intervals)
            score=(rank,sum(s.verdict==Truth.TRUE for s in steps),temporal_fit,-(end-start),-distance)
            evaluated.append((score,state,steps,edges,exclusions,start,end,sorted(set(reasons))))
        best=max(evaluated,key=lambda row:row[0])
        _,state,steps,edges,exclusions,start,end,reasons=best
        viable=[row for row in evaluated if row[0][:2]==best[0][:2]]
        if state==Truth.TRUE and len({tuple(tuple(str(x) for x in s.evidence_ids) for s in row[2]) for row in viable})>1:
            state=Truth.UNKNOWN;reasons.append('AMBIGUOUS_COMPATIBLE_ASSIGNMENT')
        # A span is a source interval, not a fabricated estimate of a missing step.
        identifier=uuid5(UUID(str(run_id)),f'{EVALUATOR_VERSION}:{root.record_id}:{root.interval.start_ms}:{root.interval.end_ms}')
        cycles.append(EvaluatedCycle(id=identifier,interval=SourceInterval(start_ms=start,end_ms=end),root_evidence_ids=list(dict.fromkeys([root.record_id,*root.lineage])),
            verdict=state,rule_verdict=state,needs_review=state!=Truth.TRUE or any(s.estimated for s in steps),steps=steps,edges=edges,
            exclusions=exclusions,reasons=reasons or ([] if state==Truth.TRUE else ['INSUFFICIENT_RULE_EVIDENCE'])))
    cycles=select_cycles(cycles,ast.selection,coverage_complete=coverage_complete)
    selected=[c for c in cycles if c.selected]
    if any(c.verdict==Truth.TRUE for c in selected):outcome='matches'
    elif any(c.verdict==Truth.UNKNOWN for c in selected):outcome='all_unknown'
    elif coverage_complete:outcome='no_match'
    else:outcome='incomplete'
    if not roots:global_reasons.append('NO_SUPPORTED_OR_SOURCE_GROUNDED_REVIEW_ROOT')
    if not coverage_complete:global_reasons.append('SOURCE_OR_PREDICATE_COVERAGE_INCOMPLETE')
    return Evaluation(cycles=cycles,coverage_complete=coverage_complete,outcome=outcome,reasons=sorted(set(global_reasons)),candidate_count=len(cycles))
