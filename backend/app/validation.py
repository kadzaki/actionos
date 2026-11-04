"""Shared bounded-data and lineage checks, independent of domain vocabulary."""
import json

from .errors import AppError
from .models import Asset, Project
from .programs.policy import LIMITS

PREDICATES = frozenset(('visible', 'present', 'matches_attribute', 'left_of', 'inside',
    'near', 'overlap', 'moves', 'approaches', 'separates', 'touches', 'holds', 'releases',
    'semantic_event', 'located', 'placed', 'picked_up', 'tool_contact'))
P0_ACTIONS = frozenset(('extract_clip', 'create_report'))


def payload_limits(kind, payload):
    """Admission limits only; owning domain adapters still validate full types."""
    bounded_json(payload, max_bytes=LIMITS['ast_bytes'] if kind == 'programs' else 4*1024*1024)
    collections = {'programs': {'entities': 'entities', 'zones': 'zones', 'steps': 'steps',
        'relations': 'relations', 'exclusions': 'exclusions', 'actions': 'actions'},
        'observations': {'observations': 'observations'}, 'temporal_events': {'events': 'events'},
        'action_plans': {'event_ids': 'action_events'}}
    for field, limit in collections.get(kind, {}).items():
        values = payload.get(field, [])
        if not isinstance(values, list) or len(values) > LIMITS[limit]:
            raise AppError('RESOURCE_LIMIT', 'Payload collection exceeds its typed application quota', 422,
                           details={'field': field, 'limit': LIMITS[limit]})
    if kind == 'programs':
        if 'instruction' in payload and (not isinstance(payload['instruction'], str)
                or len(payload['instruction']) > LIMITS['instruction_chars']):
            raise AppError('RESOURCE_LIMIT', 'Instruction exceeds the application limit', 422)
        for step in payload.get('steps', []):
            if not isinstance(step, dict) or step.get('predicate') not in PREDICATES:
                raise AppError('UNSUPPORTED_OPERATOR', 'Program contains an unsupported executable predicate', 422)
        for action in payload.get('actions', []):
            if not isinstance(action, dict) or action.get('type') not in P0_ACTIONS:
                raise AppError('UNSUPPORTED_ACTION', 'Program contains an unavailable output action', 422)


def bounded_json(value, *, max_bytes, max_depth=12):
    pending = [(value, 0)]
    while pending:
        item, depth = pending.pop()
        if depth > max_depth:
            raise AppError('RESOURCE_LIMIT', 'JSON nesting exceeds the application limit', 422)
        if isinstance(item, dict):
            pending.extend((v, depth+1) for v in item.values())
        elif isinstance(item, (list, tuple)):
            pending.extend((v, depth+1) for v in item)
    try:
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8')
    except (ValueError, TypeError, RecursionError):
        raise AppError('VALIDATION_ERROR', 'Expected bounded JSON data', 422) from None
    if len(raw) > max_bytes:
        raise AppError('RESOURCE_LIMIT', 'JSON exceeds the application size limit', 422)
    return raw


class WorkBudget:
    """Share across provider clients/tools belonging to one run/job."""
    def __init__(self, provider_calls=LIMITS['provider_calls']):
        if type(provider_calls) is not int or not 1 <= provider_calls <= LIMITS['provider_calls']:
            raise AppError('RESOURCE_LIMIT', 'Invalid provider call budget', 422)
        self.provider_calls = provider_calls
        self.used = 0

    def consume_provider_call(self):
        if self.used >= self.provider_calls:
            raise AppError('PROVIDER_CALL_LIMIT', 'Run reached its provider request limit', 422)
        self.used += 1


def owned_source(store, project_id, source_id):
    store.load(project_id, 'project', Project)
    return store.load(project_id, 'assets', Asset, source_id)


def owned_record(store, project_id, source_id, kind, model, record_id):
    owned_source(store, project_id, source_id)
    record = store.load(project_id, kind, model, record_id)
    if getattr(record, 'source_asset_id', None) != source_id:
        raise AppError('OWNERSHIP', 'Record does not belong to the selected source', 422)
    return record
