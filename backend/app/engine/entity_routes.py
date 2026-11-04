"""Inspect existing run evidence and explicitly bind one segment. No cloud calls."""
from uuid import UUID

from fastapi import Query

from .entity_repository import load_entity_batch,publish_binding
from .entity_tracks import BindingSelectionRequest
from .entity_tracks import SelectedEntityBinding,select_candidate
from ..errors import AppError
from ..programs.repository import load_program
from ..repository import Repository,fingerprint,reference


def register_entity_routes(app,settings):
    @app.get('/api/projects/{project_id}/entity-tracks')
    def list_batches(project_id:UUID,program_id:UUID,version:int=Query(ge=1),offset:int=Query(default=0,ge=0),limit:int=Query(default=25,ge=1,le=50)):
        store=app.state.store;repo=Repository(store)
        program,data=load_program(store,project_id,program_id,version)
        paths=sorted(store.path(project_id,'entity_tracks').glob('*.json'))
        items=[];errors=[]
        for path in paths[offset:offset+limit]:
            try:
                record=repo.load(project_id,'entity_tracks',path.stem)
                if record.program!=reference(program):continue
                record,batch,_=load_entity_batch(store,settings,project_id,record.run.id,record.id)
                items.append({'batch_id':record.id,'run_id':record.run.id,'entity_id':batch.entity_id,'role':batch.role,
                    'window_id':batch.window_id,'status':batch.status,'candidate_count':len(batch.candidates),
                    'created_at':record.created_at,'evidence_scope':record.payload['evidence_scope']})
            except AppError as exc:
                errors.append({'record_id':path.stem,'code':exc.code,'message':exc.message})
        next_offset=offset+limit if offset+limit<len(paths) else None
        return {'program_fingerprint':fingerprint(program),'program_origin':data.program.compiler.origin,
            'items':items,'errors':errors,'next_offset':next_offset}

    base='/api/projects/{project_id}/runs/{run_id}/entity-tracks/{batch_id}'

    @app.get(base)
    def inspect_batch(project_id:UUID,run_id:UUID,batch_id:UUID):
        record,batch,_=load_entity_batch(app.state.store,settings,project_id,run_id,batch_id)
        return {'batch_id':record.id,'batch_fingerprint':record.payload['batch_fingerprint'],
            'evidence_scope':record.payload['evidence_scope'],'batch':batch.model_dump(mode='json')}

    @app.get(base+'/frame')
    def inspect_frame(project_id:UUID,run_id:UUID,batch_id:UUID,input_frame_index:int=Query(ge=0,le=511)):
        _,batch,prepared=load_entity_batch(app.state.store,settings,project_id,run_id,batch_id)
        if input_frame_index>=batch.input_frame_count:
            raise AppError('INVALID_FRAME','Choose an actual frame from this tracking input',422)
        index=prepared.window.source_frame_indices[input_frame_index]
        frame=prepared.source.frame(0,source_frame_index=index)
        return {'input_frame_index':input_frame_index,'frame':frame.model_dump(mode='json'),
            'image_url':frame.image_url,'source_url':frame.source_url}

    @app.post(base+'/bindings')
    def bind_segment(project_id:UUID,run_id:UUID,batch_id:UUID,body:BindingSelectionRequest):
        record=publish_binding(app.state.store,settings,project_id,run_id,batch_id,body)
        return {'binding_id':record.id,'created_at':record.created_at,**record.payload}

    @app.get(base+'/bindings')
    def inspect_bindings(project_id:UUID,run_id:UUID,batch_id:UUID,offset:int=Query(default=0,ge=0),limit:int=Query(default=25,ge=1,le=50)):
        store=app.state.store;repo=Repository(store)
        record,batch,prepared=load_entity_batch(store,settings,project_id,run_id,batch_id)
        paths=sorted(store.path(project_id,'entity_bindings').glob('*.json'))
        items=[];errors=[]
        for path in paths[offset:offset+limit]:
            try:
                saved=repo.load(project_id,'entity_bindings',path.stem)
                if saved.payload.get('batch_id')!=str(record.id):continue
                if saved.run!=record.run or reference(record) not in saved.references:
                    raise AppError('OWNERSHIP','Saved binding differs from the inspected run/batch',409)
                binding=SelectedEntityBinding.model_validate(saved.payload['binding'])
                request=BindingSelectionRequest(batch_fingerprint=binding.batch_fingerprint,candidate_id=binding.candidate_id,
                    segment_id=binding.segment_id,evidence_frame_id=binding.evidence_frame_id,input_frame_index=binding.input_frame_index)
                if select_candidate(prepared,batch,request)!=binding:
                    raise AppError('EVIDENCE_MISMATCH','Saved binding differs from its original frame selection',409)
                items.append({'binding_id':saved.id,'created_at':saved.created_at,'binding':binding.model_dump(mode='json')})
            except AppError as exc:errors.append({'record_id':path.stem,'code':exc.code,'message':exc.message})
            except (ValueError,KeyError):errors.append({'record_id':path.stem,'code':'CORRUPT_RECORD','message':'Saved binding failed typed validation'})
        return {'items':items,'errors':errors,'next_offset':offset+limit if offset+limit<len(paths) else None}
