"""Keep automatic candidates within the current source/configuration plan.

History stays in events. Eligibility is checked at read time as well as extraction,
so a legacy database or a changed OCR page cannot silently reuse an old candidate.
Matching successful checkpoints may be reused without calling either model.
"""
import hashlib
import json


def signature(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def rule_signature(page, config):
    from .rule_extract import RULE_EXTRACT_VERSION
    from .retrieval import RETRIEVAL_VERSION
    return signature([RULE_EXTRACT_VERSION, RETRIEVAL_VERSION, page['text'], page['category'], config.get('icu_departments', [])])


def semantic_chunk(units, config):
    from .facts import VERSION
    from .retrieval import RETRIEVAL_VERSION
    key = '|'.join(f"{u['document_id']}:{u['page']}:{u['offset']}" for u in units)
    return key, signature([VERSION, RETRIEVAL_VERSION, units, config['llm'], config.get('hospital_reference', {})])


def link_signature(events, config):
    from .facts import VERSION
    clinical = [{k:e[k] for k in ('id','kind','event_time','record_time','document_id','page','quote')} | {
        'attributes':{k:v for k,v in e['attributes'].items() if not k.startswith('_') and k!='review_note'}} for e in events]
    return signature([VERSION, config.get('llm', {}).get('model'), clinical]), clinical


def current_scope(store, case_id, config, mode=None, events=None):
    from .facts import chunks_for
    from .rule_extract import rule_facts, semantic_kinds
    mode = mode or config.get('extraction_mode', 'rules')
    events = store.events(case_id, active=False) if events is None else events
    runs = {r['chunk_id']:r for r in store.all('SELECT * FROM extraction_runs WHERE case_id=?', (case_id,))}
    outputs = {(r['chunk_id'],r['signature']):set(json.loads(r['event_ids']))
        for r in store.all('SELECT * FROM extraction_outputs WHERE case_id=?', (case_id,))}
    rules, targets = {}, []
    by_page = {}
    for e in events:
        if e['active'] and not e['attributes'].get('_stale'):
            by_page.setdefault(e['attributes'].get('_rule_page'), []).append(e)
    for page in store.pages(case_id):
        if page['stage'] != 'full':
            continue
        key = f"rules:{page['document_id']}:{page['page']}"
        sig = rule_signature(page, config)
        rules[key] = sig
        run = runs.get(key, {})
        rows = ([e for e in by_page.get(key, []) if e['attributes'].get('_rule_signature') == sig and
                 ((key,sig) not in outputs or e['id'] in outputs[key,sig])]
                if run.get('signature') == sig and run.get('status') == 'complete' else rule_facts(page, config))
        kinds = semantic_kinds(page, rows)
        if kinds:
            targets.append({**page, '_semantic_kinds':kinds})
    helpers = {}
    if mode == 'assisted':
        for units in chunks_for(targets, int(config.get('llm', {}).get('max_chars_per_page', 12000))):
            key, sig = semantic_chunk(units, config)
            helpers[key] = sig
    manifest = {'mode':mode, 'rules':rules, 'semantic':helpers}
    return {**manifest, 'run_id':signature([case_id, manifest]), 'checkpoints':runs, 'outputs':outputs}


def scoped_events(store, case_id, config, mode=None, include_history=False):
    """Classify copies, never mutate clinical facts or human review records."""
    from .rule_extract import eligible_event, RULE_EXTRACT_VERSION
    from .specs import FIELDS
    events = store.events(case_id, active=False)
    scope = current_scope(store, case_id, config, mode, events)
    mode = scope['mode']
    runs = scope['checkpoints']

    def reason(e):
        attrs = e['attributes']
        if not e['active']:
            return '已停用'
        if attrs.get('_stale'):
            return '来源已变化，待重新复核'
        if e['verified']:
            return ''  # Explicit human decisions are not automatic cache entries.
        if not eligible_event(e, mode):
            return '当前模式不使用此自动候选'
        if e['kind'] == 'link':
            return ''  # Validate endpoints and link signature in the second pass.
        if e['origin'] not in {'rules', 'local_llm'}:
            return ''
        key_field, sig_field, plan = (('_rule_page', '_rule_signature', scope['rules'])
            if e['origin'] == 'rules' else ('_chunk', '_signature', scope['semantic']))
        key, sig = attrs.get(key_field), attrs.get(sig_field)
        if not key or not sig or plan.get(key) != sig:
            return '历史候选：不属于当前来源、分块或配置'
        checkpoint = runs.get(key, {})
        if checkpoint.get('signature') != sig or checkpoint.get('status') not in {'complete', 'complete_with_issues'}:
            return '当前提取块尚未成功完成'
        outputs = scope['outputs'].get((key,sig))
        if outputs is not None and e['id'] not in outputs:
            return '历史候选：未出现在本块最新成功结果中'
        return ''

    reasons = {e['id']:reason(e) for e in events}
    facts = [e for e in events if e['kind'] != 'link' and not reasons[e['id']]]
    ids = {e['id'] for e in facts}
    link_sig, _ = link_signature([e for e in facts if e['kind'] in FIELDS], config)
    for e in events:
        if e['kind'] != 'link' or reasons[e['id']]:
            continue
        attrs = e['attributes']
        if not {attrs.get('source_event_id'), attrs.get('target_event_id')} <= ids:
            reasons[e['id']] = '关联端点不在当前有效要素中'
        elif not e['verified'] and e['origin'] == 'local_llm':
            run = runs.get('links', {})
            if attrs.get('_signature') != link_sig or run.get('signature') != link_sig or run.get('status') != 'complete':
                reasons[e['id']] = '历史关联：输入已变化或关联任务未完成'
            elif ('links',link_sig) in scope['outputs'] and e['id'] not in scope['outputs']['links',link_sig]:
                reasons[e['id']] = '历史关联：未出现在最新成功结果中'
        elif not e['verified'] and e['origin'] == 'rules' and attrs.get('_rule_version') != RULE_EXTRACT_VERSION:
            reasons[e['id']] = '历史关联：规则版本已变化'
    result = []
    for e in events:
        excluded = reasons[e['id']]
        if include_history or not excluded:
            result.append({**e, 'scope_run_id':scope['run_id'], 'eligible_in_mode':not bool(excluded),
                'candidate_scope':'history' if excluded else 'reviewed' if e['verified'] else 'current',
                'scope_reason':excluded})
    return result
