"""Source-timed image geometry supports visual reasoning; it never proves an action."""
from bisect import bisect_right
import math

from .editing import Track
from .errors import AppError

MOTION_FEATURE_VERSION=2


def bounded_samples(values,limit=32):
    if limit<2:raise ValueError('Motion summaries need at least two endpoint slots')
    if len(values)<=limit:return values
    return [values[round(i*(len(values)-1)/(limit-1))] for i in range(limit)]


def center(sample):
    return sample.x+sample.width/2,sample.y+sample.height/2


def crosses_cut(start,end,cuts):
    index=bisect_right(cuts,start)
    return index<len(cuts) and cuts[index]<=end


def motion_features(track: Track,*,max_gap_ms=400,scene_cuts_ms=()):
    features=[];previous=None;occluded=False;cuts=sorted(scene_cuts_ms)
    for sample in track.samples:
        if not sample.visible:occluded=True;continue
        cx,cy=center(sample)
        feature={'time_ms':sample.time_ms,'center_x':round(cx,4),'center_y':round(cy,4),
                 'width':round(sample.width,4),'height':round(sample.height,4),
                 'area':round(sample.width*sample.height,6),'observation':'observed'}
        if previous:
            dt=(sample.time_ms-previous.time_ms)/1000
            feature['elapsed_ms']=sample.time_ms-previous.time_ms
            cut=crosses_cut(previous.time_ms,sample.time_ms,cuts)
            if occluded or dt*1000>max_gap_ms or cut:
                feature['continuity']='scene_cut' if cut else ('occlusion' if occluded else 'observation_gap')
            else:
                px,py=center(previous)
                feature.update(vx_per_second=round((cx-px)/dt,4),vy_per_second=round((cy-py)/dt,4),continuity='adjacent_observations')
        previous=sample;occluded=False;features.append(feature)
    return bounded_samples(features)


def box_iou(a,b):
    intersection=max(0,min(a.x+a.width,b.x+b.width)-max(a.x,b.x))*max(0,min(a.y+a.height,b.y+b.height)-max(a.y,b.y))
    union=a.width*a.height+b.width*b.height-intersection
    return min(1.,max(0.,intersection/union)) if union else 0.


def interaction_summary(primary: Track,contexts,*,expected_source_times_ms=(),scene_cuts_ms=None):
    if len(contexts)>16:
        raise AppError('CONTEXT_SELECTION_REQUIRED','More than 16 context objects require a more specific subject/context request',422)
    cuts=sorted(scene_cuts_ms or ())
    observed={s.time_ms:s for s in primary.samples if s.visible}
    expected={round(t) for t in expected_source_times_ms}
    summary={'feature_version':MOTION_FEATURE_VERSION,'time_space':'original_source_milliseconds',
             'coordinate_space':'normalized_oriented_source_display','camera_motion_compensated':False,
             'scene_cuts_measured':scene_cuts_ms is not None,'scene_cuts_ms':cuts,
             'primary_track_id':str(primary.id),'primary_label':primary.label,
             'observed_samples':len(observed),'motion':motion_features(primary,scene_cuts_ms=cuts),
             'relative_geometry':[],
             'limitations':['Image motion may come from camera motion or perspective. Box overlap/proximity is not proof of contact, catching, scoring or successful completion.']}
    if expected:summary['observed_source_frame_fraction']=round(len(set(observed)&expected)/len(expected),4)
    for context in contexts:
        if (context.project_id,context.source_asset_id)!=(primary.project_id,primary.source_asset_id):
            raise AppError('OWNERSHIP','Relative geometry requires tracks from the same source',422)
        other={s.time_ms:s for s in context.samples if s.visible}
        invisible=sorted({s.time_ms for t in (primary,context) for s in t.samples if not s.visible})
        common=sorted(set(observed)&set(other));features=[];previous=None
        for timestamp in common:
            a,b=observed[timestamp],other[timestamp];ax,ay=center(a);bx,by=center(b)
            dx,dy=bx-ax,by-ay
            item={'time_ms':timestamp,'relative_x':round(dx,4),'relative_y':round(dy,4),
                  'center_distance':round(math.hypot(dx,dy),4),'box_iou':round(box_iou(a,b),4),
                  'observation':'both_observed_at_same_source_time'}
            if previous:
                pt,pdx,pdy=previous;dt=(timestamp-pt)/1000
                if timestamp-pt<=400 and not crosses_cut(pt,timestamp,cuts) and not crosses_cut(pt,timestamp,invisible):
                    item.update(relative_vx_per_second=round((dx-pdx)/dt,4),relative_vy_per_second=round((dy-pdy)/dt,4))
                else:item['continuity']='scene_cut_occlusion_or_observation_gap'
            previous=timestamp,dx,dy;features.append(item)
        relation={'track_id':str(context.id),'label':context.label,'joint_observed_samples':len(common),
                  'samples':bounded_samples(features)}
        if len(common)>1:
            start,end=common[0],common[-1]
            a0,a1=center(observed[start]),center(observed[end]);b0,b1=center(other[start]),center(other[end])
            av=(a1[0]-a0[0],a1[1]-a0[1]);bv=(b1[0]-b0[0],b1[1]-b0[1])
            if math.hypot(*av)>.1 and math.hypot(av[0]-bv[0],av[1]-bv[1])<.02:
                relation['camera_or_shared_motion_ambiguous']=True
        summary['relative_geometry'].append(relation)
    return summary


def attach_geometry(event,primary,contexts,window,scenes):
    summary=interaction_summary(primary,contexts,expected_source_times_ms=window.source_frame_times_ms,
                                scene_cuts_ms=[cut.source_time_ms for cut in scenes.cuts])
    summary['window_id']=str(window.id)
    event.provider_metadata.update(geometry_summary=summary,motion_feature_version=MOTION_FEATURE_VERSION,
                                   scene_evidence_id=str(scenes.id))
    fraction=summary.get('observed_source_frame_fraction',0)
    broken=any(f.get('continuity') in ('scene_cut','occlusion','observation_gap') for f in summary['motion'])
    event.confidence['track_reliability']='high' if fraction>=.9 and not broken else ('medium' if fraction>=.6 else 'low')
    event.confidence['track_reliability_reason']='Qualitative observed-frame coverage and continuity; image motion is not camera-compensated or an action-success probability'
    if any(event.start_ms<cut.source_time_ms<event.end_ms for cut in scenes.cuts):
        warning='An estimated scene change crosses this action; review boundaries or explicitly choose fixed framing'
        if warning not in event.warnings:event.warnings.append(warning)
        event.review_required=True
    return summary
