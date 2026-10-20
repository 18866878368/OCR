"""Local hospital dictionaries and ledgers, supplied by the hospital."""
from datetime import date
import json
from pathlib import Path


def load_reference(path):
    data = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    if not isinstance(data,dict):
        raise ValueError('医院参考资料必须是JSON对象')
    for key in ('surgery_grades','procedure_aliases','physician_ids','department_heads'):
        value = data.get(key,{})
        if not isinstance(value,dict) or not all(isinstance(k,str) and k.strip() for k in value):
            raise ValueError('医院字典格式错误：'+key)
        if key=='surgery_grades':
            if not all(type(v) is int and v in (1,2,3,4) for v in value.values()):
                raise ValueError('手术级别必须是1至4的整数')
        elif not all(isinstance(v,str) and v.strip() for v in value.values()):
            raise ValueError('身份或术式映射必须为非空文字')
    sections = data.get('complete_sections',[])
    if not isinstance(sections,list) or not all(isinstance(x,str) for x in sections):
        raise ValueError('complete_sections必须是数组')
    for key in ('registered_types','special_antibiotics','workdays'):
        if key in data:
            if not isinstance(data[key],list) or not all(isinstance(v,str) and v.strip() for v in data[key]):
                raise ValueError('医院名单格式错误：'+key)
            if key not in sections:
                data.pop(key)
    if 'workdays' in data:
        for d in data['workdays']:
            date.fromisoformat(d)
        for key in ('calendar_start','calendar_end'):
            if not data.get(key):
                raise ValueError('完整工作日历必须有calendar_start和calendar_end')
            date.fromisoformat(data[key])
        if data['calendar_start']>data['calendar_end'] or any(not data['calendar_start']<=d<=data['calendar_end'] for d in data['workdays']):
            raise ValueError('工作日历范围错误')
    if data.get('project_inventory_complete'):
        if type(data.get('project_cycle_start')) is not int or not isinstance(data.get('projects'),list):
            raise ValueError('完整项目台账必须有周期起始年份和projects数组')
        for p in data['projects']:
            if not isinstance(p,dict) or not p.get('project_id') or type(p.get('introduced_year')) is not int or not isinstance(p.get('carried_years'),list) or not all(type(y) is int for y in p['carried_years']):
                raise ValueError('项目台账条目缺少标识、新增年份或开展年份')
    data['_source'] = str(Path(path).resolve())
    return data
