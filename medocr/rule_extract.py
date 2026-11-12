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

RULE_EXTRACT_VERSION = 'labelled-facts-v13-preop-canonical-discussion'

# Each anchor starts one record; subsequent anchors of that type start another.
ANCHORS = {
    'admission': r'入院(?:时间|日期)',
    'discharge': r'出院(?:时间|日期)',
    'transfer': r'转科(?:时间|日期)|转入时间|转出时间|转科记录|转出记录|转入记录',
    'order': r'开立时间|医嘱开具时间|医嘱开立时间',
    # Some consultation forms have a titled record followed directly by the
    # opinion and carry the timestamp on the same line as the title.
    'consultation': r'(?:会诊申请|申请会诊|申请)时间',
    # Keep the labelled anchor and add the labels used by operation records.
    'surgery': r'手术开始时间|手术时间|操作时间|手术记录|有创诊疗操作记录',
    'anesthesia': r'麻醉开始时间',
    'rescue': r'抢救开始时间',
    'death': r'死亡时间',
    'critical_value': r'危急值出现时间',
    'transfusion': r'输血开始时间',
}
HEADERS = {
    'ward_round': r'^(?:.*医师)?查房记录',
    'progress_note': r'^(?:首次|日常|阶段性|术后)?病程记录',
    # OCR frequently places the date and time before the title on the same
    # line, so the prefix is intentionally allowed here.
    'discussion': r'^.*(?:术前讨论(?:记录|结论及术前小结)|疑难病例讨论(?:记录|结论记录)?|死亡病例讨论(?:记录|结论记录)?|多学科讨论记录)',
    'consultation': r'^.*会诊记录',
    'consent': r'^(?:手术|麻醉|输血)知情同意书',
    'nursing': r'^(?:床旁交接班|护理交接班|护理|手术病人交接|交接)记录',
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
    'consultation': r'(?:会诊申请|申请会诊|申请)时间', 'surgery': r'手术开始时间|手术时间|操作时间',
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
    'surgery': {'surgery_id':r'手术编号','procedure_name':r'手术名称|术式|手术方式|操作名称|手术及操作名称','procedure_code':r'手术编码',
                'surgeon':r'术者|主刀医师|手术医师|手术指导者','surgeon_id':r'术者编号','planned_surgeon':r'计划术者',
                'planned_procedure':r'计划手术名称|拟施手术名称和方式|拟施手术名称'},
    'anesthesia': {'anesthesia_id':r'麻醉编号','physician':r'麻醉医师','physician_id':r'麻醉医师编号'},
    'discussion': {'host':r'主持人','host_role':r'主持人职务','department':r'讨论科室|参加科室',
                   'organizer':r'组织部门','conclusion':r'讨论结论|讨论结论及术前小结|结论',
                   'planned_procedure':r'计划手术名称|拟施手术名称和方式|拟施手术名称|拟手术名称',
                   'planned_surgeon':r'计划术者|手术医师|术者'},
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
    'consultation': {'arrival_time':r'到位时间|到达时间','completion_time':r'会诊完成时间|会诊时间'},
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
    pattern = r'(?:^|[\n;；|])[ \t]*(?:'+labels+r')[ \t]*(?:[:：]\s*|[ \t]+|\r?\n[ \t]*)([^\n;；|]+)'
    return {m[1].strip() for m in re.finditer(pattern,text) if m[1].strip()}


def literal(text, labels):
    found = values(text,labels)
    return next(iter(found)) if len(found)==1 else None


def labelled_time(text, labels, fallback=True):
    vals = values(text,labels)
    def trim_value(value):
        # Multiple labelled cells are sometimes OCR'd onto one line. Keep the
        # value belonging to the requested label instead of making the time
        # ambiguous with the next cell.
        if '手术开始' in labels:
            value = re.split(r'手术结束\s*时间|手术持续\s*时间', value, maxsplit=1)[0]
        if '申请' in labels:
            value = re.split(r'会诊时间|会诊意见', value, maxsplit=1)[0]
        return value.strip()
    vals = {trim_value(value) for value in vals if trim_value(value)}
    # Colonless timestamps and the next-line timestamp are common in OCR forms.
    for m in re.finditer(r'(?:^|[\n;；|])\s*(?:'+labels+r')\s*[:：]?\s*([^\n;；|]+)',text):
        vals.add(trim_value(m[1]))
        # Compose only adjacent date-only and clock-only cells under this label.
        tail = text[m.end():].lstrip('\r\n').splitlines()
        if DATE.fullmatch(m[1].strip()) and tail and CLOCK.fullmatch(tail[0].strip()):
            vals.add(trim_value(m[1].strip()+' '+tail[0].strip()))

    # A form can place two labelled cells on one OCR line. The second label
    # is then preceded by an ordinary space rather than a table delimiter.
    # Match only date/time-shaped values so ordinary prose is not captured.
    time_value = (r'(20\d{2}\s*[-年/.]\s*\d{1,2}\s*[-月/.]\s*\d{1,2}\s*日?'
                  r'(?:\s*(?:[01]?\d|2[0-3])\s*[:：]\s*[0-5]?\d'
                  r'(?:\s*[:：]\s*[0-5]?\d)?)?)')
    for m in re.finditer(r'(?:'+labels+r')\s*[:：]?\s*('+time_value+r')', text):
        vals.add(trim_value(m[1]))

    # OCR frequently joins a date and clock, e.g. 2025-04-1809:43.
    # Insert a logical separator before applying the time parsers.
    def normalise_joined_clock(value):
        return re.sub(
            r'(20\d{2}\s*[-年/.]\s*\d{1,2}\s*[-月/.]\s*\d{1,2}\s*日?)'
            r'(?=(?:[01]?\d|2[0-3])\s*[:：])', r'\1 ', value)

    # Extract time from the first few lines for ward-round records whose
    # title itself carries the timestamp.
    if not vals and fallback:
        for first_line in text.strip().split('\n')[:3] if text.strip() else []:
            time_match = re.search(
                r'(20\d{2}[-/年]\s*\d{1,2}[-/月]\s*\d{1,2}[日]?\s*'
                r'\d{1,2}\s*[:：]\s*\d{2}(?::\s*\d{2})?)', first_line)
            if time_match:
                vals.add(time_match.group(1))
                break

    stamps, days = set(),set()
    for val in vals:
        normalised = normalise_joined_clock(val)
        full = {t for _,t in times_in(normalised)}
        composed = composed_times(normalised)
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


def _normalise_short_order_time(value, year_hint=None):
    """Return a canonical timestamp for the compact dates used on order sheets.

    Order sheets commonly contain ``25.05.13 08:25`` or ``05.13 08:25``
    instead of the full ISO date used by narrative records.  The year is only
    filled when it is explicit in the row or unambiguous in the page text.
    """
    value = re.sub(r'\s+', ' ', str(value or '').strip())
    m = re.search(r'(?P<y>20\d{2}|\d{2})\s*[./-]\s*(?P<m>\d{1,2})\s*[./-]\s*(?P<d>\d{1,2})\s*(?P<h>\d{1,2})[:：](?P<mi>\d{2})', value)
    if not m:
        m = re.search(r'(?P<m>\d{1,2})\s*[./-]\s*(?P<d>\d{1,2})\s*(?P<h>\d{1,2})[:：](?P<mi>\d{2})', value)
        if not m or not year_hint:
            return None
        y = int(year_hint)
    else:
        y = int(m.group('y'))
        if y < 100:
            y += 2000
    try:
        return f'{y:04d}-{int(m.group("m")):02d}-{int(m.group("d")):02d}T{int(m.group("h")):02d}:{int(m.group("mi")):02d}:00'
    except ValueError:
        return None


def _order_rows(text):
    """Parse OCR order sheets whose columns collapsed into ordinary lines.

    This intentionally emits one raw event per visible order row.  It does not
    guess a stop date from an execution timestamp; a stop date is populated
    only when the row explicitly contains a stop/termination label.
    """
    if not re.search(r'(?:^|\n)\s*(?:长期|临时)医嘱单', text):
        return []
    years = re.findall(r'(?<!\d)(20\d{2})\s*年?', text[:2500])
    if years:
        year_hint = int(years[0])
    else:
        short_year = re.search(r'(?<!\d)(\d{2})\s*[./-]\s*\d{1,2}\s*[./-]\s*\d{1,2}', text[:2500])
        year_hint = 2000 + int(short_year.group(1)) if short_year else None
    row_start = re.compile(r'(?<!\d)(?:20\d{2}|\d{2})\s*[./-]\s*\d{1,2}\s*[./-]\s*\d{1,2}\s*\d{1,2}[:：]\d{2}|(?<!\d)\d{1,2}\s*[./-]\s*\d{1,2}\s+\d{1,2}[:：]\d{2}')
    lines = text.splitlines()
    rows, current = [], []
    for line in lines:
        if row_start.match(line.strip()):
            if current:
                rows.append(' '.join(current).strip())
            current = [line.strip()]
        elif current and line.strip():
            current.append(line.strip())
    if current:
        rows.append(' '.join(current).strip())

    parsed = []
    long_sheet = bool(re.search(r'(?:^|\n)\s*长期医嘱单', text))
    for row in rows:
        first = row_start.match(row)
        if not first:
            continue
        stamp = _normalise_short_order_time(first.group(0), year_hint)
        # Rows containing only a header or a cancellation marker are not order
        # content.  Keep genuine cancellations as evidence when they have a
        # visible order description before the marker.
        content = row[first.end():].strip(' ：:;；|')
        if re.match(r'^(?:2\s*)?取消|停止|作废', content):
            if parsed and long_sheet:
                stop = _normalise_short_order_time(first.group(0), year_hint)
                if stop:
                    parsed[-1]['stop_time'] = stop
                    parsed[-1]['stop_date'] = stop[:10]
                    parsed[-1]['stop_quote'] = row
            continue
        if not content or re.fullmatch(r'(?:取消|停止|作废|签名|日期|时间)[\s\W]*', content):
            continue
        item = {'quote': row, 'event_time': stamp, 'event_date': stamp[:10] if stamp else None,
                'content': content}
        if long_sheet:
            later = row_start.search(row, first.end())
            if later:
                stop = _normalise_short_order_time(later.group(0), year_hint)
                if stop:
                    item['stop_time'], item['stop_date'] = stop, stop[:10]
        parsed.append(item)
    return parsed


def rule_facts(page, config=None):
    config = config or {}
    text = page['text']; result = []
    def emit(kind, quote, offset, overrides=None, refs=None):
        attrs = {k:literal(quote,labels) for k,labels in TEXT_FIELDS.get(kind,{}).items()}
        attrs = {k:v for k,v in attrs.items() if v is not None}
        et,day = labelled_time(quote,TIME_LABELS.get(kind,r'事件时间'))
        rt,rday = labelled_time(quote,r'记录时间|文书记录时间',fallback=False)
        if day:
            attrs['event_date'] = day
        if rday:
            attrs['record_date'] = rday
        for key,label in EXTRA_TIMES.get(kind,{}).items():
            time,date = labelled_time(quote,label,fallback=False)
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
            # A temporary order can carry the only visible timestamp for a
            # transfer.  Preserve it as a reviewable hint; the indicator
            # logic must still confirm an actual ward transfer separately.
            transfer_hint = re.search(r'(?:转科|转入|转出)(?:医嘱|申请|病区|科室)?', quote)
            attrs['transfer_candidate'] = bool(transfer_hint and attrs.get('order_type') == 'temporary')
            # Nursing levels on collapsed order sheets often lose the Chinese
            # label but retain a distinctive row value before the QD/BID
            # frequency, such as “I级护理”. Preserve the normalized level as
            # an explicit fact while retaining the raw row as evidence.
            if not attrs.get('nursing_level') and attrs.get('content'):
                content = attrs['content']
                level = re.search(r'(特级护理|一级护理|二级护理|三级护理|[IⅠ]级护理|[IIⅡ]级护理|[IIIⅢ]级护理|[IVⅣ]级护理)', content)
                if level:
                    attrs['nursing_level'] = {'Ⅰ级护理':'一级护理','Ⅱ级护理':'二级护理','Ⅲ级护理':'三级护理','Ⅳ级护理':'四级护理'}.get(level.group(1), level.group(1))
                elif re.match(r'^I[^\s]{2,10}\s+(?:QD|BID)\b', content, re.I):
                    attrs['nursing_level'] = 'I级护理'
                elif re.match(r'^1[^\s]{2,10}\s+(?:QD|BID)\b', content, re.I):
                    attrs['nursing_level'] = '一级护理'
        if kind=='ward_round':
            # A titled主任/副主任/主治查房记录 is evidence of the role candidate.
            # It does not certify clinical completeness; norm_compliant stays unknown.
            head = quote.splitlines()[0] if quote.splitlines() else quote
            role = re.search(r'(主任医师|副主任医师|主治医师)', head)
            if role and not attrs.get('physician_role'):
                attrs['physician_role'] = role.group(1)
            if role and not attrs.get('physician'):
                name = re.search(r'([\u4e00-\u9fff]{2,4})' + role.group(1), head)
                if name:
                    attrs['physician'] = name.group(1)

            # IMPROVED: Extract analysis and plan from ward_round records
            lines = quote.split('\n')
            analysis_parts = []
            plan_parts = []
            in_plan = False

            for line in lines:
                line_stripped = line.strip()
                if not line_stripped:
                    continue

                # Detect plan section start
                if re.search(r'(医师查房嘱|嘱|诊疗[计划意见]|处置|建议|继续)', line_stripped):
                    in_plan = True

                # Collect analysis (before plan section)
                if not in_plan and re.search(r'(患者|病情|一般情况|查体|BP|体温|饮食|睡眠|大小便)', line_stripped):
                    analysis_parts.append(line_stripped)

                # Collect plan
                if in_plan:
                    plan_parts.append(line_stripped)

            if analysis_parts and not attrs.get('analysis'):
                attrs['analysis'] = ' '.join(analysis_parts[:5])[:300]  # Limit length

            if plan_parts and not attrs.get('plan'):
                attrs['plan'] = ' '.join(plan_parts[:3])[:300]  # Limit length
        if kind=='surgery' and not attrs.get('surgeon'):
            surgeon = re.search(
                r'(?:手术者|术者|主刀医师|手术医师)\s*[:：]?\s*([\u4e00-\u9fff]{2,4})',
                quote)
            if surgeon:
                attrs['surgeon'] = surgeon.group(1)
        if kind=='consultation':
            attrs['urgency'] = {'急会诊':'emergency','急':'emergency','普通':'ordinary','普通会诊':'ordinary'}.get(literal(quote,r'会诊类型'),'unknown')
            # A progress note may quote a consultant's opinion without being
            # the application form. Keep it as evidence, but label it so
            # consultation indicators do not count it as an application.
            has_application = bool(re.search(r'会诊申请|申请会诊|申请时间|会诊申请时间', quote))
            attrs['consultation_role'] = 'opinion_only' if attrs['urgency'] == 'unknown' and not has_application else 'application'
            # Narrative forms often write “神经内科医师会诊意见：...”
            # without a standalone 会诊意见 label at the start of a line.
            if not attrs.get('opinion'):
                opinion = re.search(r'(?:会诊意见|会诊医师意见)\s*[:：]\s*(.+)', quote, re.S)
                if opinion:
                    attrs['opinion'] = re.split(r'\n\s*(?:签名|电子签章|查房记录|会诊记录)', opinion.group(1), maxsplit=1)[0].strip()[:1000]
        if kind=='surgery':
            grade = literal(quote,r'手术级别')
            attrs['grade'] = {'一级':1,'二级':2,'三级':3,'四级':4,'1':1,'2':2,'3':3,'4':4}.get(grade)
        if kind=='discussion':
            attrs['discussion_type'] = next((v for label,v in [('术前讨论','preoperative'),('疑难病例讨论','difficult'),('死亡病例讨论','death'),('多学科讨论','multidisciplinary')] if re.search(label, quote[:500])), 'unknown')
            participants = literal(quote,r'参加人员|参与人员')
            if participants:
                attrs['participants'] = [x.strip() for x in re.split(r'[、,，]',participants) if x.strip()]
            departments = literal(quote,r'参加科室|讨论科室')
            if departments:
                attrs['departments'] = [x.strip() for x in re.split(r'[、,，]',departments) if x.strip()]
            if not attrs.get('planned_procedure'):
                planned = re.search(r'(?:拟施手术名称和方式|拟施手术名称|拟手术名称|计划手术名称)\s*[:：]?\s*([^\n;；|]+)', quote)
                if planned:
                    attrs['planned_procedure'] = planned.group(1).strip()
            if not attrs.get('conclusion'):
                conclusion = re.search(r'(?:讨论结论|结论|术前小结)\s*[:：]\s*([^\n;；|]+)', quote)
                if conclusion:
                    attrs['conclusion'] = conclusion.group(1).strip()
        if kind=='surgery':
            # Table OCR often loses the colon after 手术名称/术者.  Use a
            # short line-local fallback so an unrelated prose mention cannot
            # become a surgical fact.
            if not attrs.get('procedure_name'):
                m = re.search(r'(?:手术及操作名称|操作名称|手术名称|术式)\s*[:：]?\s*([^\n;；|]+)', quote)
                if m:
                    attrs['procedure_name'] = re.split(r'\s*(?:手术切口|麻醉方法|麻醉方式|手术指导者|手术者|术者)', m.group(1))[0].strip()
            if not attrs.get('surgeon'):
                for label in ('手术者','术者','主刀医师','手术指导者','手术医师'):
                    m = re.search(label + r'\s*[:：]?\s*([\u4e00-\u9fff]{2,4})(?=(?:手术者|术者|助手|外|[：:\s(（]|$))', quote)
                    if m:
                        attrs['surgeon'] = m.group(1).strip()
                        break
        if kind=='nursing':
            attrs['nursing_type'] = 'bedside_handover' if quote.startswith('床旁交接班记录') else 'unknown'
        if kind=='consent':
            attrs['consent_type'] = quote.split('知情同意书',1)[0].strip()
        if overrides:
            attrs.update({k:v for k,v in overrides.items() if k not in {'event_time','event_date'}})
            et = overrides.get('event_time')
            if kind == 'transfer' and et and not rt:
                rt = et
            if overrides.get('event_date'):
                attrs['event_date'] = overrides['event_date']
        # Order-row overrides provide the collapsed row content only after
        # the generic field extraction above; infer nursing level here too.
        if kind == 'order' and not attrs.get('nursing_level') and attrs.get('content'):
            content = attrs['content']
            level = re.search(r'(特级护理|一级护理|二级护理|三级护理|[IⅠ]级护理|[IIⅡ]级护理|[IIIⅢ]级护理|[IVⅣ]级护理)', content)
            if level:
                attrs['nursing_level'] = {'Ⅰ级护理':'一级护理','Ⅱ级护理':'二级护理','Ⅲ级护理':'三级护理','Ⅳ级护理':'四级护理'}.get(level.group(1), level.group(1))
            elif re.match(r'^I[^\s]{2,10}\s+(?:QD|BID)\b', content, re.I):
                attrs['nursing_level'] = 'I级护理'
            elif re.match(r'^1[^\s]{2,10}\s+(?:QD|BID)\b', content, re.I):
                attrs['nursing_level'] = '一级护理'
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
        # A titled order sheet is parsed row by row below.  The generic
        # 开立时间 anchor would otherwise emit one oversized, unlabeled event
        # for the whole page.
        if kind == 'order' and re.search(r'(?:^|\n)\s*(?:长期|临时)医嘱单', text):
            continue
        matches = list(re.finditer(pattern,text,re.M))
        # Transfer heading and its own timestamp are one record, not two events.
        if kind=='transfer' and any(re.search(r'记录',m[0]) for m in matches):
            matches = [m for m in matches if '记录' in m[0]]
        for i,m in enumerate(matches):
            line_start = text.rfind('\n',0,m.start())+1
            # Anchors often appear after the document title. Include the
            # title-to-end record context so fields printed before the time
            # anchor (procedure name, consultation type, etc.) are available.
            title_pattern = {
                'consultation': r'会诊记录',
                'surgery': r'手术记录',
                'discussion': r'(?:术前|疑难病例|死亡病例|多学科)讨论记录',
            }.get(kind)
            if title_pattern:
                title_matches = list(re.finditer(title_pattern, text[:m.start()]))
                if title_matches:
                    title_start = title_matches[-1].start()
                    line_start = text.rfind('\n',0,title_start)+1
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
                # Surgical handover forms contain “转入时间”, but that is a
                # transfer between operating/recovery areas, not a ward
                # transfer for the quality indicator.
                if kind == 'transfer' and re.search(r'手术病人交接记录|术后交接|手术室护士', text):
                    continue
                emit(kind,quote,line_start)
    # Order sheets in this case are OCR'd as ordinary lines rather than a
    # pipe/tab table.  Parse those rows after titled-record extraction so the
    # order facts retain the exact row as evidence.
    order_type = 'long' if re.search(r'(?:^|\n)\s*长期医嘱单', text) else 'temporary' if re.search(r'(?:^|\n)\s*临时医嘱单', text) else None
    if order_type:
        cursor = 0
        for row in _order_rows(text):
            offset = text.find(row['quote'], cursor)
            cursor = max(cursor, offset + len(row['quote']))
            emit('order', row['quote'], offset if offset >= 0 else 0,
                 {'event_time': row['event_time'], 'event_date': row['event_date'],
                  'content': row['content'], 'order_type': order_type,
                  'order_category': 'unknown', 'stop_time': row.get('stop_time'),
                  'stop_date': row.get('stop_date')})
            # Some temporary-order sheets contain the only explicit transfer
            # evidence, e.g. “转科至冠心病与大血管病外科”. Emit a separate
            # transfer candidate so indicator 1 can review it by event time;
            # missing source ward remains unknown and blocks automatic scoring.
            if order_type == 'temporary' and re.search(r'转科(?:至|到)|转入(?:至|到)|转出(?:至|到)', row['content']):
                target = re.search(r'(?:转科|转入|转出)(?:至|到)\s*([^\d\s,，;；|]+)', row['content'])
                overrides = {'event_time': row['event_time'], 'event_date': row['event_date'],
                             'occurred': True, 'to_department': target.group(1).strip() if target else None,
                             'from_department': None, 'from_icu': None, 'to_icu': None,
                             'transfer_source': 'temporary_order'}
                emit('transfer', row['quote'], offset if offset >= 0 else 0, overrides)
    # Continuation pages of preoperative discussions often omit the heading
    # but retain the explicit indication and planned procedure fields.
    if not re.search(r'(?:术前讨论|疑难病例讨论|死亡病例讨论|多学科讨论)', text[:800]) and \
       re.search(r'(?:手术指征|拟施手术名称和方式|拟手术名称|计划手术名称)', text):
        emit('discussion', text.strip(), 0, {'discussion_type': 'preoperative',
             'continuation': True})
    # The second page of the medical record front sheet contains the
    # operation table; its grade is evidence even when no surgery report is
    # present. Keep one fact per table row.
    if re.search(r'(?:手术级别|手术名称|手术日期)', text) and re.search(r'(?:四级|三级|二级|一级)', text):
        # The front-sheet table is OCR'd one operation per line.  Restrict
        # matching to lines with an explicit ISO-like date and a procedure
        # name before the grade; the previous broad expression also matched
        # unrelated costs, discharge options and numbered checkboxes.
        grade_map = {'一级':1,'二级':2,'三级':3,'四级':4}
        for line_match in re.finditer(r'(?m)^\s*(?P<date>20\d{2}[-/.年]\d{1,2}[-/.月]\d{1,2})\s+(?P<rest>[^\n]+)', text):
            line = line_match.group(0).strip()
            gm = re.search(r'(?P<name>.+?)\s+(?P<grade>[一二三四]级)\b', line)
            if not gm:
                continue
            name = gm.group('name').strip()
            if len(name) < 2 or re.search(r'(费用|离院|再住院|路径|出院|金额)', name):
                continue
            date_text = line_match.group('date')
            parts = re.findall(r'\d+', date_text)
            event_time = f'{int(parts[0]):04d}-{int(parts[1]):02d}-{int(parts[2]):02d}T00:00:00'
            emit('surgery', line, line_match.start(), {'event_time': event_time,
                 'event_date': event_time[:10], 'procedure_name': name,
                 'grade': grade_map[gm.group("grade")], 'front_sheet': True})
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
    # Front-sheet OCR may omit the colon: “总费用308763.27”.
    for m in re.finditer(r'(?:住院总费用|医疗总费用|总费用|�ܷ���)\s*[:：]?\s*(\d[\d,]*(?:\.\d+)?)\s*(万)?\s*元?',text):
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
            # Surgical forms in this case have no shared IDs.  For the
            # preoperative-discussion relation, use normalized planned/actual
            # procedure names and dates as a conservative candidate match.
            if relation == 'discussion_for_surgery' and not matches:
                norm = lambda x: re.sub(r'[\s\d年月日:/：()（）、，,.-]+', '', str(x)).replace('择期','')
                if left_kind == 'discussion':
                    discussion = left
                    candidates = [e for e in facts if e['kind']=='surgery']
                else:
                    surgery = left
                    candidates = [e for e in facts if e['kind']=='discussion']
                for candidate in candidates:
                    discussion, surgery = (left, candidate) if left_kind == 'discussion' else (candidate, left)
                    p, a = norm(discussion['attributes'].get('planned_procedure') or ''), norm(surgery['attributes'].get('procedure_name') or '')
                    name_match = bool(p and a and (p in a or a in p))
                    if not name_match and p and a:
                        p_tokens = {p[i:i+2] for i in range(max(0,len(p)-1))}
                        a_tokens = {a[i:i+2] for i in range(max(0,len(a)-1))}
                        name_match = len(p_tokens & a_tokens) >= 2
                    dt, st = discussion.get('event_time'), surgery.get('event_time')
                    date_match = True if not dt or not st else dt <= st
                    if name_match and date_match:
                        matches.append(candidate)
                # Several supporting forms may repeat the same planned
                # procedure. Use the earliest discussion candidate as the
                # canonical preoperative discussion for this surgery.
                if len(matches) > 1:
                    matches = [min(matches, key=lambda item: item.get('page', 10**9))]
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
