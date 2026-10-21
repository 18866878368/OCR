"""Conservative template extraction and selective semantic routing.

Rules capture explicit labelled values. They never certify that all clinical
objects have been enumerated, or infer a negative from an absent keyword.
"""
import hashlib
import json
import re

from .dates import canonical_date, composed_times, DATE, CLOCK
from .extract import times_in
from .specs import RELATIONS

RULE_EXTRACT_VERSION = 'labelled-facts-v2-expert-guided'

# Each anchor starts one record; subsequent anchors of that type start another.
ANCHORS = {
    'admission': r'入院(?:时间|日期)',
    'discharge': r'出院(?:时间|日期)',
    'transfer': r'转科(?:时间|日期)|转入时间|转出时间|转科记录|转出记录|转入记录',
    'order': r'开立时间|医嘱开具时间|医嘱开立时间',
    'consultation': r'会诊申请时间|申请会诊时间',
    'surgery': r'手术开始时间',
    'anesthesia': r'麻醉开始时间',
    'rescue': r'抢救开始时间',
    'death': r'死亡时间',
    'critical_value': r'危急值出现时间',
    'transfusion': r'输血开始时间',
}
HEADERS = {
    'ward_round': r'^(?:.*医师)?查房记录',
    'progress_note': r'^日常病程记录',
    'discussion': r'^(?:术前|疑难病例|死亡病例|多学科)讨论记录',
    'consent': r'^(?:手术|麻醉|输血)知情同意书',
    'nursing': r'^(?:床旁交接班|护理交接班|护理)记录',
    'transfusion_assessment': r'^输血后(?:评估|评价)记录',
    'complication': r'^术后并发症记录',
    'readmission': r'^再次入院记录',
    'dispute': r'^医疗纠纷记录',
    'upload': r'^病案上传结果',
}
TIME_LABELS = {
    'admission': r'入院时间|入院日期', 'discharge': r'出院时间|出院日期',
    'transfer': r'转科时间|转科日期|转入时间|转出时间',
    'order': r'开立时间|医嘱开具时间|医嘱开立时间',
    'consultation': r'会诊申请时间|申请会诊时间', 'surgery': r'手术开始时间',
    'anesthesia': r'麻醉开始时间', 'rescue': r'抢救开始时间',
    'death': r'死亡时间', 'critical_value': r'危急值出现时间',
    'transfusion': r'输血开始时间', 'discussion': r'讨论时间|讨论日期',
    'ward_round': r'查房时间|查房日期', 'progress_note': r'病程时间',
    'nursing': r'交接班时间|护理记录时间',
    'transfusion_assessment': r'评估时间|评价时间', 'readmission': r'再次入院时间',
}
TEXT_FIELDS = {
    'admission': {'patient_id':r'患者编号|患者ID','episode_id':r'住院号|住院编号','department':r'入院科室'},
    'transfer': {'from_department':r'转出科室','to_department':r'转入科室'},
    'order': {'order_id':r'医嘱编号|医嘱ID','content':r'医嘱内容','drug_name':r'药品名称','nursing_level':r'护理级别'},
    'consultation': {'consultation_id':r'会诊编号|会诊申请单号','opinion':r'会诊意见'},
    'surgery': {'surgery_id':r'手术编号','procedure_name':r'手术名称|术式','procedure_code':r'手术编码',
                'surgeon':r'术者|主刀医师','surgeon_id':r'术者编号','planned_surgeon':r'计划术者',
                'planned_procedure':r'计划手术名称'},
    'anesthesia': {'anesthesia_id':r'麻醉编号','physician':r'麻醉医师','physician_id':r'麻醉医师编号'},
    'discussion': {'host':r'主持人','host_role':r'主持人职务','department':r'讨论科室',
                   'organizer':r'组织部门','conclusion':r'讨论结论','planned_procedure':r'计划手术名称',
                   'planned_surgeon':r'计划术者'},
    'ward_round': {'physician':r'查房医师','physician_role':r'医师职务','analysis':r'病情分析','plan':r'诊疗计划'},
    'progress_note': {'analysis':r'病情分析','plan':r'诊疗计划'},
    'rescue': {'rescue_id':r'抢救编号'}, 'discharge': {'nursing_level':r'出院护理级别'},
    'nursing': {'nursing_level':r'护理级别'},
    'critical_value': {'critical_id':r'危急值编号','item':r'危急值项目','department':r'接收科室'},
    'transfusion': {'transfusion_id':r'输血编号'},
    'transfusion_assessment': {'assessment':r'评估内容|评价内容'},
    'complication': {'diagnosis':r'并发症诊断'},
}
EXTRA_TIMES = {
    'order': {'stop_time':r'停止时间|终止时间'},
    'consultation': {'arrival_time':r'到位时间|到达时间','completion_time':r'会诊完成时间'},
    'surgery': {'end_time':r'手术结束时间'}, 'anesthesia': {'end_time':r'麻醉结束时间'},
    'rescue': {'end_time':r'抢救结束时间','survival_confirmed_until':r'存活确认至'},
    'critical_value': {'report_time':r'危急值报告时间','received_time':r'危急值接收时间'},
    'transfusion': {'end_time':r'输血结束时间'},
    'discussion': {'completion_time':r'讨论完成时间'},
    'consent': {'physician_signed_time':r'医师签署时间','patient_signed_time':r'患者签署时间'},
    'readmission': {'previous_discharge_time':r'前次出院时间'},
}
BOOL_FIELDS = {
    'transfer': {'occurred':r'是否转科'},
    'surgery': {'unplanned':r'是否非计划手术','reoperation':r'是否再次手术',
                'emergency_lifesaving':r'是否紧急抢救手术','intraoperative_transfusion':r'是否术中输血',
                'autotransfusion':r'是否自体血回输'},
    'readmission': {'unplanned':r'是否非计划再次住院'},
    'transfusion': {'intraoperative':r'是否术中输血','autologous':r'是否自体血回输'},
    'complication': {'surgery_related':r'是否与手术相关'},
    'dispute': {'death_related':r'是否与死亡相关'},
    'upload': {'required':r'是否应上传','complete':r'是否上传完成'},
}
MARKERS = {**ANCHORS, **HEADERS,
    'admission':r'入院记录|入院时间|入院日期', 'transfer':r'转科|转入|转出',
    'order':r'医嘱|开立时间', 'consultation':r'会诊', 'surgery':r'手术|术者',
    'anesthesia':r'麻醉', 'rescue':r'抢救', 'death':r'死亡', 'cost':r'总费用',
    'critical_value':r'危急值', 'transfusion':r'输血|血回输',
    'discussion':r'讨论记录|术前讨论|疑难病例讨论|死亡病例讨论|多学科讨论',
    'ward_round':r'查房记录', 'progress_note':r'病程记录', 'consent':r'知情同意书',
    'nursing':r'护理记录|交接班', 'transfusion_assessment':r'输血后评估|输血后评价',
    'complication':r'并发症', 'readmission':r'再次住院|再次入院',
    'dispute':r'医疗纠纷', 'upload':r'病案上传', 'discharge':r'出院时间|出院记录',
}
REQUIRED = {
    'admission':['event_time'], 'discharge':['event_time','nursing_level'],
    'transfer':['record_time','occurred','from_department','to_department','from_icu','to_icu'],
    'order':['event_time','content','order_category','order_type','stop_time'],
    'consultation':['event_time','urgency','arrival_time','completion_time','opinion'],
    'surgery':['event_time','end_time','procedure_name','surgeon','grade','unplanned'],
    'anesthesia':['event_time','end_time','physician'],
    'discussion':['event_time','discussion_type','participants','conclusion'],
    'consent':['physician_signed_time','patient_signed_time'],
    'rescue':['event_time','end_time','survival_confirmed_until'], 'death':['event_time'],
    'critical_value':['event_time','received_time','item'],
    'transfusion':['event_time','end_time','intraoperative','autologous'],
    'transfusion_assessment':['event_time','assessment','norm_compliant'],
    'ward_round':['event_time','physician','norm_compliant'], 'progress_note':['event_time','plan'],
    'nursing':['event_time','nursing_type'], 'complication':['diagnosis','surgery_related'],
    'readmission':['event_time','unplanned'], 'dispute':['death_related'],
    'upload':['required','complete'], 'cost':['total'],
}
ID_LABELS = {'surgery_id':r'手术编号','consultation_id':r'会诊编号|会诊申请单号',
             'critical_id':r'危急值编号','transfusion_id':r'输血编号'}


def values(text, labels):
    # An explicit colon or a stand-alone label followed by its next line is required.
    pattern = r'(?:^|[\n;；|])[ \t]*(?:'+labels+r')[ \t]*(?:[:：]\s*|\r?\n[ \t]*)([^\n;；|]+)'
    return {m[1].strip() for m in re.finditer(pattern,text) if m[1].strip()}


def literal(text, labels):
    found = values(text,labels)
    return next(iter(found)) if len(found)==1 else None


def labelled_time(text, labels):
    vals = values(text,labels)
    # Colonless timestamps and the next-line timestamp are common in OCR forms.
    for m in re.finditer(r'(?:^|[\n;；|])\s*(?:'+labels+r')\s*[:：]?\s*([^\n;；|]+)',text):
        vals.add(m[1].strip())
        # Compose only adjacent date-only and clock-only cells under this label.
        tail = text[m.end():].lstrip('\r\n').splitlines()
        if DATE.fullmatch(m[1].strip()) and tail and CLOCK.fullmatch(tail[0].strip()):
            vals.add(m[1].strip()+' '+tail[0].strip())
    stamps, days = set(),set()
    for val in vals:
        full = {t for _,t in times_in(val)}
        composed = composed_times(val)
        stamps.update(full or composed)
        day = canonical_date(val)
        if day:
            days.add(day)
    # Multiple different times/dates for one label are ambiguous.
    if len(stamps)>1 or len(days)>1:
        return None,None
    return (next(iter(stamps)) if stamps else None, next(iter(days)) if days else None)


def icu_value(department, config):
    if not department:
        return None
    normalize = lambda x: re.sub(r'\s+','',x).upper()
    if normalize(department) in {normalize(x) for x in config.get('icu_departments',['ICU','CCU','重症医学科','重症监护室'])}:
        return True
    if re.search(r'重症|监护|ICU|CCU',department,re.I):
        return None
    return False


def rule_facts(page, config=None):
    config = config or {}
    text = page['text']; result = []
    def emit(kind, quote, offset, overrides=None, refs=None):
        attrs = {k:literal(quote,labels) for k,labels in TEXT_FIELDS.get(kind,{}).items()}
        attrs = {k:v for k,v in attrs.items() if v is not None}
        et,day = labelled_time(quote,TIME_LABELS.get(kind,r'事件时间'))
        rt,rday = labelled_time(quote,r'记录时间|文书记录时间')
        if day:
            attrs['event_date'] = day
        if rday:
            attrs['record_date'] = rday
        for key,label in EXTRA_TIMES.get(kind,{}).items():
            time,date = labelled_time(quote,label)
            attrs[key] = time
            if kind=='order' and key=='stop_time' and date:
                attrs['stop_date'] = date
        for key,label in BOOL_FIELDS.get(kind,{}).items():
            value = literal(quote,label)
            attrs[key] = True if value in {'是','有','已完成','已发生'} else False if value in {'否','无','未完成','未发生'} else None
        attrs['_reference_ids'] = {k:v for k,l in ID_LABELS.items() if (v:=literal(quote,l))}
        if kind=='transfer':
            from_dep,to_dep = attrs.get('from_department'),attrs.get('to_department')
            # Labels and record headings give affirmative evidence, planned prose does not.
            if attrs.get('occurred') is None and from_dep and to_dep and re.search(r'转科记录|转入记录|转出记录',quote) and not re.search(r'拟转|计划转|建议转|既往|外院',quote):
                attrs['occurred'] = True
            attrs.update(from_icu=icu_value(from_dep,config),to_icu=icu_value(to_dep,config))
        if kind=='order':
            # The supplied guide locates the last nursing level in long-term orders.
            # Preserve the labelled value as raw evidence; later rules choose the last one.
            if 'nursing_level' not in attrs:
                attrs['nursing_level'] = literal(quote,r'护理级别')
            typ = literal(quote,r'医嘱类型')
            attrs['order_type'] = {'长期':'long','临时':'temporary'}.get(typ,'unknown')
            category = literal(quote,r'医嘱类别')
            attrs['order_category'] = {'检查':'examination','检验':'examination','治疗':'treatment','其他':'other'}.get(category,'unknown')
            if attrs['order_type']=='unknown':
                headings = {v for label,v in [('长期医嘱','long'),('临时医嘱','temporary')] if label in text[:500]}
                if len(headings)==1:
                    attrs['order_type'] = next(iter(headings))
        if kind=='ward_round':
            # A titled主任/副主任/主治查房记录 is evidence of the role candidate.
            # It does not certify clinical completeness; norm_compliant stays unknown.
            head = quote.splitlines()[0] if quote.splitlines() else quote
            role = re.search(r'(主任医师|副主任医师|主治医师)', head)
            if role and not attrs.get('physician_role'):
                attrs['physician_role'] = role.group(1)
        if kind=='consultation':
            attrs['urgency'] = {'急会诊':'emergency','急':'emergency','普通':'ordinary','普通会诊':'ordinary'}.get(literal(quote,r'会诊类型'),'unknown')
        if kind=='surgery':
            grade = literal(quote,r'手术级别')
            attrs['grade'] = {'一级':1,'二级':2,'三级':3,'四级':4,'1':1,'2':2,'3':3,'4':4}.get(grade)
        if kind=='discussion':
            attrs['discussion_type'] = next((v for label,v in [('术前','preoperative'),('疑难病例','difficult'),('死亡病例','death'),('多学科','multidisciplinary')] if quote.startswith(label)), 'unknown')
            participants = literal(quote,r'参加人员|参与人员')
            if participants:
                attrs['participants'] = [x.strip() for x in re.split(r'[、,，]',participants) if x.strip()]
            departments = literal(quote,r'参加科室')
            if departments:
                attrs['departments'] = [x.strip() for x in re.split(r'[、,，]',departments) if x.strip()]
        if kind=='nursing':
            attrs['nursing_type'] = 'bedside_handover' if quote.startswith('床旁交接班记录') else 'unknown'
        if kind=='consent':
            attrs['consent_type'] = quote.split('知情同意书',1)[0].strip()
        if overrides:
            attrs.update({k:v for k,v in overrides.items() if k not in {'event_time','event_date'}})
            et = overrides.get('event_time')
            if overrides.get('event_date'):
                attrs['event_date'] = overrides['event_date']
        if kind=='order' and attrs.get('order_type')=='unknown':
            headings = {v for label,v in [('长期医嘱','long'),('临时医嘱','temporary')] if label in text[:500]}
            if len(headings)==1:
                attrs['order_type'] = next(iter(headings))
        if kind=='order' and attrs.get('order_category')=='unknown' and attrs.get('content'):
            content = attrs['content']
            if not re.search(r'未行|不予|取消|拟|计划',content):
                if re.search(r'护理|饮食|测体温',content):
                    attrs['order_category'] = 'other'
                elif re.search(r'检查|检验|彩超|造影|心电图|CT|MRI|培养',content,re.I):
                    attrs['order_category'] = 'examination'
                elif re.search(r'治疗|注射|输血|吸氧|引流|静滴|口服',content):
                    attrs['order_category'] = 'treatment'
        missing = [key for key in REQUIRED[kind] if (et if key=='event_time' else rt if key=='record_time' else attrs.get(key)) in (None,'unknown','')]
        attrs.update(_rule_version=RULE_EXTRACT_VERSION,_needs_semantic=missing,
                     _evidence=refs or [{'document_id':page['document_id'],'page':page['page'],'quote':quote}])
        eid = hashlib.sha256(json.dumps([RULE_EXTRACT_VERSION,page['case_id'],page['document_id'],page['page'],kind,offset,quote],ensure_ascii=False).encode()).hexdigest()[:20]
        result.append({'id':eid,'case_id':page['case_id'],'kind':kind,'event_time':et,'record_time':rt,
                       'attributes':attrs,'document_id':page['document_id'],'page':page['page'],
                       'quote':quote,'origin':'rules','verified':False})
    for kind,pattern in {**ANCHORS,**HEADERS}.items():
        matches = list(re.finditer(pattern,text,re.M))
        # Transfer heading and its own timestamp are one record, not two events.
        if kind=='transfer' and any(re.search(r'记录',m[0]) for m in matches):
            matches = [m for m in matches if '记录' in m[0]]
        for i,m in enumerate(matches):
            line_start = text.rfind('\n',0,m.start())+1
            prefix = text[line_start:m.start()]
            if re.search(r'拟|计划|建议|既往|外院|未行|未发生',prefix):
                continue
            sections = list(re.finditer(r'(?:^|\n)\s*(既往史|既往病史|外院经历|现病史|本次入院|入院记录)\s*[:：]?',text[:m.start()]))
            if sections and sections[-1][1] in {'既往史','既往病史','外院经历'}:
                continue
            if kind=='order' and re.search(r'[|\t]',text[line_start:text.find('\n',m.start()) if '\n' in text[m.start():] else len(text)]):
                continue
            end = matches[i+1].start() if i+1<len(matches) else len(text)
            quote = text[line_start:end].strip()
            # Attribute collection is bounded by document separators, not an arbitrary cut.
            boundary = re.search(r'\n\s*(?:既往史|既往病史|出院小结|打印时间)\s*[:：]',quote)
            if boundary:
                quote = quote[:boundary.start()].rstrip()
            if quote:
                emit(kind,quote,line_start)
    # Only explicit delimited column headings establish the meaning of table times.
    headers = None
    offset = 0
    for line in text.splitlines(keepends=True):
        raw = line.rstrip('\r\n')
        separator = '|' if '|' in raw else '\t' if '\t' in raw else None
        if not separator:
            offset += len(line); continue
        cells = [x.strip() for x in raw.strip('|').split(separator)]
        if any(x in {'医嘱内容','医嘱名称'} for x in cells) and any('时间' in x for x in cells):
            headers = (cells,raw,separator)
        elif headers and separator==headers[2] and len(cells)==len(headers[0]):
            row = dict(zip(headers[0],cells))
            content = row.get('医嘱内容') or row.get('医嘱名称')
            if content and not all(re.fullmatch(r'[-: ]*',x) for x in cells):
                opened = next((row[k] for k in ('开立时间','开具时间','医嘱开立时间') if k in row),None)
                et,day = labelled_time('值：'+(opened or ''),'值')
                stop,sday = labelled_time('值：'+row.get('停止时间',''),'值')
                overrides = {'event_time':et,'event_date':day,'content':content,'stop_time':stop,
                             'order_category':'unknown','order_type':{'长期':'long','临时':'temporary'}.get(row.get('医嘱类型'),'unknown')}
                if row.get('医嘱编号'):
                    overrides['order_id'] = row['医嘱编号']
                if sday:
                    overrides['stop_date'] = sday
                refs = [{'document_id':page['document_id'],'page':page['page'],'quote':q} for q in (headers[1],raw)]
                emit('order',raw,offset,overrides,refs)
        offset += len(line)
    for m in re.finditer(r'(?:^|[\n;；])\s*(?:住院总费用|医疗总费用|总费用)\s*[:：]\s*(\d[\d,]*(?:\.\d+)?)\s*(万)?\s*元?',text):
        emit('cost',m[0].strip(),m.start())
        result[-1]['attributes']['total'] = float(m[1].replace(',',''))*(10000 if m[2] else 1)
        result[-1]['attributes']['_needs_semantic'] = []
    return result


def semantic_kinds(page, rows):
    """Only unresolved kinds with document evidence are routed to the model."""
    found = {e['kind'] for e in rows}
    unresolved = {e['kind'] for e in rows if e['attributes'].get('_needs_semantic')}
    return sorted(unresolved | {k for k,p in MARKERS.items() if k not in found and re.search(p,page['text'],re.M)})


def eligible_event(event, mode):
    if event['verified']:
        return True
    if event['origin']=='regex' and not event['attributes'].get('_rule_version'):
        return False  # Old "first table time" guesses are retained for review only.
    return mode=='assisted' or event['origin']!='local_llm'


def batch_mode(store, batch_id, config):
    row = store.one('SELECT mode FROM batch_processing WHERE batch_id=?',(batch_id,))
    return row['mode'] if row else config.get('extraction_mode','rules')


def rule_first_events(events):
    """Compose unreviewed assistance into an unambiguous rule event for calculation.

    Stored candidates stay untouched. Shared IDs, or identical/nested evidence on
    the same page with the same event time, establish identity; proximity does not.
    A helper can fill an unknown value, never replace a populated rule value.
    Human-reviewed candidates remain independent for the normal review engine.
    """
    result = list(events)
    rules = [e for e in events if e['origin']=='rules' and e['kind']!='link' and
             not e['verified'] and not e['attributes'].get('_stale')]
    keys = {'order':'order_id','surgery':'surgery_id','consultation':'consultation_id',
            'rescue':'rescue_id','critical_value':'critical_id','transfusion':'transfusion_id',
            'anesthesia':'anesthesia_id'}
    def unknown(value):
        return value is None or value=='' or value=='unknown'
    def matches(rule, helper):
        if rule['kind']!=helper['kind']:
            return False
        key = keys.get(rule['kind'])
        if key and rule['attributes'].get(key) and rule['attributes'].get(key)==helper['attributes'].get(key):
            return True
        if (rule['document_id'],rule['page'])!=(helper['document_id'],helper['page']):
            return False
        rq,hq = rule['quote'].strip(),helper['quote'].strip()
        return bool(rq and hq and (rq==hq or (rule.get('event_time') and
                    rule.get('event_time')==helper.get('event_time') and (rq in hq or hq in rq))))
    for helper in events:
        if helper['origin']!='local_llm' or helper['kind']=='link' or helper['verified'] or helper['attributes'].get('_stale') or helper['attributes'].get('_validation_issues'):
            continue
        candidates = [r for r in rules if matches(r,helper)]
        if len(candidates)!=1:
            continue
        source = candidates[0]
        index = next(i for i,e in enumerate(result) if e['id']==source['id'])
        current = result[index]
        attrs = dict(current['attributes'])
        conflicts = list(attrs.get('_assistance_conflicts',[]))
        fields = {k:v for k,v in helper['attributes'].items() if not k.startswith('_')}
        times = {}
        for key,value in fields.items():
            if unknown(attrs.get(key)):
                attrs[key] = value
            elif not unknown(value) and attrs[key]!=value:
                conflicts.append(key)
        for key in ('event_time','record_time'):
            value = helper.get(key)
            times[key] = current.get(key) or value
            if current.get(key) and value and current[key]!=value:
                conflicts.append(key)
        attrs['_assistance_conflicts'] = sorted(set(conflicts))
        refs = helper['attributes'].get('_evidence',[{'document_id':helper['document_id'],'page':helper['page'],'quote':helper['quote']}])
        attrs['_evidence'] = list(attrs.get('_evidence',[]))+refs
        result[index] = {**current,**times,'attributes':attrs,
                         '_ids':current.get('_ids',[current['id']])+[helper['id']]}
        result = [e for e in result if e['id']!=helper['id']]
    return result


def rule_links(store, case_id, config=None):
    from .candidate_scope import scoped_events
    events = scoped_events(store,case_id,config,'rules') if config is not None else store.events(case_id)
    facts = [e for e in events if e['origin']=='rules' and e['kind']!='link' and not e['attributes'].get('_stale')]
    valid_ids = {e['id'] for e in facts}
    for e in store.events(case_id):
        if e['origin']=='rules' and e['kind']=='link' and (e['attributes'].get('source_event_id') not in valid_ids or e['attributes'].get('target_event_id') not in valid_ids):
            with store.conn:
                store.conn.execute('UPDATE events SET attributes=? WHERE id=?',
                    (json.dumps({**e['attributes'],'_stale':True},ensure_ascii=False),e['id']))
    count = 0
    for relation,(left_kind,right_kind) in RELATIONS.items():
        id_key = 'consultation_id' if relation in {'implements_consultation','consultation_for_antibiotic'} else 'critical_id' if 'critical_value' in relation else 'transfusion_id' if relation=='assessment_for_transfusion' else 'surgery_id'
        for left in [e for e in facts if e['kind']==left_kind]:
            identifiers = left['attributes'].get('_reference_ids',{})
            # Only explicit shared clinical IDs associate events; proximity is insufficient.
            matches = [e for e in facts if e['kind']==right_kind and identifiers.get(id_key) and
                       e['attributes'].get('_reference_ids',{}).get(id_key)==identifiers[id_key]]
            if len(matches)!=1:
                continue
            right = matches[0]
            refs = left['attributes']['_evidence']+right['attributes']['_evidence']
            store.save_event({'id':hashlib.sha256((RULE_EXTRACT_VERSION+relation+left['id']+right['id']).encode()).hexdigest()[:20],
                'case_id':case_id,'kind':'link','document_id':left['document_id'],'page':left['page'],
                'quote':left['quote'],'origin':'rules','verified':False,
                'attributes':{'relation':relation,'source_event_id':left['id'],'target_event_id':right['id'],
                              '_rule_version':RULE_EXTRACT_VERSION,'_evidence':refs}})
            count += 1
    return count
