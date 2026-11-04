"""Finite JSON-pointer AST edits and structural invalidation; no executable paths."""
from copy import deepcopy
from typing import Literal
from pydantic import Field,JsonValue,model_validator
from ..models import Model
from ..errors import AppError
from ..validation import bounded_json
from .validation import require_valid_program

ROOTS={'instruction','entities','zones','steps','relations','cycle','selection','exclusions','unknown_policy','actions','assumptions','ambiguities','unrepresented_clauses','clauses'}
PERCEPTION={'entities','zones','steps','unknown_policy'}


class PatchOperation(Model):
    op:Literal['add','replace','remove']
    path:str=Field(min_length=2,max_length=256)
    value:JsonValue=None


def apply_patch(ast,operations,*,duration_ms):
    raw=ast.model_dump(mode='json',by_alias=True)
    updated=deepcopy(raw)
    operations=[PatchOperation.model_validate(p) for p in operations]
    if not 1<=len(operations)<=64:raise AppError('PATCH_LIMIT','Use 1–64 AST operations',422)
    bounded_json([p.model_dump(mode='json') for p in operations],max_bytes=131072,max_depth=16)
    for operation in operations:
        parts=operation.path.split('/')[1:]
        if any('~' in p or not p for p in parts) or parts[0] not in ROOTS:
            raise AppError('INVALID_PATCH_PATH','Only declared AST fields and literal array indices can be edited',422)
        parent=updated
        try:
            for part in parts[:-1]:
                parent=parent[int(part)] if isinstance(parent,list) and part.isdecimal() else parent[part]
            key=parts[-1]
            if isinstance(parent,list):
                if key=='-' and operation.op=='add':parent.append(deepcopy(operation.value));continue
                if not key.isdecimal() or (len(key)>1 and key.startswith('0')):raise ValueError()
                index=int(key)
                if operation.op=='add':
                    if index>len(parent):raise IndexError()
                    parent.insert(index,deepcopy(operation.value))
                elif operation.op=='remove':parent.pop(index)
                else:parent[index]=deepcopy(operation.value)
            elif isinstance(parent,dict):
                if operation.op in ('replace','remove') and key not in parent:raise KeyError()
                if operation.op=='remove':del parent[key]
                else:parent[key]=deepcopy(operation.value)
            else:raise ValueError()
        except (KeyError,IndexError,ValueError,TypeError):
            raise AppError('INVALID_PATCH_PATH','Patch target does not exist or index is invalid',422) from None
    result=require_valid_program(updated,source_duration_ms=duration_ms,require_clauses=True)
    return result,structural_diff(raw,result.model_dump(mode='json',by_alias=True))


def structural_diff(before,after):
    differences=[]
    def walk(a,b,path):
        if a==b:return
        if isinstance(a,dict) and isinstance(b,dict):
            for key in sorted(a.keys()|b.keys()):
                if key not in a:differences.append({'path':path+'/'+key,'before':None,'after':b[key]})
                elif key not in b:differences.append({'path':path+'/'+key,'before':a[key],'after':None})
                else:walk(a[key],b[key],path+'/'+key)
        elif isinstance(a,list) and isinstance(b,list) and len(a)==len(b):
            for i,(left,right) in enumerate(zip(a,b)):walk(left,right,path+'/'+str(i))
        else:differences.append({'path':path,'before':a,'after':b})
    walk(before,after,'')
    changed={d['path'].split('/')[1] for d in differences}
    # Changed clauses/instruction can alter interpretation even if nodes look
    # unchanged. User must approve the new version before any reuse.
    stage='observe' if changed&PERCEPTION else ('evaluate' if changed-{'actions','clauses','instruction','assumptions','ambiguities','unrepresented_clauses'} else 'actions')
    return {'changes':differences,'no_op':not differences,'invalidates':stage if differences else 'none',
        'reuse_requires_exact_node_source_provider_fingerprints':True}
