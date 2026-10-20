"""User-facing workflow shared by the web interface and its tests."""
import csv
import io
from pathlib import Path
import re

from .batch import enqueue, process_alive
from .pipeline import import_pdf
from .report import safe_cell

STAGES = {'pending':'等待处理','importing':'读取文件','ocr':'识别病历文字',
          'extracting':'提取指标要素','complete':'处理完成','partial':'规则完成，辅助待重试','error':'处理未完成'}


def add_cases(store, batch_id, uploads=(), local_path='', project_root='.'):
    paths = []
    folder = store.root/'uploads'; folder.mkdir(exist_ok=True)
    import hashlib
    for upload in uploads:
        content = upload.getvalue()
        name = re.sub(r'[^\w\u4e00-\u9fff.()-]', '_', Path(upload.name).name)
        path = folder/(hashlib.sha256(content).hexdigest()[:16]+'_'+name)
        if not path.exists():
            path.write_bytes(content)
        paths.append(path)
    if local_path.strip():
        path = Path(local_path.strip().strip('"'))
        if path.is_dir():
            if path.resolve()==Path(project_root).resolve():
                raise ValueError('请使用只包含病例的专用目录；项目根目录还含指标资料PDF。单份测试可以填写病例文件路径。')
            paths.extend(path.glob('*.pdf'))
        else:
            paths.append(path)
    added = enqueue(store,batch_id,paths)
    for path in paths:
        imported = import_pdf(store,batch_id,path)
        with store.conn:
            store.conn.execute('UPDATE jobs SET case_id=? WHERE batch_id=? AND path=?',
                               (imported['case_id'],batch_id,str(Path(path).resolve())))
    return len(set(Path(p).resolve() for p in paths)), added


def queue_existing_cases(store, batch_id):
    """Earlier UI imports also become eligible for the single start button."""
    existing = {row['case_id'] for row in store.all('SELECT case_id FROM jobs WHERE batch_id=?',(batch_id,))}
    docs = store.all('SELECT d.* FROM documents d JOIN batch_cases b ON b.case_id=d.case_id WHERE b.batch_id=?',(batch_id,))
    added = 0
    for doc in docs:
        if doc['case_id'] not in existing:
            added += enqueue(store,batch_id,[doc['path']])
            with store.conn:
                store.conn.execute('UPDATE jobs SET case_id=? WHERE batch_id=? AND path=?',
                                   (doc['case_id'],batch_id,str(Path(doc['path']).resolve())))
    return added


def worker_running(store, batch_id):
    worker = store.one('SELECT * FROM batch_workers WHERE batch_id=?',(batch_id,))
    return bool(worker and worker['state']=='running' and process_alive(worker['pid']))


def case_labels(store, batch_id):
    labels = {}
    for number,case in enumerate(store.cases(batch_id),1):
        pages = store.one('SELECT SUM(page_count) n FROM documents WHERE case_id=?',(case['id'],))['n'] or 0
        job = store.one('SELECT path FROM jobs WHERE batch_id=? AND case_id=? ORDER BY path LIMIT 1',(batch_id,case['id']))
        name = Path(job['path']).name if job else ''
        if re.fullmatch(r'[a-f0-9]{64}\.pdf',name):
            name = ''
        name = re.sub(r'^[a-f0-9]{16}_','',name)
        labels[case['id']] = f'病例 {number}'+(' · '+name if name else '')+f' · {pages} 页'
    return labels


def jobs_table(store, batch_id):
    labels = case_labels(store,batch_id)
    return [{'病例':labels.get(j['case_id'],Path(j['path']).name),
             '状态':STAGES.get(j['stage'],'等待处理'),'进度':j.get('progress') or '—',
             '提示':('可点击继续处理重试；详细记录见设置中的诊断信息' if j['error_code'] else '')}
            for j in store.all('SELECT * FROM jobs WHERE batch_id=? ORDER BY path',(batch_id,))]


def format_value(value, kind):
    if value is None:
        return '待补充 / 待复核'
    if kind in {'rate','catalog_rate'}:
        return f'{value*100:.2f}%'
    if kind=='median':
        return f'{value:g} 分钟'
    return f'{value:.4g}'


def summary_rows(report, draft=True):
    prefix = 'draft_' if draft else ''
    def contribution(s,key):
        count = s.get(prefix+key)
        if count is None:
            return '未知'
        partial = s.get(prefix+'unknown_denominator_groups',0) or (key=='numerator' and s.get(prefix+'unknown_numerator_objects',0))
        return f'{count}（尚不完整）' if partial else str(count)
    return [{'编号':i,'指标':s['name'],'分子':contribution(s,'numerator'),
             '分母':contribution(s,'denominator'),
             '结果':'不适用' if s.get(prefix+'status')=='不适用' else format_value(s.get(prefix+'value'),s['kind']),
             '状态':s.get(prefix+'status',s['status']),
             '待解决事项':'；'.join(s.get(prefix+'missing',[]))}
            for i,s in enumerate(report['summary'],1)]


def chinese_csv(rows):
    stream = io.StringIO(newline='')
    if rows:
        writer = csv.DictWriter(stream,fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows({key:safe_cell(value) for key,value in row.items()} for row in rows)
    return stream.getvalue().encode('utf-8-sig')
