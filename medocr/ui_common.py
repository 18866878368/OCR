"""Shared helpers for the local application."""
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

FACT_LABELS = {'admission':'入院','discharge':'出院','transfer':'转科','order':'医嘱','ward_round':'查房','progress_note':'普通病程记录',
 'consultation':'会诊','surgery':'手术','anesthesia':'麻醉','discussion':'病例讨论','consent':'知情同意',
 'rescue':'抢救','death':'死亡','cost':'费用','nursing':'护理/交接班','critical_value':'危急值',
 'transfusion':'输血','transfusion_assessment':'输血后评估','complication':'并发症','readmission':'再次住院',
 'dispute':'纠纷','upload':'病案上传','link':'事件关联'}

def save_local_settings(updates):
    destination = Path(st.session_state.get('_config_path',Path(__file__).resolve().parent.parent/'config.local.json'))
    if destination==Path(__file__).resolve().parent.parent/'config.json':
        destination = destination.with_name('config.local.json')
    current = json.loads((destination if destination.exists() else Path(__file__).resolve().parent.parent/'config.json').read_text(encoding='utf-8-sig'))
    current.update(updates)
    destination.write_text(json.dumps(current,ensure_ascii=False,indent=2),encoding='utf-8')
    return '设置已保存；刷新后生效'

def action(fn):
    try:
        with st.spinner('本地处理中…'):
            result = fn()
        st.success(result if isinstance(result,str) else '操作已完成。')
        return result
    except Exception as exc:
        if isinstance(exc,(ValueError,RuntimeError)):
            st.error(str(exc))
        else:
            st.error(f'操作失败：{type(exc).__name__}；请检查环境或输入。')
        return None

def display_rows(rows):
    return [{k:json.dumps(v,ensure_ascii=False) if isinstance(v,(list,dict)) else v for k,v in row.items()} for row in rows]

