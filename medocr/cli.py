import argparse
import importlib.metadata
import importlib.util
import json
from pathlib import Path
import sys

from .config import load_config
from .extract import extract_case
from .pipeline import import_pdf, process_pages
from .report import export_report
from .rules import batch_report, catalog, get_metric, validate_measure
from .store import Store


def doctor(config):
    modules = [('pypdf','pypdf'),('fitz','PyMuPDF'),('PIL','Pillow'),('streamlit','streamlit'),('paddle','paddlepaddle-gpu'),('paddleocr','paddleocr')]
    print('Python:',sys.version.split()[0])
    for module,distribution in modules:
        present = importlib.util.find_spec(module) is not None
        version = 'missing'
        if present:
            try:
                version = importlib.metadata.version(distribution)
            except importlib.metadata.PackageNotFoundError:
                version = 'present (distribution varies)'
        print(f'{module}: {version}')
    for key in ('detection_dir','recognition_dir','orientation_dir'):
        folder = Path(config['ocr'][key])
        print(f'{key}: {"ready" if (folder/"inference.yml").is_file() else "missing"}')
    print('OCR device:',config['ocr']['device'])
    print('Local LLM (optional):', 'configured' if config['llm']['enabled'] else 'disabled')
    print('Default extraction: rules; --llm enables selective assistance')
    print('配置说明：ENVIRONMENT.md；doctor 不下载模型、不导入病例。')


def main(argv=None):
    parser = argparse.ArgumentParser(description='本地病案 OCR、证据复核与指标计算')
    parser.add_argument('--config',default='config.json')
    subs = parser.add_subparsers(dest='command',required=True)
    subs.add_parser('doctor')
    subs.add_parser('catalog')
    p = subs.add_parser('specs')
    p.add_argument('--output',default='outputs/core_specs.json')
    p = subs.add_parser('queue')
    p.add_argument('--input',required=True)
    p.add_argument('--batch-name',required=True)
    p.add_argument('--start')
    p.add_argument('--end')
    for command in ('jobs','stop'):
        p = subs.add_parser(command); p.add_argument('--batch',required=True)
    p = subs.add_parser('process-batch')
    p.add_argument('--batch',required=True)
    p.add_argument('--scope',choices=['all','relevant'],default='all')
    p.add_argument('--llm',action='store_true')
    p.add_argument('--force',action='store_true')
    p = subs.add_parser('facts')
    p.add_argument('--case',required=True)
    p = subs.add_parser('import-facts')
    p.add_argument('--batch',required=True)
    p.add_argument('--file',required=True)
    p.add_argument('--reviewer',required=True)
    p.add_argument('--verified',action='store_true',help='医院已逐条复核，仅在明确确认后使用')
    p = subs.add_parser('import')
    p.add_argument('--pdf',required=True)
    p.add_argument('--batch-name',required=True)
    p.add_argument('--start')
    p.add_argument('--end')
    p.add_argument('--episode-key',help='明确同次住院才填写；仅存散列，勿使用单独患者姓名')
    p = subs.add_parser('list')
    p.add_argument('--batch')
    for command in ('index','ocr','extract'):
        p = subs.add_parser(command)
        p.add_argument('--case',required=True)
        if command in ('index','ocr'):
            p.add_argument('--pages',help='1,5,8 或 1-10；按每份PDF的原始页码')
            p.add_argument('--force',action='store_true')
            p.add_argument('--engine',choices=['ocr','structure','vl'],default='ocr')
            p.add_argument('--scope',choices=['all','relevant'],default='all')
        else:
            p.add_argument('--llm',action='store_true')
            p.add_argument('--force',action='store_true')
    p = subs.add_parser('report')
    p.add_argument('--batch',required=True)
    p = subs.add_parser('import-measures')
    p.add_argument('--batch',required=True)
    p.add_argument('--file',required=True)
    p.add_argument('--reviewer',required=True)
    p = subs.add_parser('merge')
    p.add_argument('--source',required=True)
    p.add_argument('--target',required=True)
    p.add_argument('--reviewer',required=True)
    p = subs.add_parser('run')
    p.add_argument('--input',required=True,help='病案专用目录或单个病例PDF；勿选含指标文件的项目根目录')
    p.add_argument('--batch-name',required=True)
    p.add_argument('--start')
    p.add_argument('--end')
    p.add_argument('--scope',choices=['all','relevant'],default='all')
    p.add_argument('--llm',action='store_true')
    p.add_argument('--force',action='store_true')
    args = parser.parse_args(argv)
    store = None
    try:
        config = load_config(args.config)
        if args.command=='doctor':
            doctor(config)
            return 0
        if args.command=='catalog':
            for metric in catalog():
                print(metric['id'],metric['name'],metric['unit'],metric['automation'])
            return 0
        if args.command=='specs':
            from .specs import core_specs
            path = Path(args.output); path.parent.mkdir(parents=True,exist_ok=True)
            path.write_text(json.dumps(core_specs(),ensure_ascii=False,indent=2),encoding='utf-8')
            print(path.resolve()); return 0
        store = Store(config['data_dir'])
        if args.command=='queue':
            from .batch import queue_directory, enqueue
            bid = store.batch(args.batch_name,args.start,args.end)
            source = Path(args.input)
            added = enqueue(store,bid,[source]) if source.is_file() else queue_directory(store,bid,source,config['root'])
            print(json.dumps({'batch_id':bid,'queued':added})); return 0
        if args.command=='jobs':
            print(json.dumps(store.all('SELECT source_id,case_id,stage,progress,error_code FROM jobs WHERE batch_id=?',(args.batch,)),ensure_ascii=False,indent=2)); return 0
        if args.command=='stop':
            from .batch import stop_worker
            stop_worker(store,args.batch); print('已请求在当前处理步骤后停止，可继续处理'); return 0
        if args.command=='process-batch':
            from .batch import run_batch
            stats = run_batch(store,args.batch,config,args.llm,args.scope,args.force)
            print(json.dumps(stats)); return 2 if stats['failed'] else 0
        if args.command=='facts':
            print(json.dumps(store.events(args.case),ensure_ascii=False,indent=2)); return 0
        if args.command=='import-facts':
            from .import_facts import import_hospital_facts
            print('Imported:',import_hospital_facts(store,args.batch,args.file,args.reviewer,args.verified)); return 0
        if args.command=='import':
            bid = store.batch(args.batch_name,args.start,args.end)
            print(json.dumps({'batch_id':bid,**import_pdf(store,bid,args.pdf,args.episode_key)},ensure_ascii=False))
        elif args.command=='list':
            rows = store.cases(args.batch) if args.batch else store.all('SELECT * FROM batches ORDER BY created DESC')
            print(json.dumps(rows,ensure_ascii=False,indent=2))
        elif args.command in ('index','ocr'):
            selected = parse_pages(args.pages) if args.pages else None
            result = process_pages(store,args.case,config,'header' if args.command=='index' else 'full',args.scope,args.engine,selected,args.force,
                                   lambda i,n,c: print(f'page {i}/{n} processed={c["processed"]} failed={c["failed"]}',flush=True))
            print(json.dumps(result))
            return 2 if result['failed'] else 0
        elif args.command=='extract':
            result = extract_case(store,args.case,config,args.llm,lambda page,code: print(f'page {page}: extraction {code}'),force=args.force)
            print(json.dumps(result))
            return 2 if result['failed_pages'] else 0
        elif args.command=='report':
            print(export_report(store,args.batch,config))
        elif args.command=='merge':
            store.merge_cases(args.source,args.target,args.reviewer)
            print('Merged; please regenerate reports.')
        elif args.command=='import-measures':
            rows = json.loads(Path(args.file).read_text(encoding='utf-8-sig'))
            if not isinstance(rows,list):
                raise ValueError('补充文件必须是 JSON 数组')
            for row in rows:
                validate_measure(get_metric(row['metric_id']),row['measure'],row['value'])
                if not row.get('source','').strip():
                    raise ValueError('每个值必须有 source')
            for row in rows:
                store.save_measure(args.batch,row['metric_id'],row['measure'],row['value'],row['source'],args.reviewer)
            print(f'Imported {len(rows)} measurements. Run report to validate combined formula.')
        elif args.command=='run':
            from .batch import enqueue, queue_directory, run_batch
            source = Path(args.input).resolve()
            bid = store.batch(args.batch_name,args.start,args.end)
            enqueue(store,bid,[source]) if source.is_file() else queue_directory(store,bid,source,config['root'])
            stats = run_batch(store,bid,config,args.llm,args.scope,args.force)
            print('batch_id:',bid)
            print(export_report(store,bid,config))
            print(json.dumps(stats)); print('候选事实等待人工复核；结果范围为提交病例集合。')
            return 2 if stats['failed'] else 0
        return 0
    except (ValueError,RuntimeError,KeyError) as exc:
        print('操作未完成：'+str(exc),file=sys.stderr)
        return 1
    except Exception as exc:
        # Do not print exception strings that may contain patient text or filenames.
        print(f'操作未完成：{type(exc).__name__}；查看环境配置和输入格式。',file=sys.stderr)
        return 1
    finally:
        if store:
            store.close()


def parse_pages(value):
    pages = set()
    for item in value.split(','):
        if '-' in item:
            a,b = map(int,item.split('-'))
            if a<1 or b<a:
                raise ValueError('无效页码范围')
            pages.update(range(a,b+1))
        else:
            page = int(item)
            if page < 1:
                raise ValueError('页码从1开始')
            pages.add(page)
    return pages


if __name__=='__main__':
    raise SystemExit(main())
