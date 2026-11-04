"""Deterministic strong Kleene truth; missing evidence never becomes false."""
from enum import Enum


class Truth(str,Enum):
    TRUE='true'
    FALSE='false'
    UNKNOWN='unknown'


def conjunction(values):
    values=[Truth(v) for v in values]
    if Truth.FALSE in values:return Truth.FALSE
    return Truth.UNKNOWN if Truth.UNKNOWN in values else Truth.TRUE


def disjunction(values):
    values=[Truth(v) for v in values]
    if Truth.TRUE in values:return Truth.TRUE
    return Truth.UNKNOWN if Truth.UNKNOWN in values else Truth.FALSE


def negate(value):
    return {Truth.TRUE:Truth.FALSE,Truth.FALSE:Truth.TRUE,Truth.UNKNOWN:Truth.UNKNOWN}[Truth(value)]
