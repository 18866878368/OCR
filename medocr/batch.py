"""Persistent local batch queue with resumable OCR/extraction checkpoints."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from .errors import ProcessingStopped
from .extract import extract_case
from .facts import request_json, release_local_model, VERSION
from .rules import RULE_VERSION
from .pipeline import import_pdf, process_pages
from .report import export_report
from .store import digest, stamp


def process_alive(pid):
    if not pid:
        return False
    if os.name=='nt':
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE,wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x100000,False,int(pid))
        if not handle:
            return False
        try:
            return kernel.WaitForSingleObject(handle,0)==0x102
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(int(pid),0)
        return True
    except OSError:
        return False


def preflight(config, use_llm):
    if use_llm:
        model = config['llm'].get('model','').strip()
        if not model or '请填写' in model:
            raise ValueError('请填写已部署的本机模型名称')
        request_json(config,'这是无病例的连通性测试，只输出JSON对象。',{'test':'return {"ready":true}'})


def enqueue(store, batch_id, paths):
    if not store.one('SELECT id FROM batches WHERE id=?',(batch_id,)):
        raise ValueError('批次不存在')
    resolved = sorted({Path(p).resolve() for p in paths})
    if not resolved:
        raise ValueError('没有找到病例PDF')
    for path in resolved:
        if not path.is_file() or path.suffix.lower()!='.pdf':
            raise ValueError('队列只接受已存在的PDF文件')
    added = 0
    with store.conn:
        for path in resolved:
            info = path.stat()
            sid = digest(json.dumps([str(path),info.st_size,info.st_mtime_ns]))
            result = store.conn.execute('INSERT OR IGNORE INTO jobs(batch_id,source_id,path,stage,error_code,updated) VALUES(?,?,?,?,?,?)',
                                       (batch_id,sid,str(path),'pending','',stamp()))
            added += result.rowcount
    return added


def queue_directory(store, batch_id, directory, project_root):
    root = Path(directory).resolve()
    if not root.is_dir():
        raise ValueError('病例目录不存在')
    if root==Path(project_root).resolve():
        raise ValueError('请使用只包含病例的专用目录；项目根目录还含指标资料PDF')
    return enqueue(store,batch_id,root.glob('*.pdf'))


def run_batch(store, batch_id, config, use_llm=False, scope='all', force=False):
    row = store.one('SELECT * FROM batch_workers WHERE batch_id=?',(batch_id,))
    store.conn.execute('BEGIN IMMEDIATE')
    try:
        row = store.one('SELECT * FROM batch_workers WHERE batch_id=?',(batch_id,))
        if row and row['state']=='running' and process_alive(row['pid']):
            raise ValueError('该批次已有本地工作进程，请先等待或停止')
        store.conn.execute('INSERT OR REPLACE INTO batch_workers VALUES(?,?,?,?,?,?)',
                           (batch_id,os.getpid(),'running',0,'',stamp()))
        store.conn.commit()
    except Exception:
        store.conn.rollback(); raise
    stats = {'complete':0,'cached':0,'failed':0,'assistance_failed':0,'stopped':False}
    mode = 'assisted' if use_llm else 'rules'
    config = {**config,'extraction_mode':mode}
    with store.conn:
        store.conn.execute('INSERT OR REPLACE INTO batch_processing VALUES(?,?,?)',(batch_id,mode,stamp()))
    signature = digest(json.dumps([mode,config.get('icu_departments'),config.get('ocr'),config.get('llm') if use_llm else None,
                                   scope,config.get('selected_metrics'),config.get('hospital_reference',{}),VERSION,RULE_VERSION],sort_keys=True))
    def checkpoint():
        worker = store.one('SELECT * FROM batch_workers WHERE batch_id=?',(batch_id,))
        if worker and worker['stop_requested']:
            raise ProcessingStopped('已请求停止；当前任务保留检查点，可继续处理')
    try:
        assistance_available = use_llm
        assistance_error = ''
        try:
            preflight(config,use_llm)
        except Exception as exc:
            assistance_available = False
            assistance_error = type(exc).__name__
        if assistance_available:
            try:
                release_local_model(config)
            except Exception as exc:
                assistance_available = False
                assistance_error = type(exc).__name__
        jobs = store.all('SELECT * FROM jobs WHERE batch_id=? ORDER BY path',(batch_id,))
        if not jobs:
            raise ValueError('批次没有待处理病例，请先加入队列')
        for job in jobs:
            checkpoint()
            if not force and job['stage']=='complete' and job['config_signature']==signature:
                stats['cached'] += 1; continue
            def update(stage, cid=None, error='', progress=''):
                with store.conn:
                    store.conn.execute('UPDATE jobs SET stage=?,case_id=COALESCE(?,case_id),error_code=?,progress=?,config_signature=?,updated=? WHERE batch_id=? AND source_id=?',
                                       (stage,cid,error,progress,signature,stamp(),batch_id,job['source_id']))
            try:
                update('importing')
                imported = import_pdf(store,batch_id,job['path'])
                cid = imported['case_id']; update('ocr',cid)
                def ocr_progress(i,n,counts):
                    update('ocr',cid,progress=f'已识别 {i} / {n} 页'+(f'；{counts["failed"]} 页需重试' if counts['failed'] else ''))
                    checkpoint()
                if scope=='relevant':
                    process_pages(store,cid,config,mode='header',progress=ocr_progress)
                result = process_pages(store,cid,config,scope=scope,force=force,progress=ocr_progress)
                if result['failed']:
                    update('error',cid,'OCRPageFailure'); stats['failed'] += 1; continue
                # Release cached OCR allocations before the local text model is called.
                import gc
                gc.collect()
                if assistance_available and 'paddle' in sys.modules:
                    import paddle
                    if paddle.device.is_compiled_with_cuda():
                        paddle.device.cuda.empty_cache()
                checkpoint(); update('extracting',cid)
                def extraction_progress(page,code):
                    message = (f'第 {page} 页要素已保存' if code=='complete' else
                               code if code.startswith('正在') or code.startswith('已复用') else
                               f'第 {page} 页提取未完成，可继续处理重试')
                    update('extracting',cid,progress=message)
                extracted = extract_case(store,cid,config,assistance_available,extraction_progress,force=force,checkpoint=checkpoint)
                if assistance_available:
                    try:
                        release_local_model(config)
                    except Exception as exc:
                        assistance_error = type(exc).__name__
                if use_llm and (extracted['failed_pages'] or assistance_error):
                    update('partial',cid,assistance_error or 'SemanticAssistancePartial',
                           '规则提取已完成；部分语义辅助待重试，现有事实可复核')
                    stats['assistance_failed'] += 1
                elif extracted['failed_pages']:
                    update('error',cid,'ExtractionFailure'); stats['failed'] += 1
                else:
                    update('complete',cid,progress='规则及可选语义辅助完成，等待复核' if use_llm else '规则提取完成，证据不足处待人工核查；未调用语义模型')
                    stats['complete'] += 1
            except ProcessingStopped:
                update('pending',progress='已停止，可继续'); raise
            except Exception as exc:
                update('error',error=type(exc).__name__); stats['failed'] += 1
        export_report(store,batch_id,config)
        with store.conn:
            store.conn.execute("UPDATE batch_workers SET state='complete',updated=? WHERE batch_id=?",(stamp(),batch_id))
    except ProcessingStopped:
        stats['stopped'] = True
        with store.conn:
            store.conn.execute("UPDATE batch_workers SET state='stopped',updated=? WHERE batch_id=?",(stamp(),batch_id))
    except Exception as exc:
        with store.conn:
            store.conn.execute("UPDATE batch_workers SET state='error',error_code=?,updated=? WHERE batch_id=?",(type(exc).__name__,stamp(),batch_id))
        raise
    return stats


def launch_worker(store, batch_id, config_path, use_llm=False, scope='all', force=False):
    previous = store.one('SELECT * FROM batch_workers WHERE batch_id=?',(batch_id,))
    if previous and previous['state']=='running' and process_alive(previous['pid']):
        raise ValueError('该批次已在处理')
    folder = store.root/'worker_logs'; folder.mkdir(exist_ok=True)
    command = [sys.executable,'-m','medocr','--config',str(Path(config_path).resolve()),'process-batch','--batch',batch_id,'--scope',scope]
    if use_llm:
        command.append('--llm')
    if force:
        command.append('--force')
    with (folder/(batch_id+'.log')).open('a',encoding='utf-8') as output:
        process = subprocess.Popen(command,cwd=Path(__file__).resolve().parent.parent,stdout=output,stderr=subprocess.STDOUT,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    # Give the child a short opportunity to claim its SQLite lease.
    for _ in range(20):
        if process.poll() is not None:
            break
        worker = store.one('SELECT * FROM batch_workers WHERE batch_id=?',(batch_id,))
        if worker and worker['pid']==process.pid and worker['state']=='running':
            break
        time.sleep(0.05)
    return {'pid':process.pid,'log':str(folder/(batch_id+'.log'))}


def stop_worker(store,batch_id):
    with store.conn:
        store.conn.execute('UPDATE batch_workers SET stop_requested=1,updated=? WHERE batch_id=?',(stamp(),batch_id))
