"""Read-only run evidence/index routes. They never invoke a provider or evaluate events."""
from typing import Literal
from uuid import UUID

from fastapi import Query

from .entity_repository import load_binding
from .fusion_repository import load_fused_observation
from .geometry_repository import load_geometry
from .observation_repository import load_muse_record
from ..errors import AppError
from ..repository import Repository,reference


def _root(project,run):return f'/api/projects/{project}/runs/{run}'


def _muse_frames(batch):
    return [{'observation_id':o.id,'step_id':o.step_id,'source_interval':o.source_interval,
        'frames':[{**f.model_dump(mode='json'),'image_url':f.image_url,'source_url':f.source_url} for f in o.frames]}
        for o in batch.observations] if batch else []


def register_evidence_routes(app,settings):
    base='/api/projects/{project_id}/runs/{run_id}'

    @app.get(base+'/evidence')
    def evidence_index(project_id:UUID,run_id:UUID,kind:Literal['muse_observations','geometry_evidence','observations']|None=None,
                       offset:int=Query(default=0,ge=0),limit:int=Query(default=25,ge=1,le=50)):
        store=app.state.store;repo=Repository(store);run=repo.load(project_id,'runs',run_id)
        kinds=[kind] if kind else ['muse_observations','geometry_evidence','observations']
        paths=sorted((k,p) for k in kinds for p in store.path(project_id,k).glob('*.json'))
        items=[];errors=[]
        for k,path in paths[offset:offset+limit]:
            try:
                header=repo._read(project_id,k,path.stem,1)
                if header.run.id!=run.id:continue
                record=repo.load(project_id,k,path.stem)
                root=_root(project_id,run_id)
                if k=='muse_observations':
                    record,data,prepared=load_muse_record(store,settings,project_id,run_id,record.id)
                    item={'state':record.payload['state'],'count':record.payload['observation_count'],
                        'step_id':None,'evidence_scopes':[record.payload['evidence_scope']],
                        'url':root+'/muse-observations/'+str(record.id)}
                elif k=='geometry_evidence':
                    record,data,prepared,_=load_geometry(store,settings,project_id,run_id,record.id)
                    item={'state':'derived_geometry','count':len(data.measurements),'step_id':data.step_id,
                        'evidence_scopes':record.payload['evidence_scopes'],'url':root+'/geometry/'+str(record.id)}
                else:
                    record,data,prepared=load_fused_observation(store,settings,project_id,run_id,record.id)
                    item={'state':data.verdict,'count':len(data.supports),'step_id':data.step_id,'needs_review':data.needs_review,
                        'interpretation':data.interpretation,'evidence_scopes':record.payload['evidence_scopes'],
                        'url':root+'/observations/'+str(record.id)}
                items.append({'kind':k,'id':record.id,'created_at':record.created_at,'window_id':prepared.window.id,**item})
            except AppError as exc:errors.append({'kind':k,'record_id':path.stem,'code':exc.code,'message':exc.message})
        return {'run':reference(run),'items':items,'errors':errors,'next_offset':offset+limit if offset+limit<len(paths) else None}

    @app.get(base+'/muse-observations/{record_id}')
    def inspect_muse(project_id:UUID,run_id:UUID,record_id:UUID):
        record,batch,_=load_muse_record(app.state.store,settings,project_id,run_id,record_id)
        return {'record':reference(record),'manifest':record.payload,'batch':batch,'source_evidence':_muse_frames(batch)}

    @app.get(base+'/muse-observations/{record_id}/observations/{observation_id}')
    def inspect_muse_observation(project_id:UUID,run_id:UUID,record_id:UUID,observation_id:UUID):
        record,batch,_=load_muse_record(app.state.store,settings,project_id,run_id,record_id)
        found=next((o for o in batch.observations if o.id==observation_id),None) if batch else None
        if found is None:raise AppError('NOT_FOUND','Observation not found in this exact Muse evidence record',404)
        return {'record':reference(record),'evidence_scope':record.payload['evidence_scope'],'observation':found,
            'frames':[{**f.model_dump(mode='json'),'image_url':f.image_url,'source_url':f.source_url} for f in found.frames]}

    @app.get(base+'/geometry/{record_id}')
    def inspect_geometry(project_id:UUID,run_id:UUID,record_id:UUID):
        record,geometry,_,_=load_geometry(app.state.store,settings,project_id,run_id,record_id)
        root=_root(project_id,run_id)+'/geometry/'+str(record.id)
        return {'record':reference(record),'manifest':record.payload,'geometry':geometry,
            'measurement_links':[{'id':m.id,'frames_url':root+'/measurements/'+str(m.id)+'/frames'} for m in geometry.measurements]}

    @app.get(base+'/geometry/{record_id}/measurements/{measurement_id}/frames')
    def geometry_frames(project_id:UUID,run_id:UUID,record_id:UUID,measurement_id:UUID):
        record,geometry,prepared,_=load_geometry(app.state.store,settings,project_id,run_id,record_id)
        item=next((m for m in geometry.measurements if m.id==measurement_id),None)
        if item is None:raise AppError('NOT_FOUND','Measurement not found in this exact geometry record',404)
        frames=[prepared.source.frame(0,source_frame_index=i) for i in sorted({item.source_start_frame,item.source_end_frame})]
        return {'record':reference(record),'measurement':item,
            'frames':[{**f.model_dump(mode='json'),'image_url':f.image_url,'source_url':f.source_url} for f in frames]}

    @app.get(base+'/entity-bindings/{binding_id}')
    def inspect_binding(project_id:UUID,run_id:UUID,binding_id:UUID):
        record,binding,batch_record,_,prepared=load_binding(app.state.store,settings,project_id,run_id,binding_id)
        frame=prepared.source.frame(0,source_frame_index=binding.source_frame_index)
        return {'record':reference(record),'binding':binding,'batch':reference(batch_record),
            'image_url':frame.image_url,'source_url':frame.source_url}

    @app.get(base+'/observations/{record_id}')
    def inspect_fused(project_id:UUID,run_id:UUID,record_id:UUID):
        record,result,_=load_fused_observation(app.state.store,settings,project_id,run_id,record_id)
        root=_root(project_id,run_id);payload=record.payload
        muse=payload['muse'];geometry=payload['geometry']
        links=[]
        if muse:
            links += [{'kind':'muse_observation','id':identifier,
                'url':root+'/muse-observations/'+str(muse['id'])+'/observations/'+str(identifier)} for identifier in result.observation_ids]
        if geometry:
            links += [{'kind':'geometry_measurement','id':identifier,
                'url':root+'/geometry/'+str(geometry['id'])+'/measurements/'+str(identifier)+'/frames'} for identifier in result.geometry_ids]
        links += [{'kind':'entity_binding','entity_id':entity,'id':ref['id'],
            'url':root+'/entity-bindings/'+str(ref['id'])} for entity,ref in payload['bindings'].items()]
        return {'record':reference(record),'manifest':payload,'result':result,'evidence_links':links}
