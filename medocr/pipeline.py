import hashlib
import importlib.util
import json
import os
import re
import shutil
from pathlib import Path

from .store import digest, stamp
from .runtime_paths import native_model_dir

TAGS = [
    ('transfer', r'转科记录|转出记录|转入记录'),
    ('admission', r'入院记录|入院情况|首次病程'),
    ('cover', r'病案首页|住院病案首'),
    ('discharge', r'出院记录|出院小结'),
    ('death', r'死亡记录|死亡小结'),
    ('orders', r'长期医嘱|临时医嘱|医嘱单'),
    ('consultation', r'会诊记录|会诊申请'),
    ('discussion', r'讨论记录|术前讨论|疑难病例讨论|死亡病例讨论'),
    ('surgery', r'手术记录|手术安全核查'),
    ('anesthesia', r'麻醉记录|麻醉小结'),
    ('nursing', r'护理记录|护理交接|交接记录|体温记录'),
    ('progress', r'病程记录|查房记录|术后首次病程'),
    ('consent', r'同意书|知情告知|授权委托'),
    ('lab', r'检验报告|化验粘贴|检验结果'),
    ('imaging', r'影像学检查|超声检查|心电图|心电监护')
]
# A full core-indicator run requires all these categories, including lab and imaging.
RELEVANT = {'unknown','transfer','admission','cover','discharge','death','orders','progress','nursing',
            'consultation','discussion','surgery','anesthesia','consent','lab','imaging'}


def classify(text):
    text = re.sub(r'\s+', '', text)
    for tag, pattern in TAGS:
        if re.search(pattern, text):
            return tag
    return 'unknown'


def dependency(name, hint):
    if importlib.util.find_spec(name) is None:
        raise RuntimeError(f'缺少 {name}。{hint}')


def import_pdf(store, batch_id, path, episode_key=None):
    dependency('pypdf','请按 ENVIRONMENT.md 配置基础环境')
    from pypdf import PdfReader
    path = Path(path).resolve()
    if path.suffix.lower() != '.pdf' or not path.is_file():
        raise ValueError('请输入本地 PDF 文件')
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024), b''):
            h.update(block)
    file_hash = h.hexdigest()
    snapshots = store.root/'source_pdfs'; snapshots.mkdir(parents=True,exist_ok=True)
    snapshot = snapshots/(file_hash+'.pdf')
    if not snapshot.exists():
        shutil.copyfile(path,snapshot)
    path = snapshot.resolve()
    existing = store.one('SELECT * FROM documents WHERE file_hash=?',(file_hash,))
    if existing:
        cid,did = existing['case_id'],existing['id']
        store.conn.execute('UPDATE documents SET path=? WHERE id=?',(str(path),did))
    else:
        reader = PdfReader(path)
        if reader.is_encrypted:
            raise ValueError('PDF 已加密，请提供医院授权的可读版本')
        count = len(reader.pages)
        key = digest(episode_key) if episode_key else None
        previous = store.one('SELECT id FROM cases WHERE episode_key=?',(key,)) if key else None
        cid = previous['id'] if previous else file_hash[:20]
        did = file_hash[:24]
        with store.conn:
            store.conn.execute('INSERT OR IGNORE INTO cases(id,episode_key) VALUES(?,?)',(cid,key))
            store.conn.execute('INSERT INTO documents VALUES(?,?,?,?,?)',(did,cid,str(path),file_hash,count))
            store.conn.executemany('INSERT INTO pages(document_id,page) VALUES(?,?)',[(did,i) for i in range(1,count+1)])
    with store.conn:
        store.conn.execute('INSERT OR IGNORE INTO batch_cases VALUES(?,?)',(batch_id,cid))
    return {'case_id':cid,'document_id':did,'duplicate':bool(existing)}


def render_page(path, page, dpi=250, header=False):
    dependency('fitz','请安装 PyMuPDF')
    import fitz
    with fitz.open(path) as doc:
        pg = doc[page-1]
        clip = pg.rect
        if header:
            clip = fitz.Rect(clip.x0,clip.y0,clip.x1,clip.y0+clip.height*0.40)
        pix = pg.get_pixmap(matrix=fitz.Matrix(dpi/72,dpi/72),clip=clip,alpha=False)
        return pix.tobytes('png')


def grouped_lines(texts, polys, scores):
    cells = []
    for i,text in enumerate(texts):
        poly = polys[i] if i < len(polys) else []
        if poly:
            xs,ys = [float(p[0]) for p in poly],[float(p[1]) for p in poly]
            x,y,height = min(xs),sum(ys)/len(ys),max(ys)-min(ys)
        else:
            x,y,height = 0,i*30,20
        cells.append({'text':str(text),'x':x,'y':y,'height':height,'score':float(scores[i]) if i<len(scores) else None,'polygon':poly})
    rows = []
    for cell in sorted(cells,key=lambda x:(x['y'],x['x'])):
        if rows and abs(cell['y']-rows[-1]['y']) <= max(3,min(cell['height'],rows[-1]['height'])*0.45):
            rows[-1]['cells'].append(cell)
        else:
            rows.append({'y':cell['y'],'height':cell['height'],'cells':[cell]})
    lines = [' '.join(c['text'] for c in sorted(row['cells'],key=lambda x:x['x'])) for row in rows]
    return lines,cells


def normalize_result(result):
    payload = result.json
    if isinstance(payload,str):
        payload = json.loads(payload)
    payload = payload.get('res',payload)
    if 'rec_texts' in payload:
        lines,cells = grouped_lines(payload['rec_texts'],payload.get('rec_polys',[]),payload.get('rec_scores',[]))
        return '\n'.join(lines),{'cells':cells,'scores':payload.get('rec_scores',[])}
    blocks = payload.get('parsing_res_list',[])
    text = '\n'.join(str(block.get('block_content','')) for block in blocks)
    if not text and hasattr(result,'markdown'):
        markdown = result.markdown
        text = markdown.get('markdown_texts','') if isinstance(markdown,dict) else str(markdown)
    return text,{'blocks':blocks}


class OCREngine:
    def __init__(self, config, engine='ocr'):
        dependency('paddleocr','请按 ENVIRONMENT.md 配置 OCR 环境与模型')
        self.config = config
        self.engine = engine
        ocr = config['ocr']
        # Explicit directories ensure processing never implicitly downloads weights.
        for key in ('detection_dir','recognition_dir','orientation_dir'):
            folder = Path(ocr[key])
            if not folder.is_dir() or not (folder/'inference.yml').is_file():
                raise RuntimeError(f'本地模型未就绪：{key}；运行 tools/download_models.py 下载或手动解压模型')
        model_dirs = {key:native_model_dir(ocr[key]) for key in ('detection_dir','recognition_dir','orientation_dir')}
        os.environ.setdefault('HF_HUB_OFFLINE','1')
        os.environ.setdefault('TRANSFORMERS_OFFLINE','1')
        os.environ['PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK'] = 'True'
        if engine == 'ocr':
            from paddleocr import PaddleOCR
            self.pipeline = PaddleOCR(device=ocr['device'],ocr_version=ocr['version'],
                text_detection_model_name=ocr['detection_name'],text_recognition_model_name=ocr['recognition_name'],
                text_detection_model_dir=model_dirs['detection_dir'],text_recognition_model_dir=model_dirs['recognition_dir'],
                use_doc_orientation_classify=True,doc_orientation_classify_model_name='PP-LCNet_x1_0_doc_ori',
                doc_orientation_classify_model_dir=model_dirs['orientation_dir'],use_doc_unwarping=False,use_textline_orientation=False)
        else:
            config_path = config.get(engine+'_config')
            if not config_path:
                raise ValueError(f'请先设置 {engine}_config 为全部权重已配置本地路径的 PaddleX YAML；详见 ENVIRONMENT.md')
            import yaml
            config_path = (Path(config['root'])/config_path).resolve()
            data = yaml.safe_load(config_path.read_text(encoding='utf-8'))
            def check(node):
                if isinstance(node,dict):
                    if node.get('model_name'):
                        folder = node.get('model_dir')
                        if not folder or not Path(folder).is_absolute() or not Path(folder).is_dir():
                            raise ValueError('可选解析流程的每个 model_name 必须配置存在的本地绝对 model_dir；禁止自动下载')
                    for value in node.values():
                        check(value)
                elif isinstance(node,list):
                    for value in node:
                        check(value)
                elif isinstance(node,str) and node.startswith(('http://','https://')):
                    from .extract import local_endpoint
                    local_endpoint(node)
            check(data)
            if engine == 'structure':
                from paddleocr import PPStructureV3
                cls = PPStructureV3
            elif engine == 'vl':
                from paddleocr import PaddleOCRVL
                cls = PaddleOCRVL
            else:
                raise ValueError('未知 OCR 引擎')
            self.pipeline = cls(device=ocr['device'],paddlex_config=str(config_path))

    def predict(self, image_path):
        texts,payloads = [],[]
        # Decode with Python so native OpenCV never opens a Unicode path.
        import numpy as np
        from PIL import Image
        with Image.open(image_path) as image:
            image_data = np.asarray(image.convert('RGB'))[:, :, ::-1].copy()
        for result in self.pipeline.predict(image_data):
            text,payload = normalize_result(result)
            texts.append(text)
            payloads.append(payload)
        return '\n'.join(texts),{'results':payloads}


def process_pages(store, case_id, config, mode='full', scope='all', engine='ocr', selected=None, force=False, progress=None, filter_document=None):
    dependency('pypdf','请配置基础环境')
    from pypdf import PdfReader
    runner = None
    pages = store.pages(case_id)
    if not pages:
        raise ValueError('病例不存在或未导入页')
    available = {p['page'] for p in pages if not filter_document or p['document_id']==filter_document}
    if selected and selected - available:
        raise ValueError('指定页码不在所选PDF范围内')
    readers = {}
    extra_config = config.get(engine+'_config')
    extra_hash = None
    if extra_config:
        extra_path = (Path(config['root'])/extra_config).resolve()
        if extra_path.exists():
            extra_hash = hashlib.sha256(extra_path.read_bytes()).hexdigest()
    fingerprint = digest(json.dumps({'ocr':config['ocr'],'mode':mode,'engine':engine,'config':extra_config,'extra_hash':extra_hash},sort_keys=True))
    count = {'processed':0,'cached':0,'skipped':0,'failed':0}
    for i,page in enumerate(pages,1):
        if filter_document and page['document_id'] != filter_document:
            count['skipped'] += 1
            continue
        if selected and page['page'] not in selected:
            count['skipped'] += 1
            continue
        from .specs import relevant_categories
        relevant = relevant_categories(config.get('selected_metrics')) if config.get('calculation_engine')=='core35' else RELEVANT
        if mode=='full' and scope=='relevant' and page['category'] not in relevant and page['category']!='death':
            count['skipped'] += 1
            continue
        if not force and ((mode=='header' and page['stage']=='full') or (page['stage']==mode and page['signature']==fingerprint)):
            count['cached'] += 1
            continue
        try:
            reader = readers.get(page['document_id'])
            if reader is None:
                reader = PdfReader(page['path'])
                readers[page['document_id']] = reader
            native = reader.pages[page['page']-1].extract_text() or ''
            usable = len(re.sub(r'\s+','',native)) >= 30 and native.count('\ufffd')/max(1,len(native)) < 0.03
            payload = {}
            image_path = page['image_path']
            current_mode = mode
            if usable:
                text = native
                current_mode,used_engine = 'full','pdf_text'
            else:
                if runner is None:
                    runner = OCREngine(config,engine)
                folder = Path(config['work_dir'])/page['document_id']
                folder.mkdir(parents=True,exist_ok=True)
                target = folder/f"{page['page']:04}_{mode}_{engine}.png"
                target.write_bytes(render_page(page['path'],page['page'],config['ocr']['index_dpi'] if mode=='header' else config['ocr']['dpi'],mode=='header'))
                image_path = str(target)
                text,payload = runner.predict(target)
                used_engine = engine
                if not text.strip():
                    raise ValueError('no_text')
            if page['stage']=='full' and page['text']!=text:
                # Preserve human edits, but explicitly invalidate facts whose OCR evidence changed.
                for event in store.events(case_id):
                    refs = event['attributes'].get('_evidence',[{'document_id':event['document_id'],'page':event['page']}])
                    if any(r['document_id']==page['document_id'] and r['page']==page['page'] for r in refs):
                        attrs = {**event['attributes'],'_stale':True}
                        store.conn.execute('UPDATE events SET attributes=? WHERE id=?',(json.dumps(attrs,ensure_ascii=False),event['id']))
                        store.audit('ocr_source_changed',event['id'],None,{'stale':True},'system')
                store.conn.execute('DELETE FROM extraction_runs WHERE case_id=?',(case_id,))
            store.conn.execute('UPDATE pages SET text=?,category=?,stage=?,engine=?,signature=?,image_path=?,error=?,payload=?,updated=? WHERE document_id=? AND page=?',
                (text,classify(text[:4000]),current_mode,used_engine,fingerprint,image_path,'',json.dumps(payload,ensure_ascii=False),stamp(),page['document_id'],page['page']))
            store.conn.commit()
            count['processed'] += 1
        except RuntimeError:
            # Missing environment/model setup is a task failure, not a zero result.
            raise
        except Exception as exc:
            code = type(exc).__name__
            store.conn.execute("UPDATE pages SET stage='error',error=?,updated=? WHERE document_id=? AND page=?",(code,stamp(),page['document_id'],page['page']))
            store.conn.commit()
            count['failed'] += 1
        if progress:
            progress(i,len(pages),count)
    return count


def process_single_page(store,case_id,page,config,engine='ocr'):
    return process_pages(store,case_id,config,selected={page['page']},force=True,engine=engine,filter_document=page['document_id'])
