"""Explicit local record deletion, with shared-case and worker checks."""
import hashlib
from contextlib import closing
import json
from pathlib import Path
import shutil
import sqlite3
from uuid import uuid4

from .batch import process_alive
from .candidate_scope import current_scope, scoped_events
from .store import stamp

REPORT_FILES = ('report.json','summary.csv','details.csv','facts.json','facts.csv','processing.json','README.txt')


def deletion_plan(store, batch_id, config, kind):
    if kind not in {'history','batch'}:
        raise ValueError('删除范围不正确')
    batch = store.one('SELECT * FROM batches WHERE id=?',(batch_id,))
    if not batch:
        raise ValueError('批次不存在或已经删除')
    cases = store.cases(batch_id)
    case_ids = [c['id'] for c in cases]
    related = {batch_id}
    for cid in case_ids:
        related.update(r['batch_id'] for r in store.all('SELECT batch_id FROM batch_cases WHERE case_id=?',(cid,)))
    blocked = any(w['batch_id'] in related and w['state']=='running' and process_alive(w['pid'])
                  for w in store.all('SELECT * FROM batch_workers'))
    shared = [cid for cid in case_ids if store.one('SELECT batch_id FROM batch_cases WHERE case_id=? AND batch_id!=?',(cid,batch_id))]
    exclusive = [cid for cid in case_ids if cid not in shared]
    ids = []
    protected = 0
    if kind == 'history':
        for cid in case_ids:
            # Protect current automatic candidates in either mode, including reusable
            # assistance when this batch currently runs rules only.
            rows = scoped_events(store,cid,config,'assisted',include_history=True)
            scope = current_scope(store,cid,config,'assisted')
            reviewed = {r['object_id'] for r in store.all("SELECT object_id FROM audit WHERE kind='event_review'")}
            referenced = set()
            for e in rows:
                if e['kind']=='link' and (e['verified'] or e['origin']=='manual' or e['id'] in reviewed):
                    referenced.update(e['attributes'].get(k) for k in ('source_event_id','target_event_id'))
            human_rows = store.all('SELECT * FROM reviews WHERE case_id=?',(cid,))
            human_text = json.dumps(human_rows,ensure_ascii=False)
            for e in rows:
                if e['origin'] not in {'rules','regex','local_llm'} or e['verified'] or e['eligible_in_mode']:
                    continue
                attrs = e['attributes']
                plan = scope['rules'] if e['origin']=='rules' else scope['semantic']
                key = attrs.get('_rule_page') if e['origin']=='rules' else attrs.get('_chunk')
                sig = attrs.get('_rule_signature') if e['origin']=='rules' else attrs.get('_signature')
                # Keep incomplete current outputs available for retry/inspection.
                if e['scope_reason']=='当前提取块尚未成功完成' and plan.get(key)==sig:
                    continue
                if e['id'] in reviewed or e['id'] in referenced or e['id'] in human_text:
                    protected += 1
                    continue
                ids.append(e['id'])
        # Never leave automatic relations pointing to deleted endpoints.
        deleting = set(ids)
        for cid in case_ids:
            for e in store.events(cid,active=False):
                if (e['kind']=='link' and not e['verified'] and e['origin'] in {'rules','local_llm'} and
                        any(e['attributes'].get(k) in deleting for k in ('source_event_id','target_event_id'))):
                    ids.append(e['id'])
        ids = sorted(set(ids))
    else:
        ids = sorted(e['id'] for cid in exclusive for e in store.events(cid,active=False))
    documents = [d['id'] for cid in exclusive for d in store.all('SELECT id FROM documents WHERE case_id=?',(cid,))] if kind=='batch' else []
    data = {'batch_id':batch_id,'kind':kind,'case_ids':case_ids,'shared_case_ids':shared,
        'exclusive_case_ids':exclusive,'event_ids':ids,'document_ids':documents,'protected_history':protected,
        'blocked':blocked}
    data['signature'] = hashlib.sha256(json.dumps(data,sort_keys=True).encode()).hexdigest()
    return data


def report_folder(config, batch_id):
    # Validate the resolved target before touching files. Only generated filenames
    # are removed; original PDFs, model files and arbitrary directories are untouched.
    root = Path(config['report_dir']).resolve()
    target = root/batch_id
    if target.is_symlink() or target.resolve().parent != root or not batch_id or Path(batch_id).name != batch_id:
        raise ValueError('报告目录超出允许的删除范围')
    for name in REPORT_FILES:
        path = target/name
        if path.is_symlink() or path.resolve().parent != target.resolve():
            raise ValueError('报告文件链接超出允许的删除范围')
    return target


def delete_records(store, batch_id, config, kind, confirmed=False, backup=True, expected_signature=None):
    if not confirmed:
        raise ValueError('请先勾选确认删除并核对删除范围')
    folder = report_folder(config,batch_id)
    backup_dir = None
    store.conn.execute('BEGIN IMMEDIATE')
    try:
        plan = deletion_plan(store,batch_id,config,kind)
        if plan['blocked']:
            raise ValueError('本批次或共用病例的批次正在处理，请先停止并等待进程退出')
        if expected_signature and expected_signature != plan['signature']:
            raise ValueError('记录已发生变化，请刷新页面，重新核对删除范围')
        if kind=='history' and not plan['event_ids']:
            raise ValueError('没有可删除的历史自动候选')
        if backup:
            backup_dir=store.root/'deletion_backups'/(stamp().replace(':','-')+'_'+uuid4().hex[:8])
            backup_dir.mkdir(parents=True,exist_ok=False)
            # Use a second read connection: backing up the transaction connection
            # while it holds a write reservation can block SQLite's backup API.
            with closing(sqlite3.connect((store.root/'medocr.sqlite3').resolve().as_uri()+'?mode=ro',uri=True)) as source:
                with closing(sqlite3.connect(backup_dir/'medocr.sqlite3')) as destination:
                    source.backup(destination)
            for name in REPORT_FILES:
                path=folder/name
                if path.is_file():
                    target=backup_dir/'report'; target.mkdir(exist_ok=True)
                    shutil.copy2(path,target/name)
            (backup_dir/'manifest.json').write_text(json.dumps({'batch_id':batch_id,'kind':kind,'created':stamp()},indent=2),encoding='utf-8')
        if kind=='history':
            removing=set(plan['event_ids'])
            for cid in plan['case_ids']:
                for row in store.all('SELECT * FROM extraction_outputs WHERE case_id=?',(cid,)):
                    old=json.loads(row['event_ids']); kept=[eid for eid in old if eid not in removing]
                    if kept!=old:
                        store.conn.execute('UPDATE extraction_outputs SET event_ids=? WHERE case_id=? AND chunk_id=? AND signature=?',
                            (json.dumps(kept),cid,row['chunk_id'],row['signature']))
            store.conn.executemany('DELETE FROM events WHERE id=?',[(eid,) for eid in removing])
        else:
            for table in ('reviews','coverage','measures','rule_approvals','jobs','batch_workers','batch_processing','batch_cases'):
                store.conn.execute(f'DELETE FROM {table} WHERE batch_id=?',(batch_id,))
            for cid in plan['exclusive_case_ids']:
                for table in ('extraction_runs','extraction_outputs','extraction_plans','events'):
                    store.conn.execute(f'DELETE FROM {table} WHERE case_id=?',(cid,))
                store.conn.execute('DELETE FROM pages WHERE document_id IN (SELECT id FROM documents WHERE case_id=?)',(cid,))
                store.conn.execute('DELETE FROM documents WHERE case_id=?',(cid,))
                store.conn.execute('DELETE FROM cases WHERE id=?',(cid,))
            store.conn.execute('DELETE FROM batches WHERE id=?',(batch_id,))
        result={'kind':kind,'deleted_events':len(plan['event_ids']),
            'deleted_cases':len(plan['exclusive_case_ids']) if kind=='batch' else 0,
            'preserved_shared_cases':len(plan['shared_case_ids']),
            'backup_dir':str(backup_dir) if backup_dir else None,'report_error':None}
        store.audit('delete_records',batch_id,None,{k:v for k,v in result.items() if k!='backup_dir'},'local_user')
        store.conn.commit()
    except Exception:
        store.conn.rollback()
        raise
    try:
        if kind=='history':
            from .report import export_report
            export_report(store,batch_id,config)
        else:
            for name in REPORT_FILES:
                (folder/name).unlink(missing_ok=True)
            if folder.is_dir() and not any(folder.iterdir()):
                folder.rmdir()
    except Exception as exc:
        result['report_error']=type(exc).__name__
    return result
