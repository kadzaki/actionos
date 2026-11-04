"""Domain-record pagination only; these fixtures are not video detections."""
from uuid import uuid4

from app.models import ActionEvent,ActionIntent,Asset,Occurrence,Project


def test_initial_event_page_contains_prompt_selection_and_other_pages_preserve_all_matches(client):
    store=client.app.state.store
    project=Project(name='pagination domain fixture');store.create_project(project)
    asset=Asset(project_id=project.id,source_path='source/unit.media',original_name='unit fixture',byte_size=1,sha256='0'*64)
    store.save(project.id,'assets',asset)
    intent=ActionIntent(project_id=project.id,source_asset_id=asset.id,original_prompt='last map opening',action_description='map opens',
                        occurrence=Occurrence(mode='last'),provider_metadata={'scan_complete':True})
    store.save(project.id,'intents',intent)
    project.active_asset_id=asset.id;project.active_intent_id=intent.id;store.save(project.id,'project',project)
    records=[]
    for i in range(37):
        event=ActionEvent(project_id=project.id,source_asset_id=asset.id,intent_id=intent.id,
                          action_label='map opens',start_ms=i*1000,end_ms=i*1000+500,
                          match_reason='unit pagination fixture',evidence_ms=[i*1000+100])
        records.append(event);store.save(project.id,'events',event)
    url=f'/api/projects/{project.id}/events'
    initial=client.get(url).json()
    assert initial['offset']==36 and initial['selected_event_ids']==[str(records[-1].id)]
    assert initial['events'][0]['id']==str(records[-1].id) and initial['total_count']==37
    found=[];offset=0
    while offset is not None:
        page=client.get(url,params={'offset':offset,'limit':12}).json()
        found.extend(e['id'] for e in page['events']);offset=page['next_offset']
    assert found==[str(e.id) for e in records]
    assert client.get(url,params={'offset':99999}).json()['offset']==36
    assert client.get(url,params={'limit':101}).status_code==422
    assert client.get(url,params={'offset':-1}).status_code==422
