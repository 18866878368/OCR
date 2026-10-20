import csv
import json
from pathlib import Path

from .rules import batch_report


def safe_cell(value):
    if isinstance(value,(dict,list)):
        value = json.dumps(value,ensure_ascii=False)
    if isinstance(value,str) and value.lstrip().startswith(('=','+','-','@')):
        value = "'"+value
    return value


def write_csv(path, rows, fields):
    with Path(path).open('w',encoding='utf-8-sig',newline='') as stream:
        writer = csv.DictWriter(stream,fieldnames=fields,extrasaction='ignore')
        writer.writeheader()
        for row in rows:
            writer.writerow({k:safe_cell(row.get(k,'')) for k in fields})


def export_report(store,batch_id,config):
    report = batch_report(store,batch_id,config)
    folder = Path(config['report_dir'])/batch_id
    folder.mkdir(parents=True,exist_ok=True)
    (folder/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    write_csv(folder/'summary.csv',report['summary'],['metric_id','name','unit','kind','status','numerator','denominator','unknown_denominator_groups','unknown_numerator_objects','value','lower','upper','missing','sources',
              'draft_numerator','draft_denominator','draft_value','draft_status','draft_missing','measures','draft_measures','rule_warning'])
    write_csv(folder/'details.csv',report['details'],['metric_id','case_id','item_key','denominator','numerator','suggested_numerator','reason','evidence','fact_ids','reviewer','origin','updated','calculation_mode'])
    from .candidate_scope import scoped_events, current_scope
    mode = report['calculation_config'].get('extraction_mode','rules')
    facts = [e for c in store.cases(batch_id) for e in scoped_events(store,c['id'],config,mode,include_history=True)]
    (folder/'facts.json').write_text(json.dumps(facts,ensure_ascii=False,indent=2),encoding='utf-8')
    write_csv(folder/'facts.csv',facts,['case_id','id','kind','event_time','record_time','attributes','document_id','page','quote','origin','verified','active','eligible_in_mode','candidate_scope','scope_reason','scope_run_id'])
    runs = [r for c in store.cases(batch_id) for r in store.all('SELECT * FROM extraction_runs WHERE case_id=?',(c['id'],))]
    scopes = {c['id']:{k:v for k,v in current_scope(store,c['id'],config,mode).items() if k not in {'checkpoints','outputs'}} for c in store.cases(batch_id)}
    (folder/'processing.json').write_text(json.dumps({'jobs':store.all('SELECT * FROM jobs WHERE batch_id=?',(batch_id,)),
                                                   'extraction_runs':runs,'current_scopes':scopes},ensure_ascii=False,indent=2),encoding='utf-8')
    (folder/'README.txt').write_text(f"{report['scope_label']}\n规则版本：{report['rule_version']}\n提取模式：{mode}\n住院人次数：{report['case_count']}\nvalue 是已复核结果，draft_value 是未复核初判；二者不能混用。\nvalue/lower/upper 的比例值为小数，0.1 表示10%。ratio、rate_ratio不乘100%；median单位为分钟。\n空值表示未知或不适用，详见status和missing；处理完成不代表临床对象完整枚举，未找到证据不能推定未发生。\nfacts.json/csv 保留全部来源页与证据；eligible_in_mode 标明当前可用性，candidate_scope 区分 current/reviewed/history，scope_reason 说明隔离原因，scope_run_id 对应当前来源清单。历史未复核候选在任何模式均不参与当前计算或自动关联；明确的人工复核保留，来源变化仍须重新复核。\nprocessing.json 记录队列、提取检查点与当前来源清单。\n文件包含病案证据，请在授权本地范围内保存。\n",encoding='utf-8')
    return folder
