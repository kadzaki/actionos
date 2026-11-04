"""A24 local planning proof on the explicitly recorded-response inspector QA data.

No cloud requests, perception, evaluation or events are produced. Run the A22
browser inspector proof first. Its simulated review is test evidence only.
"""
from dataclasses import replace
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.config import Settings
from app.engine.planning import ObservationPlan, initial_coverage, plan_run
from app.runs import snapshot_run
from app.storage import FileStore


def main():
    root=Path(__file__).resolve().parents[2]
    output=root/'.tmp/observation-plan-proof'
    output.mkdir(parents=True,exist_ok=True)
    ui=json.loads((root/'.tmp/inspector-proof/workflow.json').read_text(encoding='utf-8'))
    settings=replace(Settings.from_env(),data_dir=root/'.tmp/inspector-proof/data',model_api_key='')
    store=FileStore(settings.data_dir)
    paths=list(store.projects.glob(f'*/programs/{ui["program_id"]}/v2.json'))
    assert len(paths)==1,'Inspector QA program owner is missing or ambiguous'
    project_id=paths[0].parents[2].name
    store.acquire_process_lock()
    try:
        run=snapshot_run(store,settings,project_id,ui['program_id'],2,
            muse_adapter_version='observation-adapter-not-implemented',sam_adapter_version='sam-adapter-1',
            evaluator_version='not-implemented')
        record=plan_run(store,settings,project_id,run.id)
        plan=ObservationPlan.model_validate(record.payload['plan'])
        coverage=initial_coverage(plan)
        assert not coverage.scan_complete and all(not p.sufficient_negative for p in coverage.predicates.values())
        assert not list(store.path(project_id,'observations').glob('*.json'))
        proof={'scope':'local_planning_on_recorded_response_qa_fixture','cloud_requests_made':0,
            'simulated_review':'A22 isolated browser QA only; not a live perception run',
            'scan_complete':coverage.scan_complete,'processing_complete':coverage.processing_complete,
            'plan_record':record.model_dump(mode='json')}
        (output/'plan.json').write_text(json.dumps(proof,indent=2),encoding='utf-8')
    finally:
        store.release_process_lock()
    print(json.dumps({'passed':True,'scope':proof['scope'],'muse_planned':plan.muse_requests,
        'sam_planned':plan.sam_requests,'cloud_requests_made':0,'output':str(output/'plan.json')}),flush=True)


if __name__=='__main__':main()
