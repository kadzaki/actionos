"""Read-only per-stage measurements and checksum inventory of an existing run."""
import argparse,json,hashlib
from datetime import datetime
from pathlib import Path
from uuid import UUID


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project-dir',type=Path,required=True)
    parser.add_argument('--run-id',type=UUID,required=True)
    parser.add_argument('--out',type=Path,required=True)
    args=parser.parse_args();root=args.project_dir.resolve()
    run_path=root/'runs'/f'{args.run_id}.json'
    if run_path.stat().st_size>4*1024*1024:raise SystemExit('Run snapshot exceeds its bounded size')
    snapshot=json.loads(run_path.read_text(encoding='utf-8'))
    program=snapshot['program']
    jobs=[];inventory=[];size=0
    for path in sorted(root.rglob('*.json')):
        if path.stat().st_size>4*1024*1024:continue
        record=json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(record,dict):continue
        compile_for_program=(record.get('result',{}).get('program_id')==program['id'] and record.get('result',{}).get('version')==program['version'])
        if (record.get('run_id')==str(args.run_id) or compile_for_program) and 'stage_history' in record:
            stages=[]
            for stage in record['stage_history']:
                start=datetime.fromisoformat(stage['started_at']);end=stage.get('finished_at')
                stages.append({'stage':stage['step'],'seconds':(datetime.fromisoformat(end)-start).total_seconds() if end else None})
            jobs.append({'job_id':record['id'],'type':record['type'],'status':record['status'],'stages':stages,
                'queue_seconds':(datetime.fromisoformat(record['stage_history'][0]['started_at'])-datetime.fromisoformat(record['created_at'])).total_seconds() if record['stage_history'] else None})
        pinned_root=(record.get('kind')=='runs' and record.get('id')==str(args.run_id)) or (record.get('kind')=='programs' and record.get('id')==program['id'] and record.get('version')==program['version'])
        if pinned_root or (record.get('run') and record['run'].get('id')==str(args.run_id)):
            raw=path.read_bytes();size+=len(raw)
            inventory.append({'path':path.relative_to(root).as_posix(),'sha256':hashlib.sha256(raw).hexdigest(),'bytes':len(raw),'kind':record.get('kind')})
    project_bytes=sum(p.stat().st_size for p in root.rglob('*') if p.is_file())
    output={'schema_version':'actionos-run-inventory-1','run_id':str(args.run_id),'jobs':jobs,'records':inventory,
        'record_bytes':size,'current_project_bytes':project_bytes,'peak_memory':None,'disk_high_water':None,'monetary_cost':None,
        'qualification':'Existing job timestamps only. No workload was run; current disk size is not a high-water measurement.'}
    args.out.parent.mkdir(parents=True,exist_ok=True)
    with args.out.open('x',encoding='utf-8') as stream:json.dump(output,stream,indent=2)
    print('Existing-run measurements and hashes saved; no provider requests made.')


if __name__=='__main__':main()
