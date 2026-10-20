"""Export the 35 implementation contracts without reading patient records."""
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from medocr.specs import core_specs


def main():
    specs=core_specs()
    output=ROOT/'outputs'; output.mkdir(exist_ok=True)
    (output/'core_specs.json').write_text(json.dumps(specs,ensure_ascii=False,indent=2),encoding='utf-8')
    labels={'admission':'入院','discharge':'出院','transfer':'转科','order':'医嘱','ward_round':'查房','progress_note':'病程',
      'consultation':'会诊','surgery':'手术','anesthesia':'麻醉','discussion':'讨论','consent':'同意书','rescue':'抢救',
      'death':'死亡','cost':'费用','nursing':'护理','critical_value':'危急值','transfusion':'输血',
      'transfusion_assessment':'输血后评估','complication':'并发症','readmission':'再次住院','dispute':'纠纷','upload':'上传结果'}
    lines=['# 35项指标实现说明','',
      '下表列的是程序已实现的事实输入和计数契约。完整字段及院方原始分子、分母提示词见 outputs/core_specs.json；界面逐项展示原始指导。所有项目均有规则代码，提取结果必须经过证据核查。','',
      '| 编号 | 指标 | 需要的事实 | 计数单位 | 补充资料 | 口径确认 |',
      '|---|---|---|---|---|---|']
    for s in specs:
        row=[s['id'],s['name'],'、'.join(labels[k] for k in s['fact_types']),s['counting_unit'],
             '；'.join(s['external_requirements']) or 'PDF缺失时需医院补充','需要' if s['rule_conflict'] else '常规核查']
        lines.append('| '+' | '.join(v.replace('|','/').replace('\n',' ') for v in row)+' |')
    lines += ['','## 医院提示词冲突与当前实现','',
       '下面是程序采用的待确认口径。医院确认前，相应已复核指标值保留为空；自动初判可以用于检查规则。直接导入的医院已审核汇总值有独立来源标记。','']
    lines += [f"- {s['id']}：{s['rule_conflict']}" for s in specs if s['rule_conflict']]
    lines += ['','## 字段不是统计量','',
       '指标一提取本次入院时间、实际转科是否发生、转出/转入科室、ICU属性及转科记录时间。按医院提示默认以转科文书记录时间判断48小时窗口；需要变更时修改 transfer_time_basis 并重新确认口径。每次住院先判定分母资格和分子条件，最后汇总。','',
       '会诊、手术、长期医嘱、危急值、输血按事件枚举。同一段文字重复出现不应重复计数；相同标识却不同时间会提示不确定。医师时间重合跨病例比较，并需要完整手术/麻醉时间区间。','',
       '项目留存由完整四年台账计算；术种比例需要完整备案目录；死亡讨论工作日判断需要医院日历；规范完整性应按医院标准由医生核查。主任医师职称不能自动证明科主任职务。','',
       '当前费用阈值为200000元，统计期间按每项契约中的 period_anchor 判定；无完整日期/时分时保留未知，不补成午夜。不同临床事件的关联不能只依据时间接近。','',
       '初判不替代已复核结果。全册OCR完成也不证明医疗事件已全部抽取；医生需要核查对象完整性。仅提供一部分病案时，结果范围为提交病例集合。','',
       '## 验证边界','',
       '测试采用合成事实及合成扫描PDF。真实语义模型尚未部署，尚无医院人工标注病例上的准确率、召回率和35项汇总误差报告。先进行小批次标注验收，再用于大批次统计。','']
    (ROOT/'35项指标实现说明.md').write_text('\n'.join(lines),encoding='utf-8')
    print('Exported 35 core indicator contracts and implementation matrix.')


if __name__=='__main__':
    main()
