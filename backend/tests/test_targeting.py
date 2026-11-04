from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.targeting import TargetRequest,object_prompt


def test_point_and_box_prompts_use_exact_window_frame_and_inclusive_media_pixels():
    frame=uuid4()
    point=TargetRequest(evidence_frame_id=frame,x=.5,y=.25,negative_points=[{'x':1,'y':1}])
    assert object_prompt(point,37,320,180)=='<37f>1<|point;x=160;y=45;w=320;h=180|>-<|point;x=319;y=179;w=320;h=180|>'
    box=TargetRequest(evidence_frame_id=frame,mode='box',x=.25,y=.5,width=.25,height=.5)
    assert object_prompt(box,17,320,180)=='<17f>1<|box;x1=80;y1=90;x2=159;y2=179;w=320;h=180|>'
    with pytest.raises(ValidationError):TargetRequest(evidence_frame_id=frame,mode='box',x=.8,y=.2,width=.3,height=.1)
    with pytest.raises(ValidationError):TargetRequest(evidence_frame_id=frame,mode='point',x=.1,y=.2,width=.1)
