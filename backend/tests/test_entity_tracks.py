"""Generated media and labeled SAM replays test bindings, not tabletop recognition."""
from dataclasses import replace
import json
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from app.editing import Track
from app.engine.entity_tracks import (ENTITY_ADAPTER_VERSION,batch_fingerprint,compatibility_track,
    normalize_entity_tracks,observe_entity,select_candidate,selection_prompt)
from app.errors import AppError
from app.providers.meta import MetaCloud
from test_step_observations import context  # Reuse real VFR window/source fixture.

META={'provider':'meta_cloud','model':'recorded-test','response_id':'fixture-response','remote_file_deleted':True}


def wire(indices,objects=(1,),*,moving=False):
    lines=[]
    for index in indices:
        records=[]
        for object_id in objects:
            x=20+index if moving else 20
            records.append(f'{object_id}<|box;x1={x};y1=70;x2={x+39};y2=109;w=320;h=180|><|mask;x=0;y=0;data=320,180,~fixture|>')
        lines.append(f'<{index}f>'+','.join(records))
    return '\n'.join(lines)


def normalized(context,indices=(0,1,3,4),objects=(1,),**kwargs):
    return normalize_entity_tracks(context,'component',wire(indices,objects,**kwargs),META)


def test_identical_candidates_require_selection_and_gaps_split_identity_scope(context):
    batch=normalized(context,objects=(1,2))
    assert batch.status=='candidates' and len(batch.candidates)==2
    assert batch.negative_coverage_verified is False
    for candidate in batch.candidates:
        assert candidate.identity=='unknown' and candidate.selection_required
        assert [(s.input_frames.start_frame,s.input_frames.stop_frame) for s in candidate.segments]==[(0,2),(3,5)]
        assert candidate.lost_at_window_end and candidate.missing_input_ranges[0].start_frame==2
        assert 'MULTIPLE_ENTITY_CANDIDATES' in candidate.uncertainty
        assert 'IDENTITY_NOT_PROPAGATED_ACROSS_MISSING_SAMPLES' in candidate.uncertainty
        for sample in candidate.samples:
            source=context.window.source_frame_indices[sample.input_frame_index]
            assert sample.source_frame_index==source
            assert sample.source_time_ms==context.source.timeline.source_frame_times_ms[source]
            assert sample.source_pts==context.source.timeline.source_frame_pts[source]
        assert candidate.samples[0].width==pytest.approx(40/320)
        assert candidate.samples[0].height==pytest.approx(40/180)
    assert batch.transforms[-1].to_space=='normalized_source_display'


def test_provider_id_is_not_global_across_responses_or_entities(context):
    first=normalized(context)
    second=normalize_entity_tracks(context,'component',wire([0,1,3,4]),{**META,'response_id':'another-response'})
    other_entity=normalize_entity_tracks(context,'tool',wire([0,1,3,4]),META)
    assert len({b.candidates[0].id for b in [first,second,other_entity]})==3
    assert first.candidates[0].id==normalized(context).candidates[0].id
    assert all(b.candidates[0].identity=='unknown' for b in [first,second,other_entity])


def test_single_noun_candidate_is_still_unknown_and_empty_output_is_not_absence(context):
    batch=normalized(context)
    assert batch.candidates[0].identity=='unknown'
    empty=normalize_entity_tracks(context,'component','',META)
    assert empty.status=='not_observed' and empty.candidates==[]
    assert not empty.negative_coverage_verified and 'absence is unverified' in empty.uncertainty[0]


def selection_request(context,batch,index=0):
    candidate=batch.candidates[0];segment=candidate.segments[0]
    frame=context.source.frame(0,source_frame_index=context.window.source_frame_indices[index])
    return {'batch_fingerprint':batch_fingerprint(batch),'candidate_id':candidate.id,'segment_id':segment.id,
        'input_frame_index':index,'evidence_frame_id':frame.evidence_frame_id}


def test_explicit_selection_is_source_bound_and_limited_to_one_segment(context):
    batch=normalized(context)
    request=selection_request(context,batch)
    binding=select_candidate(context,batch,request)
    assert binding.identity=='user_selected_segment' and binding.scope=='selected_segment_only'
    assert not binding.cross_window_identity_verified
    assert binding.segment_id!=batch.candidates[0].segments[1].id
    assert batch.candidates[0].identity=='unknown'  # Overlay, not mutation of raw evidence.
    for changes,code in [({'batch_fingerprint':'a'*64},'STALE_TRACK_SELECTION'),
                         ({'evidence_frame_id':uuid4()},'EVIDENCE_MISMATCH'),
                         ({'input_frame_index':2},'INVALID_TRACK_SELECTION')]:
        with pytest.raises(AppError) as exc:select_candidate(context,batch,{**request,**changes})
        assert exc.value.code==code
    with pytest.raises(AppError) as exc:
        select_candidate(context,batch.model_copy(update={'source_asset_id':uuid4()}),request)
    assert exc.value.code=='OWNERSHIP'


def test_point_and_box_target_prompts_reuse_owned_original_frame_mapping(context):
    frame=context.source.frame(0,source_frame_index=context.window.source_frame_indices[3])
    point={'evidence_frame_id':frame.evidence_frame_id,'x':.5,'y':.25,'negative_points':[{'x':1,'y':1}]}
    prompt,index=selection_prompt(context,'component',point)
    assert context.window.source_frame_indices[index]==frame.source_frame_index
    assert prompt==f'<{index}f>1<|point;x=160;y=45;w=320;h=180|>-<|point;x=319;y=179;w=320;h=180|>'
    prompt,_=selection_prompt(context,'component',{'evidence_frame_id':frame.evidence_frame_id,'mode':'box',
        'x':.25,'y':.5,'width':.25,'height':.5})
    assert 'box;x1=80;y1=90;x2=159;y2=179' in prompt


def test_corrupt_cached_pts_cannot_anchor_an_entity_selection(context):
    from app.models import EvidenceFrame
    reference=context.source.frame(0,source_frame_index=context.window.source_frame_indices[0])
    store=context.source.store;project=context.source.asset.project_id
    cached=store.load(project,'evidence',EvidenceFrame,reference.evidence_frame_id)
    cached.source_pts+=1
    store.save(project,'evidence',cached)
    with pytest.raises(AppError) as exc:
        selection_prompt(context,'component',{'evidence_frame_id':cached.id,'x':.5,'y':.5})
    assert exc.value.code=='EVIDENCE_MISMATCH'


def test_compressed_compatibility_track_preserves_exact_pts_map_on_restart(context):
    batch=normalized(context,indices=range(8));candidate=batch.candidates[0]
    track=compatibility_track(context,batch,candidate.id,candidate.segments[0].id)
    store=context.source.store;project=context.source.asset.project_id
    store.save(project,'tracks',track)
    manifest=json.loads(store.path(project,f'tracks/{track.id}.json').read_text())
    assert manifest['sample_storage']['encoding']=='gzip-json-v1' and 'samples' not in manifest
    restored=store.load(project,'tracks',Track,track.id)
    assert restored==track
    assert len(restored.provider_metadata['exact_samples'])==8
    assert restored.provider_metadata['entity_binding']=='unknown'
    assert len(restored.samples)<8  # Held original identities deduplicate without invented frames.


def test_conflicting_boxes_on_held_frame_are_retained_but_not_collapsed_for_rendering(context):
    batch=normalized(context,indices=range(8),moving=True);candidate=batch.candidates[0]
    assert 'CONFLICTING_BOXES_FOR_HELD_ORIGINAL_FRAME' in candidate.uncertainty
    assert len(candidate.samples)==8
    with pytest.raises(AppError) as exc:compatibility_track(context,batch,candidate.id,candidate.segments[0].id)
    assert exc.value.code=='AMBIGUOUS_TRACK'


def test_recorded_actual_sam_grammar_keeps_explicit_indices_and_original_pts(context):
    # Replay from different generated media: transport/mapping only, no claim
    # that this recording recognized the VFR fixture used here.
    raw=json.loads((Path(__file__).parent/'fixtures/meta/sam-video.json').read_text())
    text=''.join(p.get('text','') for message in raw['output'] for p in message.get('content',[]) if p.get('type')=='output_text')
    batch=normalize_entity_tracks(context,'component',text,{**META,'recording_scope':'different-source wire replay'})
    samples=batch.candidates[0].samples
    assert len(samples)==18 and [s.input_frame_index for s in samples]==list(range(18))
    assert samples[17].x==pytest.approx(132/320)
    assert samples[17].source_frame_index==context.window.source_frame_indices[17]


def test_limits_and_foreign_entities_fail_without_truncation(context):
    for entity,text,kwargs,code in [('foreign',wire([0]),{},'UNKNOWN_ENTITY'),
        ('component',wire([0,1]),{'remaining_samples':1},'OBSERVATION_LIMIT'),
        ('component',wire([0],range(33)),{},'OBSERVATION_LIMIT'),
        ('component',wire([999]),{},'PROVIDER_SCHEMA')]:
        with pytest.raises(AppError) as exc:normalize_entity_tracks(context,entity,text,META,**kwargs)
        assert exc.value.code==code


def transport_for(text,requests,*,failure=False):
    def transport(request):
        requests.append((request.method,request.url.path))
        if request.method=='DELETE':return httpx.Response(200,json={'deleted':True})
        if request.url.path.endswith('/files'):return httpx.Response(200,json={'id':'file-fixture-only'})
        if failure:return httpx.Response(403,json={'error':'fixture rejection'})
        events=[{'type':'response.output_text.delta','delta':text},
            {'type':'response.completed','response':{'id':'mock-sam-response','status':'completed'}}]
        return httpx.Response(200,headers={'content-type':'text/event-stream'},
            content=''.join('data: '+json.dumps(e)+'\n\n' for e in events))
    return httpx.MockTransport(transport)


@pytest.mark.parametrize('text,failure,status',[(wire([0,1]),False,'candidates'),('',False,'not_observed'),
    ('',True,'unavailable'),('malformed output',False,'unavailable')])
def test_actual_adapter_mocked_transport_separates_empty_failed_and_candidate_outputs(context,settings,text,failure,status):
    context.plan.providers['sam_adapter_version']=ENTITY_ADAPTER_VERSION
    requests=[];recordings=[]
    with MetaCloud(replace(settings,model_api_key='fixture-key'),transport_for(text,requests,failure=failure)) as cloud:
        result=observe_entity(cloud,context,'component',record_response=lambda raw,metadata:recordings.append((raw,metadata)))
    assert result.status==status and not result.negative_coverage_verified
    assert requests[-1][0]=='DELETE'
    if failure:
        assert result.error_code=='PROVIDER_ACCESS_DENIED'
        assert result.provider_provenance['error']['details']['remote_file_deleted'] is True
    else:
        assert result.provider_provenance['remote_file_deleted'] is True
        assert recordings[0][0]==text
    assert 'fixture-key' not in result.model_dump_json()


def test_configuration_and_capacity_fail_before_any_media_transmission(context,settings):
    def no_network(request):pytest.fail('Preflight rejection must not upload media')
    with MetaCloud(replace(settings,model_api_key='fixture-key'),httpx.MockTransport(no_network)) as cloud:
        with pytest.raises(AppError) as exc:observe_entity(cloud,context,'component')
        assert exc.value.code=='PROVIDER_CONFIGURATION_MISMATCH'
        context.plan.providers['sam_adapter_version']=ENTITY_ADAPTER_VERSION
        with pytest.raises(AppError) as exc:observe_entity(cloud,context,'component',remaining_samples=0)
        assert exc.value.code=='OBSERVATION_LIMIT'
