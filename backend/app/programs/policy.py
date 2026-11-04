"""Frozen DSL 1.0 policy, independent of storage schema and provider claims."""
from typing import Literal

from pydantic import Field, model_validator

from ..models import Model

DSL_VERSION = '1.0'
POLICY_VERSION = 'actionos-policy-1'
EVIDENCE_POLICY_VERSION = 'predicate-evidence-policy-1'

# Application limits; these are not provider quotas.
LIMITS = {
    'instruction_chars': 8000, 'ast_bytes': 131072, 'ast_depth': 12,
    'entities': 16, 'zones': 16, 'steps': 32, 'relations': 64,
    'exclusions': 16, 'actions': 2, 'windows': 24,
    'window_ms': 12000, 'source_ms': 120000, 'observations': 4096,
    'events': 256, 'action_events': 32, 'provider_calls': 64,
    'trace_entries': 1024, 'patch_operations': 64,
}


class SourceInterval(Model):
    """Half-open source-time milliseconds; frames are separate point evidence."""
    start_ms: int = Field(ge=0, le=LIMITS['source_ms'], strict=True)
    end_ms: int = Field(gt=0, le=LIMITS['source_ms'], strict=True)

    @model_validator(mode='after')
    def ordered(self):
        if self.end_ms <= self.start_ms:
            raise ValueError('Source interval must have positive duration')
        return self


class TemporalPolicy(Model):
    version: Literal['actionos-policy-1'] = POLICY_VERSION
    endpoint_convention: Literal['half_open'] = 'half_open'
    overlap_tolerance_ms: int = Field(default=0, ge=0, le=500, strict=True)
    max_cycle_ms: int = Field(default=12000, gt=0, le=LIMITS['source_ms'], strict=True)
    max_gap_ms: int = Field(default=12000, ge=0, le=LIMITS['source_ms'], strict=True)

    @model_validator(mode='after')
    def consistent(self):
        if self.overlap_tolerance_ms > self.max_cycle_ms:
            raise ValueError('Overlap tolerance cannot exceed cycle duration')
        if self.max_gap_ms > self.max_cycle_ms:
            raise ValueError('Gap bound cannot exceed cycle duration')
        return self


class UnknownPolicy(Model):
    observation: Literal['require_observed', 'allow_estimated'] = 'require_observed'
    presentation: Literal['needs_review'] = 'needs_review'
    # Coverage cannot be loosened by changing presentation or crop preferences.
    negative_coverage: Literal['complete_predicate_window'] = 'complete_predicate_window'
