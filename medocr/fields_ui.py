"""Chinese clinical field editor; no model or remote service is used here."""
import streamlit as st

from .facts import ENUMS
from .specs import FIELDS

LABELS = {
 'event_date':'事件日期（仅有日期时填写）','record_date':'记录日期（仅有日期时填写）','stop_date':'医嘱停止日期（仅有日期时填写）',
 'patient_id':'患者关联标识','episode_id':'本次住院标识','department':'所在科室','nursing_level':'护理级别',
 'occurred':'转科实际发生','from_department':'转出科室','to_department':'转入科室','from_icu':'转出科室为ICU','to_icu':'转入科室为ICU',
 'order_id':'医嘱标识','order_category':'医嘱类别','order_type':'长期/临时医嘱','content':'医嘱内容','stop_time':'医嘱停止时间',
 'drug_name':'药品名称','is_medication':'属于用药医嘱','physician':'医师姓名','physician_role':'医师身份',
 'analysis':'病情分析','plan':'诊疗意见','severity':'病情等级','norm_compliant':'符合医院规范',
 'consultation_id':'会诊标识','urgency':'会诊类型','arrival_time':'会诊到位时间','completion_time':'完成时间','opinion':'会诊意见',
 'surgery_id':'手术标识','procedure_name':'实际术式','procedure_code':'统一术式编码','grade':'手术级别（1—4）','end_time':'结束时间',
 'surgeon':'实际术者','surgeon_id':'实际术者唯一标识','planned_surgeon':'计划术者','planned_surgeon_id':'计划术者唯一标识',
 'planned_procedure':'计划术式','procedure_consistent':'计划与实际术式一致','unplanned':'非计划事件','reoperation':'属于再次手术',
 'emergency_lifesaving':'抢救生命的急诊手术','intraoperative_transfusion':'术中进行了输血','autotransfusion':'术中自体血回输',
 'anesthesia_id':'麻醉事件标识','physician_id':'医师唯一标识','discussion_type':'讨论类型','participants':'参与人员（每行一人）',
 'departments':'参与科室（每行一科室）','host':'主持人','host_role':'主持人身份','organizer':'组织部门','conclusion':'讨论结论',
 'complete':'记录或上传完整','consent_type':'知情同意类型','physician_signed_time':'医师签字时间','patient_signed_time':'患者签字时间',
 'rescue_id':'抢救事件标识','survival_confirmed_until':'有证据确认存活至','total':'住院总费用（元）','nursing_type':'护理记录类型',
 'critical_id':'危急值条目标识','item':'危急值项目','report_time':'危急值报告时间','received_time':'临床接收时间',
 'transfusion_id':'输血治疗事件标识','intraoperative':'术中输血','autologous':'自体血','assessment':'输血后评估内容',
 'diagnosis':'并发症诊断','surgery_related':'与手术有关','previous_discharge_time':'前次出院时间','death_related':'死亡病例涉及纠纷',
 'required':'属于应上传病案',
}
ENUM_LABELS = {'examination':'检查','treatment':'治疗','other':'其他','unknown':'未知','long':'长期','temporary':'临时',
 'emergency':'急会诊','ordinary':'普通会诊','preoperative':'术前讨论','difficult':'疑难病例讨论','death':'死亡病例讨论',
 'multidisciplinary':'多学科讨论','bedside_handover':'床旁交接班','level':'护理级别记录'}


def edit_fields(kind, attrs, key_prefix):
    columns = st.columns(2)
    values = {}
    for i,(key,typ) in enumerate(FIELDS[kind].items()):
        label = LABELS.get(key,key)
        old = attrs.get(key)
        with columns[i%2]:
            if typ=='bool':
                options = [None,True,False]
                values[key] = st.selectbox(label,options,index=options.index(old) if type(old) is bool else 0,
                    format_func=lambda x:'未知' if x is None else ('是' if x else '否'),key=key_prefix+'_'+key)
            elif key in ENUMS:
                options = [None]+sorted(ENUMS[key])
                values[key] = st.selectbox(label,options,index=options.index(old) if old in options else 0,
                    format_func=lambda x:ENUM_LABELS.get(x,'未知'),key=key_prefix+'_'+key)
            else:
                text = '\n'.join(old) if typ=='texts' and isinstance(old,list) else (str(old) if old is not None else '')
                values[key] = st.text_area(label,text,key=key_prefix+'_'+key,height=70) if typ=='texts' else st.text_input(label,text,key=key_prefix+'_'+key)
    return values


def parse_fields(kind, values):
    result = {}
    for key,typ in FIELDS[kind].items():
        value = values[key]
        if isinstance(value,str):
            value = value.strip()
            if not value:
                value = None
            elif typ=='texts':
                value = [x.strip() for x in value.splitlines() if x.strip()]
            elif typ=='int':
                value = int(value)
            elif typ=='number':
                value = float(value)
        result[key] = value
    return result
