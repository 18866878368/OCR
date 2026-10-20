"""Deterministic rules over shared facts; drafts and reviewed results are separate."""
import hashlib
import json
from datetime import date, datetime, timedelta
import re

from .rules import aggregate, automatic_case, calculate_measures, in_period, parse_time, RULE_VERSION
from .specs import core_specs


def rule_hash(spec):
    return hashlib.sha256(json.dumps([spec['id'],spec['rule_conflict'],spec['counting_unit'],
                                     RULE_VERSION],ensure_ascii=False).encode()).hexdigest()


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
                if e.get('event_time') and existing.get('event_time') and e['event_time']!=existing['event_time']:
                    return False
                nested = e['quote'] in existing['quote'] or existing['quote'] in e['quote']
                common = set(e['attributes']) & set(existing['attributes'])
                agree = all(e['attributes'][k]==existing['attributes'][k] for k in common if not k.startswith('_') and
                            e['attributes'][k] is not None and existing['attributes'][k] is not None)
                return (explicit or (same_time and nested)) and agree
            explicit_keys = {'order':'order_id','surgery':'surgery_id','consultation':'consultation_id',
                             'rescue':'rescue_id','critical_value':'critical_id','transfusion':'transfusion_id','anesthesia':'anesthesia_id'}
            ident = e['attributes'].get(explicit_keys.get(e['kind'],''))
            candidates = explicit_index.get((e['kind'],ident),[]) if ident else time_index.get((e['kind'],e.get('event_time')),[])
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
    if number in (1,2):
        result = automatic_case(f'core_{number:02}',ctx.case_id,ctx.events,ctx.batch,ctx.config)
        # Draft events still carry their original reviewed flag. For a draft only, the caller supplies a copy.
        if result['denominator']==1 and result['numerator'] is None and ctx.complete:
            ambiguous = False
            if number==1:
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
                ambiguous = ambiguous or '；' in result['reason']
            else:
                ambiguous = any(not e.get('event_time') or e['attributes'].get('order_category') in {None,'unknown'} for e in ctx.get('order'))
            if not ambiguous:
                result['numerator'] = 0
                result['reason'] = '完整枚举后没有符合条件的事件'
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
        surgery_exists = True if surgeries else (False if ctx.complete else None)
        levels = {e['attributes'].get('nursing_level') for e in ctx.get('discharge') if e['attributes'].get('nursing_level')}
        level = next(iter(levels)) if len(levels)==1 else None
        n = any(x in level for x in ('特级','一级','1级','I级')) if level else None
        return [ctx.case_row(n,'按出院时护理级别判定',surgery_exists,surgeries+ctx.get('discharge'),'discharge')]
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
                n = preop_complete
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
            rows.append(ctx.row(s['id'],d,n,'依据具体手术关联的术前讨论、计划及实际记录判定',refs))
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
            rows.append(ctx.row(e['id'],d,n,'逐条比较长期医嘱开立和停止自然日',[e]))
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
                    details.append(ctx.row(s['id'],q,n,'手术级别及关联并发症',[s]+related))
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
                details.append(ctx.row(s['id'],q,None,'三四级实际开展术种，按统一编码去重',[s]))
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
                if q is None or not start or not end or end<start:
                    missing.append('危急值出现/接收时间或期间缺失')
                else:
                    values['minutes'].append((end-start).total_seconds()/60)
                details.append(ctx.row(e['id'],q,None,'危急值出现到临床接收时长',[e]))
    return values,details,sorted(set(missing))


def evaluate_core(store, batch, config, draft=False):
    cases = store.cases(batch['id'])
    # Read each case once; a large batch must not deserialize all facts 35 times.
    from .rule_extract import rule_first_events
    from .candidate_scope import scoped_events
    fact_cache = {c['id']:rule_first_events(scoped_events(store,c['id'],config)) for c in cases}
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
        contexts = []
        items = []
        for case in cases:
            events = fact_cache[case['id']]
            available = [e for e in events if not e['attributes'].get('_stale')]
            coverage = coverage_cache.get((case['id'],mid))
            complete = bool(coverage and coverage['complete'])
            allowed = set(spec['fact_types'])|{'admission'}
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
                    reviews = review_cache.get((ctx.case_id,mid),[])
                    # Explicit reviewed object contributions override the same automatic object.
                    review_map = {r['item_key']:r for r in reviews}
                    if len(rows)==1 and rows[0]['item_key']=='admission' and 'case' in review_map and 'admission' not in review_map:
                        review_map['admission'] = {**review_map.pop('case'),'item_key':'admission'}
                    rows = [review_map.pop(r['item_key'],r) for r in rows]
                    rows.extend(review_map.values())
                    items.extend(rows)
            if number in EVENT_METRICS:
                for ctx in contexts:
                    if not ctx.complete:
                        items.append(inventory_unknown(ctx,number))
            result = aggregate(spec,items)
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
        details.extend({**row,'metric_id':mid,'calculation_mode':'draft' if draft else 'reviewed'} for row in items)
    return results,details
