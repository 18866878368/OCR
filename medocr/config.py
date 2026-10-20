import json
from pathlib import Path


def load_config(path="config.json"):
    path = Path(path).resolve()
    config = json.loads(path.read_text(encoding="utf-8-sig"))
    config.setdefault('indicator_set','core')
    config.setdefault('calculation_engine','core35')
    config.setdefault('extraction_mode','rules')
    if config['extraction_mode'] not in {'rules','assisted'}:
        raise ValueError('extraction_mode 必须为 rules 或 assisted')
    config["root"] = str(path.parent)
    for key in ("data_dir", "work_dir", "report_dir"):
        config[key] = str((path.parent / config[key]).resolve())
    if config.get("transfer_time_basis") not in {"event_time", "record_time"}:
        raise ValueError("transfer_time_basis 必须为 event_time 或 record_time")
    for key in ("detection_dir", "recognition_dir", "orientation_dir"):
        config["ocr"][key] = str((path.parent / config["ocr"][key]).resolve())
    if config.get('hospital_reference_file'):
        from .references import load_reference
        config['hospital_reference'] = load_reference(path.parent/config['hospital_reference_file'])
    else:
        config.setdefault('hospital_reference',{})
    return config
