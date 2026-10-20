import json
import math
import re
from datetime import datetime
from pathlib import Path
from statistics import median

RULE_VERSION = '2025-core35-facts-v5-current-scope'


def catalog():
    return json.loads(Path(__file__).with_name('catalog.json').read_text(encoding='utf-8'))


def get_metric(mid):
    for metric in catalog():
        if metric['id'] == mid:
            return metric
    raise ValueError('未知指标 ID')


DATE_PATTERN = re.compile(r'(20\d{2})\s*[-年/.]\s*(\d{1,2})\s*[-月/.]\s*(\d{1,2})\s*日?\s*[T ]?\s*(\d{1,2})\s*[:：时]\s*(\d{1,2})(?:\s*[:：分]\s*(\d{1,2}))?')


def parse_time(value):
    if not value:
        return None
    match = DATE_PATTERN.search(str(value))
    if not match:
        return None
    try:
        return datetime(*[int(x or 0) for x in match.groups()])
    except ValueError:
        return None


def canonical_time(value):
    result = parse_time(value)
    return result.isoformat(timespec='seconds') if result else None


def in_period(time, batch):
    return (not batch.get('start') or time.date().isoformat() >= batch['start']) and (not batch.get('end') or time.date().isoformat() <= batch['end'])


def known_admission(events):
    values = {parse_time(x['event_time']) for x in events if x['kind']=='admission' and x['verified']}
    values.discard(None)
    if len(values) == 1:
        return next(iter(values)), None
    return None, '入院时间未复核、缺失或相互冲突'


def event_evidence(events):
    return [{'event_id':e['id'],'document_id':e['document_id'],'page':e['page'],'quote':e['quote']} for e in events]


def department_icu(name, mapping):
    if not isinstance(name, str) or not name.strip():
        return None
    compact = re.sub(r'\s+', '', name).upper()
    # CCU and other configured ICU types are matched at word boundaries.
    for token in mapping:
        token = re.sub(r'\s+', '', token).upper()
        if token.isascii():
            if re.search(r'(?<![A-Z])' + re.escape(token) + r'(?![A-Z])', compact):
                return True
        elif token in compact:
            return True
    return False


def automatic_case(metric_id, case_id, events, batch, config):
    item = {'case_id':case_id,'item_key':'admission','denominator':None,'numerator':None,
            'reason':'','evidence':event_evidence(events),'origin':'rules','suggested_numerator':None}
    admission, error = known_admission(events)
    if error:
        item['reason'] = error
        return item
    if not in_period(admission, batch):
        item.update(denominator=0,numerator=0,reason='入院日期不在本批次统计期间')
        return item
    item['denominator'] = 1
    positive = []
    problems = []
    if metric_id == 'core_01':
        for event in events:
            if event['kind'] != 'transfer':
                continue
            attrs = event['attributes']
            time = parse_time(event.get(config['transfer_time_basis']))
            if not time:
                problems.append('转科时间缺失')
                continue
            hours = (time-admission).total_seconds()/3600
            if hours < 0:
                problems.append('转科时间早于入院，需核对')
                continue
            if hours > 48:
                continue
            if attrs.get('occurred') is not True:
                problems.append('转科是否实际发生未确认')
                continue
            if type(attrs.get('from_icu')) is bool:
                from_icu = attrs['from_icu']
            else:
                from_icu = department_icu(attrs.get('from_department'), config['icu_departments'])
            if type(attrs.get('to_icu')) is bool:
                to_icu = attrs['to_icu']
            else:
                to_icu = department_icu(attrs.get('to_department'), config['icu_departments'])
            if from_icu is True or to_icu is True:
                continue
            if from_icu is None or to_icu is None:
                problems.append('转入/转出科室或 ICU 属性未确认')
                continue
            positive.append(event)
    else:
        for event in events:
            if event['kind'] != 'order':
                continue
            attrs = event['attributes']
            if attrs.get('order_category') not in {'examination','treatment'}:
                continue
            time = parse_time(event['event_time'])
            if time and 0 <= (time-admission).total_seconds() <= 8*3600:
                positive.append(event)
    if any(e['verified'] for e in positive):
        item.update(numerator=1,reason='存在已复核且符合时间及排除条件的事件',evidence=event_evidence([e for e in positive if e['verified']]))
    elif positive:
        item.update(suggested_numerator=1,reason='发现符合条件的候选事件，需人工复核')
    else:
        item['reason'] = '未发现已确认的符合事件；记录缺失不能证明没有发生'
        if problems:
            item['reason'] += '；' + '；'.join(sorted(set(problems)))
    return item


def validate_measure(metric, key, value):
    if key not in metric['required_measures']:
        raise ValueError('不属于此指标的测量字段')
    if key in {'performed_types','registered_types'}:
        if not isinstance(value,list) or not all(isinstance(x,str) and x.strip() for x in value):
            raise ValueError('术种必须是非空名称/编码组成的 JSON 数组')
    elif key == 'minutes':
        if not isinstance(value,list) or not all(type(x) in (int,float) and math.isfinite(x) and x >= 0 for x in value):
            raise ValueError('报告时长必须是非负分钟数数组')
    elif type(value) is not int or value < 0:
        raise ValueError('计数必须为非负整数')


def calculate_measures(metric, values):
    missing = [key for key in metric['required_measures'] if key not in values]
    result = {'numerator':None,'denominator':None,'value':None,'status':'无法计算','missing':missing}
    if missing:
        return result
    for key,value in values.items():
        validate_measure(metric,key,value)
    kind = metric['kind']
    if kind in {'rate','ratio'}:
        n,d = values['numerator'],values['denominator']
        if kind == 'rate' and n > d:
            raise ValueError('分子超过分母')
    elif kind == 'rate_ratio':
        f,t = values['four_total'],values['three_total']
        if values['four_success'] > f or values['three_success'] > t:
            raise ValueError('发生例数超过对应手术/患者总数')
        if not f or not t or not values['three_success']:
            return {**result,'status':'不适用','missing':['对应总数或三级手术发生率为零']}
        return {**result,'value':(values['four_success']/f)/(values['three_success']/t),'status':'已复核补充数据','missing':[]}
    elif kind == 'catalog_rate':
        registered = set(values['registered_types'])
        performed = set(values['performed_types'])
        if performed - registered:
            raise ValueError('实际开展术种存在未匹配备案目录的项，请统一编码或核对备案版本')
        n,d = len(performed),len(registered)
    elif kind == 'retention':
        n = sum(values[key] for key in 'ACE')
        d = sum(values[key] for key in 'ABCDEF')
    else:
        data = values['minutes']
        if len(data) != values['eligible_total']:
            return {**result,'denominator':values['eligible_total'],'status':'无法计算','missing':['时长条目数与应统计总数不一致']}
        return {**result,'denominator':len(data),'value':median(data) if data else None,'status':'已复核补充数据' if data else '不适用','missing':[]}
    return {**result,'numerator':n,'denominator':d,'value':n/d if d else None,'status':'已复核补充数据' if d else '不适用','missing':[]}


def aggregate(metric, items):
    n = sum(x['numerator'] or 0 for x in items if x['denominator'] != 0 or metric['kind']=='ratio')
    d = sum(x['denominator'] or 0 for x in items)
    ud = sum(x['denominator'] is None for x in items)
    un = (sum(x['numerator'] is None for x in items) if metric['kind']=='ratio' else
          sum(x['denominator'] or 0 for x in items if x['denominator'] is not None and x['numerator'] is None))
    result = {'numerator':n,'denominator':d,'unknown_denominator_groups':ud,'unknown_numerator_objects':un,
              'value':None,'lower':None,'upper':None,'status':'待复核','missing':[]}
    if ud:
        result['missing'].append('存在分母资格或统计对象枚举未知')
    elif d == 0:
        result['status'] = '不适用'
    elif un:
        if metric['kind']=='rate':
            result.update(lower=n/d,upper=(n+un)/d)
        result['missing'].append('存在分子判定未知')
    else:
        result.update(value=n/d,status='已复核病案集合')
    return result


def batch_report(store, batch_id, config):
    from .rule_extract import batch_mode
    config = {**config,'extraction_mode':batch_mode(store,batch_id,config)}
    batch = store.one('SELECT * FROM batches WHERE id=?',(batch_id,))
    if not batch:
        raise ValueError('批次不存在')
    cases = store.cases(batch_id)
    from .identity import possible_duplicate_admissions
    identity_issues = possible_duplicate_admissions(store,batch_id,config)
    details,summary = [],[]
    from .specs import active_metrics
    automated,drafts = {},{}
    if config.get('calculation_engine')=='core35':
        from .core_engine import evaluate_core
        automated,confirmed_details = evaluate_core(store,batch,config)
        drafts,draft_details = evaluate_core(store,batch,config,draft=True)
        details.extend(confirmed_details+draft_details)
    for metric in active_metrics(config):
        measures = store.all('SELECT * FROM measures WHERE batch_id=? AND metric_id=?',(batch_id,metric['id']))
        if measures:
            result = calculate_measures(metric,{m['measure']:json.loads(m['value']) for m in measures})
            result['sources'] = [{k:m[k] for k in ('measure','source','reviewer','updated')} for m in measures]
        elif metric['id'] in automated:
            result = dict(automated[metric['id']])
        elif metric['kind'] not in {'rate','ratio'}:
            result = calculate_measures(metric,{})
        else:
            items = []
            for case in cases:
                reviews = store.all('SELECT * FROM reviews WHERE batch_id=? AND case_id=? AND metric_id=? ORDER BY item_key',(batch_id,case['id'],metric['id']))
                if metric['id'] in {'core_01','core_02'}:
                    from .candidate_scope import scoped_events
                    items.extend(reviews or [automatic_case(metric['id'],case['id'],scoped_events(store,case['id'],config),batch,config)])
                else:
                    items.extend(reviews)
                    coverage = store.one('SELECT * FROM coverage WHERE batch_id=? AND case_id=? AND metric_id=?',(batch_id,case['id'],metric['id']))
                    if not coverage or not coverage['complete']:
                        items.append({'case_id':case['id'],'item_key':'coverage_unknown','numerator':None,'denominator':None,
                                      'reason':'本病例统计对象尚未完整复核/枚举','evidence':[],'origin':'coverage'})
            result = aggregate(metric,items)
            details.extend({**item,'metric_id':metric['id']} for item in items)
            if not cases:
                result.update(status='无法计算',missing=['批次没有病例'])
        draft = drafts.get(metric['id'],{}) if not measures else {}
        if identity_issues and metric['id']!='core_30' and not measures:
            result.update(value=None,status='住院人次待核对',missing=list(result.get('missing',[]))+['存在疑似同次住院的多个病例文件'])
        summary.append({'metric_id':metric['id'],'name':metric['name'],'kind':metric['kind'],'unit':metric['unit'],**result,
                        'draft_numerator':draft.get('numerator'),'draft_denominator':draft.get('denominator'),
                        'draft_value':draft.get('value'),'draft_status':draft.get('status'),'draft_missing':draft.get('missing',[]),
                        'draft_unknown_denominator_groups':draft.get('unknown_denominator_groups',0),
                        'draft_unknown_numerator_objects':draft.get('unknown_numerator_objects',0),
                        'draft_measures':draft.get('measures')})
    page_counts = store.one("SELECT COUNT(*) total, COALESCE(SUM(p.stage='full'),0) recognized, COALESCE(SUM(p.stage='error'),0) failed FROM pages p JOIN documents d ON p.document_id=d.id JOIN batch_cases b ON b.case_id=d.case_id WHERE b.batch_id=?",(batch_id,))
    return {'batch':batch,'rule_version':RULE_VERSION,'scope_label':'医院确认完整病案集合（文书统计）' if batch['population']=='hospital_confirmed_complete' else '提交病案集合/样本统计',
            'case_count':len(cases),'pages':page_counts,'summary':summary,'details':details,'identity_issues':identity_issues,
            'calculation_config':{k:config.get(k) for k in ('indicator_set','calculation_engine','transfer_time_basis','high_cost_threshold','extraction_mode')}}
