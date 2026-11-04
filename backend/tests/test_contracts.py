from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.models import ActionEvent, ActionIntent, Occurrence


def test_action_vocabulary_is_open_and_clauses_survive():
    intent = ActionIntent(project_id=uuid4(), source_asset_id=uuid4(),
                          original_prompt="The last time a robot folds a napkin, excluding failed folds",
                          action_description="robot folds a napkin", subjects=["robot", "napkin"],
                          occurrence=Occurrence(mode="last"), exclusions=["failed folds"], context_after_ms=1200)
    assert ActionIntent.model_validate_json(intent.model_dump_json()) == intent
    assert intent.action_description == "robot folds a napkin"


def test_ordinal_number_is_required_only_for_ordinal_mode():
    for value in ({"mode": "ordinal"}, {"mode": "all", "ordinal": 2}):
        with pytest.raises(ValidationError):
            Occurrence(**value)
    assert Occurrence(mode="ordinal", ordinal=2).ordinal == 2


def test_dynamic_phases_are_chronological_and_evidence_bounded():
    data = dict(project_id=uuid4(), source_asset_id=uuid4(), intent_id=uuid4(),
                action_label="cat opens a cupboard", start_ms=1000, end_ms=3000,
                match_reason="Paw pulls handle", evidence_ms=[2200],
                phases=[dict(id="pull", label="handle pulled", role="key_moment", time_ms=2000,
                             status="estimated", evidence_ms=[2000])])
    assert len(ActionEvent(**data).phases) == 1
    with pytest.raises(ValidationError):
        ActionEvent(**{**data, "evidence_ms": [999]})
    with pytest.raises(ValidationError):
        ActionEvent(**{**data, "phases": data["phases"] * 2})


def test_config_rejects_extra_workers_non_cloud_endpoints_and_nonpositive_limits():
    for kwargs in ({"max_concurrent_jobs": 2}, {"meta_api_base_url": "http://localhost:8080/v1"},
                   {"meta_api_base_url": "https://api.meta.ai.evil.test/v1"}, {"max_video_seconds": 0}):
        with pytest.raises(ValueError):
            Settings(**kwargs)
    assert "secret" not in repr(Settings(model_api_key="secret"))
