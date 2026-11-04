"""Recorded Muse schema regressions; not independent recognition annotations."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from app.engine.observations import ObservationResponse

FIXTURES=Path(__file__).parent/'fixtures/meta'


@pytest.mark.parametrize('window',['w000','w001','w002','w003','w004'])
def test_actual_video_response_preserves_provider_assessments_and_input_frame_bounds(window):
    recording=json.loads((FIXTURES/f'muse-step-observations-{window}.json').read_text(encoding='utf-8'))
    original=deepcopy(recording['output'])
    response=ObservationResponse.model_validate(recording['output'])
    assert response.schema_version=='predicate-observations-1'
    assert {h.step_id for h in response.hypotheses}=={'s_place','s_use_tool'}
    assert len(response.hypotheses)==2
    # These recordings each use a twelve-second, 30fps input, not source FPS.
    for hypothesis in response.hypotheses:
        if hypothesis.interval:
            assert 0<=hypothesis.interval.start_frame<hypothesis.interval.stop_frame<=360
        assert all(0<=index<360 for index in hypothesis.evidence_frame_indices)
        raw=next(h for h in original['hypotheses'] if h['step_id']==hypothesis.step_id)
        assert hypothesis.assessment==raw['assessment']
        assert hypothesis.uncertainty==raw.get('uncertainty',[])
    assert recording['output']==original
    assert recording['provenance']['provider']=='meta_cloud'
    assert recording['provenance']['remote_file_deleted'] is True


@pytest.mark.parametrize('window',['w000','w001'])
def test_actual_adapted_responses_retain_distinct_steps_repeats_and_uncertainty(window):
    recording=json.loads((FIXTURES/f'muse-adapted-observations-{window}.json').read_text(encoding='utf-8'))
    original=deepcopy(recording['output'])
    response=ObservationResponse.model_validate(recording['output'])
    assert {h.step_id for h in response.hypotheses}=={'engage','turning','withdrawal'}
    assert len(response.hypotheses)==len(original['hypotheses'])
    for parsed,raw in zip(response.hypotheses,original['hypotheses']):
        assert parsed.assessment==raw['assessment'] and parsed.uncertainty==raw.get('uncertainty',[])
        if parsed.interval:assert parsed.interval.stop_frame<=360
        assert all(i<360 for i in parsed.evidence_frame_indices)
    assert recording['output']==original
