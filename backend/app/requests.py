"""Persistent request identity without a database or raw idempotency tokens."""
import hashlib
import json

from .errors import AppError
from .models import Job


def identify_request(store,project_id,kind,payload,key,settings):
    contract={'version':1,'kind':kind,'payload':payload,'muse':settings.muse_model,'sam':settings.sam_model}
    fingerprint=hashlib.sha256(json.dumps(contract,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    request={**payload,'request_fingerprint':fingerprint}
    if key is None:return request,None
    if not 1<=len(key)<=128 or any(ord(c)<33 or ord(c)>126 for c in key):
        raise AppError('INVALID_IDEMPOTENCY_KEY','Use 1–128 printable ASCII characters for Idempotency-Key',422)
    key_hash=hashlib.sha256(key.encode()).hexdigest()
    request['idempotency_key_hash']=key_hash
    for prior in store.list_records(project_id,'jobs',Job):
        if prior.request.get('idempotency_key_hash')==key_hash:
            if prior.request.get('request_fingerprint')!=fingerprint:
                raise AppError('IDEMPOTENCY_CONFLICT','This request key was already used with different parameters',409)
            return request,prior
    return request,None
