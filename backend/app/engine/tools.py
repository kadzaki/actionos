"""Finite internal tool authority. Model text cannot register or authorize tools."""
from typing import Literal
from uuid import UUID
from pydantic import Field
from ..models import Model
from ..errors import AppError
from ..source_evidence import SourceEvidenceAdapter
from ..repository import Repository
from ..actions.contracts import ActionRequest


class ToolArguments(Model):
    project_id:UUID
    run_id:UUID|None=None
    source_asset_id:UUID|None=None
    job_id:UUID|None=None


class NoArguments(Model):
    pass


class ActionArguments(Model):
    request:ActionRequest


class ExecuteArguments(Model):
    action_id:UUID


TOOL_POLICY={
    'inspect_media':{'available':True,'authority':'read'},
    'compile_program':{'available':True,'authority':'explicit_cloud'},
    'run_perception':{'available':True,'authority':'reviewed_cloud'},
    'evaluate_rule':{'available':True,'authority':'local'},
    'build_action_plan':{'available':True,'authority':'explicit_action'},
    'render_clip':{'available':True,'authority':'explicit_action'},
    'build_report':{'available':True,'authority':'explicit_action'},
    'get_status':{'available':True,'authority':'read'},
    'compare_events':{'available':False,'authority':'unavailable'},
}


class BoundedToolRegistry:
    def __init__(self,handlers):
        if set(handlers)-set(TOOL_POLICY):raise ValueError('Unknown tool handler')
        self.handlers=dict(handlers)

    def dispatch(self,name,arguments,*,allowed):
        policy=TOOL_POLICY.get(name)
        if not policy or not policy['available'] or name not in self.handlers:
            raise AppError('TOOL_UNAVAILABLE','Tool is not implemented or registered',422)
        if policy['authority'] not in allowed:
            raise AppError('TOOL_AUTHORITY_REQUIRED','Explicit application authority is required for this operation',409)
        # Each handler declares its closed Pydantic request; code/network/path
        # payloads fail validation before dispatch. No eval/import/shell tools.
        model,handler=self.handlers[name]
        return handler(model.model_validate(arguments))

    def schemas(self):
        return {name:{**TOOL_POLICY[name],'parameters':model.model_json_schema()} for name,(model,_) in self.handlers.items()}


def for_job(store,runner,settings,job,run=None):
    """Construct handlers bound to trusted job context, never model-supplied paths."""
    def require_run():
        if run is None:raise AppError('RUN_REQUIRED','This tool needs an exact reviewed run context',409)
        return run
    def inspect(args):
        source=SourceEvidenceAdapter(store,settings,job.project_id,job.asset_id)
        return {'source_asset_id':str(source.asset.id),'source_sha256':source.asset.sha256,'metadata':source.asset.metadata.model_dump(mode='json')}
    def compile(args):
        from ..programs.routes import compile_job
        if job.type!='compile':raise AppError('INVALID_TOOL_CONTEXT','Compilation requires a captured compile job',409)
        return compile_job(store,runner,settings,job)
    def perceive(args):
        from .runtime import observe_run
        return observe_run(store,runner,settings,require_run(),job)
    def evaluate(args):
        from .runtime import evaluate_run
        return evaluate_run(store,runner,settings,require_run(),job)
    def plan(args):
        from ..actions.registry import build_action_plan
        current=require_run()
        return build_action_plan(store,settings,current.project_id,current.id,args.request.model_dump(mode='json'),initiated=True)
    def execute(args,kind):
        from ..actions.execution import execute as execute_action
        from ..repository import reference
        current=require_run();plan=Repository(store).load(current.project_id,'action_plans',args.action_id)
        if plan.run!=reference(current) or plan.payload['request']['type']!=kind or job.action_id!=plan.id:
            raise AppError('INVALID_TOOL_CONTEXT','Output tool requires the exact action/run/job binding',409)
        return execute_action(store,runner,settings,plan,job)
    return BoundedToolRegistry({
        'inspect_media':(NoArguments,inspect),'compile_program':(NoArguments,compile),
        'run_perception':(NoArguments,perceive),'evaluate_rule':(NoArguments,evaluate),
        'build_action_plan':(ActionArguments,plan),
        'render_clip':(ExecuteArguments,lambda args:execute(args,'extract_clip')),
        'build_report':(ExecuteArguments,lambda args:execute(args,'create_report')),
        'get_status':(NoArguments,lambda args:runner.find(job.id).model_dump(mode='json')),
    })
