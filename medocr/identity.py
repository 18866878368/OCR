"""Flag possible duplicate hospital admissions without merging model guesses."""
from .dates import canonical_date


def possible_duplicate_admissions(store,batch_id,config=None):
    from .candidate_scope import scoped_events
    groups = {}
    for case in store.cases(batch_id):
        for e in (scoped_events(store,case['id'],config) if config is not None else store.events(case['id'])):
            if e['kind']!='admission' or e['attributes'].get('_stale'):
                continue
            day = canonical_date(e.get('event_time') or e['attributes'].get('event_date'))
            if not day:
                continue
            for field in ('patient_id','episode_id'):
                value = e['attributes'].get(field)
                if value:
                    groups.setdefault((field,value,day),set()).add(case['id'])
    return [{'case_ids':list(ids),'reason':'相同院内标识及入院日期；需确认是否同次住院的多个文件'}
            for ids in sorted({tuple(sorted(v)) for v in groups.values() if len(v)>1})]
