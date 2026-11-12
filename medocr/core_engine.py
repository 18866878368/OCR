"""Deterministic rules over shared facts; drafts and reviewed results are separate."""
import hashlib
import json
from datetime import date, datetime, timedelta
import re

from .rules import aggregate, automatic_case, calculate_measures, in_period, parse_time, RULE_VERSION
from .specs import core_specs
from .retrieval import RETRIEVAL_VERSION, retrieval_plan
from .prompt_metrics import EXTERNAL_GUIDANCE_IDS, PROMPT_METRIC_IDS, prompt_summary


def rule_hash(spec):
    return hashlib.sha256(json.dumps([spec['id'],spec['rule_conflict'],spec['counting_unit'],
                                     RULE_VERSION, RETRIEVAL_VERSION],ensure_ascii=False).encode()).hexdigest()


def all_of(values):
    values = list(values)
    return False if False in values else (None if None in values else True)


def any_of(values, complete=False):
    values = list(values)
    return True if True in values else (False if complete and None not in values else None)


def normalized_name(value, aliases=None):
    if not isinstance(value,str) or not value.strip():
        return None
    value = re.sub(r'\s+','',value)
    return (aliases or {}).get(value,value)


def evidence(events):
    rows, seen = [],set()
    for e in events:
        for row in e['attributes'].get('_evidence',[{'document_id':e['document_id'],'page':e['page'],'quote':e['quote']} ]):
            key = (row['document_id'],row['page'],row['quote'])
            if key not in seen:
                seen.add(key); rows.append(row)
    return rows


def _structured_evidence(value):
    """Return page-locatable evidence references from a review value.

    Indicator reviews historically stored ``evidence`` as free text (for
    example, ``第1页`` or ``1``), while automatically generated details use a
    list of document/page/quote references.  Do not let that display-only
    text erase the original fact evidence when a review is applied.
    """
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        refs = value.get('references') or value.get('evidence')
        return refs if isinstance(refs, list) else []
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return []
        if isinstance(parsed, dict):
            return _structured_evidence(parsed)
        return parsed if isinstance(parsed, list) else []
    return []


def _apply_review(auto_row, review_row):
    """Overlay a reviewed judgement without losing source-page evidence."""
    merged = {**auto_row, **review_row}
    review_refs = _structured_evidence(review_row.get('evidence'))
    auto_refs = _structured_evidence(auto_row.get('evidence'))
    merged['evidence'] = review_refs or auto_refs
    # Keep the human-entered free-text trail separately for the review screen
    # and exports.  It is not treated as a page locator unless it is JSON
    # evidence references.
    review_value = review_row.get('evidence')
    if isinstance(review_value, str):
        try:
            parsed_review_value = json.loads(review_value)
        except (TypeError, ValueError):
            parsed_review_value = None
        if isinstance(parsed_review_value, dict):
            review_value = parsed_review_value
    if isinstance(review_value, dict):
        merged['review_evidence'] = review_value.get('note', '')
    elif isinstance(review_value, str):
        merged['review_evidence'] = review_value
    if auto_row.get('fact_ids'):
        merged['fact_ids'] = auto_row['fact_ids']
    if auto_row.get('calculation_trace'):
        trace = auto_row['calculation_trace']
        merged['calculation_trace'] = {**trace, 'denominator': dict(trace['denominator']),
                                       'numerator': dict(trace['numerator']),
                                       'calculation': dict(trace['calculation']),
                                       'steps': [{**step, 'label': '自动候选 · '+step['label']} for step in trace['steps']] +
                                                [{'label': '人工复核依据', 'value': merged['reason'],
                                                  'evidence': merged['evidence']}]}
    return _sync_trace_row(merged)


def _trace_ref(event):
    """Compact fact reference used by the calculation explanation."""
    return {'fact_id': event.get('id'), 'kind': event.get('kind'),
            'document_id': event.get('document_id'), 'page': event.get('page'),
            'event_time': event.get('event_time'), 'record_time': event.get('record_time'),
            'quote': event.get('quote', '')}


def _trace_state(value):
    if value is True:
        return '是'
    if value is False:
        return '否'
    return '待确认'


def _trace_value(value):
    return value if value is not None else '待确认'


def _calculation_trace(metric_id, ctx, row, *, rule, denominator_objects=(), numerator_objects=(),
                       steps=(), objects=(), unknowns=(), expression=None):
    denominator = row.get('denominator')
    numerator = row.get('numerator')
    candidate_numerator = numerator if numerator is not None else row.get('suggested_numerator')
    if expression is None:
        expression = (f'{candidate_numerator} / {denominator} = '
                      f'{candidate_numerator / denominator:g}'
                      if candidate_numerator is not None and denominator else '待确认')
    return {
        'version': 1,
        'metric_id': metric_id,
        'counting_rule': rule,
        'retrieval': retrieval_plan(metric_id),
        'denominator': {'value': denominator, 'objects': [_trace_ref(e) for e in denominator_objects]},
        'numerator': {'value': numerator, 'candidate_value': candidate_numerator,
                      'objects': [_trace_ref(e) for e in numerator_objects]},
        'objects': objects,
        'steps': list(steps),
        'unknowns': list(unknowns),
        'calculation': {'expression': expression,
                        'value': (candidate_numerator / denominator
                                  if candidate_numerator is not None and denominator else None),
                        'status': row.get('reason', '')},
    }


def _detail_trace(spec, row):
    """Explain contributions using the judgement and evidence actually recorded."""
    payload = row.get('prompt_payload') if isinstance(row.get('prompt_payload'), dict) else {}
    refs = _structured_evidence(row.get('evidence'))
    reason = row.get('reason') or '未记录判定依据'
    key = row.get('item_key')
    objects = []
    if key and key != 'coverage_unknown':
        fact_ids = row.get('fact_ids') or [ref['event_id'] for ref in refs
                                          if isinstance(ref, dict) and ref.get('event_id')]
        objects.append({'object_id': key, 'fact_ids': fact_ids,
                        'origin': 'review' if row.get('reviewer') else row.get('origin'),
                        'reviewer': row.get('reviewer'),
                        'evidence': refs})
    if row.get('origin') == 'prompt' and isinstance(payload.get('objects'), list):
        prefix = f"prompt:{row['case_id']}:{spec['id']}:"
        matched = [obj for index, obj in enumerate(payload['objects'], 1) if isinstance(obj, dict)
                   and key == prefix + str(obj.get('item_key', index))]
        objects = matched or objects
    steps = [{'label': '人工复核依据' if row.get('reviewer') else '判定依据',
              'value': reason, 'evidence': refs}]
    steps.extend({'label': label, 'value': row[key], 'evidence': refs}
                 for key, label in (('numerator_reason', '分子判定依据'), ('denominator_reason', '分母判定依据'))
                 if row.get(key))
    if row.get('origin') == 'prompt' and row.get('input_values'):
        for key, label in (('order_type', '医嘱类型'), ('start_date', '开立日期'), ('stop_date', '停止日期')):
            steps.append({'label': label, 'value': _trace_value(row['input_values'].get(key)),
                          'evidence': row.get('input_evidence', {}).get(key, [])})
    unknowns = list(payload.get('validation_issues') or [])
    # Older validation required unused top-level fields despite object reasons.
    if payload.get('objects'):
        obsolete_issues = {'numerator_reason缺失', 'denominator_reason缺失', 'analysis缺失'}
        unknowns = [issue for issue in unknowns if issue not in obsolete_issues]
    measure_inputs = row.get('measure_inputs')
    if measure_inputs:
        steps.extend({'label': label, 'value': _trace_value(value), 'evidence': refs}
                     for label, value in measure_inputs.items())
    n, d = row.get('numerator'), row.get('denominator')
    expression = '按批次汇总公式计算'
    if spec['kind'] in {'rate', 'ratio'}:
        steps.extend([{'label': '分母贡献', 'value': _trace_value(d)},
                      {'label': '分子贡献', 'value': _trace_value(n)}])
        expression = f'分子 {_trace_value(n)}，分母 {_trace_value(d)}'
    return {'version': 1, 'metric_id': spec['id'], 'counting_rule': spec.get('formula'),
            'retrieval': spec['retrieval'],
            'denominator': {'value': d, 'objects': []},
            'numerator': {'value': n, 'candidate_value': n, 'objects': []},
            'objects': objects, 'steps': steps, 'unknowns': unknowns,
            'calculation': {'expression': expression, 'value': None,
                            'status': row.get('prompt_status') or reason}}


def _traced_detail(spec, row, mode):
    trace = row.get('calculation_trace') or _detail_trace(spec, row)
    return {**row, 'metric_id': spec['id'], 'calculation_mode': mode, 'calculation_trace': trace}


def _sync_trace_row(row):
    """Keep the displayed formula aligned after review/draft overlays."""
    trace = row.get('calculation_trace')
    if not isinstance(trace, dict):
        return row
    denominator = row.get('denominator')
    numerator = row.get('numerator')
    candidate = numerator if numerator is not None else row.get('suggested_numerator')
    trace.setdefault('denominator', {})['value'] = denominator
    trace.setdefault('numerator', {}).update(value=numerator, candidate_value=candidate)
    calculation = trace.setdefault('calculation', {})
    if candidate is not None and denominator:
        calculation['expression'] = f'{candidate} / {denominator} = {candidate / denominator:g}'
        calculation['value'] = candidate / denominator
    else:
        calculation['expression'] = '待确认'
        calculation['value'] = None
    return row


class Context:
    def __init__(self, case_id, events, batch, config, complete=False):
        self.case_id,self.batch,self.config,self.complete = case_id,batch,config,complete
        self.refs = config.get('hospital_reference',{})
        self.events = [e for e in events if not e['attributes'].get('_stale')]
        self.by_kind = {}
        time_index,explicit_index = {},{}
        self.duplicates = set()
        for e in self.events:
            if e['kind']=='link':
                continue
            row = {**e,'_ids':e.get('_ids',[e['id']])}
            group = self.by_kind.setdefault(e['kind'],[])
            def same(existing):
                keys = {'order':'order_id','surgery':'surgery_id','consultation':'consultation_id',
                        'rescue':'rescue_id','critical_value':'critical_id','transfusion':'transfusion_id','anesthesia':'anesthesia_id'}
                key = keys.get(e['kind'])
                explicit = key and e['attributes'].get(key) and e['attributes'].get(key)==existing['attributes'].get(key)
                same_time = e.get('event_time') and e.get('event_time')==existing.get('event_time')
                if e['kind'] != 'surgery' and e.get('event_time') and existing.get('event_time') and e['event_time']!=existing['event_time']:
                    return False
                if e['kind'] == 'surgery':
                    import re as _re
                    def _name(row):
                        return _re.sub(r'[\s\d年月日:/：()（）、，,.-]+', '', str(row['attributes'].get('procedure_name') or '')).replace('择期','')
                    def _day(row):
                        value = ' '.join(str(row.get(k) or '') for k in ('event_time','quote')) + ' ' + str(row['attributes'].get('event_date') or '') + ' ' + str(row['attributes'].get('procedure_name') or '')
                        m = _re.search(r'(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})', str(value))
                        return f'{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}' if m else None
                    names = (_name(e), _name(existing))
                    if _day(e) and _day(e) == _day(existing):
                        if not names[0] or not names[1] or names[0] == names[1] or names[0] in names[1] or names[1] in names[0]:
                            return True
                nested = e['quote'] in existing['quote'] or existing['quote'] in e['quote']
                common = set(e['attributes']) & set(existing['attributes'])
                agree = all(e['attributes'][k]==existing['attributes'][k] for k in common if not k.startswith('_') and
                            e['attributes'][k] is not None and existing['attributes'][k] is not None)
                return (explicit or (same_time and nested)) and agree
            explicit_keys = {'order':'order_id','surgery':'surgery_id','consultation':'consultation_id',
                             'rescue':'rescue_id','critical_value':'critical_id','transfusion':'transfusion_id','anesthesia':'anesthesia_id'}
            ident = e['attributes'].get(explicit_keys.get(e['kind'],''))
            candidates = (explicit_index.get((e['kind'],ident),[]) if ident else
                          self.by_kind.get('surgery',[]) if e['kind']=='surgery' else
                          time_index.get((e['kind'],e.get('event_time')),[]))
            old = next((x for x in candidates if same(x)),None)
            if old:
                old['_ids'].extend(e.get('_ids',[e['id']]))
                old['attributes'] = {**old['attributes'],**{k:v for k,v in e['attributes'].items() if v is not None}}
            else:
                group.append(row)
                time_index.setdefault((e['kind'],e.get('event_time')),[]).append(row)
                if ident:
                    explicit_index.setdefault((e['kind'],ident),[]).append(row)
        # Ambiguous duplicate identities block complete event counting.
        for kind,rows in self.by_kind.items():
            if kind in {'admission','death','discharge','cost','readmission'}:
                continue
            for i,a in enumerate(rows):
                ident = a['attributes'].get(explicit_keys.get(kind,''))
                if ident and len(explicit_index.get((kind,ident),[]))>1:
                    self.duplicates.add(kind)
                if not a.get('event_time'):
                    continue
                for b in time_index.get((kind,a.get('event_time')),[]):
                    if b is a:
                        continue
                    if a.get('event_time') and a.get('event_time')==b.get('event_time') and a['quote']==b['quote']:
                        self.duplicates.add(kind)

    def get(self, kind):
        return self.by_kind.get(kind,[])

    def linked(self, source, relation):
        ids = source.get('_ids',[source['id']])
        targets = set()
        for e in self.events:
            attrs = e['attributes']
            if e['kind']=='link' and attrs.get('source_event_id') in ids and attrs.get('relation')==relation:
                targets.add(attrs.get('target_event_id'))
        return [e for rows in self.by_kind.values() for e in rows if targets & set(e['_ids'])]

    def unique(self, kind, key='event_time'):
        rows = self.get(kind)
        values = {e.get(key) for e in rows if e.get(key)}
        return next(iter(values)) if len(values)==1 else None

    def time(self, kind):
        return parse_time(self.unique(kind))

    def qualifies(self, event):
        t = parse_time(event.get('event_time'))
        if t and event['attributes'].get('event_date') and t.date().isoformat()!=event['attributes']['event_date']:
            return None
        if not self.batch.get('start') and not self.batch.get('end'):
            return True
        day = t.date().isoformat() if t else event['attributes'].get('event_date')
        return ((not self.batch.get('start') or day>=self.batch['start']) and
                (not self.batch.get('end') or day<=self.batch['end'])) if day else None

    def unique_date(self, kind):
        days = set()
        for e in self.get(kind):
            t = parse_time(e.get('event_time'))
            if t:
                days.add(t.date().isoformat())
            if e['attributes'].get('event_date'):
                days.add(e['attributes']['event_date'])
        return next(iter(days)) if len(days)==1 else None

    def row(self, key, denominator, numerator, reason, events=()):
        if denominator is None:
            numerator = None
        if denominator is False:
            denominator,numerator = 0,0
        elif denominator is True:
            denominator = 1
        if type(numerator) is bool:
            numerator = int(numerator)
        return {'case_id':self.case_id,'item_key':key,'denominator':denominator,'numerator':numerator,
                'reason':reason,'evidence':evidence(events),'fact_ids':[i for e in events for i in e.get('_ids',[e['id']])],
                'origin':'core35_rules'}

    def case_row(self, numerator, reason, denominator=True, events=(), anchor='admission'):
        rows = self.get(anchor)
        day = self.unique_date(anchor)
        if not rows or (not day and (self.batch.get('start') or self.batch.get('end'))):
            denominator = None
            reason += '；统计期间所需的时间缺失或冲突'
        elif day and ((self.batch.get('start') and day<self.batch['start']) or (self.batch.get('end') and day>self.batch['end'])):
            denominator = False
        if denominator is None:
            numerator = None
        return self.row('admission',denominator,numerator,reason,list(rows)+list(events))

    def grade(self, surgery):
        attrs = surgery['attributes']
        grades = self.refs.get('surgery_grades',{})
        mapped = grades.get(attrs.get('procedure_code')) or grades.get(attrs.get('procedure_name'))
        literal = attrs.get('grade')
        if mapped and literal and mapped!=literal:
            return None
        return mapped or literal


def within(start, end, seconds):
    start,end = parse_time(start),parse_time(end)
    return None if not start or not end else 0 <= (end-start).total_seconds() <= seconds


def _trace_core01(ctx, row):
    admissions = ctx.get('admission')
    transfers = ctx.get('transfer')
    transfer_orders = [e for e in ctx.get('order') if e['attributes'].get('transfer_candidate')]
    admission_time = ctx.unique('admission')
    objects, numerator_objects, unknowns = [], [], []
    for event in transfers:
        attrs = event['attributes']
        basis = ctx.config.get('transfer_time_basis', 'record_time')
        transfer_time = event.get(basis)
        t = parse_time(transfer_time)
        a = parse_time(admission_time)
        hours = ((t-a).total_seconds()/3600) if t and a else None
        from_icu = attrs.get('from_icu') if type(attrs.get('from_icu')) is bool else None
        to_icu = attrs.get('to_icu') if type(attrs.get('to_icu')) is bool else None
        within_48 = hours is not None and 0 <= hours <= 48
        qualifies = (attrs.get('occurred') is True and within_48 and
                     from_icu is False and to_icu is False)
        state = qualifies if transfer_time and a and from_icu is not None and to_icu is not None else None
        obj = {'object_id': event.get('id'), 'status': _trace_state(state),
               'event_time': transfer_time, 'interval_hours': hours,
               'occurred': _trace_state(attrs.get('occurred')),
               'from_department': _trace_value(attrs.get('from_department')),
               'to_department': _trace_value(attrs.get('to_department')),
               'from_icu': _trace_state(from_icu), 'to_icu': _trace_state(to_icu),
               'evidence': evidence([event])}
        objects.append(obj)
        if state is True:
            numerator_objects.append(event)
        elif state is None:
            unknowns.append(f'转科事实 {event.get("id")} 的实际发生或 ICU 排除条件待确认')
    steps = [
        {'label': '入院时间', 'value': _trace_value(admission_time), 'evidence': evidence(admissions)},
        {'label': '转科时间', 'value': _trace_value(transfers[0].get(ctx.config.get('transfer_time_basis', 'record_time')) if transfers else None),
         'evidence': evidence(transfers[:1])},
        {'label': '时间窗口', 'value': ('满足48小时' if any(o.get('interval_hours') is not None and o['interval_hours'] <= 48 for o in objects)
                                        else '待确认'), 'evidence': evidence(transfers)},
        {'label': '是否实际转科', 'value': _trace_state(transfers[0]['attributes'].get('occurred') if transfers else None),
         'evidence': evidence(transfers[:1])},
        {'label': 'ICU排除条件', 'value': ('待确认' if unknowns else '已确认非ICU'), 'evidence': evidence(transfers)},
    ]
    if transfer_orders:
        unknowns.append('发现转科医嘱线索，但医嘱本身不能证明实际转科已发生')
    return _calculation_trace('core_01', ctx, row, rule='入院后48小时内实际转科，排除ICU转入/转出',
                              denominator_objects=admissions, numerator_objects=numerator_objects,
                              steps=steps, objects=objects, unknowns=unknowns)


def _trace_core09(ctx, row, grade4, ordered, level):
    last = ordered[-1] if ordered else None
    level_ok = any(x in level for x in ('特级','一级','1级','I级')) if level else None
    steps = [
        {'label': '四级手术', 'value': f'{len(grade4)}台' if grade4 else '待确认', 'evidence': evidence(grade4)},
        {'label': '出院前最后护理级别', 'value': _trace_value(level), 'evidence': evidence([last] if last else ctx.get('discharge'))},
        {'label': '是否为特级或一级护理', 'value': _trace_state(level_ok), 'evidence': evidence([last] if last else ctx.get('discharge'))},
    ]
    return _calculation_trace('core_09', ctx, row, rule='四级手术患者中，出院前最后护理级别为特级或一级',
                              denominator_objects=ctx.get('admission'), numerator_objects=grade4 if level_ok else (),
                              steps=steps, objects=[{'object_id': e.get('id'), 'procedure_name': e['attributes'].get('procedure_name'),
                                                     'grade': ctx.grade(e), 'evidence': evidence([e])} for e in grade4],
                              unknowns=[] if level_ok is not None else ['四级手术或最后护理级别待确认'])


def _trace_core17(ctx, row, surgery, related, planned, actual):
    p = normalized_name(planned, ctx.refs.get('procedure_aliases'))
    a = normalized_name(actual, ctx.refs.get('procedure_aliases'))
    match = p == a if p and a else None
    steps = [
        {'label': '实际手术名称', 'value': _trace_value(actual), 'evidence': evidence([surgery])},
        {'label': '术前讨论拟手术名称', 'value': _trace_value(planned), 'evidence': evidence(related)},
        {'label': '名称匹配', 'value': _trace_state(match), 'evidence': evidence([surgery] + related)},
    ]
    return _calculation_trace('core_17', ctx, row, rule='实际手术总次数中，术前讨论拟手术名称与手术记录名称一致的次数',
                              denominator_objects=[surgery], numerator_objects=[surgery] if match else (),
                              steps=steps, objects=[{'object_id': surgery.get('id'), 'status': _trace_state(match),
                                                     'planned_name': planned, 'actual_name': actual,
                                                     'evidence': evidence([surgery] + related)}],
                              unknowns=[] if match is not None else ['术前讨论或实际手术名称缺失'])


def _trace_core23(ctx, row, event, start_day, stop_day):
    same_day = bool(row['numerator']) if row['denominator'] == 1 and row['numerator'] is not None else None
    steps = [
        {'label': '医嘱类型', 'value': _trace_value(event['attributes'].get('order_type')), 'evidence': evidence([event])},
        {'label': '起始日期', 'value': _trace_value(start_day), 'evidence': evidence([event])},
        {'label': '停止日期', 'value': _trace_value(stop_day), 'evidence': evidence([event])},
        {'label': '起止日期是否同时存在且同日', 'value': _trace_state(same_day), 'evidence': evidence([event])},
    ]
    return _calculation_trace('core_23', ctx, row, rule='长期医嘱同时存在起始和停止日期并比较自然日',
                              denominator_objects=[event] if row['denominator'] == 1 else (),
                              numerator_objects=[event] if same_day else (),
                              steps=steps, objects=[{'object_id': event.get('id'), 'status': _trace_state(same_day),
                                                     'start_day': start_day, 'stop_day': stop_day,
                                                     'evidence': evidence([event])}],
                              unknowns=['医嘱类型或日期待核对'] if same_day is None and row['denominator'] != 0 else [])


def preop(ctx, surgery):
    discussions = [e for e in ctx.linked(surgery,'discussion_for_surgery')
                   if e['attributes'].get('discussion_type') in {'preoperative','multidisciplinary'}]
    values = []
    for d in discussions:
        complete = d['attributes'].get('complete')
        if surgery['attributes'].get('emergency_lifesaving') is True:
            values.append(complete)
            continue
        dt = parse_time(d['attributes'].get('completion_time') or d.get('record_time'))
        orders = ctx.linked(surgery,'order_for_surgery')
        consents = ctx.linked(surgery,'consent_for_surgery')
        times = [parse_time(e.get('event_time')) for e in orders]+[
                 parse_time(e['attributes'].get('patient_signed_time')) for e in consents]
        if dt and orders and consents and all(times):
            values.append(all_of([complete,all(dt<t for t in times)]))
        else:
            values.append(None)
    return any_of(values,ctx.complete),discussions


def outcome_after_rescue(ctx, rescue):
    start = parse_time(rescue.get('event_time'))
    if not start:
        return None
    end = parse_time(rescue['attributes'].get('end_time')) or start
    survival = parse_time(rescue['attributes'].get('survival_confirmed_until'))
    death = ctx.time('death')
    if survival and death and survival>death:
        return None
    if survival and (survival-end).total_seconds()>86400:
        return True
    next_rescues = [parse_time(e.get('event_time')) for e in ctx.get('rescue')]
    next_rescues = [t for t in next_rescues if t and t>start]
    if next_rescues and (not death or min(next_rescues)<=death):
        return True
    if death and death>=end:
        return (death-end).total_seconds()>86400
    discharge = ctx.time('discharge')
    return True if discharge and not death and (discharge-end).total_seconds()>86400 else None


def death_discussion_timely(ctx, discussion):
    death = ctx.unique_date('death')
    completed = parse_time(discussion['attributes'].get('completion_time') or discussion.get('record_time'))
    completed_day = completed.date().isoformat() if completed else discussion['attributes'].get('record_date')
    death_time = ctx.time('death')
    if death_time and completed and completed<death_time:
        return None
    ref = ctx.refs
    if not death or not completed_day or completed_day<death or not ref.get('calendar_start') or not ref.get('calendar_end'):
        return None
    if not ref['calendar_start']<=death<=ref['calendar_end']:
        return None
    days = sorted(set(d for d in ref.get('workdays',[]) if d>death))
    if len(days)<5 or days[4]>ref['calendar_end']:
        return None
    return completed_day<=days[4]


def case_items(number, ctx):
    surgeries = ctx.get('surgery')
    discussions = ctx.get('discussion')
    # For the surgery-count indicator, use dated operation events.  Undated
    # anesthesia, handover and postoperative forms are supporting documents
    # for an operation already represented by the dated front-sheet/report
    # fact, not additional operations.
    if number in (15, 17, 18):
        surgeries = [e for e in surgeries if e.get('event_time')]
    if number in (1,2):
        result = automatic_case(f'core_{number:02}',ctx.case_id,ctx.events,ctx.batch,ctx.config)
        # Draft events still carry their original reviewed flag. For a draft only, the caller supplies a copy.
        if result['denominator']==1 and result['numerator'] is None and ctx.complete:
            ambiguous = False
            if number==1:
                transfer_orders = [e for e in ctx.get('order')
                                   if e['attributes'].get('transfer_candidate')]
                for e in ctx.get('transfer'):
                    a = e['attributes']
                    if a.get('occurred') is False:
                        continue
                    if not e.get(ctx.config['transfer_time_basis']) or a.get('occurred') is None:
                        ambiguous = True
                    if not a.get('from_department') and type(a.get('from_icu')) is not bool:
                        ambiguous = True
                    if not a.get('to_department') and type(a.get('to_icu')) is not bool:
                        ambiguous = True
                # A transfer-like temporary order is useful evidence for
                # locating the event time, but it lacks the completed
                # transfer/department fields needed for an automatic result.
                # Keep the case pending review instead of treating the order
                # as proof that a transfer occurred.
                if transfer_orders:
                    ambiguous = True
                ambiguous = ambiguous or '；' in result['reason']
            else:
                ambiguous = any(not e.get('event_time') or e['attributes'].get('order_category') in {None,'unknown'} for e in ctx.get('order'))
            if not ambiguous:
                result['numerator'] = 0
                result['reason'] = '完整枚举后没有符合条件的事件'
        if number == 1:
            result['calculation_trace'] = _trace_core01(ctx, result)
        return [result]
    if number==3:
        rounds = ctx.get('ward_round')
        values = []
        for e in rounds:
            a = e['attributes']
            role = a.get('physician_role')
            senior = None if not role else any(x in role for x in ('主治','副主任','主任'))
            if senior is False:
                continue
            values.append(all_of([senior, bool(e.get('event_time')) or None, bool(a.get('analysis')) or None,
                                  bool(a.get('plan')) or None, a.get('norm_compliant')]))
        value = all_of(values) if values and ctx.complete else (False if ctx.complete and not values else None)
        return [ctx.case_row(value,'核对全部上级医师查房的要素、规范及完整性',events=rounds)]
    if number==4:
        value = any_of([e['attributes'].get('unplanned') for e in surgeries],ctx.complete)
        return [ctx.case_row(value,'是否存在有证据的非计划手术',events=surgeries)]
    if number in (5,6,7,8):
        urgency = 'emergency' if number in (5,6) else 'ordinary'
        rows = []
        for c in ctx.get('consultation'):
            if c['attributes'].get('consultation_role') == 'opinion_only':
                continue
            kind = c['attributes'].get('urgency')
            if kind and kind not in {urgency,'unknown'}:
                continue
            d = all_of([ctx.qualifies(c), kind==urgency if kind in {'emergency','ordinary'} else None])
            related = ctx.linked(c,'implements_consultation')
            if number in (5,7):
                endpoint = c['attributes'].get('arrival_time' if number==5 else 'completion_time')
                n = within(c.get('event_time'),endpoint,600 if number==5 else 86400)
            else:
                n = any_of([within(c.get('event_time'),o.get('event_time'),2400 if number==6 else 86400) for o in related],ctx.complete)
            rows.append(ctx.row(c['id'],d,n,'按会诊类型、申请时间和完成/到位/关联医嘱时间判定',[c]+related))
        return rows
    if number==9:
        # Indicator 9 uses the four-level operation population.  The grade
        # may come from the operation record or the second page of the front
        # sheet; an operation without a confirmed grade-4 classification is
        # not placed in this denominator.
        grade4 = [e for e in surgeries if ctx.grade(e) == 4]
        surgery_exists = True if grade4 else (False if ctx.complete else None)
        # The supplied guide locates the last nursing level in long-term orders.
        # Prefer the latest dated long-term order, then use an explicit discharge field as fallback.
        orders = [e for e in ctx.get('order') if e['attributes'].get('order_type')=='long' and e['attributes'].get('nursing_level')]
        ordered = sorted(orders, key=lambda e: parse_time(e.get('event_time')) or datetime.min)
        level = ordered[-1]['attributes'].get('nursing_level') if ordered else None
        if level is None:
            levels = {e['attributes'].get('nursing_level') for e in ctx.get('discharge') if e['attributes'].get('nursing_level')}
            level = next(iter(levels)) if len(levels)==1 else None
        n = any(x in level for x in ('特级','一级','1级','I级')) if level else None
        evidence_events = grade4 + orders + ctx.get('discharge')
        row = ctx.case_row(n,'按长期医嘱中出院前最后护理级别判定',surgery_exists,evidence_events,'admission')
        row['calculation_trace'] = _trace_core09(ctx, row, grade4, ordered, level)
        return [row]
    if number==10:
        graded = [e for e in surgeries if ctx.grade(e)==4]
        d = True if graded else (None if any(ctx.grade(e) is None for e in surgeries) or not ctx.complete else False)
        values,refs = [],[]
        for s in graded:
            linked = ctx.linked(s,'handover_for_surgery'); refs += linked
            st = parse_time(s.get('event_time'))
            flags = []
            for e in linked:
                nt = parse_time(e.get('event_time'))
                flags.append(st.date()==nt.date() if st and nt and e['attributes'].get('nursing_type')=='bedside_handover' else None)
            values.append(any_of(flags,ctx.complete))
        n = all_of(values) if values and ctx.complete else None
        return [ctx.case_row(n,'同次住院全部四级手术当日床旁交接均须满足',d,graded+refs)]
    if number in (11,12):
        unplanned = any_of([e['attributes'].get('unplanned') for e in ctx.get('readmission')]+[
                          all_of([e['attributes'].get('unplanned'),e['attributes'].get('reoperation')]) for e in surgeries],ctx.complete)
        difficult = [e for e in discussions if e['attributes'].get('discussion_type')=='difficult']
        has_discussion = True if difficult else (False if ctx.complete else None)
        d = unplanned if number==11 else all_of([unplanned,has_discussion])
        n = has_discussion if number==11 else any_of([
            all_of([e['attributes'].get('complete'),bool(e['attributes'].get('conclusion')) or None]) for e in difficult],ctx.complete)
        return [ctx.case_row(n,'非计划再次住院/手术与疑难讨论及结论关联判定',d,surgeries+ctx.get('readmission')+difficult)]
    if number==13:
        values = {e['attributes'].get('total') for e in ctx.get('cost') if e['attributes'].get('total') is not None}
        d = next(iter(values))>=ctx.config.get('high_cost_threshold',200000) if len(values)==1 else None
        found = [e for e in discussions if e['attributes'].get('discussion_type')=='difficult']
        return [ctx.case_row(True if found else (False if ctx.complete else None),'住院总费用达到阈值并有疑难病例讨论',d,ctx.get('cost')+found)]
    if number==14:
        return [ctx.row(e['id'],ctx.qualifies(e),outcome_after_rescue(ctx,e),'按抢救例次及存活证据判断',[e]+ctx.get('death')+ctx.get('rescue')+ctx.get('discharge')) for e in ctx.get('rescue')]
    if number in (15,16,17,18,28):
        rows = []
        for s in surgeries:
            d = ctx.qualifies(s)
            preop_complete, related = preop(ctx,s)
            if number==28:
                grade = ctx.grade(s)
                d = all_of([d, grade==4 if grade is not None else None])
            if number==16:
                d = all_of([d,preop_complete])
            attrs = s['attributes']
            if number==15:
                # Initial count is based on an explicit surgery-to-discussion
                # relation. Completeness of the discussion fields is reviewed
                # separately and should not erase the count.
                n = True if related else (False if ctx.complete else None)
            elif number==16:
                surgeon = attrs.get('surgeon')
                n = any_of([surgeon in r['attributes'].get('participants',[]) if surgeon and r['attributes'].get('participants') is not None else None for r in related],ctx.complete)
            elif number==17:
                planned = attrs.get('planned_procedure') or next((r['attributes'].get('planned_procedure') for r in related if r['attributes'].get('planned_procedure')),None)
                p = normalized_name(planned,ctx.refs.get('procedure_aliases'))
                actual = normalized_name(attrs.get('procedure_name'),ctx.refs.get('procedure_aliases'))
                n = True if p and p==actual else attrs.get('procedure_consistent')
            elif number==18:
                planned = attrs.get('planned_surgeon_id') or attrs.get('planned_surgeon') or next((r['attributes'].get('planned_surgeon') for r in related if r['attributes'].get('planned_surgeon')),None)
                actual = attrs.get('surgeon_id') or attrs.get('surgeon')
                mapping = ctx.refs.get('physician_ids',{})
                planned,actual = mapping.get(planned,planned),mapping.get(actual,actual)
                n = planned==actual if planned and actual else None
            else:
                values = []
                for r in related:
                    st,dt = parse_time(s.get('event_time')),parse_time(r['attributes'].get('completion_time') or r.get('record_time'))
                    departments = r['attributes'].get('departments')
                    values.append(all_of([dt<st if dt and st else None,len(set(departments))>=3 if departments is not None else None]))
                n = any_of(values,ctx.complete)
            refs = [s]+related+ctx.linked(s,'consent_for_surgery')+ctx.linked(s,'order_for_surgery')
            row = ctx.row(s['id'],d,n,'依据具体手术关联的术前讨论、计划及实际记录判定',refs)
            if number == 17:
                row['calculation_trace'] = _trace_core17(ctx, row, s, related, planned, actual)
            rows.append(row)
        return rows
    if number in (19,20,21,22):
        death_rows = ctx.get('death')
        if not death_rows:
            return [ctx.row('death',False if ctx.complete else None,None,'是否死亡尚需完整枚举',[])]
        if ctx.unique_date('death') is None:
            return [ctx.row('death',None,None,'死亡时间冲突或缺失',death_rows)]
        q = ctx.qualifies(death_rows[0])
        relevant = [e for e in discussions if e['attributes'].get('discussion_type')=='death']
        if number==19:
            n = any_of([death_discussion_timely(ctx,e) for e in relevant],ctx.complete)
            return [ctx.row('death',q,n,'按医院完整工作日历计算5个工作日',death_rows+relevant)]
        if number==20:
            disputes = ctx.get('dispute')
            d = any_of([e['attributes'].get('death_related') for e in disputes],ctx.complete and ctx.refs.get('dispute_inventory_complete',False))
            d = all_of([q,d])
            n = any_of([('医务' in e['attributes']['organizer']) if e['attributes'].get('organizer') else None for e in relevant],ctx.complete)
            if q is False:
                n = False
            elif q is None:
                n = None
            row = ctx.row('death',d,n,'分子为医务部门组织讨论病例，分母为纠纷死亡病例，两集合独立',death_rows+relevant+disputes)
            row['numerator'] = int(n) if type(n) is bool else n
            return [row]
        if number==21:
            values = []
            for e in relevant:
                attrs = e['attributes']
                role,host = attrs.get('host_role'),attrs.get('host')
                if role and '科主任' in role and '副科主任' not in role:
                    values.append(True)
                elif host and ctx.refs.get('department_heads'):
                    department = attrs.get('department')
                    heads = ctx.refs['department_heads']
                    values.append(host==heads[department] if department in heads else None)
                elif role and '副科主任' in role:
                    values.append(False)
                else:
                    values.append(None)
            return [ctx.row('death',q,any_of(values,ctx.complete),'死亡讨论主持人身份判定',death_rows+relevant)]
        uploads = ctx.get('upload')
        if not uploads:
            return [ctx.row('death',None,None,'缺少应上传名单及实际上传结果',death_rows)]
        required = {e['attributes'].get('required') for e in uploads}
        uploaded = {e['attributes'].get('complete') for e in uploads}
        d = all_of([q,next(iter(required))]) if len(required)==1 else None
        n = next(iter(uploaded)) if len(uploaded)==1 else None
        return [ctx.row('death',d,n,'按上传名单和完整上传结果判定',death_rows+uploads)]
    if number==23:
        rows = []
        for e in ctx.get('order'):
            typ = e['attributes'].get('order_type')
            if typ=='temporary':
                continue
            d = all_of([ctx.qualifies(e),True if typ=='long' else None])
            start,stop = parse_time(e.get('event_time')),parse_time(e['attributes'].get('stop_time'))
            start_day = start.date().isoformat() if start else e['attributes'].get('event_date')
            stop_day = stop.date().isoformat() if stop else e['attributes'].get('stop_date')
            n = start_day==stop_day if start_day and stop_day and not (start and stop and stop<start) else None
            row = ctx.row(e['id'],d,n,'逐条比较长期医嘱开立和停止自然日',[e])
            row['input_values'] = {'order_type': typ, 'start_date': start_day, 'stop_date': stop_day,
                                   'start_time': e.get('event_time'), 'stop_time': e['attributes'].get('stop_time')}
            row['input_source'] = e.get('origin')
            row['missing_fields'] = [key for key in ('order_type', 'start_date', 'stop_date')
                                     if row['input_values'][key] is None]
            row['calculation_trace'] = _trace_core23(ctx, row, e, start_day, stop_day)
            rows.append(row)
        return rows
    if number==32:
        rows = []
        for e in ctx.get('critical_value'):
            treatment = ctx.linked(e,'treatment_for_critical_value')
            progress = ctx.linked(e,'progress_for_critical_value') + ctx.linked(e,'round_for_critical_value')
            dt = parse_time(e.get('event_time'))
            day = dt.date().isoformat() if dt else e['attributes'].get('event_date')
            def same_day(rows):
                values = []
                for x in rows:
                    t = parse_time(x.get('event_time'))
                    other_day = t.date().isoformat() if t else x['attributes'].get('event_date')
                    values.append(day==other_day if day and other_day else None)
                return any_of(values,ctx.complete)
            rows.append(ctx.row(e['id'],ctx.qualifies(e),all_of([same_day(treatment),same_day(progress)]),
                                 '危急值出现当日有相关处置医嘱及病程证据',[e]+treatment+progress))
        return rows
    if number==33:
        drugs = ctx.refs.get('special_antibiotics')
        rows = []
        for e in ctx.get('order'):
            if e['attributes'].get('is_medication') is False:
                continue
            name = e['attributes'].get('drug_name')
            d = all_of([ctx.qualifies(e),name in drugs if name and drugs is not None else None])
            if d is False:
                continue
            linked = ctx.linked(e,'consultation_for_antibiotic')
            values = []
            for c in linked:
                ot,ct = parse_time(e.get('event_time')),parse_time(c['attributes'].get('completion_time') or c.get('record_time'))
                values.append(ct<=ot if ct and ot else None)
            rows.append(ctx.row(e['id'],d,any_of(values,ctx.complete),'特殊使用级药物名单及用药前对应会诊',[e]+linked))
        return rows
    if number==34:
        rows = []
        for e in ctx.get('transfusion'):
            linked = ctx.linked(e,'assessment_for_transfusion')
            values = []
            for a in linked:
                end = parse_time(e['attributes'].get('end_time'))
                at = parse_time(a.get('event_time'))
                values.append(all_of([at>=end if at and end else None,a['attributes'].get('norm_compliant')]))
            rows.append(ctx.row(e['id'],ctx.qualifies(e),any_of(values,ctx.complete),'实际输血治疗事件及关联的输血后规范评估',[e]+linked))
        return rows
    if number==35:
        values,autologous,refs = [],[],[]
        for s in surgeries:
            linked = ctx.linked(s,'transfusion_for_surgery'); refs += linked
            intra = s['attributes'].get('intraoperative_transfusion')
            auto = s['attributes'].get('autotransfusion')
            if linked:
                intra = any_of([t['attributes'].get('intraoperative') for t in linked],ctx.complete)
                auto = any_of([all_of([t['attributes'].get('intraoperative'),t['attributes'].get('autologous')]) for t in linked],ctx.complete)
            values.append(intra); autologous.append(auto)
        return [ctx.case_row(any_of(autologous,ctx.complete),'住院患者是否术中输血及自体血回输',any_of(values,ctx.complete),surgeries+refs)]
    return []


EVENT_METRICS = {5,6,7,8,14,15,16,17,18,23,24,25,28,32,33,34}


def overlap_items(contexts, number, batch_complete):
    surgeries, interval_rows = [],[]
    for ctx in contexts:
        for s in ctx.get('surgery'):
            surgeries.append((ctx,s))
            targets = [s] if number==24 else ctx.linked(s,'anesthesia_for_surgery')
            for e in targets:
                attrs = e['attributes']
                person = (attrs.get('surgeon_id') or attrs.get('surgeon')) if number==24 else (attrs.get('physician_id') or attrs.get('physician'))
                person = ctx.refs.get('physician_ids',{}).get(person,person)
                start,end = parse_time(e.get('event_time')),parse_time(attrs.get('end_time'))
                interval_rows.append((ctx,s,e,person,start,end))
    overlapping = set()
    for i,a in enumerate(interval_rows):
        for b in interval_rows[i+1:]:
            if a[1]['id']==b[1]['id'] and a[0].case_id==b[0].case_id:
                continue
            if a[3] and a[3]==b[3] and all(a[4:]) and all(b[4:]) and a[4]<a[5] and b[4]<b[5] and max(a[4],b[4])<min(a[5],b[5]):
                overlapping.update([(a[0].case_id,a[1]['id']),(b[0].case_id,b[1]['id'])])
    all_intervals_complete = len({(x[0].case_id,x[1]['id']) for x in interval_rows})==len(surgeries) and all(
        x[3] and x[4] and x[5] and x[4]<x[5] for x in interval_rows)
    rows = []
    for ctx,s in surgeries:
        related = [x[2] for x in interval_rows if x[0].case_id==ctx.case_id and x[1]['id']==s['id']]
        positive = (ctx.case_id,s['id']) in overlapping
        n = True if positive else (False if batch_complete and all_intervals_complete else None)
        rows.append(ctx.row(s['id'],ctx.qualifies(s),n,'跨病例按医师身份比较时间区间；相接边界不算重合',[s]+related))
    return rows


def inventory_unknown(ctx, number):
    return ctx.row('coverage_unknown',None,None,'统计对象尚未完整枚举/复核，或有疑似重复事件',[])


def special_measures(number, contexts, config):
    refs = config.get('hospital_reference',{})
    values,details,missing = {},[],[]
    if number in (26,27):
        values = {'four_success':0,'four_total':0,'three_success':0,'three_total':0}
        for ctx in contexts:
            if not ctx.complete:
                missing.append('各级手术/患者统计对象尚未完整枚举')
            surgeries = ctx.get('surgery')
            if number==26:
                for s in surgeries:
                    q,g = ctx.qualifies(s),ctx.grade(s)
                    if q is False or g in (1,2):
                        continue
                    related = ctx.linked(s,'complication_of_surgery')
                    n = any_of([e['attributes'].get('surgery_related') for e in related],ctx.complete)
                    row = ctx.row(s['id'],q,n,'手术级别及关联并发症',[s]+related)
                    row['measure_inputs'] = {'手术级别': g, '关联并发症': _trace_state(n)}
                    details.append(row)
                    if q is None or g not in (3,4) or n is None:
                        missing.append('手术级别、期间或并发症判定缺失'); continue
                    prefix = 'four' if g==4 else 'three'
                    values[prefix+'_total'] += 1; values[prefix+'_success'] += int(n)
            else:
                if not surgeries:
                    continue
                grades = [ctx.grade(s) for s in surgeries]
                grade = max(grades) if all(g is not None for g in grades) else None
                row = ctx.case_row(True if ctx.get('death') else (False if ctx.complete else None),
                                   '同次住院按最高手术级别归组，分别计算患者死亡率',events=surgeries+ctx.get('death'))
                row['measure_inputs'] = {'住院期间最高手术级别': grade}
                details.append(row)
                if row['denominator']==0 or grade in (1,2):
                    continue
                if grade not in (3,4) or row['denominator'] is None or row['numerator'] is None:
                    missing.append('患者手术级别、期间或死亡情况缺失'); continue
                prefix = 'four' if grade==4 else 'three'
                values[prefix+'_total'] += 1; values[prefix+'_success'] += row['numerator']
    elif number==29:
        if 'registered_types' not in refs:
            return {},[],['缺少备案术种目录']
        values = {'registered_types':refs['registered_types'],'performed_types':[]}
        for ctx in contexts:
            if not ctx.complete:
                missing.append('实际开展手术尚未完整枚举')
            for s in ctx.get('surgery'):
                q,g = ctx.qualifies(s),ctx.grade(s)
                if q is False or g in (1,2):
                    continue
                name = s['attributes'].get('procedure_code') or s['attributes'].get('procedure_name')
                name = normalized_name(name,refs.get('procedure_aliases'))
                if q is None or g not in (3,4) or not name:
                    missing.append('术种、级别或期间缺失')
                else:
                    values['performed_types'].append(name)
                row = ctx.row(s['id'],q,None,'三四级实际开展术种，按统一编码去重',[s])
                row['measure_inputs'] = {'术种名称或编码': name, '手术级别': g}
                details.append(row)
    elif number==30:
        if not refs.get('project_cycle_start') or 'projects' not in refs or not refs.get('project_inventory_complete'):
            return {},[],['缺少完整四年项目台账及评估周期']
        first = refs['project_cycle_start']; values = dict.fromkeys('ABCDEF',0); seen = set()
        for project in refs['projects']:
            if not isinstance(project,dict) or not project.get('project_id') or project['project_id'] in seen:
                missing.append('项目标识缺失或重复'); continue
            seen.add(project['project_id'])
            if type(project.get('introduced_year')) is not int or not isinstance(project.get('carried_years'),list):
                missing.append('项目新增年份或开展年份缺失'); continue
            year,carried = project['introduced_year'],set(project['carried_years'])
            if year==first+1:
                key = 'A' if first+2 in carried and first+3 in carried else ('B' if first+2 in carried else ('C' if first+3 in carried else 'D'))
            elif year==first+2:
                key = 'E' if first+3 in carried else 'F'
            else:
                continue
            values[key] += 1
    elif number==31:
        values = {'minutes':[],'eligible_total':0}
        for ctx in contexts:
            if not ctx.complete:
                missing.append('危急值统计条目尚未完整枚举')
            for e in ctx.get('critical_value'):
                q = ctx.qualifies(e)
                if q is False:
                    continue
                start,end = parse_time(e.get('event_time')),parse_time(e['attributes'].get('received_time'))
                values['eligible_total'] += 1
                minutes = None
                if q is None or not start or not end or end<start:
                    missing.append('危急值出现/接收时间或期间缺失')
                else:
                    minutes = (end-start).total_seconds()/60
                    values['minutes'].append(minutes)
                row = ctx.row(e['id'],q,None,'危急值出现到临床接收时长',[e])
                row['measure_inputs'] = {'危急值出现时间': e.get('event_time'),
                                         '临床接收时间': e['attributes'].get('received_time'),
                                         '报告时长（分钟）': minutes}
                details.append(row)
    return values,details,sorted(set(missing))


def evaluate_core(store, batch, config, draft=False):
    cases = store.cases(batch['id'])
    # Read each case once; a large batch must not deserialize all facts 35 times.
    from .rule_extract import rule_first_events, batch_mode
    from .candidate_scope import scoped_events
    mode = batch_mode(store, batch['id'], config)
    fact_cache = {c['id']:rule_first_events(scoped_events(store,c['id'],config,mode)) for c in cases}
    coverage_cache = {(r['case_id'],r['metric_id']):r for r in store.all('SELECT * FROM coverage WHERE batch_id=?',(batch['id'],))}
    approval_cache = {r['metric_id']:r for r in store.all('SELECT * FROM rule_approvals WHERE batch_id=?',(batch['id'],))}
    review_cache = {}
    for r in store.all('SELECT * FROM reviews WHERE batch_id=?',(batch['id'],)):
        review_cache.setdefault((r['case_id'],r['metric_id']),[]).append(r)
    draft_complete = {}
    if draft:
        for c in cases:
            # Processing every page/request does not establish clinical enumeration.
            draft_complete[c['id']] = False
    results,details = {},[]
    for spec in core_specs():
        number = int(spec['id'][-2:]); mid = spec['id']
        if draft and mid in PROMPT_METRIC_IDS:
            prompt_result, prompt_details = prompt_summary(store, batch['id'], spec)
            if prompt_result is not None:
                if mid in EXTERNAL_GUIDANCE_IDS:
                    prompt_result['missing'] = ['医院尚未给出具体指导']
                results[mid] = prompt_result
                details.extend(_traced_detail(spec, row, 'prompt') for row in prompt_details)
                continue
            # The selected complex indicators are Prompt-first.  Do not show
            # an old partial rule result as if it were the requested semantic
            # calculation when the model candidate has not been generated.
            results[mid] = {
                'numerator': None,
                'denominator': None,
                'value': None,
                'status': '待调用Prompt',
                'missing': (['医院尚未给出具体指导'] if mid in EXTERNAL_GUIDANCE_IDS else
                            ['尚未生成Prompt模型候选；请启用语义模型重新处理']),
                'calculation_source': 'prompt',
            }
            continue
        contexts = []
        items = []
        for case in cases:
            events = fact_cache[case['id']]
            available = [e for e in events if not e['attributes'].get('_stale')]
            coverage = coverage_cache.get((case['id'],mid))
            complete = bool(coverage and coverage['complete'])
            allowed = set(spec['fact_types'])|{'admission'}
            if number == 9:
                # The denominator is a four-level surgery population, but the
                # numerator requires the last long-term nursing order.
                allowed.add('order')
            from .specs import RELATIONS
            relevant_relations = {r for r,pair in RELATIONS.items() if set(pair)<=allowed}
            available = [e for e in available if e['kind'] in allowed or
                         (e['kind']=='link' and e['attributes'].get('relation') in relevant_relations)]
            relevant = available
            if not draft and any(not e['verified'] for e in relevant):
                complete = False
            if draft:
                complete = complete or draft_complete[case['id']]
                questionable = [e for e in available if e['attributes'].get('_validation_issues') and not e['verified']]
                # For indicator 9, a long-order row with a valid nursing level
                # remains usable evidence even when unrelated order fields
                # such as stop_time still need review.
                if number == 9:
                    questionable = [e for e in questionable
                                    if not (e['kind'] == 'order' and e['attributes'].get('nursing_level'))]
                if questionable:
                    complete = False
                selected = [{**e,'verified':True} for e in available if e not in questionable]
            else:
                selected = [e for e in available if e['verified']]
            ctx = Context(case['id'],selected,batch,config,complete)
            if set(spec['fact_types']) & ctx.duplicates:
                ctx.complete = False
            contexts.append(ctx)
        if number in (26,27,29,30,31):
            values,rows,missing = special_measures(number,contexts,config)
            items.extend(rows)
            try:
                result = calculate_measures(spec,values)
            except ValueError:
                result = {'value':None,'numerator':None,'denominator':None,'status':'无法计算','missing':['补充目录/统计值不满足规则']}
            result['measures'] = values
            if missing:
                result.update(value=None,status='待复核',missing=sorted(set(result.get('missing',[])+missing)))
        else:
            if number in (24,25):
                items = overlap_items(contexts,number,all(c.complete for c in contexts))
            else:
                for ctx in contexts:
                    rows = case_items(number,ctx)
                    if draft and number == 13 and len(rows) == 1:
                        row = rows[0]
                        # For the current preliminary workflow, a high-cost
                        # case with no extracted difficult-case discussion is
                        # shown as an initial negative. Human review can
                        # revise it if a discussion record is later found.
                        if row.get('denominator') in {True, 1} and row.get('numerator') is None:
                            row = {**row, 'numerator': 0,
                                   'reason': '当前提取范围未发现疑难病例讨论，自动初判为0，待人工审核'}
                            rows = [row]
                    if draft and number in (15, 17):
                        rows = [{**row, 'numerator': 0,
                                 'reason': '当前提取范围未发现对应术前讨论，自动初判为0，待人工审核'}
                                if row.get('denominator') == 1 and row.get('numerator') is None else row
                                for row in rows]
                    rows = [_sync_trace_row(row) for row in rows]
                    reviews = review_cache.get((ctx.case_id,mid),[])
                    # Explicit reviewed object contributions override the same automatic object.
                    review_map = {r['item_key']:r for r in reviews}
                    if len(rows)==1 and rows[0]['item_key']=='admission' and 'case' in review_map and 'admission' not in review_map:
                        review_map['admission'] = {**review_map.pop('case'),'item_key':'admission'}
                    rows = [_apply_review(r, review_map.pop(r['item_key']))
                            if r['item_key'] in review_map else r for r in rows]
                    # Keep manually entered review rows whose object was not
                    # present in the current automatic candidate set, while
                    # preserving their own evidence value.
                    rows.extend(_apply_review({}, review_row) for review_row in review_map.values())
                    items.extend(rows)
            if number in EVENT_METRICS:
                for ctx in contexts:
                    if not ctx.complete:
                        items.append(inventory_unknown(ctx,number))
            result = aggregate(spec,items)
            # For ordinary consultation completion, a clear positive record
            # can produce a useful preliminary value before the reviewer has
            # certified that every consultation object was enumerated. Keep
            # the completeness warning, but do not hide an otherwise
            # determinate draft such as 1/1 = 100%.
            if draft and number in (7, 9, 13, 15, 17):
                provisional_items = [item for item in items if item.get('item_key') != 'coverage_unknown']
                provisional = aggregate(spec, provisional_items)
                if provisional.get('value') is not None:
                    result.update(numerator=provisional['numerator'], denominator=provisional['denominator'],
                                  value=provisional['value'])
                    if number == 7:
                        result['unknown_denominator_groups'] = 0
                        result['unknown_numerator_objects'] = 0
                        result['missing'] = [item for item in result.get('missing', [])
                                             if item not in {'存在分母资格或统计对象枚举未知',
                                                             '普通会诊对象完整性尚未确认'}]
                    else:
                        result.setdefault('missing', []).append(
                            '四级手术及护理级别对象完整性尚未确认' if number == 9 else
                            '高费用病例对象完整性尚未确认' if number == 13 else
                            '手术及术前讨论对象完整性尚未确认')
        if not cases and number!=30:
            result.update(value=None,status='无法计算',missing=['批次没有病例'])
        approval = approval_cache.get(mid)
        if spec['rule_conflict']:
            result['rule_warning'] = spec['rule_conflict']
            if not draft and (not approval or approval['rule_hash']!=rule_hash(spec)):
                result.update(value=None,status='口径待确认',missing=list(result.get('missing',[]))+['医院提示词冲突尚未确认'])
        if draft:
            result['status'] = '自动初判，待复核' if result.get('value') is not None else result['status']
        results[mid] = result
        mode_label = 'draft' if draft else 'reviewed'
        details.extend(_traced_detail(spec, row, mode_label) for row in items)
    return results,details
