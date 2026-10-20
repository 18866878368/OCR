"""Hospital-supplied JSON/CSV facts with an immutable local source snapshot."""
import csv
import hashlib
import json
import math
from pathlib import Path

from .rules import canonical_time
from .specs import FIELDS
from .store import digest
from .dates import canonical_date


def validate_attributes(kind, attrs):
    if kind not in FIELDS or not isinstance(attrs,dict):
        raise ValueError('事实类型或属性格式错误')
    for key,value in attrs.items():
        if key.startswith('_'):
            raise ValueError('导入资料不能设置内部审核或来源标记')
        if key not in FIELDS[kind]:
            raise ValueError('事实属性不属于对应类型：'+key)
        if value is None:
            continue
        typ = FIELDS[kind][key]
        if typ=='bool' and type(value) is not bool:
            raise ValueError('布尔属性须使用true/false/null')
        if typ=='text' and not isinstance(value,str):
            raise ValueError('文字属性须为字符串')
        if typ=='texts' and (not isinstance(value,list) or not all(isinstance(x,str) for x in value)):
            raise ValueError('人员/科室列表须为字符串数组')
        if typ=='int' and type(value) is not int:
            raise ValueError('整数属性格式错误')
        if typ=='number' and (type(value) not in (int,float) or not math.isfinite(value) or value<0):
            raise ValueError('数值属性必须为非负数字')
        if typ=='time':
            t = canonical_time(value)
            if not t:
                raise ValueError('时间属性必须包含完整日期和时分')
            attrs[key] = t
        if typ=='date':
            d = canonical_date(value)
            if not d:
                raise ValueError('日期属性必须包含完整年月日')
            attrs[key] = d
        if key=='grade' and value not in (1,2,3,4):
            raise ValueError('手术级别须为1至4')
        from .facts import ENUMS
        if key in ENUMS and value not in ENUMS[key]:
            raise ValueError('属性枚举值无效：'+key)
    return attrs


def import_hospital_facts(store, batch_id, filename, reviewer, verified=False):
    if not reviewer.strip():
        raise ValueError('导入医院资料须填写导入人')
    path = Path(filename).resolve()
    content = path.read_bytes()
    if path.suffix.lower()=='.json':
        rows = json.loads(content.decode('utf-8-sig'))
    elif path.suffix.lower()=='.csv':
        rows = list(csv.DictReader(content.decode('utf-8-sig').splitlines()))
        for row in rows:
            row['attributes'] = json.loads(row.get('attributes') or '{}')
    else:
        raise ValueError('医院事实资料支持JSON数组或UTF-8 CSV')
    if not isinstance(rows,list):
        raise ValueError('医院事实资料必须是数组')
    parsed = []
    for i,row in enumerate(rows,1):
        if not isinstance(row,dict) or not store.one('SELECT * FROM batch_cases WHERE batch_id=? AND case_id=?',(batch_id,row.get('case_id'))):
            raise ValueError('医院资料第'+str(i)+'条的case_id不属于此批次')
        attrs = validate_attributes(row.get('kind'),dict(row.get('attributes',{})))
        normalized = {**row,'attributes':attrs}
        for key in ('event_time','record_time'):
            value = row.get(key)
            normalized[key] = canonical_time(value) if value else None
            if value and not normalized[key]:
                raise ValueError('医院资料时间必须包含完整日期和时分')
        parsed.append(normalized)
    sha = hashlib.sha256(content).hexdigest()
    folder = store.root/'hospital_sources'; folder.mkdir(exist_ok=True)
    snapshot = folder/(sha+path.suffix.lower())
    if not snapshot.exists():
        snapshot.write_bytes(content)
    for i,row in enumerate(parsed,1):
        cid = row['case_id']; did = digest(sha+cid)
        with store.conn:
            store.conn.execute('INSERT OR IGNORE INTO documents VALUES(?,?,?,?,?)',(did,cid,str(snapshot),sha+':'+cid,0))
        quote = json.dumps(rows[i-1],ensure_ascii=False,sort_keys=True)
        attrs = {**row['attributes'],'_external_source':str(snapshot),'_source_row':i,
                 '_evidence':[{'document_id':did,'page':0,'quote':quote,'source_row':i,'source_path':str(snapshot)}]}
        eid = store.save_event({'case_id':cid,'kind':row['kind'],'event_time':row['event_time'],'record_time':row['record_time'],
                               'attributes':attrs,'document_id':did,'page':0,'quote':quote,'origin':'hospital_import','verified':False})
        if verified:
            store.review_event(eid,{'verified':True},reviewer)
    with store.conn:
        store.audit('hospital_facts_import',sha,None,{'records':len(parsed),'verified':verified},reviewer)
    return len(parsed)
