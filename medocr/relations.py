"""Human-reviewed event relations, with source evidence and an audit trail."""
from .specs import RELATIONS

RELATION_LABELS = {
 'implements_consultation':'会诊意见对应的执行医嘱','discussion_for_surgery':'手术对应的术前讨论',
 'consent_for_surgery':'手术对应的知情同意','order_for_surgery':'手术对应的术前医嘱',
 'handover_for_surgery':'手术对应的护理交接','anesthesia_for_surgery':'手术对应的麻醉',
 'complication_of_surgery':'手术对应的并发症','assessment_for_transfusion':'输血对应的输血后评估',
 'treatment_for_critical_value':'危急值对应的处置医嘱','progress_for_critical_value':'危急值对应的普通病程',
 'round_for_critical_value':'危急值对应的查房记录','consultation_for_antibiotic':'抗菌药物对应的用药前会诊',
 'transfusion_for_surgery':'手术对应的输血',
}


def confirm_relation(store, case_id, relation, source_id, target_id, quote, reviewer, note):
    if relation not in RELATIONS or not reviewer.strip() or not note.strip() or not quote.strip():
        raise ValueError('请选择关联类型并填写证据、复核人和说明')
    facts = {e['id']:e for e in store.events(case_id)}
    source,target = facts.get(source_id),facts.get(target_id)
    if not source or not target or (source['kind'],target['kind'])!=RELATIONS[relation]:
        raise ValueError('两端必须为本病例中类型匹配的有效要素')
    if source['attributes'].get('_stale') or target['attributes'].get('_stale'):
        raise ValueError('请先复核来源已变化的要素')
    evidence = [e for fact in (source,target) for e in fact['attributes'].get('_evidence',[]) if quote in e.get('quote','')]
    if not evidence:
        evidence = [{'document_id':e['document_id'],'page':e['page'],'quote':quote} for e in (source,target) if quote in e['quote']]
    if not evidence:
        raise ValueError('关联证据须复制两端要素已有证据中的连续原文')
    # Include endpoint IDs so the same excerpt can support multiple different relations.
    from .store import digest
    eid = digest('|'.join([case_id,relation,source_id,target_id,quote]))
    attrs = {'relation':relation,'source_event_id':source_id,'target_event_id':target_id,
             '_evidence':evidence,'review_note':note,'_stale':False}
    store.save_event({'id':eid,'case_id':case_id,'kind':'link','attributes':attrs,
                     'document_id':evidence[0]['document_id'],'page':evidence[0]['page'],'quote':quote,'origin':'manual'})
    store.review_event(eid,{'attributes':attrs,'verified':True,'active':True},reviewer)
    return eid
