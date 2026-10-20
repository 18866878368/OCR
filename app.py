"""Local medical-record workflow: import, process, inspect results."""
import os
from pathlib import Path
import streamlit as st
from medocr.batch import launch_worker, stop_worker
from medocr.config import load_config
from medocr.report import export_report
from medocr.rules import batch_report
from medocr.store import Store
from medocr.rule_extract import batch_mode
from medocr.ui_common import action
from medocr.ui_workflow import (add_cases, case_labels, chinese_csv, jobs_table,
                               queue_existing_cases, summary_rows, worker_running)

st.set_page_config(page_title='病案指标助手',layout='wide',initial_sidebar_state='expanded')
st.markdown('<style>.block-container {padding-top:2rem;padding-bottom:2rem;} '
            '[data-testid="stAppDeployButton"] {display:none;}</style>',unsafe_allow_html=True)
config_path = Path(os.environ.get('MEDOCR_CONFIG',Path(__file__).with_name('config.local.json')))
if not config_path.exists():
    config_path = Path(__file__).with_name('config.json')
config = load_config(config_path)
st.session_state['_config_path'] = str(config_path)
store = Store(config['data_dir'])
if '_next_batch' in st.session_state:
    st.session_state['active_batch'] = st.session_state.pop('_next_batch')
if '_next_navigation' in st.session_state:
    st.session_state['navigation'] = st.session_state.pop('_next_navigation')
st.title('病案指标助手')
st.caption('导入病例 → 一键处理 → 查看 35 项指标。病例和处理结果保存在本机。')
if '_record_delete_notice' in st.session_state:
    st.success(st.session_state.pop('_record_delete_notice'))
if '_record_delete_warning' in st.session_state:
    st.warning(st.session_state.pop('_record_delete_warning'))
with st.sidebar:
    st.header('工作区')
    nav = st.radio('页面',['处理病例','查看结果','核对与复核','设置'],key='navigation',label_visibility='collapsed')
    batches = store.all('SELECT * FROM batches ORDER BY created DESC')
    if batches:
        ids = [b['id'] for b in batches]
        if st.session_state.get('active_batch') not in ids:
            st.session_state['active_batch'] = ids[0]
        bid = st.selectbox('当前批次',ids,key='active_batch',format_func=lambda x:next(b['name'] for b in batches if b['id']==x))
        selected_batch = next(b for b in batches if b['id']==bid)
        cases = store.cases(bid)
        st.caption(f'{len(cases)} 次住院 · '+('医院确认的完整范围' if selected_batch['population']=='hospital_confirmed_complete' else '所提交病例范围'))
    else:
        bid, cases = None, []
        st.caption('导入第一份病例后，会创建一个批次。')
    st.divider()
    st.caption('自动结果是初判。缺少资料时保留未知，需要核对后才能作为正式结果。')

@st.fragment(run_every=3)
def show_progress(batch_id):
    # Refresh this read-only panel without restarting the processing worker.
    with Store(config['data_dir']) as fresh:
        jobs = fresh.all('SELECT * FROM jobs WHERE batch_id=?',(batch_id,))
        active = worker_running(fresh,batch_id)
        key = '_processing_active_'+batch_id
        was_active = st.session_state.get(key,False)
        st.session_state[key] = active
        if was_active and not active:
            st.rerun(scope='app')
        complete = sum(j['stage'] in {'complete','partial'} for j in jobs)
        partial = sum(j['stage']=='partial' for j in jobs)
        errors = sum(j['stage']=='error' for j in jobs)
        if jobs:
            st.progress(complete/len(jobs),text=f'已完成 {complete} / {len(jobs)} 份文件'+(' · 正在后台处理' if active else ''))
            st.dataframe(jobs_table(fresh,batch_id),hide_index=True,width='stretch')
        else:
            pages = [p for c in fresh.cases(batch_id) for p in fresh.pages(c['id'])]
            if pages:
                st.info(f'已有 {len(pages)} 页资料，其中 {sum(p["stage"]=="full" for p in pages)} 页已识别。点击开始处理即可继续提取指标要素。')
        worker = fresh.one('SELECT * FROM batch_workers WHERE batch_id=?',(batch_id,))
        if worker and worker['state']=='error':
            st.error('处理未完成，请在“设置 → 诊断信息”查看原因，然后继续处理。')
        elif worker and worker['state']=='running' and not active:
            st.warning('后台进程已退出，点击继续处理可从保存的进度重试。')
        if errors:
            st.warning(f'{errors} 份文件尚未处理完成。已成功保存的识别结果可以复用。')
        if partial:
            st.warning(f'{partial} 份文件的规则处理已完成，语义辅助待重试；可以查看现有结果或取消语义辅助后继续。')
        if active:
            st.caption('可以切换页面查看已有结果。长病例的要素提取需要等待，页面会自动更新进度。')
        if st.button('更新状态 / 显示下一步',key='refresh_processing'):
            st.rerun()

def processing_page():
    st.subheader('1 · 导入病例')
    if cases:
        total_pages = sum(store.one('SELECT SUM(page_count) n FROM documents WHERE case_id=?',(c['id'],))['n'] or 0 for c in cases)
        st.caption(f'已导入 {len(cases)} 次住院，共 {total_pages} 页。可以直接开始处理。')
    import_panel = st.expander('添加更多病例或创建新批次',expanded=False) if cases else st.container()
    with import_panel:
        st.write('一份或多份病例均可。每个 PDF 默认代表一次住院；同次住院的多个文件可在复核页合并。')
        with st.form('import_cases',clear_on_submit=False):
            uploads = st.file_uploader('选择病例 PDF（可多选）',type=['pdf'],accept_multiple_files=True)
            with st.expander('也可以使用本机文件或文件夹'):
                local_path = st.text_input('病例 PDF 路径或专用病例文件夹',placeholder=r'F:\病例\某份病例.pdf')
            with st.expander('批次名称与统计期间（选填）'):
                new_batch = st.checkbox('创建新批次',value=not bool(bid))
                name = st.text_input('批次名称',value='病例测试' if not bid else selected_batch['name'])
                left,right = st.columns(2)
                start = left.text_input('开始日期',placeholder='YYYY-MM-DD')
                end = right.text_input('结束日期',placeholder='YYYY-MM-DD')
                complete = st.checkbox('医院已确认：本批次包含该期间全部应统计病例')
            imported = st.form_submit_button('导入病例',type='primary')
        if imported:
            def import_inputs():
                if not uploads and not local_path.strip():
                    raise ValueError('请先选择病例 PDF，或填写本机病例路径。')
                target = store.batch(name,start or None,end or None,'hospital_confirmed_complete' if complete else 'submitted') if new_batch or not bid else bid
                count,_ = add_cases(store,target,uploads,local_path,config['root'])
                st.session_state['_next_batch'] = target
                return f'已导入 {count} 份病例。下一步点击“开始处理”。'
            if action(import_inputs):
                st.rerun()
    st.subheader('2 · 开始处理')
    st.write('默认识别所有页面，再按明确字段和文书规则提取指标要素。证据不足的项目保持未知；已完成步骤自动复用。')
    running = bool(bid and worker_running(store,bid))
    jobs = store.all('SELECT * FROM jobs WHERE batch_id=?',(bid,)) if bid else []
    worker = store.one('SELECT * FROM batch_workers WHERE batch_id=?',(bid,)) if bid else None
    done = bool(jobs and all(j['stage'] in {'complete','partial'} for j in jobs))
    assist = st.checkbox('使用本机语义模型辅助疑难片段（可选）',
                         value=bool(bid and batch_mode(store,bid,config)=='assisted' and config['llm'].get('enabled')),
                         disabled=running or not config['llm'].get('enabled'),key='semantic_assist_'+str(bid))
    st.caption('不勾选即可完成规则提取，无需启动 Ollama。勾选后只辅助规则未解决的文书类型，结果仍需核对。')
    left,right = st.columns([3,1])
    label = '继续处理' if (worker and worker['state'] in {'stopped','error'}) or (jobs and any(j['stage'] in {'error','partial','ocr','extracting'} for j in jobs)) else '开始处理'
    if left.button(label,type='primary',disabled=not bool(bid and (cases or jobs)) or running,key='start_processing'):
        def start_processing():
            queue_existing_cases(store,bid)
            launch_worker(store,bid,config_path,use_llm=assist,scope='all')
            return '已开始后台处理，可以留在此页查看进度。'
        if action(start_processing):
            st.rerun()
    if right.button('停止处理',disabled=not running):
        action(lambda:(stop_worker(store,bid) or '已请求停止；当前步骤完成后保存进度。'))
    if bid:
        show_progress(bid)
    st.subheader('3 · 查看结果')
    if done:
        st.success('规则处理已完成，可以查看 35 项初判。提取完成不代表事件已完整枚举，未知项仍需核查。')
    else:
        st.caption('处理完成后可以查看完整初判。只有一份病例也可以测试提取和计算流程。')
    if st.button('查看 35 项指标',disabled=not bool(cases),key='go_results'):
        st.session_state['_next_navigation'] = '查看结果'
        st.rerun()

def results_page():
    if not cases:
        st.info('请先到“处理病例”导入病例。')
        return
    st.subheader('35 项指标结果')
    st.caption('统计范围为当前批次提交的病例。仅一份病例的测试结果不能代表全院指标。')
    st.caption('本批次处理方式：'+('规则优先 + 可选语义辅助' if batch_mode(store,bid,config)=='assisted' else '规则提取 + 人工复核（未复核的旧模型候选不参与计算）'))
    mode = st.radio('结果类型',['自动初判','已复核结果'],horizontal=True)
    report = batch_report(store,bid,config)
    draft = mode=='自动初判'
    rows = summary_rows(report,draft)
    known = sum(s.get(('draft_' if draft else '')+'value') is not None for s in report['summary'])
    a,b,c = st.columns(3)
    a.metric('住院人次',report['case_count'])
    b.metric('已有数值',known)
    c.metric('待补充或待复核',len(rows)-known)
    if draft:
        st.info('自动初判仅供核对；“待补充 / 待复核”表示证据或统计条件不足。')
    st.dataframe(rows,hide_index=True,width='stretch')
    st.download_button('下载指标汇总（Excel 可打开）',chinese_csv(rows),'35项指标汇总.csv','text/csv')
    with st.expander('查看某项指标的计算依据'):
        mid = st.selectbox('选择指标',[s['metric_id'] for s in report['summary']],key='result_metric',format_func=lambda x:next(s['name'] for s in report['summary'] if s['metric_id']==x))
        summary = next(s for s in report['summary'] if s['metric_id']==mid)
        prefix = 'draft_' if draft else ''
        missing = summary.get(prefix+'missing',[])
        if missing:
            st.write('**待解决事项**')
            for item in missing:
                st.write('• '+item)
        selected = [d for d in report['details'] if d['metric_id']==mid and d.get('calculation_mode')==('draft' if draft else 'reviewed')]
        labels = case_labels(store,bid)
        st.dataframe([{'病例':labels.get(d['case_id'],'补充统计'),'分母贡献':d.get('denominator'),'分子贡献':d.get('numerator'),'判定依据':d.get('reason'),'证据':d.get('evidence')} for d in selected],hide_index=True,width='stretch')
    with st.expander('导出完整证据与明细'):
        if st.button('保存完整本地报告'):
            folder = action(lambda:export_report(store,bid,config))
            if folder:
                st.success('已保存到 '+str(folder))

try:
    if nav=='处理病例':
        processing_page()
    elif nav=='查看结果':
        results_page()
    elif nav=='核对与复核':
        from medocr.review_ui import render_merge, render_review
        st.subheader('核对与复核')
        if not cases:
            st.info('请先导入病例。')
        else:
            labels = case_labels(store,bid)
            cid = st.selectbox('选择病例',list(labels),format_func=labels.get,key='review_case')
            section = st.radio('核对内容',['原文核对','事实复核','指标复核','补充资料'],horizontal=True,key='review_section')
            render_review(store,config,bid,cid,section)
            with st.expander('合并同一次住院的多个文件'):
                render_merge(store,bid,cases)
    else:
        from medocr.review_ui import render_settings
        st.subheader('设置')
        render_settings(store,config,bid)
        with st.expander('诊断信息'):
            st.code(f'运行环境：{__import__("sys").executable}\n配置文件：{config_path}\n处理记录：{store.root / "worker_logs"}')
            if bid:
                st.dataframe(store.all('SELECT stage,error_code,progress,updated FROM jobs WHERE batch_id=?',(bid,)),hide_index=True)
finally:
    store.close()
