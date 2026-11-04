"""Cache last successful cloud access by credential fingerprint, never the key."""
import hashlib
from datetime import datetime,timezone

from ..config import Settings
from ..errors import AppError
from ..storage import FileStore


def credential_fingerprint(settings):
    return hashlib.sha256(settings.model_api_key.encode()).hexdigest()


def _record(settings):
    store=FileStore(settings.data_dir)
    try:record=store.read_json(store.root/'provider-status.json')
    except AppError:return {}
    if not isinstance(record,dict) or record.get('credential_fingerprint')!=credential_fingerprint(settings):return {}
    return record


def read_contract_status(settings):
    contracts=_record(settings).get('contracts',{})
    if not isinstance(contracts,dict):return {}
    return {name:value for name,value in contracts.items()
            if isinstance(value,dict) and value.get('endpoint')==settings.meta_api_base_url
            and value.get('model')==settings.muse_model}


def read_status(settings: Settings):
    store=FileStore(settings.data_dir)
    path=store.root/'provider-status.json'
    try:record=store.read_json(path)
    except AppError:return {}
    if not isinstance(record,dict) or record.get('credential_fingerprint')!=credential_fingerprint(settings):return {}
    models=record.get('models',{})
    if not isinstance(models,dict):return {}
    # Access evidence is scoped to the actual endpoint and model. It does not
    # qualify any application response schema or guarantee current quota.
    return {model:value for model,value in models.items()
            if isinstance(value,dict) and value.get('endpoint')==settings.meta_api_base_url}


def note_success(settings: Settings,model: str):
    store=FileStore(settings.data_dir)
    models=read_status(settings)
    models[model]={'verified_at':datetime.now(timezone.utc).isoformat(),'endpoint':settings.meta_api_base_url}
    store.write_json(store.root/'provider-status.json',{'schema_version':1,
                     'credential_fingerprint':credential_fingerprint(settings),'models':models,
                     'contracts':read_contract_status(settings)})


def note_contract_success(settings,name,version,response_id):
    if name not in ('compiler','predicate_observations'):
        raise ValueError('Unknown application contract')
    contracts=read_contract_status(settings)
    contracts[name]={'version':version,'model':settings.muse_model,'endpoint':settings.meta_api_base_url,
                     'verified_at':datetime.now(timezone.utc).isoformat(),'response_id':response_id}
    store=FileStore(settings.data_dir)
    store.write_json(store.root/'provider-status.json',{'schema_version':1,
        'credential_fingerprint':credential_fingerprint(settings),'models':read_status(settings),'contracts':contracts})
