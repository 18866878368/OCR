import json
from pathlib import Path


def load_config(path="config.json"):
    path = Path(path).resolve()
    config = json.loads(path.read_text(encoding="utf-8-sig"))
    config.setdefault('indicator_set','core')
    config.setdefault('calculation_engine','core35')
    config.setdefault('extraction_mode','rules')
    semantic_options = config.setdefault('semantic_assist', {})
    semantic_options.setdefault('enabled', False)
    from .prompt_metrics import PROMPT_METRIC_IDS
    prompt_options = config.setdefault('prompt_metrics', {})
    prompt_options.setdefault('enabled', True)
    prompt_options.setdefault('provider', 'deepseek')
    prompt_options.setdefault('base_url', 'https://api.deepseek.com')
    prompt_options.setdefault('model', 'deepseek-flash')
    prompt_options.setdefault('key_env', 'DEEPSEEK_API_KEY')
    prompt_options.setdefault('timeout_seconds', 600)
    prompt_options.setdefault('max_output_tokens', 16384)
    prompt_options.setdefault('max_retries', 0)
    prompt_options.setdefault('thinking', 'disabled')
    prompt_options.setdefault('metric_ids', sorted(PROMPT_METRIC_IDS))
    prompt_options.setdefault('max_input_chars', 240000)
    prompt_options.setdefault('version', 'ocr-page-prompt-v1')
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
