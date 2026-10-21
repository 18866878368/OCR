"""Expert-guided retrieval plans for core quality indicators.

The plans describe where evidence is found and which raw fields are needed.
They never contain a numerator or denominator value and never replace clinical
review.  A plan is deliberately declarative so the hospital can review the
source document aliases and thresholds before using it for statistics.
"""
from copy import deepcopy

RETRIEVAL_VERSION = "expert-guided-retrieval-v1"


def _source(document_types, page_categories, event_types, target_fields, aliases):
    return {
        "document_types": list(document_types),
        "page_categories": list(page_categories),
        "event_types": list(event_types),
        "target_fields": list(target_fields),
        "aliases": list(aliases),
    }


def _plan(sources, numerator, denominator, calculation_rules, notes=(), conflicts=()):
    return {
        "sources": sources,
        "numerator": numerator,
        "denominator": denominator,
        "calculation_rules": calculation_rules,
        "missing_data_policy": "待核验",
        "require_evidence": True,
        "notes": list(notes),
        "conflicts": list(conflicts),
    }


RETRIEVAL_RULES = {
    "core_01": _plan(
        [_source(["入院记录"], ["admission", "cover"], ["admission"], ["event_time"], ["入院时间", "入院日期"]),
         _source(["病程记录", "转科记录", "转入记录", "转出记录"], ["progress", "transfer"], ["transfer"], ["record_time", "occurred", "from_department", "to_department", "from_icu", "to_icu"], ["转科记录", "转入记录", "转出记录", "记录时间", "文书记录时间"])],
        {"event_types": ["transfer"], "fields": ["admission.event_time", "transfer.record_time", "transfer.occurred", "transfer.from_department", "transfer.to_department"], "conditions": ["转科记录时间在入院后48小时内", "排除转入或转出ICU"], "counting_unit": "住院人次"},
        {"fields": ["admission.event_time"], "conditions": ["每次住院纳入1个分母"], "counting_unit": "住院人次"},
        {"time_window_hours": 48, "time_basis": "transfer.record_time", "exclude": ["from_icu", "to_icu"], "deduplicate_by": "episode_id"},
        notes=("文档明确使用入院记录和病程/转科记录；必须保留原文页码和记录时间证据。",)),
    "core_02": _plan(
        [_source(["入院记录"], ["admission", "cover"], ["admission"], ["event_time"], ["入院时间", "入院日期"]),
         _source(["临时医嘱", "医嘱记录"], ["orders"], ["order"], ["event_time", "order_category", "content", "order_type"], ["临时医嘱", "医嘱内容", "开立时间", "医嘱开具时间"])],
        {"event_types": ["order"], "fields": ["admission.event_time", "order.event_time", "order.order_category", "order.content"], "conditions": ["首次检查或治疗医嘱开具时间在入院后8小时内"], "counting_unit": "住院人次"},
        {"fields": ["admission.event_time"], "conditions": ["每次住院纳入1个分母"], "counting_unit": "住院人次"},
        {"time_window_hours": 8, "time_basis": "order.event_time", "allowed_order_categories": ["examination", "treatment"], "deduplicate_by": "episode_id"},
        notes=("指标名称虽含查房，文档定义实际使用检查或治疗医嘱。",)),
    "core_03": _plan(
        [_source(["病程记录", "主任医师查房记录", "副主任医师查房记录", "主治医师查房记录"], ["progress", "admission"], ["ward_round"], ["event_time", "physician", "physician_role", "analysis", "plan", "severity", "norm_compliant"], ["主任医师查房记录", "副主任医师查房记录", "主治医师查房记录", "查房时间", "病情分析", "处置意见", "诊疗意见"])],
        {"event_types": ["ward_round"], "fields": ["ward_round.event_time", "ward_round.physician_role", "ward_round.analysis", "ward_round.plan", "ward_round.norm_compliant"], "conditions": ["主治医师及以上", "每条查房记录有时间、病情分析和诊疗处置意见", "按患者病情分级核对完整性"], "counting_unit": "住院病例"},
        {"fields": ["admission.event_time"], "conditions": ["每次住院纳入1个病例分母"], "counting_unit": "住院病例"},
        {"required_fields": ["event_time", "physician_role", "analysis", "plan"], "all_records_must_pass": True},
        notes=("记录标题可以作为医师身份候选，但仍需保留原文并人工确认规范性。",)),
    "core_04": _plan(
        [_source(["首次病程记录", "入院记录", "病程记录"], ["admission", "progress"], ["progress_note"], ["plan"], ["首次病程记录", "诊疗计划", "手术计划"]),
         _source(["手术记录", "介入治疗记录", "内镜下治疗记录"], ["surgery"], ["surgery"], ["event_time", "procedure_name", "unplanned"], ["手术记录", "介入治疗", "内镜下治疗", "是否非计划手术"])],
        {"event_types": ["surgery"], "fields": ["progress_note.plan", "surgery.event_time", "surgery.procedure_name", "surgery.unplanned"], "conditions": ["先核对首次病程诊疗计划", "仅在明确语义证据支持时判定非计划手术"], "counting_unit": "住院人次"},
        {"fields": ["admission.event_time"], "conditions": ["每次住院纳入1个分母"], "counting_unit": "住院人次"},
        {"planned_source": "progress_note.plan", "actual_source": "surgery", "require_semantic_confirmation": True},
        notes=("没有手术计划的缺失证据不能单独证明非计划手术。",)),
    "core_05": _plan(
        [_source(["会诊记录", "急会诊申请单"], ["consultation", "progress"], ["consultation"], ["event_time", "consultation_id", "urgency", "arrival_time"], ["急会诊", "会诊申请时间", "到位时间", "到达时间"])],
        {"event_types": ["consultation"], "fields": ["consultation.event_time", "consultation.arrival_time", "consultation.urgency"], "conditions": ["会诊类型为急会诊", "请求发出后10分钟内到位"], "counting_unit": "急会诊次数"},
        {"fields": ["consultation.consultation_id"], "conditions": ["每份急会诊申请单计1次"], "counting_unit": "急会诊次数"},
        {"time_window_minutes": 10, "time_basis": "consultation.event_time_to_arrival_time", "deduplicate_by": "consultation_id"}),
    "core_06": _plan(
        [_source(["会诊记录", "急会诊申请单"], ["consultation", "progress"], ["consultation"], ["event_time", "consultation_id", "urgency", "opinion"], ["急会诊", "会诊申请时间", "会诊意见"]),
         _source(["临时医嘱", "医嘱记录"], ["orders"], ["order"], ["event_time", "content", "order_id"], ["临时医嘱", "医嘱内容", "开立时间"])],
        {"event_types": ["consultation", "order"], "fields": ["consultation.consultation_id", "consultation.event_time", "consultation.opinion", "order.event_time", "order.content"], "conditions": ["急会诊申请后40分钟内", "医嘱内容与会诊意见对应"], "counting_unit": "急会诊次数"},
        {"fields": ["consultation.consultation_id"], "conditions": ["每份急会诊申请单计1次"], "counting_unit": "急会诊次数"},
        {"time_window_minutes": 40, "relation": "implements_consultation", "deduplicate_by": "consultation_id"}),
    "core_07": _plan(
        [_source(["会诊记录", "普通会诊申请单"], ["consultation", "progress"], ["consultation"], ["event_time", "consultation_id", "urgency", "completion_time"], ["普通会诊", "会诊申请时间", "会诊完成时间", "电子签章时间"])],
        {"event_types": ["consultation"], "fields": ["consultation.event_time", "consultation.completion_time", "consultation.urgency"], "conditions": ["会诊类型为普通会诊", "申请后24小时内完成，以电子签章时间为完成时间"], "counting_unit": "普通会诊次数"},
        {"fields": ["consultation.consultation_id"], "conditions": ["每份普通会诊申请单计1次"], "counting_unit": "普通会诊次数"},
        {"time_window_hours": 24, "time_basis": "consultation.event_time_to_completion_time", "deduplicate_by": "consultation_id"},
        conflicts=("文档截图中出现10分钟字样，指标定义和表格说明使用24小时；当前按24小时执行，待医院确认。",)),
    "core_08": _plan(
        [_source(["会诊记录", "普通会诊申请单"], ["consultation", "progress"], ["consultation"], ["event_time", "consultation_id", "urgency", "opinion"], ["普通会诊", "会诊申请时间", "会诊意见"]),
         _source(["临时医嘱", "医嘱记录"], ["orders"], ["order"], ["event_time", "content", "order_id"], ["临时医嘱", "医嘱内容", "开立时间"])],
        {"event_types": ["consultation", "order"], "fields": ["consultation.consultation_id", "consultation.event_time", "consultation.opinion", "order.event_time", "order.content"], "conditions": ["普通会诊申请后24小时内", "医嘱内容与会诊意见对应"], "counting_unit": "普通会诊次数"},
        {"fields": ["consultation.consultation_id"], "conditions": ["普通会诊患者/申请单计数口径需确认"], "counting_unit": "普通会诊次数"},
        {"time_window_hours": 24, "relation": "implements_consultation", "deduplicate_by": "consultation_id"},
        conflicts=("定义写普通会诊患者总次数，提示按申请单计次；当前按会诊事件保存，待医院确认。",)),
    "core_09": _plan(
        [_source(["长期医嘱", "医嘱记录"], ["orders", "discharge"], ["order"], ["event_time", "stop_time", "order_type", "nursing_level", "content"], ["长期医嘱", "护理级别", "特级护理", "一级护理", "停止时间"]),
         _source(["手术记录", "介入治疗记录", "内镜下治疗记录"], ["surgery"], ["surgery"], ["event_time", "procedure_name"], ["手术记录", "介入治疗", "内镜下治疗"])],
        {"event_types": ["order", "surgery"], "fields": ["order.order_type", "order.event_time", "order.stop_time", "order.nursing_level", "surgery.event_time"], "conditions": ["存在手术", "出院前最后一次护理级别为特级或一级"], "counting_unit": "手术患者住院人次"},
        {"fields": ["surgery.event_time"], "conditions": ["存在手术即纳入分母"], "counting_unit": "手术患者住院人次"},
        {"last_value_before": "discharge", "allowed_nursing_levels": ["特级护理", "一级护理", "1级护理", "I级护理"]},
        notes=("按文档从长期医嘱寻找出院前最后护理级别；缺失时保留待核验。",)),
    "core_10": _plan(
        [_source(["手术记录", "病案首页第二页", "四级手术/介入记录"], ["surgery", "cover"], ["surgery"], ["event_time", "procedure_name", "procedure_code", "grade"], ["四级手术", "四级", "手术级别"]),
         _source(["交接记录单", "床旁交接班记录"], ["nursing"], ["nursing"], ["event_time", "nursing_type"], ["床旁交接班", "交接班时间"])],
        {"event_types": ["surgery", "nursing"], "fields": ["surgery.grade", "surgery.event_time", "nursing.event_time", "nursing.nursing_type"], "conditions": ["手术分级为四级", "交接班日期与手术日期相同"], "counting_unit": "四级手术患者住院人次"},
        {"fields": ["surgery.grade", "surgery.event_time"], "conditions": ["存在四级手术或四级介入"], "counting_unit": "四级手术患者住院人次"},
        {"grade": 4, "same_calendar_day": ["surgery.event_time", "nursing.event_time"], "deduplicate_by": "surgery_id"},
        conflicts=("定义按患者统计，提示示例按手术记录；当前按同次住院四级手术事件核对，待医院确认。",)),
    "core_11": _plan(
        [_source(["再次入院记录", "手术记录"], ["admission", "surgery"], ["readmission", "surgery"], ["unplanned", "reoperation", "event_time"], ["非计划再次住院", "是否再次手术", "非计划手术"]),
         _source(["病程记录", "疑难病例讨论记录"], ["progress", "discussion"], ["discussion"], ["discussion_type", "complete", "conclusion"], ["疑难病例讨论", "讨论结论"])],
        {"event_types": ["readmission", "surgery", "discussion"], "fields": ["readmission.unplanned", "surgery.reoperation", "discussion.discussion_type", "discussion.complete"], "conditions": ["非计划再次住院或非计划再次手术", "存在正式疑难病例讨论"], "counting_unit": "再次住院/手术对象"},
        {"fields": ["readmission.unplanned", "surgery.reoperation"], "conditions": ["只统计符合非计划条件的对象"], "counting_unit": "再次住院/手术对象"},
        {"discussion_type": "difficult", "require_complete_document": True}),
    "core_12": _plan(
        [_source(["再次入院记录", "手术记录"], ["admission", "surgery"], ["readmission", "surgery"], ["unplanned", "reoperation", "event_time"], ["非计划再次住院", "是否再次手术"]),
         _source(["病程记录", "疑难病例讨论记录"], ["progress", "discussion"], ["discussion"], ["discussion_type", "complete", "conclusion"], ["疑难病例讨论", "讨论结论", "记录结论"])],
        {"event_types": ["discussion"], "fields": ["discussion.discussion_type", "discussion.complete", "discussion.conclusion"], "conditions": ["对象符合非计划再次住院/手术", "讨论结论已记入病历"], "counting_unit": "讨论对象"},
        {"event_types": ["discussion"], "conditions": ["对象已开展疑难病例讨论"], "counting_unit": "讨论对象"},
        {"discussion_type": "difficult", "require_complete_and_conclusion": True}),
    "core_13": _plan(
        [_source(["病案首页", "费用信息"], ["cover", "discharge"], ["cost"], ["total"], ["总费用", "住院总费用", "医疗总费用"]),
         _source(["病程记录", "疑难病例讨论记录"], ["progress", "discussion"], ["discussion"], ["discussion_type", "complete", "conclusion"], ["疑难病例讨论", "讨论结论"])],
        {"event_types": ["cost", "discussion"], "fields": ["cost.total", "discussion.discussion_type"], "conditions": ["住院周期总费用达到20万元或医院配置阈值", "存在疑难病例讨论"], "counting_unit": "异常费用患者"},
        {"event_types": ["cost"], "fields": ["cost.total"], "conditions": ["总费用达到阈值"], "counting_unit": "异常费用患者"},
        {"threshold_source": "config.high_cost_threshold", "default_threshold": 200000}),
    "core_14": _plan(
        [_source(["病程记录", "抢救记录", "临时医嘱"], ["progress", "orders", "nursing"], ["rescue"], ["event_time", "end_time", "survival_confirmed_until"], ["抢救记录", "抢救开始时间", "抢救结束时间", "存活确认至"]),
         _source(["死亡记录", "出院记录"], ["death", "discharge"], ["death", "discharge"], ["event_time"], ["死亡时间", "出院时间"])],
        {"event_types": ["rescue"], "fields": ["rescue.event_time", "rescue.end_time", "rescue.survival_confirmed_until"], "conditions": ["逐次统计抢救事件", "结束后存活超过24小时或至下一次抢救开始"], "counting_unit": "抢救事件"},
        {"event_types": ["rescue"], "conditions": ["每个实际抢救事件计1次"], "counting_unit": "抢救事件"},
        {"success_window_hours": 24, "deduplicate_by": "rescue_id"},
        conflicts=("抢救医嘱条数与实际抢救事件可能不同；当前按实际抢救事件计数，待医院确认。",)),
    "core_15": _plan(
        [_source(["病程记录", "术前讨论记录"], ["progress", "discussion"], ["discussion"], ["discussion_type", "complete", "completion_time"], ["术前讨论", "讨论完成时间"]),
         _source(["手术记录", "介入治疗记录", "内镜下治疗记录"], ["surgery"], ["surgery"], ["event_time", "procedure_name", "emergency_lifesaving"], ["手术记录", "介入治疗", "内镜下治疗"]),
         _source(["手术医嘱", "手术知情同意书"], ["orders", "consent"], ["order", "consent"], ["event_time", "patient_signed_time"], ["手术医嘱", "手术同意书"])],
        {"event_types": ["surgery", "discussion"], "fields": ["surgery.event_time", "discussion.completion_time", "discussion.complete"], "conditions": ["一台手术对应一次术前讨论", "除抢救生命急诊手术外，讨论完成早于手术医嘱和知情同意签署"], "counting_unit": "手术事件"},
        {"event_types": ["surgery"], "conditions": ["存在实际手术/介入/内镜治疗"], "counting_unit": "手术事件"},
        {"emergency_exception": "surgery.emergency_lifesaving", "require_before": ["order.event_time", "consent.patient_signed_time"]}),
    "core_16": _plan(
        [_source(["病程记录", "术前讨论记录"], ["progress", "discussion"], ["discussion"], ["discussion_type", "participants", "planned_surgeon"], ["术前讨论", "参加人员", "手术医师", "计划术者"]),
         _source(["手术记录"], ["surgery"], ["surgery"], ["surgeon", "surgeon_id", "event_time"], ["手术记录", "手术指导者", "术者", "主刀医师"])],
        {"event_types": ["surgery", "discussion"], "fields": ["surgery.surgeon", "surgery.surgeon_id", "discussion.participants", "discussion.planned_surgeon"], "conditions": ["术前讨论参与人员包含手术指导者或手术者"], "counting_unit": "手术事件"},
        {"event_types": ["surgery"], "conditions": ["存在已开展且有术前讨论的手术"], "counting_unit": "手术事件"},
        {"relation": "discussion_for_surgery", "match": "surgeon_in_participants"}),
    "core_17": _plan(
        [_source(["病程记录", "术前讨论记录"], ["progress", "discussion"], ["discussion"], ["discussion_type", "planned_procedure"], ["术前讨论", "拟手术名称", "计划手术名称", "术式"]),
         _source(["手术记录"], ["surgery"], ["surgery"], ["procedure_name", "procedure_code", "event_time"], ["手术记录", "手术名称", "实际手术名称", "术式"])],
        {"event_types": ["surgery", "discussion"], "fields": ["discussion.planned_procedure", "surgery.procedure_name", "surgery.procedure_code"], "conditions": ["计划手术名称/术式与实际实施名称/术式一致"], "counting_unit": "手术事件"},
        {"event_types": ["surgery"], "conditions": ["存在实际手术"], "counting_unit": "手术事件"},
        {"relation": "discussion_for_surgery", "match": "normalized_procedure_name_or_code"}),
    "core_18": _plan(
        [_source(["病程记录", "术前讨论记录"], ["progress", "discussion"], ["discussion"], ["discussion_type", "planned_surgeon", "participants"], ["术前讨论", "手术医师", "计划术者", "参加人员"]),
         _source(["手术记录"], ["surgery"], ["surgery"], ["surgeon", "surgeon_id", "event_time"], ["手术记录", "手术指导者", "术者", "主刀医师"])],
        {"event_types": ["surgery", "discussion"], "fields": ["discussion.planned_surgeon", "discussion.participants", "surgery.surgeon", "surgery.surgeon_id"], "conditions": ["按医院确认的指标口径比较计划术者与实际术者"], "counting_unit": "手术事件"},
        {"event_types": ["surgery"], "conditions": ["存在实际手术"], "counting_unit": "手术事件"},
        {"relation": "discussion_for_surgery", "match": "planned_surgeon_equals_actual_surgeon"},
        conflicts=("文档截图将本项也写成手术医师参与讨论，现有指标定义按计划术者与实际术者一致执行，待医院确认。",)),
}


def retrieval_plan(metric_id, metric=None):
    """Return a copy so UI/model code cannot mutate the shared contract."""
    if metric_id in RETRIEVAL_RULES:
        return deepcopy(RETRIEVAL_RULES[metric_id])
    metric = metric or {}
    fields = metric.get("fact_fields", {})
    sources = [{"document_types": [], "page_categories": metric.get("page_categories", []),
                "event_types": [kind], "target_fields": list(values), "aliases": []}
               for kind, values in fields.items()]
    return _plan(sources,
                 {"event_types": metric.get("fact_types", []), "fields": [], "conditions": [], "counting_unit": metric.get("unit")},
                 {"event_types": metric.get("fact_types", []), "fields": [], "conditions": [], "counting_unit": metric.get("unit")},
                 {})


def retrieval_for_specs(metrics):
    return {m["id"]: retrieval_plan(m["id"], m) for m in metrics}
