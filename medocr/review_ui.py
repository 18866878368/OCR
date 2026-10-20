"""Secondary clinical review and settings screens."""
import hashlib
import json
from pathlib import Path
import streamlit as st
from medocr.config import load_config
from medocr.extract import extract_case
from medocr.pipeline import import_pdf, process_pages, render_page
from medocr.report import export_report
from medocr.rules import batch_report, calculate_measures, canonical_time, catalog
from medocr.store import Store
from medocr.specs import FIELDS, RELATIONS, active_metrics, core_specs
from medocr.core_engine import rule_hash
from medocr.batch import enqueue, queue_directory, launch_worker, stop_worker, process_alive, preflight
from medocr.extract import local_endpoint
from medocr.import_facts import validate_attributes, import_hospital_facts
from medocr.references import load_reference
from medocr.fields_ui import edit_fields, parse_fields
from medocr.relations import confirm_relation, RELATION_LABELS
from medocr.identity import possible_duplicate_admissions
from medocr.ui_common import FACT_LABELS, action, display_rows, save_local_settings

def show_extraction_guidance(metric):
    with st.expander('医院提供的分子、分母提取指导', expanded=False):
        st.markdown('**分子提取提示词**')
        st.write(metric.get('numerator_prompt') or '尚未导入')
        st.markdown('**分母提取提示词**')
        st.write(metric.get('denominator_prompt') or '尚未导入')
        if metric.get('hospital_notes'):
            st.markdown('**医院补充备注**')
            st.write(metric['hospital_notes'])
        sources = metric.get('prompt_sources', {})
        st.caption('来源：核心制度监测指标统计提示词.xlsx · ' + ', '.join(sources.values()))
        spec = next((s for s in core_specs() if s['id']==metric['id']),None)
        if spec:
            st.caption('所需要素：'+', '.join(FACT_LABELS[k] for k in spec['fact_types'])+'；计数单位：'+spec['counting_unit'])
            if spec['external_requirements']:
                st.caption('补充资料要求：'+'；'.join(spec['external_requirements']))
        else:
            st.caption('此附加指标使用人工判定或补充统计数据。')

def render_review(store, config, bid, cid, section):
    pages = store.pages(cid)
    metrics = active_metrics(config)
    if section == '原文核对':
        a,b,c = st.columns(3)
        a.metric('病历总页数',len(pages))
        b.metric('已识别页数',sum(p['stage']=='full' for p in pages))
        c.metric('待识别或需重试',sum(p['stage']!='full' for p in pages))
        options = [(p['document_id'],p['page']) for p in pages]
        if not options:
            st.info('还没有病历页面。')
            return
        doc_numbers = {d:i for i,d in enumerate(dict.fromkeys(p['document_id'] for p in pages),1)}
        selected = st.selectbox('选择原始页',options,format_func=lambda x:(f'文件 {doc_numbers[x[0]]} · ' if len(doc_numbers)>1 else '')+f'PDF 第 {x[1]} 页')
        page = next(p for p in pages if (p['document_id'],p['page'])==selected)
        st.caption('已识别' if page['stage']=='full' else '此页尚未完成识别，请到处理病例页继续。')
        left,right = st.columns(2)
        left.text_area('识别文字（只读）',page['text'],height=500,disabled=True)
        if right.checkbox('显示原始 PDF 页图'):
            try:
                right.image(render_page(page['path'],page['page'],120),caption=f'原始 PDF 第 {page["page"]} 页')
            except Exception as exc:
                right.error(f'页图无法显示：{type(exc).__name__}')
        with st.expander('重新识别当前页'):
            st.caption('如果文字识别有问题，可重新识别这一页。完成后需重新处理指标要素。')
            if st.button('重新识别这一页'):
                from medocr.pipeline import process_single_page
                action(lambda:process_single_page(store,cid,page,config))
    if section == '事实复核':
        from medocr.candidate_scope import scoped_events
        from medocr.rule_extract import batch_mode
        all_events = scoped_events(store,cid,config,batch_mode(store,bid,config),include_history=True)
        history = [e for e in all_events if not e['eligible_in_mode']]
        show_history = st.checkbox('查看历史及已隔离候选（不参与当前自动计算）',value=False,key='review_history_'+cid)
        st.caption(f'当前可用 {len(all_events)-len(history)} 条；历史或已隔离 {len(history)} 条。历史记录可查看，逐条核对并确认后才可作为人工事实使用。')
        events = all_events if show_history else [e for e in all_events if e['eligible_in_mode']]
        if events:
            st.dataframe([{'要素ID':e['id'],'类型':FACT_LABELS.get(e['kind'],e['kind']),'事件时间':e['event_time'],'记录时间':e['record_time'],
                           '来源页':e['page'],'已复核':bool(e['verified']),'来源已变化':bool(e['attributes'].get('_stale')),
                           '候选范围':{'current':'当前候选','reviewed':'已复核','history':'历史/已隔离'}[e['candidate_scope']],
                           '隔离原因':e['scope_reason'],
                           '需核对字段':'；'.join(e['attributes'].get('_validation_issues',[])),
                           '提取值':json.dumps({k:v for k,v in e['attributes'].items() if not k.startswith('_')},ensure_ascii=False)} for e in events],hide_index=True)
            with st.expander('批量确认已核对的要素'):
                pending = [e for e in events if e['eligible_in_mode'] and not e['verified']]
                with st.form('bulk_fact_review'):
                    ids = st.multiselect('选择已逐条查看原文/原图的要素',[e['id'] for e in pending],format_func=lambda x:next(FACT_LABELS.get(e['kind'],e['kind'])+' · 第'+str(e['page'])+'页 · '+x[:8] for e in pending if e['id']==x))
                    confirmed = st.checkbox('我已核对所选要素的提取值、时间含义及关联关系')
                    note = st.text_input('批量复核说明')
                    reviewer = st.text_input('批量复核人')
                    submitted = st.form_submit_button('确认选中要素')
                if submitted:
                    def bulk_verify():
                        if not confirmed or not ids or not note.strip() or not reviewer.strip():
                            raise ValueError('请选择已核对的要素并填写复核人和说明')
                        for eid in ids:
                            e = next(e for e in pending if e['id']==eid)
                            store.review_event(eid,{'verified':True,'attributes':{**e['attributes'],'review_note':note}},reviewer)
                        return '已确认'+str(len(ids))+'条要素；空值仍保留为未知'
                    action(bulk_verify)
        if events:
            eid = st.selectbox('候选事件',[e['id'] for e in events],format_func=lambda x:next(f"{FACT_LABELS.get(e['kind'],e['kind'])} · {e['event_time'] or e['record_time'] or '时间未知'} · 第{e['page']}页 · {'已复核' if e['verified'] else '候选'}" for e in events if e['id']==x))
            event = next(e for e in events if e['id']==eid)
            if not event['eligible_in_mode']:
                st.warning(event['scope_reason']+'。此记录未进入当前自动计算；确认前请核对当前原文与事件身份。')
            st.code(event['quote'],language=None)
            st.caption(f"文档 {event['document_id']}，PDF第{event['page']}页。可在页面与OCR页签查看原图。")
            if event['attributes'].get('_external_source'):
                st.caption('医院补充资料：'+event['attributes']['_external_source']+'，记录序号'+str(event['attributes'].get('_source_row')))
            if event['attributes'].get('_stale'):
                st.warning('来源或关联输入已变化；需要核对当前原文/原图后重新确认。')
            with st.expander('全部来源证据'):
                st.json(event['attributes'].get('_evidence',[]))
            with st.form('event_review_'+eid):
                et = st.text_input('实际事件时间 YYYY-MM-DDTHH:mm',value=event['event_time'] or '')
                rt = st.text_input('文书记录时间 YYYY-MM-DDTHH:mm',value=event['record_time'] or '')
                attrs = dict(event['attributes'])
                if event['kind'] in FIELDS:
                    field_values = edit_fields(event['kind'],attrs,'field_'+eid)
                else:
                    body = st.text_area('关联属性JSON：来源要素ID、目标要素ID、关联类型',value=json.dumps({k:v for k,v in attrs.items() if k in {'source_event_id','target_event_id','relation'}},ensure_ascii=False,indent=2),height=180)
                verified = st.checkbox('已查看原文/原图并确认事件',value=bool(event['verified']))
                active = st.checkbox('保留此事件用于判断',value=bool(event['active']))
                attrs['review_note'] = st.text_input('复核说明',value=attrs.get('review_note',''))
                reviewer = st.text_input('复核人')
                save = st.form_submit_button('保存事实复核')
            if save:
                def save_event():
                    for value in (et,rt):
                        if value and canonical_time(value) is None:
                            raise ValueError('时间格式不正确，必须有完整日期与时分')
                    if verified and event['kind'] in {'admission','order'} and not et:
                        raise ValueError('确认入院/医嘱事件前必须填写实际时间')
                    if not attrs['review_note'].strip():
                        raise ValueError('请填写复核说明')
                    edited = parse_fields(event['kind'],field_values) if event['kind'] in FIELDS else json.loads(body)
                    if event['kind'] in FIELDS:
                        validate_attributes(event['kind'],edited)
                    elif event['kind']=='link':
                        if edited.get('relation') not in RELATIONS:
                            raise ValueError('关联类型不正确')
                        endpoints = [next((e for e in events if e['id']==edited.get(k)),None) for k in ('source_event_id','target_event_id')]
                        if not all(endpoints) or tuple(e['kind'] for e in endpoints)!=RELATIONS[edited['relation']]:
                            raise ValueError('关联两端必须为本病例中类型匹配的实际要素')
                    # Metadata retains the original evidence and extraction provenance.
                    attrs.update(edited)
                    attrs['_stale'] = False if verified else attrs.get('_stale',False)
                    store.review_event(eid,{'event_time':canonical_time(et),'record_time':canonical_time(rt),'attributes':attrs,'verified':verified,'active':active},reviewer)
                    return '已保存；汇总将重新计算'
                action(save_event)
            with st.expander('人工建立事件关联'):
                relation = st.selectbox('关联类型',list(RELATIONS),format_func=lambda r:RELATION_LABELS[r])
                left_kind,right_kind = RELATIONS[relation]
                sources = [e for e in events if e['eligible_in_mode'] and e['kind']==left_kind]
                targets = [e for e in events if e['eligible_in_mode'] and e['kind']==right_kind]
                if sources and targets:
                    def label_event(x):
                        e = next(e for e in events if e['id']==x)
                        return (e['event_time'] or e['record_time'] or '时间未知')+' · 第'+str(e['page'])+'页 · '+x[:8]
                    with st.form('manual_relation_'+relation):
                        source_id = st.selectbox('关联来源要素',[e['id'] for e in sources],format_func=label_event)
                        target_id = st.selectbox('关联目标要素',[e['id'] for e in targets],format_func=label_event)
                        quote = st.text_area('证明两者关联的原文（从两端已有证据中复制）')
                        relation_checked = st.checkbox('已核对两者属于同一事件，不只依据时间接近')
                        relation_note = st.text_input('关联复核说明')
                        relation_reviewer = st.text_input('关联复核人')
                        relation_save = st.form_submit_button('保存已复核关联')
                    if relation_save:
                        action(lambda:confirm_relation(store,cid,relation,source_id,target_id,quote,relation_reviewer,relation_note) if relation_checked else (_ for _ in ()).throw(ValueError('请核对并确认关联')))
                else:
                    st.info('当前病例缺少此关联所需的两类要素；可先提取或导入医院补充明细。')
        else:
            st.info('先识别相关页面并提取；也可在指标逐项判定中依据原PDF录入人工结果。')
    if section == '指标复核':
        mid = st.selectbox('指标',[m['id'] for m in metrics],format_func=lambda x:next(m['id']+' '+m['name'] for m in metrics if m['id']==x),key='review_metric')
        metric = next(m for m in metrics if m['id']==mid)
        st.write(metric['formula'])
        st.caption(metric['notes'])
        show_extraction_guidance(metric)
        spec = next((s for s in core_specs() if s['id']==mid),None)
        if spec and spec['rule_conflict']:
            st.warning(spec['rule_conflict'])
            approval = store.one('SELECT * FROM rule_approvals WHERE batch_id=? AND metric_id=?',(bid,mid))
            st.caption('本批次口径已确认' if approval and approval['rule_hash']==rule_hash(spec) else '本批次口径尚未确认，已复核结果暂不发布指标值')
            with st.form('rule_approval_'+mid):
                rule_confirmed = st.checkbox('医院确认采用上述程序计算口径')
                rule_note = st.text_input('口径确认说明')
                rule_reviewer = st.text_input('口径确认人')
                rule_submit = st.form_submit_button('保存本批次口径确认')
            if rule_submit:
                action(lambda:store.approve_rule(bid,mid,rule_hash(spec),rule_reviewer,rule_note) if rule_confirmed else (_ for _ in ()).throw(ValueError('请先确认医院认可上述口径')))
        with st.expander('统计对象完整性复核'):
            with st.form('coverage_review_'+bid+cid):
                coverage_metrics = st.multiselect('已经完整核查并枚举对象的指标',[m['id'] for m in metrics],default=[mid])
                coverage_confirmed = st.checkbox('已核查对应完整资料，包括重复事件、遗漏对象以及未发生事项的依据')
                coverage_note = st.text_area('核查范围和完整性依据')
                coverage_reviewer = st.text_input('完整性复核人')
                coverage_submit = st.form_submit_button('保存所选指标的对象完整性')
            if coverage_submit:
                def save_coverage():
                    if not coverage_confirmed or not coverage_metrics:
                        raise ValueError('请选择已完整核查的指标并确认')
                    for metric_id in coverage_metrics:
                        store.set_coverage(bid,cid,metric_id,True,coverage_note,coverage_reviewer)
                    return '已保存对象完整性；未确认或缺失的字段仍会阻止计算'
                action(save_coverage)
        automatic_details = [r for r in batch_report(store,bid,config)['details'] if r['case_id']==cid and r['metric_id']==mid and r.get('calculation_mode')=='draft']
        if automatic_details:
            st.dataframe(display_rows(automatic_details),hide_index=True)
        if metric['kind'] in {'rate','ratio'}:
            st.dataframe(store.all('SELECT item_key,denominator,numerator,reason,evidence,reviewer FROM reviews WHERE batch_id=? AND case_id=? AND metric_id=?',(bid,cid,mid)),hide_index=True)
            with st.form('metric_review_'+bid+cid+mid):
                default_item = next((r['item_key'] for r in automatic_details if r['item_key']!='coverage_unknown'),'admission' if spec else 'case')
                item = st.text_input('统计对象标识',value=default_item,help='优先使用自动明细中的对象标识。多次手术/会诊/医嘱逐条填写。')
                du = st.checkbox('分母资格/数量未知')
                d = st.number_input('分母贡献',min_value=0,value=1,step=1)
                nu = st.checkbox('分子判定未知',value=True)
                n = st.number_input('分子贡献',min_value=0,value=0,step=1)
                reason = st.text_area('判定理由')
                evidence = st.text_area('证据：PDF文档及页码、原文，或补充文件与行号')
                reviewer = st.text_input('复核人',key='metric_reviewer')
                complete = st.checkbox('已完整枚举本病例在此指标中的所有统计对象',help='仅适用于其他人工辅助指标；没有相关事件也须有完整核查依据，不能仅因没找到记录而勾选。')
                save = st.form_submit_button('保存此对象判定')
            if save:
                def save_judgment():
                    store.save_review(bid,cid,mid,item,None if du else int(d),None if nu else int(n),reason,evidence,reviewer)
                    if mid not in {'core_01','core_02'}:
                        store.set_coverage(bid,cid,mid,complete,evidence,reviewer)
                    return '判定与覆盖状态已保存'
                action(save_judgment)
        else:
            st.info('该指标为中位数、发生率比、术种或项目统计，请在补充统计数据中输入结构化值。')
    if section == '补充资料':
        with st.expander('导入医院补充事实：危急值、纠纷、上传或输血明细'):
            with st.form('external_facts_import'):
                external_file = st.file_uploader('补充事实JSON或UTF-8 CSV',type=['json','csv'])
                external_reviewer = st.text_input('资料导入人')
                external_verified = st.checkbox('医院已经逐条复核文件中的事实值和住院人次对应关系')
                external_submit = st.form_submit_button('导入补充事实')
            if external_submit:
                def import_external():
                    if external_file is None:
                        raise ValueError('请选择补充事实文件')
                    content = external_file.getvalue()
                    path = store.root/('external_'+hashlib.sha256(content).hexdigest()[:20]+Path(external_file.name).suffix.lower())
                    path.write_bytes(content)
                    return '已导入'+str(import_hospital_facts(store,bid,path,external_reviewer,external_verified))+'条补充事实'
                action(import_external)
        mid = st.selectbox('补充数据指标',[m['id'] for m in metrics],format_func=lambda x:next(m['id']+' '+m['name'] for m in metrics if m['id']==x),key='measure_metric')
        metric = next(m for m in metrics if m['id']==mid)
        st.write(metric['formula'])
        st.caption(metric['notes'])
        show_extraction_guidance(metric)
        st.caption('直接输入批次汇总值时，将以这些已复核数值计算此指标，病例逐项判定不再作为该指标的计算来源。必须覆盖同一统计期间与口径。')
        if metric['kind']=='median':
            example = {'minutes':[5,8,9],'eligible_total':3}
        elif metric['kind']=='catalog_rate':
            example = {'performed_types':['统一术式编码A'],'registered_types':['统一术式编码A','统一术式编码B']}
        else:
            example = {k:0 for k in metric['required_measures']}
        current = store.all('SELECT * FROM measures WHERE batch_id=? AND metric_id=?',(bid,mid))
        values = {m['measure']:json.loads(m['value']) for m in current}
        with st.form('measure_import_'+bid+mid):
            body = st.text_area('JSON测量值（示例须替换为实际数据）',value=json.dumps(values or example,ensure_ascii=False,indent=2),height=180)
            source = st.text_area('来源与范围说明：补充文件、统计期间、口径、行号等')
            reviewer = st.text_input('数据复核人')
            save = st.form_submit_button('保存补充统计值')
        if save:
            def save_measures():
                new = json.loads(body)
                if not isinstance(new,dict):
                    raise ValueError('JSON必须为字段和值的对象')
                calculated = calculate_measures(metric,{**values,**new})
                for key,value in new.items():
                    store.save_measure(bid,mid,key,value,source,reviewer)
                return calculated['status']
            action(save_measures)


def render_settings(store, config, batch_id=None):
    st.caption('默认规则提取可独立运行。本机模型用于可选的疑难片段辅助；医院目录和台账通过本地资料补充。')
    with st.form('local_model_settings'):
        enabled = st.checkbox('启用本机语义模型',value=config['llm']['enabled'])
        url = st.text_input('本机模型接口地址',value=config['llm']['base_url'])
        protocols = ['openai','ollama']
        protocol = st.selectbox('本机服务类型',protocols,index=protocols.index(config['llm'].get('protocol','openai')),
                                format_func=lambda x:'Ollama本机服务' if x=='ollama' else '本机OpenAI兼容接口')
        model = st.text_input('已部署的模型名称',value=config['llm']['model'])
        max_chars = st.number_input('每块病历文字上限',min_value=1000,max_value=32000,value=int(config['llm'].get('max_chars_per_page',12000)),step=1000)
        save = st.form_submit_button('保存模型设置')
    if save:
        def save_model():
            local_endpoint(url)
            if enabled and (not model.strip() or '请填写' in model):
                raise ValueError('请填写已经在本机部署的实际模型名称')
            return save_local_settings({'llm':{**config['llm'],'enabled':enabled,'base_url':url,'protocol':protocol,
                                               'model':model,'max_chars_per_page':int(max_chars)}})
        action(save_model)
    if st.button('测试本机模型连接',disabled=not config['llm']['enabled']):
        action(lambda:(preflight(config,True) or '本机模型能够返回JSON'))
    with st.form('reference_import'):
        reference_file = st.file_uploader('医院参考资料JSON：手术分级、药物名单、工作日历等',type=['json'])
        reference_submit = st.form_submit_button('保存医院参考资料')
    if reference_submit:
        def save_reference():
            if reference_file is None:
                raise ValueError('请选择医院参考资料JSON')
            content = reference_file.getvalue()
            temporary = store.root/('reference_'+hashlib.sha256(content).hexdigest()[:16]+'.json')
            temporary.write_bytes(content)
            load_reference(temporary)
            return save_local_settings({'hospital_reference_file':str(temporary)})
        action(save_reference)
    st.caption('格式示例：examples/hospital_reference.template.json。未声明完整的名单不作为完整统计依据。')
    render_record_management(store,config,batch_id)


def render_record_management(store, config, batch_id=None):
    from medocr.record_management import deletion_plan, delete_records
    st.divider()
    st.subheader('记录管理')
    batches=store.all('SELECT * FROM batches ORDER BY created DESC')
    if not batches:
        st.info('暂无可删除的记录。')
        return
    ids=[b['id'] for b in batches]
    if st.session_state.get('delete_batch_choice') not in ids:
        st.session_state['delete_batch_choice']=batch_id if batch_id in ids else ids[0]
    selected=st.selectbox('选择要清理的批次',ids,key='delete_batch_choice',
        format_func=lambda x:next(b['name'] for b in batches if b['id']==x))
    operation=st.radio('删除范围',['历史自动候选','整个批次'],horizontal=True,key='delete_record_kind')
    kind='history' if operation=='历史自动候选' else 'batch'
    plan=deletion_plan(store,selected,config,kind)
    if kind=='history':
        st.write(f'将删除 {len(plan["event_ids"])} 条历史自动候选。当前可用候选、人工录入和人工复核记录保留。')
        if plan['protected_history']:
            st.caption(f'{plan["protected_history"]} 条历史候选有人工处理或引用，已排除在删除范围外。')
    else:
        st.write(f'将删除此批次的指标复核、补充统计及本地导出报告，并删除 {len(plan["exclusive_case_ids"])} 次独占住院的 OCR、候选和处理记录。')
        st.caption(f'{len(plan["shared_case_ids"])} 次住院与其他批次共用，其病例资料会保留。')
    st.caption('此功能删除系统中的记录；原始 PDF、上传副本、页面图片和操作日志仍保存在本机。模型文件保留。')
    if plan['blocked']:
        st.warning('本批次或共用病例的批次正在处理，请先停止并等待处理进程退出。')
    with st.form('delete_records_'+selected+'_'+kind,clear_on_submit=True):
        backup=st.checkbox('删除前保存本地数据库备份',value=True,key='delete_make_backup_'+selected+'_'+kind)
        st.caption('备份仍包含病案资料，保存在 data/deletion_backups 中；可取消备份后删除。')
        confirmed=st.checkbox('我已核对上述范围，确认删除',value=False,key='delete_confirm_'+selected+'_'+kind)
        submitted=st.form_submit_button('删除历史候选' if kind=='history' else '删除整个批次',
            disabled=plan['blocked'] or (kind=='history' and not plan['event_ids']))
    if submitted:
        result=action(lambda:delete_records(store,selected,config,kind,confirmed,backup,plan['signature']))
        if result:
            message=(f'已删除 {result["deleted_events"]} 条历史自动候选。' if kind=='history' else
                f'已删除批次及 {result["deleted_cases"]} 次独占住院记录；保留 {result["preserved_shared_cases"]} 次共用住院。')
            if result['backup_dir']:
                message+=' 本地备份：'+result['backup_dir']
            st.session_state['_record_delete_notice']=message
            if result['report_error']:
                st.session_state['_record_delete_warning']='数据库记录已删除，本地报告未完成更新或清理：'+result['report_error']
            if kind=='batch' and selected==batch_id:
                remaining=store.all('SELECT id FROM batches ORDER BY created DESC')
                st.session_state['_next_batch']=remaining[0]['id'] if remaining else None
            st.rerun()

def render_merge(store, bid, cases):
    if len(cases)>1:
        with st.form('merge_admission'):
            source_case = st.selectbox('来源住院人次',[c['id'] for c in cases],key='merge_source')
            target_case = st.selectbox('保留的住院人次',[c['id'] for c in cases],key='merge_target')
            merge_confirm = st.checkbox('已核对原始资料，确认两个文件属于同一次住院')
            merge_reviewer = st.text_input('合并确认人')
            merge_submit = st.form_submit_button('合并同次住院资料')
        if merge_submit:
            def merge_admission():
                if not merge_confirm:
                    raise ValueError('请先核对并确认同次住院')
                store.merge_cases(source_case,target_case,merge_reviewer)
                return '同次住院已合并；请刷新并重新提取跨文书关联'
            action(merge_admission)
