"""Offline scoring of frozen independent labels; never invokes a provider."""
import argparse
import hashlib
import json
from pathlib import Path


def iou(a,b):
    intersection=max(0,min(a['end_ms'],b['end_ms'])-max(a['start_ms'],b['start_ms']))
    union=max(a['end_ms'],b['end_ms'])-min(a['start_ms'],b['start_ms'])
    return intersection/union if union else 0


def score(labels,predictions):
    if labels.get('schema_version')!='actionos-human-labels-1' or labels.get('independent_human_labels') is not True:
        raise ValueError('Frozen independent human labels are required; model labels cannot qualify acceptance')
    if not labels.get('annotator') or not labels.get('frozen_at'):
        raise ValueError('Record the independent annotator and freeze timestamp before scoring')
    threshold=labels['temporal_iou_threshold']
    if not isinstance(threshold,(int,float)) or not 0<threshold<=1:raise ValueError('Invalid frozen overlap threshold')
    expected=labels['events']
    if len(expected)>256 or len(predictions)>256:raise ValueError('Event quota exceeded')
    if any(e.get('source_sha256')!=labels['source_sha256'] for e in predictions):raise ValueError('Prediction source differs from the frozen labels')
    for event in expected:
        interval=event['interval']
        if not 0<=interval['start_ms']<interval['end_ms'] or event['verdict'] not in ('true','false','unknown'):raise ValueError('Invalid human event')
    result={}
    for group in ('unassisted','human_corrected'):
        actual=[e for e in predictions if (e['payload']['origin']=='user_correction')==(group=='human_corrected')]
        candidates={i:sorted([j for j,e in enumerate(expected) if iou(p['payload']['interval'],e['interval'])>=threshold],
            key=lambda j:(-iou(p['payload']['interval'],expected[j]['interval']),j)) for i,p in enumerate(actual)}
        assigned={}
        def match(index,visited):
            for target in candidates[index]:
                if target in visited:continue
                visited.add(target)
                if target not in assigned or match(assigned[target],visited):assigned[target]=index;return True
            return False
        for index in range(len(actual)):match(index,set())
        confusion={a:{b:0 for b in ('true','false','unknown')} for a in ('true','false','unknown')}
        correct_steps=step_denominator=0;pairs=[]
        for target,index in sorted(assigned.items()):
            truth=expected[target];prediction=actual[index]['payload']
            confusion[truth['verdict']][prediction['verdict']]+=1
            predicted_steps={s['step_id']:s for s in prediction['steps']}
            for step in truth['steps']:
                step_denominator+=1
                correct_steps+=int(predicted_steps.get(step['step_id'],{}).get('verdict')==step['verdict'])
            pairs.append({'expected_index':target,'predicted_id':actual[index]['id'],
                'interval_iou':iou(truth['interval'],prediction['interval']),'expected':truth['verdict'],'predicted':prediction['verdict']})
        result[group]={'predicted_events':len(actual),'expected_events':len(expected),'matched_events':len(assigned),
            'unmatched_expected':len(expected)-len(assigned),'unmatched_predicted':len(actual)-len(assigned),
            'verdict_confusion':confusion,'step_correct':correct_steps,'matched_step_denominator':step_denominator,'pairs':pairs,
            'qualification':'scored only; program fidelity, crop quality, permissions and deployment require separate review'}
    return {'schema_version':'actionos-score-1','source_sha256':labels['source_sha256'],'temporal_iou_threshold':threshold,'groups':result}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--labels',type=Path,required=True)
    parser.add_argument('--labels-sha256',required=True,help='Hash frozen independently before inference')
    parser.add_argument('--events',type=Path,required=True,help='JSON array of immutable TemporalEvent envelopes')
    parser.add_argument('--out',type=Path,required=True)
    args=parser.parse_args()
    if args.labels.stat().st_size>4*1024*1024 or args.events.stat().st_size>8*1024*1024:raise SystemExit('Scoring input exceeds its bounded size')
    raw=args.labels.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=args.labels_sha256:raise SystemExit('Labels differ from their frozen hash')
    report=score(json.loads(raw),json.loads(args.events.read_text(encoding='utf-8')))
    report['labels_sha256']=args.labels_sha256
    report['prediction_file_sha256']=hashlib.sha256(args.events.read_bytes()).hexdigest()
    args.out.parent.mkdir(parents=True,exist_ok=True)
    with args.out.open('x',encoding='utf-8') as stream:json.dump(report,stream,indent=2)
    print('Offline scoring report saved; no provider requests made.')


if __name__=='__main__':main()
