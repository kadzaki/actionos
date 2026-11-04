"""Inspectable declarative VisualProgram DSL; no event timestamps or code."""
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import ConfigDict, Field, field_validator, model_validator

from ..actions.contracts import ReportParameters
from ..editing import OutputSettings
from ..models import Model, now
from .policy import TemporalPolicy, UnknownPolicy
from .predicates import NodeId, PredicateStep, normalize_alias


class Entity(Model):
    id: NodeId
    description: str = Field(min_length=1, max_length=1000)
    role: Literal['actor', 'object', 'instrument', 'context']
    attributes: list[Annotated[str, Field(min_length=1, max_length=256)]] = Field(default_factory=list, max_length=16)


class Zone(Model):
    id: NodeId
    description: str = Field(min_length=1, max_length=1000)
    coordinate_space: Literal['normalized_source_display'] = 'normalized_source_display'
    x: float = Field(ge=0, le=1, strict=True)
    y: float = Field(ge=0, le=1, strict=True)
    width: float = Field(gt=0, le=1, strict=True)
    height: float = Field(gt=0, le=1, strict=True)

    @model_validator(mode='after')
    def bounded(self):
        if self.x + self.width > 1 or self.y + self.height > 1:
            raise ValueError('Image zone must lie fully within the normalized display')
        return self


class Relation(Model):
    model_config = ConfigDict(populate_by_name=True)
    id: NodeId
    op: Literal['before', 'after', 'then', 'within', 'during']
    from_step: NodeId = Field(alias='from')
    to: NodeId
    within_ms: int | None = Field(default=None, ge=0, le=120000, strict=True)

    @model_validator(mode='after')
    def bounded(self):
        if (self.op == 'within') != (self.within_ms is not None):
            raise ValueError('Only within relations require a within_ms bound')
        return self


class NodeReference(Model):
    kind: Literal['entity', 'zone', 'step', 'relation', 'exclusion', 'action',
                  'selection', 'unknown_policy', 'cycle']
    id: NodeId


class BooleanReference(Model):
    """Boolean operands are executable evidence, not entity declarations."""
    kind: Literal['step', 'relation']
    id: NodeId


class BooleanExpression(Model):
    model_config = ConfigDict(json_schema_extra={'allOf': [
        {'if': {'properties': {'op': {'const': 'ref'}}, 'required': ['op']},
         'then': {'properties': {'reference': {'type': 'object'}, 'args': {'maxItems': 0}}, 'required': ['reference']},
         'else': {'properties': {'reference': {'type': 'null'}}}},
        {'if': {'properties': {'op': {'const': 'not'}}, 'required': ['op']},
         'then': {'properties': {'args': {'minItems': 1, 'maxItems': 1}}, 'required': ['args']}},
        {'if': {'properties': {'op': {'enum': ['and', 'or']}}, 'required': ['op']},
         'then': {'properties': {'args': {'minItems': 2}}, 'required': ['args']}},
    ]})
    op: Literal['ref', 'not', 'and', 'or']
    reference: BooleanReference | None = Field(default=None,
        description='Only step or relation IDs have Boolean truth. Entity, zone and action IDs are not valid operands.')
    args: list['BooleanExpression'] = Field(default_factory=list, max_length=32)

    @model_validator(mode='after')
    def shape(self):
        if self.op == 'ref':
            if self.reference is None or self.args or self.reference.kind not in ('step', 'relation'):
                raise ValueError('Boolean reference must identify one predicate/relation')
        elif self.reference is not None or (len(self.args) != 1 if self.op == 'not' else len(self.args) < 2):
            raise ValueError('Boolean expression has invalid operands')
        return self


class Exclusion(Model):
    id: NodeId
    expression: BooleanExpression
    source_wording: str = Field(min_length=1, max_length=2000)


class Selection(Model):
    occurrences: Literal['all', 'first', 'second', 'last', 'ordinal'] = 'all'
    ordinal: int | None = Field(default=None, ge=1, le=256, strict=True)

    @model_validator(mode='after')
    def count(self):
        if (self.occurrences == 'ordinal') != (self.ordinal is not None):
            raise ValueError('Only ordinal selection accepts/requires an ordinal number')
        return self


class Cycle(Model):
    root_step_id: NodeId
    policy: TemporalPolicy = Field(default_factory=TemporalPolicy)


class ClipDeclarationParameters(Model):
    # Keep the cross-field validator visible in the schema sent to the compiler.
    # Omitted framing defaults to full_frame, so it must also take the else arm.
    model_config = ConfigDict(json_schema_extra={'allOf': [{
        'if': {'properties': {'framing': {'const': 'track_relevant_entities'}}, 'required': ['framing']},
        'then': {'properties': {'entity_ids': {'minItems': 1}}, 'required': ['entity_ids']},
        'else': {'properties': {'entity_ids': {'maxItems': 0}}},
    }]})
    pre_ms: int = Field(default=0, ge=0, le=10000, strict=True)
    post_ms: int = Field(default=0, ge=0, le=10000, strict=True)
    framing: Literal['full_frame', 'fixed', 'track_relevant_entities'] = 'full_frame'
    entity_ids: list[NodeId] = Field(default_factory=list, max_length=16,
        description='Required and nonempty only for track_relevant_entities framing; empty for full_frame or fixed.')
    output: OutputSettings = Field(default_factory=OutputSettings)

    @model_validator(mode='after')
    def selection(self):
        if (self.framing == 'track_relevant_entities') != bool(self.entity_ids):
            raise ValueError('Only entity-aware framing accepts/requires relevant entity IDs')
        return self


class ExtractDeclaration(Model):
    id: NodeId
    type: Literal['extract_clip']
    when: Literal['match', 'review', 'both'] = 'match'
    parameters: ClipDeclarationParameters = Field(default_factory=ClipDeclarationParameters)


class ReportDeclaration(Model):
    id: NodeId
    type: Literal['create_report']
    when: Literal['match', 'review', 'both'] = 'both'
    parameters: ReportParameters = Field(default_factory=ReportParameters)


ProgramAction = Annotated[ExtractDeclaration | ReportDeclaration, Field(discriminator='type')]


class InterpretationIssue(Model):
    code: str = Field(pattern=r'^[A-Z][A-Z0-9_]{0,63}$')
    message: str = Field(min_length=1, max_length=2000)
    material: bool = True
    node: NodeReference | None = None


class ClauseBinding(Model):
    start_char: int = Field(ge=0, le=8000, strict=True)
    end_char: int = Field(gt=0, le=8000, strict=True)
    text: str = Field(min_length=1, max_length=8000)
    status: Literal['represented', 'ambiguous', 'unsupported', 'unrepresented']
    represented_by: list[NodeReference] = Field(default_factory=list, max_length=32)
    note: str = Field(default='', max_length=2000)


class ProgramAST(Model):
    schema_version: Literal['1.0'] = '1.0'
    instruction: str = Field(min_length=1, max_length=8000)
    entities: list[Entity] = Field(min_length=1, max_length=16)
    zones: list[Zone] = Field(default_factory=list, max_length=16)
    steps: list[PredicateStep] = Field(min_length=1, max_length=32)
    relations: list[Relation] = Field(default_factory=list, max_length=64)
    cycle: Cycle
    selection: Selection = Field(default_factory=Selection)
    exclusions: list[Exclusion] = Field(default_factory=list, max_length=16)
    unknown_policy: UnknownPolicy = Field(default_factory=UnknownPolicy)
    actions: list[ProgramAction] = Field(default_factory=list, max_length=2)
    assumptions: list[InterpretationIssue] = Field(default_factory=list, max_length=32)
    ambiguities: list[InterpretationIssue] = Field(default_factory=list, max_length=32)
    unrepresented_clauses: list[InterpretationIssue] = Field(default_factory=list, max_length=32)
    clauses: list[ClauseBinding] = Field(default_factory=list, max_length=64)

    @field_validator('instruction')
    @classmethod
    def meaningful(cls, value):
        if not value.strip():
            raise ValueError('Instruction cannot be blank')
        return value  # Preserve exact wording and offsets.

    @model_validator(mode='before')
    @classmethod
    def lower_aliases(cls, value):
        if isinstance(value, dict) and isinstance(value.get('steps'), list):
            value = {**value, 'steps': [normalize_alias(step) for step in value['steps']]}
        if isinstance(value, dict) and isinstance(value.get('relations'), list):
            relations = []
            for relation in value['relations']:
                if (isinstance(relation, dict) and relation.get('op') == 'before'
                        and relation.get('within_ms') is not None and isinstance(relation.get('id'), str)):
                    relations.append({**relation, 'within_ms': None})
                    relations.append({**relation, 'op': 'within', 'id': relation['id']+'_within'})
                else:
                    relations.append(relation)
            value = {**value, 'relations': relations}
        return value


class CompilerProvenance(Model):
    origin: Literal['meta_cloud', 'user_edit', 'test_fixture']
    compiler_version: str = Field(min_length=1, max_length=64)
    prompt_version: str = Field(min_length=1, max_length=64)
    requested_model: str | None = Field(default=None, max_length=128)
    returned_model: str | None = Field(default=None, max_length=128)
    response_id: str | None = Field(default=None, max_length=256)
    endpoint: Literal['https://api.meta.ai/v1'] | None = None
    usage: dict = Field(default_factory=dict)
    compiled_at: datetime = Field(default_factory=now)


class VisualProgram(Model):
    program_id: UUID = Field(default_factory=uuid4)
    version: int = Field(default=1, ge=1, strict=True)
    project_id: UUID
    source_asset_id: UUID
    source_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    ast: ProgramAST
    compiler: CompilerProvenance
    explanation: str = Field(default='', max_length=8000)
