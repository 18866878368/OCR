"""Core indicator contracts. Hospital prompt text remains preserved in catalog.json."""
from .rules import catalog
from .retrieval import RETRIEVAL_VERSION, retrieval_plan

# Types describe facts, never an LLM-computed numerator or denominator.
FIELDS = {
    'admission': {'patient_id':'text', 'episode_id':'text', 'department':'text'},
    'discharge': {'nursing_level':'text'},
    'transfer': {'occurred':'bool','from_department':'text','to_department':'text','from_icu':'bool','to_icu':'bool'},
    'order': {'order_id':'text','order_category':'text','order_type':'text','content':'text','stop_time':'time','drug_name':'text','nursing_level':'text','is_medication':'bool'},
    'ward_round': {'physician':'text','physician_role':'text','analysis':'text','plan':'text','severity':'text','norm_compliant':'bool'},
    'progress_note': {'analysis':'text','plan':'text'},
    'consultation': {'consultation_id':'text','urgency':'text','arrival_time':'time','completion_time':'time','opinion':'text'},
    'surgery': {'surgery_id':'text','procedure_name':'text','procedure_code':'text','grade':'int','end_time':'time',
                'surgeon':'text','surgeon_id':'text','planned_surgeon':'text','planned_surgeon_id':'text',
                'planned_procedure':'text','procedure_consistent':'bool','unplanned':'bool','reoperation':'bool','emergency_lifesaving':'bool','intraoperative_transfusion':'bool',
                'autotransfusion':'bool'},
    'anesthesia': {'anesthesia_id':'text','end_time':'time','physician':'text','physician_id':'text'},
    'discussion': {'discussion_type':'text','participants':'texts','departments':'texts','host':'text','host_role':'text',
                   'department':'text','organizer':'text','conclusion':'text','complete':'bool','completion_time':'time','planned_procedure':'text','planned_surgeon':'text'},
    'consent': {'consent_type':'text','physician_signed_time':'time','patient_signed_time':'time'},
    'rescue': {'rescue_id':'text','end_time':'time','survival_confirmed_until':'time'},
    'death': {},
    'cost': {'total':'number'},
    'nursing': {'nursing_type':'text','nursing_level':'text'},
    'critical_value': {'critical_id':'text','item':'text','report_time':'time','received_time':'time','department':'text'},
    'transfusion': {'transfusion_id':'text','end_time':'time','intraoperative':'bool','autologous':'bool'},
    'transfusion_assessment': {'assessment':'text','norm_compliant':'bool'},
    'complication': {'diagnosis':'text','surgery_related':'bool'},
    'readmission': {'unplanned':'bool','previous_discharge_time':'time'},
    'dispute': {'death_related':'bool'},
    'upload': {'required':'bool','complete':'bool'},
}

RELATIONS = {
    'implements_consultation': ('consultation','order'),
    'discussion_for_surgery': ('surgery','discussion'),
    'consent_for_surgery': ('surgery','consent'),
    'order_for_surgery': ('surgery','order'),
    'handover_for_surgery': ('surgery','nursing'),
    'anesthesia_for_surgery': ('surgery','anesthesia'),
    'complication_of_surgery': ('surgery','complication'),
    'assessment_for_transfusion': ('transfusion','transfusion_assessment'),
    'treatment_for_critical_value': ('critical_value','order'),
    'progress_for_critical_value': ('critical_value','progress_note'),
    'round_for_critical_value': ('critical_value','ward_round'),
    'consultation_for_antibiotic': ('order','consultation'),
    'transfusion_for_surgery': ('surgery','transfusion'),
}

for _fields in FIELDS.values():
    _fields.update(event_date='date',record_date='date')
FIELDS['order']['stop_date'] = 'date'

# number: fact types, routing categories, counting unit, period anchor
CONTRACTS = {
 1: (['admission','transfer'], ['cover','admission','transfer','progress','orders','nursing'], '住院人次','admission'),
 2: (['admission','order'], ['cover','admission','orders'], '住院人次','admission'),
 3: (['admission','ward_round'], ['admission','progress'], '住院病例','admission'),
 4: (['admission','surgery'], ['cover','admission','surgery','progress','discussion'], '住院人次','admission'),
 5: (['consultation'], ['consultation','progress'], '急会诊事件','consultation'),
 6: (['consultation','order'], ['consultation','orders','progress'], '急会诊事件','consultation'),
 7: (['consultation'], ['consultation','progress'], '普通会诊事件','consultation'),
 8: (['consultation','order'], ['consultation','orders','progress'], '普通会诊事件','consultation'),
 9: (['admission','surgery','discharge'], ['cover','surgery','discharge','orders','nursing'], '手术患者住院人次','discharge'),
10: (['admission','surgery','nursing'], ['cover','surgery','nursing'], '四级手术患者住院人次','admission'),
11: (['admission','readmission','surgery','discussion'], ['cover','admission','surgery','discussion','progress'], '住院病例','admission'),
12: (['admission','readmission','surgery','discussion'], ['cover','admission','surgery','discussion','progress'], '住院病例','admission'),
13: (['admission','cost','discussion'], ['cover','discussion','discharge'], '住院病例','admission'),
14: (['rescue','death','discharge'], ['orders','progress','nursing','discharge'], '抢救事件','rescue'),
15: (['surgery','discussion','order','consent'], ['surgery','discussion','orders','consent'], '手术事件','surgery'),
16: (['surgery','discussion','order','consent'], ['surgery','discussion','orders','consent'], '已完成术前讨论的手术事件','surgery'),
17: (['surgery','discussion'], ['surgery','discussion'], '手术事件','surgery'),
18: (['surgery','discussion'], ['surgery','discussion'], '手术事件','surgery'),
19: (['death','discussion'], ['orders','progress','discussion','discharge'], '死亡病例','death'),
20: (['death','discussion','dispute'], ['discussion','discharge','progress'], '两个死亡病例集合','death'),
21: (['death','discussion'], ['discussion','progress','discharge'], '死亡病例','death'),
22: (['death','upload'], ['discharge','progress','orders'], '应上传死亡病案','death'),
23: (['order'], ['orders'], '长期医嘱条目','order'),
24: (['surgery'], ['surgery'], '手术事件','surgery'),
25: (['surgery','anesthesia'], ['surgery','anesthesia'], '手术事件','surgery'),
26: (['surgery','complication'], ['cover','surgery','progress','discharge'], '手术事件','surgery'),
27: (['admission','surgery','death'], ['cover','surgery','discharge','progress'], '各级手术患者住院人次','admission'),
28: (['surgery','discussion'], ['cover','surgery','discussion'], '四级手术事件','surgery'),
29: (['surgery'], ['cover','surgery'], '术种','surgery'),
30: (['surgery'], ['surgery','progress'], '项目种类','external'),
31: (['critical_value'], ['lab','imaging','progress','nursing'], '危急值条目时长','critical_value'),
32: (['critical_value','order','ward_round','progress_note'], ['lab','imaging','progress','orders','nursing'], '危急值条目','critical_value'),
33: (['order','consultation'], ['orders','consultation','progress'], '特殊级抗菌药物医嘱条目','order'),
34: (['transfusion','transfusion_assessment'], ['orders','progress','nursing'], '输血治疗事件','transfusion'),
35: (['admission','surgery','transfusion'], ['surgery','anesthesia','orders','progress'], '术中输血患者住院人次','admission'),
}

CONFLICTS = {
 7:'分子提示写急会诊10分钟，定义要求普通会诊24小时完成；采用定义，需医院确认。',
 8:'分母的“患者总次数”与按申请单计次存在歧义；采用会诊事件，需医院确认。',
10:'定义按患者统计，分子提示按每次手术判定；采用住院人次内全部四级手术均符合，需医院确认。',
12:'分子提示仅要求有讨论文书，定义还要求结论记入病历；采用结论完整性，需医院确认。',
14:'抢救医嘱条数可能不同于实际抢救例次；采用实际抢救事件，需医院确认。',
18:'分子提示复制了术者参与讨论的条件；采用计划与实际术者一致，需医院确认。',
20:'提示把死亡病例当分母、参与当组织；采用纠纷死亡病例和组织主体，需医院确认。',
22:'分子分母提示都只判断死亡；采用应上传名单和完整上传结果，需医院确认。',
24:'分子提示固定0；改为跨病例医师时间重合检测，需医院确认。',
25:'分子提示固定0；改为跨病例麻醉时间重合检测，需医院确认。',
26:'提示仅给两个并发症计数；需要三级和四级各自分子、分母，需医院确认。',
27:'提示仅给两个死亡计数；需要三级和四级各自患者总数；同次住院按最高手术级别归组，需医院确认。',
28:'分母提示误写三级手术；采用四级手术，需医院确认。',
32:'提示要求相关医嘱，备注要求当日病程记录；同时要求关联处置医嘱和病程证据，需医院确认。',
34:'提示按医嘱条数统计，定义按输血治疗例次；采用实际输血事件，需医院确认。',
}

EXTERNAL = {
19:['医院工作日历'],20:['纠纷死亡病例资料'],21:['科主任身份映射'],22:['应上传名单及完整上传结果'],
26:['医院手术分级目录'],27:['医院手术分级目录'],28:['医院手术分级目录'],29:['医院手术备案目录'],
30:['四年新技术新项目台账'],31:['危急值出现和接收时间（PDF无记录时需导出）'],
32:['完整危急值明细（PDF无记录时需导出）'],33:['医院特殊使用级抗菌药物名单'],
35:['自体血回输证据（PDF无记录时需输血资料）'],
}


def core_specs():
    result = []
    for m in catalog():
        if not m['id'].startswith('core_'):
            continue
        n = int(m['id'][-2:])
        kinds, pages, unit, anchor = CONTRACTS[n]
        enriched = {**m,'fact_types':kinds,'page_categories':pages,'unit':unit,
                    'fact_fields':{k:FIELDS[k] for k in kinds}}
        result.append({**enriched,'counting_unit':unit,
                       'period_anchor':anchor,'rule_conflict':CONFLICTS.get(n),
                       'retrieval_version':RETRIEVAL_VERSION,'retrieval':retrieval_plan(m['id'],enriched),
                       'external_requirements':EXTERNAL.get(n,[]),
                       'fact_fields':{k:FIELDS[k] for k in kinds}})
    return result


def active_metrics(config):
    items = catalog()
    return [m for m in items if m['id'].startswith('core_')] if config.get('indicator_set')=='core' else items


def relevant_categories(metric_ids=None):
    specs = core_specs()
    if metric_ids:
        specs = [m for m in specs if m['id'] in metric_ids]
    return {'unknown'} | {c for m in specs for c in m['page_categories']}
