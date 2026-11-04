"""DSL 1.0 executable signatures, independent of open action descriptions."""
from copy import deepcopy
from typing import Annotated, Literal

from pydantic import Field, model_validator

from ..models import Model

NodeId = Annotated[str, Field(pattern=r'^[a-z][a-z0-9_]{0,63}$')]
Ratio = Annotated[float, Field(gt=0, le=1, strict=True)]


class EmptyParameters(Model):
    pass


class AttributeParameters(Model):
    attribute: str = Field(min_length=1, max_length=256)


class NearParameters(Model):
    distance_ratio: Ratio


class OverlapParameters(Model):
    min_iou: Ratio


class MoveParameters(Model):
    min_displacement_ratio: Ratio
    duration_ms: int = Field(gt=0, le=120000, strict=True)


class DistanceChangeParameters(Model):
    min_distance_change_ratio: Ratio
    duration_ms: int = Field(gt=0, le=120000, strict=True)


class Node(Model):
    id: NodeId
    anchor: Literal['interval', 'cycle_start'] = 'interval'
    source_wording: str = Field(default='', max_length=2000)
    original_operator: Literal['placed', 'picked_up', 'tool_contact', 'located'] | None = None


class EntityNode(Node):
    entities: list[NodeId] = Field(min_length=1, max_length=8)

    @model_validator(mode='after')
    def unique_entities(self):
        if len(set(self.entities)) != len(self.entities):
            raise ValueError('Predicate entity arguments must be distinct')
        return self


class PresenceStep(EntityNode):
    predicate: Literal['visible', 'present']
    entities: list[NodeId] = Field(min_length=1, max_length=1)
    parameters: EmptyParameters = Field(default_factory=EmptyParameters)


class AttributeStep(EntityNode):
    predicate: Literal['matches_attribute']
    entities: list[NodeId] = Field(min_length=1, max_length=1)
    parameters: AttributeParameters


class BinaryStep(EntityNode):
    predicate: Literal['left_of', 'touches', 'holds', 'releases']
    entities: list[NodeId] = Field(min_length=2, max_length=2)
    parameters: EmptyParameters = Field(default_factory=EmptyParameters)


class InsideStep(EntityNode):
    predicate: Literal['inside']
    entities: list[NodeId] = Field(min_length=1, max_length=1)
    zone_id: NodeId
    parameters: EmptyParameters = Field(default_factory=EmptyParameters)


class NearStep(EntityNode):
    predicate: Literal['near']
    entities: list[NodeId] = Field(min_length=2, max_length=2)
    parameters: NearParameters


class OverlapStep(EntityNode):
    predicate: Literal['overlap']
    entities: list[NodeId] = Field(min_length=2, max_length=2)
    parameters: OverlapParameters


class MoveStep(EntityNode):
    predicate: Literal['moves']
    entities: list[NodeId] = Field(min_length=1, max_length=1)
    parameters: MoveParameters


class DistanceChangeStep(EntityNode):
    predicate: Literal['approaches', 'separates']
    entities: list[NodeId] = Field(min_length=2, max_length=2)
    parameters: DistanceChangeParameters


class SemanticStep(EntityNode):
    predicate: Literal['semantic_event']
    phrase: str = Field(min_length=1, max_length=1000)
    zone_id: NodeId | None = None
    parameters: EmptyParameters = Field(default_factory=EmptyParameters)


PredicateStep = Annotated[PresenceStep | AttributeStep | BinaryStep | InsideStep | NearStep
    | OverlapStep | MoveStep | DistanceChangeStep | SemanticStep, Field(discriminator='predicate')]


def normalize_alias(step):
    """Deterministic syntax lowering; never reinterpret an unfamiliar phrase."""
    if not isinstance(step, dict):
        return step
    value = deepcopy(step)
    op = value.get('predicate')
    if op not in ('placed', 'picked_up', 'tool_contact', 'located'):
        return value
    value['original_operator'] = op
    if 'arguments' in value:
        if 'entities' in value or 'zone_id' in value:
            raise ValueError('Alias cannot combine canonical and positional arguments')
        arguments = value.pop('arguments')
        count = 1 if op == 'picked_up' else 2
        if not isinstance(arguments, list) or len(arguments) != count:
            raise ValueError('Alias has invalid argument count')
        if op in ('placed', 'located'):
            value['entities'], value['zone_id'] = arguments[:1], arguments[1]
        else:
            value['entities'] = arguments
    if op == 'located':
        value['predicate'] = 'inside'
    elif op == 'tool_contact':
        value['predicate'] = 'touches'
    else:
        if len(value.get('entities', [])) != 1 or (op == 'placed' and not value.get('zone_id')):
            raise ValueError('Placement/pickup alias requires its exact entity/zone signature')
        if op == 'picked_up' and 'zone_id' in value:
            raise ValueError('Pickup alias does not accept a zone')
        value['predicate'] = 'semantic_event'
        phrase = 'is picked up' if op == 'picked_up' else 'is placed in the zone'
        if value.get('phrase') not in (None, phrase):
            raise ValueError('Use semantic_event for an open phrase; an alias cannot change its defined meaning')
        value['phrase'] = phrase
    return value


CATALOG_VERSION = 'predicates-1.0'
CATALOG = {
    'visible': {'entities': 1, 'evidence': 'identified frame visibility'},
    'present': {'entities': 1, 'evidence': 'positive identified presence; no occlusion persistence'},
    'matches_attribute': {'entities': 1, 'parameters': ['attribute'], 'evidence': 'visible attribute detail'},
    'left_of': {'entities': 2, 'evidence': 'strict oriented image center x ordering'},
    'inside': {'entities': 1, 'zone': True, 'evidence': 'bbox center in half-open image zone'},
    'near': {'entities': 2, 'parameters': ['distance_ratio'], 'units': 'image diagonal'},
    'overlap': {'entities': 2, 'parameters': ['min_iou'], 'units': 'bbox IoU, not contact'},
    'moves': {'entities': 1, 'parameters': ['min_displacement_ratio', 'duration_ms'], 'units': 'image diagonal and ms'},
    'approaches': {'entities': 2, 'parameters': ['min_distance_change_ratio', 'duration_ms'], 'units': 'image diagonal and ms'},
    'separates': {'entities': 2, 'parameters': ['min_distance_change_ratio', 'duration_ms'], 'units': 'image diagonal and ms'},
    'touches': {'entities': 2, 'evidence': 'visible semantic contact plus compatible geometry, never overlap alone'},
    'holds': {'entities': 2, 'evidence': 'visible semantic possession plus compatible geometry'},
    'releases': {'entities': 2, 'evidence': 'visible semantic release plus compatible geometry'},
    'semantic_event': {'entities': '1-8', 'phrase': 'open text', 'evidence': 'visible semantic support plus entity/frame grounding'},
}
