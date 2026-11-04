"""Explicit developer/manual render proof, never an automatic detection path."""
import argparse
import json
from pathlib import Path

from .config import Settings
from .editing import EditPlan
from .errors import AppError
from .rendering import render
from .storage import FileStore
from .jobs import JobRunner
from .models import Job, JobStatus, Project


def main():
    parser=argparse.ArgumentParser(description="Render a manually authored EDL; stop the server first")
    parser.add_argument("plan_file",type=Path)
    parser.add_argument('--regenerate',action='store_true',help='Render again instead of reusing a verified identical export')
    args=parser.parse_args()
    settings=Settings.from_env()
    plan=EditPlan.model_validate_json(args.plan_file.read_text(encoding="utf-8"))
    if plan.origin!="manual":
        parser.error("This developer tool accepts origin=manual plans only")
    store=FileStore(settings.data_dir)
    store.acquire_process_lock()
    runner=JobRunner(store,settings.max_queued_jobs)
    try:
        runner.recover()
        job=Job(project_id=plan.project_id,asset_id=plan.source_asset_id,type="render")
        with store.mutation():
            project=store.load(plan.project_id,"project",Project)
            project.active_job_id=job.id
            project.status="processing"
            store.save(project.id,"project",project)
        def operation(job):
            def stage(step,progress):
                runner.stage(job,step,progress)
                print(step,flush=True)
            result=render(store,settings,plan,stage,regenerate=args.regenerate)
            with store.mutation():
                project=store.load(plan.project_id,"project",Project)
                project.status="ready"
                store.save(project.id,"project",project)
            return {"export_id":str(result.id),"plan_id":str(result.plan_id),"origin":"manual"}
        runner.submit(job,operation)
        runner.close()
        saved=store.load(plan.project_id,"jobs",Job,job.id)
        print(saved.model_dump_json(indent=2))
        if saved.status!=JobStatus.completed:
            raise SystemExit(1)
    except AppError as exc:
        print(json.dumps(exc.envelope()))
        raise SystemExit(1)
    finally:
        runner.close()
        store.release_process_lock()


if __name__=="__main__":
    main()
