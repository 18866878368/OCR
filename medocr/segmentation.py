"""Document segmentation: identify document boundaries in medical records.

A document (文书) is a structured unit like "入院记录", "主任医师查房记录", "手术记录".
One page may contain multiple documents, and one document may span multiple pages.

This module identifies document boundaries based on headers and produces segments
that subsequent extraction layers can work within.
"""
import hashlib
import json
import re
from typing import List, Dict, Any, Optional


# Document type patterns (from rule_extract.py HEADERS)
# IMPROVED: Removed ^ anchors to match titles anywhere in the line
# Examples: "...查体。路平副主任医师查房嘱：患者..." or "2025-05-01 09:19 厉泉主任医师查房记录"
DOCUMENT_HEADERS = {
    'admission': r'入院记录',
    'discharge': r'出院记录',
    # “查房记录” is a document title. “查房嘱” is a subsection inside
    # that document and must never start a new segment.
    'ward_round': r'(?:.*医师)?查房记录',
    'progress_note': r'病程记录',  # Simplified: matches "日常病程记录", "病程记录", etc.
    'discussion': r'(?:术前|疑难病例|死亡病例|多学科)?讨论(?:记录|结论)',
    'consent': r'(?:手术|麻醉|输血)知情同意书',
    'nursing': r'(?:床旁交接班|护理交接班|护理)记录',
    'consultation': r'会诊记录',
    'surgery': r'手术记录',
    'anesthesia': r'麻醉记录',
    'long_order_sheet': r'长期医嘱单',
    'temporary_order_sheet': r'临时医嘱单',
    'order_sheet': r'(?:长期|临时)医嘱单',
}

# These headings describe the contents of a document. They are retained as
# metadata, but they are deliberately excluded from the document-boundary
# detector below. OCR does not preserve font size, so hierarchy is inferred
# from the title vocabulary and position rather than from visual formatting.
SECONDARY_HEADINGS = {
    'ward_round_instruction': r'查房嘱|查房意见|诊疗计划',
    'progress_subrecord': r'(?:首次|日常|阶段性|术后)病程记录',
    'progress_section': r'现病史|病情变化|目前诊断|诊疗经过|下一步计划',
    'consultation_opinion': r'会诊意见|会诊建议',
    'discussion_conclusion': r'讨论意见|讨论结论',
    'signature': r'签名|医师签字|患者签字',
}


# Per-document level-2 title rules. The PDF bookmark is the level-1 parent;
# these rules only identify children inside that parent's page range.
SECONDARY_HEADINGS_BY_OUTLINE = {
    '病程记录': {
        'progress_subrecord': r'(?:首次|日常|阶段性|术后)病程记录',
        'ward_round_record': r'(?:主任|副主任|主治|住院)医师查房记录',
        'discussion_record': r'(?:术前|疑难病例|死亡病例|多学科)讨论(?:记录|结论)',
    },
    '入院记录': {
        'admission_section': r'(?:主诉|现病史|既往史|个人史|家族史|体格检查|专科检查|辅助检查|初步诊断)',
    },
    '手术记录': {
        'surgery_section': r'(?:手术名称|手术日期|术前诊断|术后诊断|手术经过|术者|麻醉方式)',
    },
    '会诊记录单': {
        'consultation_opinion': r'(?:会诊意见|会诊建议|会诊医师)',
    },
    '长期医嘱单': {
        'order_section': r'(?:长期医嘱|医嘱内容|开立时间|停止时间)',
    },
    '临时医嘱单': {
        'order_section': r'(?:临时医嘱|医嘱内容|开立时间|执行时间|停止时间)',
    },
    '护理记录': {
        'nursing_section': r'(?:护理记录|生命体征|出入量|护理评估|护理措施)',
    },
}

_OUTLINE_TYPE_ALIASES = {
    '病案首页': 'cover',
    '入院证明': 'admission_certificate',
    '出院记录': 'discharge',
    '入院记录': 'admission',
    '病程记录': 'progress_note',
    '手术记录': 'surgery',
    '影像报告单': 'exam_report',
    '手术相关材料': 'surgical_materials',
    '麻醉同意书': 'consent',
    '麻醉记录单': 'anesthesia',
    '会诊记录单': 'consultation',
    '知情同意书': 'consent',
    '心电图报告': 'exam_report',
    '心电图相关材料': 'exam_report',
    '化验报告单': 'lab_report',
    '长期医嘱单': 'order_sheet',
    '临时医嘱单': 'order_sheet',
    '体 温 单': 'temperature_chart',
    '护理记录': 'nursing',
    '行政文件': 'administrative',
}

# Only these rule types are promoted to visible level-2 directory entries.
# Generic evidence headings such as signatures and narrative instructions stay
# available in segment metadata but do not become navigation items.
_DIRECTORY_SECONDARY_TYPES = {
    '病程记录': {'progress_subrecord', 'ward_round_record', 'discussion_record'},
    '入院记录': {'admission_section'},
    '手术记录': {'surgery_section'},
    '会诊记录单': {'consultation_opinion'},
    '护理记录': {'nursing_section'},
}


def _normalise_outline_text(value: str) -> str:
    return re.sub(r'[\s\u3000]+', '', str(value or '')).strip()


def read_pdf_outline(path) -> list:
    """Read embedded PDF bookmarks without OCR or page rendering."""
    if not path:
        return []
    try:
        import fitz
        with fitz.open(str(path)) as document:
            raw = document.get_toc(simple=True)
            page_count = document.page_count
    except Exception:
        return []
    result = []
    for order, item in enumerate(raw or []):
        if not isinstance(item, (list, tuple)) or len(item) < 3:
            continue
        try:
            level, title, start_page = int(item[0]), str(item[1]).strip(), int(item[2])
        except (TypeError, ValueError):
            continue
        if not title or start_page < 1:
            continue
        result.append({'level': level, 'title': title, 'start_page': start_page,
                       'page_count': page_count, '_order': order})
    return result


def _build_pdf_outline_index(pages):
    """Return top-level embedded bookmarks grouped by PDF document."""
    grouped = {}
    for page in pages:
        document_id = page.get('document_id') or page.get('path')
        if document_id in grouped:
            continue
        entries = read_pdf_outline(page.get('path'))
        top = [entry.copy() for entry in entries if entry.get('level') == 1]
        if not top:
            top = [entry.copy() for entry in entries]
        if not top:
            grouped[document_id] = []
            continue
        page_count = max(
            [entry.get('page_count', 0) for entry in top]
            + [int(page.get('page', 0) or 0) for page in pages
               if (page.get('document_id') or page.get('path')) == document_id]
        )
        for index, entry in enumerate(top):
            following = [candidate['start_page'] for candidate in top[index + 1:]
                         if candidate['start_page'] > entry['start_page']]
            entry['end_page'] = (min(following) - 1) if following else page_count
            entry['end_page'] = max(entry['start_page'], entry['end_page'])
            entry['outline_type'] = _OUTLINE_TYPE_ALIASES.get(
                _normalise_outline_text(entry['title']), 'unknown')
            entry['source'] = 'embedded_pdf_bookmark'
        grouped[document_id] = top
    return grouped


def _outline_for_page(entries, page_number):
    candidates = [entry for entry in entries
                  if entry['start_page'] <= page_number <= entry['end_page']]
    return candidates[-1] if candidates else None


def _outline_for_segment(segment, entries):
    if not entries:
        return None
    start_page = int(segment.get('start_page', 0) or 0)
    header_text = _normalise_outline_text(' '.join(
        header.get('line', '') for header in segment.get('headers_found', [])))
    same_page = [entry for entry in entries if entry['start_page'] == start_page]
    for entry in same_page:
        title = _normalise_outline_text(entry['title'])
        if title and (title in header_text or header_text in title):
            return entry
    return _outline_for_page(entries, start_page)


def _attach_pdf_outline_metadata(segments, pages):
    index = _build_pdf_outline_index(pages)
    for segment in segments:
        document_id = segment.get('document_id')
        entries = index.get(document_id, [])
        if not entries:
            continue
        primary = _outline_for_segment(segment, entries)
        overlaps = [entry for entry in entries
                    if entry['start_page'] <= segment.get('end_page', 0)
                    and entry['end_page'] >= segment.get('start_page', 0)]
        if primary:
            segment.update({
                'outline_level': 1,
                'outline_title': primary['title'],
                'outline_type': primary['outline_type'],
                'outline_start_page': primary['start_page'],
                'outline_end_page': primary['end_page'],
                'outline_source': primary['source'],
            })
            segment['secondary_headings'] = _find_secondary_headings(
                segment.get('text', ''), primary['title'])
        if len(overlaps) > 1:
            segment['outline_mixed'] = True
            segment['outline_titles'] = [entry['title'] for entry in overlaps]
    return segments

def _directory_heading_title(heading):
    """Reduce an OCR line to the navigable title portion."""
    line = heading.get('line', '').strip()
    kind = heading.get('type')
    if kind in {'progress_subrecord', 'ward_round_record', 'discussion_record'}:
        patterns = {
            'progress_subrecord': r'(?:首次|日常|阶段性|术后)病程记录',
            'ward_round_record': r'(?:主任|副主任|主治|住院)医师查房记录',
            'discussion_record': r'(?:术前|疑难病例|死亡病例|多学科)讨论(?:记录|结论)',
        }
        match = re.search(patterns[kind], line)
        return match.group(0) if match else line[:80]
    labels = {
        'admission_section': r'^(主诉|现病史|既往史|个人史|家族史|体格检查|专科检查|辅助检查|初步诊断)',
        'surgery_section': r'^(手术名称|手术日期|术前诊断|术后诊断|手术经过|术者|麻醉方式)',
        'consultation_opinion': r'(会诊意见|会诊建议|会诊医师)',
        'nursing_section': r'^(护理记录|生命体征|出入量|护理评估|护理措施)',
    }
    pattern = labels.get(kind)
    match = re.search(pattern, line) if pattern else None
    return match.group(1) if match and match.lastindex else (match.group(0) if match else line[:80])



def build_hierarchical_outline(pages, segments=None):
    """Build level-1 PDF bookmarks plus rule-based level-2 children."""
    index = _build_pdf_outline_index(pages)
    if segments is None:
        segments = segment_documents(pages)
    documents = []
    for document_id, entries in index.items():
        document_pages = [page for page in pages
                          if (page.get('document_id') or page.get('path')) == document_id]
        for entry in entries:
            children = []
            seen = set()
            allowed_types = _DIRECTORY_SECONDARY_TYPES.get(
                _normalise_outline_text(entry['title']), set())
            for page in document_pages:
                if not (entry['start_page'] <= page.get('page', 0) <= entry['end_page']):
                    continue
                for heading in _find_secondary_headings(page.get('text', ''), entry['title']):
                    if heading['type'] not in allowed_types:
                        continue
                    title = _directory_heading_title(heading)
                    key = (heading['type'], title, page.get('page'))
                    if key in seen:
                        continue
                    seen.add(key)
                    children.append({
                        'level': 2,
                        'type': heading['type'],
                        'title': title,
                        'start_page': page.get('page'),
                        'end_page': page.get('page'),
                        'source': 'rule',
                    })
            documents.append({
                'document_id': document_id,
                'level': 1,
                'type': entry['outline_type'],
                'title': entry['title'],
                'start_page': entry['start_page'],
                'end_page': entry['end_page'],
                'source': entry['source'],
                'children': children,
            })
    return {
        'version': 'hierarchical-outline-v1',
        'source': 'embedded_pdf_bookmarks_plus_rules',
        'documents': documents,
        'segments': segments,
    }


# Page-level category patterns (for pages that don't have clear document headers)
# These are secondary patterns when no explicit header is found
PAGE_CATEGORIES = {
    'cover': r'病案首页|入院时间.*出院时间',
    'summary': r'出院小结',
    'order_table': r'医嘱.*开立时间|长期医嘱|临时医嘱',
    'surgery_table': r'手术及操作人员|术者.*麻醉方式',
    'medication': r'(?:呋塞米|螺内酯|华法林|氯化钾|医嘱)[^。]*(?:每日|mg|片)',
    'physical_exam': r'体格检查.*(?:T\s+\d+℃|BP\s+\d+)',
    'diagnosis': r'(?:诊断|临床诊断).*(?:二尖瓣|心脏|疾病)',
    'progress': r'(?:症状|既往|既往史|专科检查).*(?:患者|病情)',

    # New: Examination reports
    'exam_report': r'(?:超声|心电图|CT|影像|X光|B超|彩超)(?:检查|报告)',
    'lab_report': r'(?:化验|检验|血糖|血脂|白细胞|血红蛋白|检测)(?:报告|结果)',

    # New: Nursing and monitoring
    'nursing_eval': r'(?:VTE风险|营养筛查|压疮|跌倒).*(?:评估|筛查)',
    'monitor_record': r'(?:监护|生命体征|心率|血压|呼吸|体温).*(?:记录|数据)',

    # New: Surgical materials
    'surgical_materials': r'(?:耗材|器械|物品|消耗|医疗用品).*(?:数量|规格)',

    # New: Assessment scales
    'assessment_scale': r'(?:评分|评估|筛查|量表).*(?:得分|评价)',

    # New: Billing
    'billing': r'(?:费用|价格|金额|账单|收费).*(?:合计|总额)',

    # Additional: Anesthesia summary and surgical info
    'anesthesia_summary': r'麻醉小结|麻醉方法.*(?:全身麻醉|局部麻醉)',
    'surgery_info': r'(?:手术名称|手术时间|手术风险).*(?:术|操作)',
    'consent_form': r'(?:知情同意|同意书).*(?:患者|签名)',
}


def segment_documents(pages: List[Dict[str, Any]],
                     max_segment_pages: int = 50,
                     enable_intra_page_split: bool = True,
                     merge_cross_page: bool = True) -> List[Dict[str, Any]]:
    """Segment pages into document units with intra-page splitting.

    Args:
        pages: List of page dicts with 'page', 'text', 'document_id', 'case_id'
        max_segment_pages: Maximum pages per segment (safety limit)
        enable_intra_page_split: If True, split pages with multiple record headers

    Returns:
        List of segment dicts with:
        - segment_id: unique identifier
        - document_type: type of document
        - case_id: case identifier
        - document_id: PDF document identifier
        - start_page: first page number
        - end_page: last page number (inclusive)
        - start_line: first line in start_page (for intra-page segments)
        - end_line: last line in end_page (for intra-page segments)
        - text: text content of this segment
        - pages: list of page numbers
        - boundary_confidence: 0.0-1.0, how confident the boundaries are
        - headers_found: list of header matches
        - is_intra_page: True if this segment shares a page with others
    """
    if not pages:
        return []

    segments = []
    active_segment = None

    for page in pages:
        page_num = page['page']
        text = page['text']

        if enable_intra_page_split:
            # Find all headers in this page
            headers = _find_all_headers_with_position(text)

            if len(headers) > 1:
                # Multiple level-1 titles in one page - split them. Level-2
                # headings such as “查房嘱” are not returned here.
                page_segments = _split_intra_page_records(page, headers)
                segments.extend(page_segments)
                active_segment = page_segments[-1]
                continue
            elif len(headers) == 1:
                # A new level-1 title closes the previous document and starts
                # a new one, even when the title occurs on a continuation page.
                seg = _create_segment_from_header(page, headers[0], is_intra_page=False)
                segments.append(seg)
                active_segment = seg
                continue

        # A page without a level-1 title is usually a continuation of the
        # current document. Merge it conservatively: unknown/progress pages
        # can continue a narrative document, while clearly typed table pages
        # start their own generic segment.
        page_category = _classify_page(text)
        if (merge_cross_page and active_segment is not None and
                _can_merge_continuation(active_segment, page, page_category,
                                        max_segment_pages)):
            _append_page_to_segment(active_segment, page)
            continue

        # No headers found - classify as generic page
        seg = {
            'segment_id': _generate_segment_id(page, {'type': page_category}),
            'document_type': page_category,
            'case_id': page.get('case_id'),
            'document_id': page.get('document_id'),
            'start_page': page_num,
            'end_page': page_num,
            'start_line': 0,
            'end_line': len(text.split('\n')),
            'text': text,
            'pages': [page_num],
            'headers_found': [],
            'boundary_confidence': 0.5,
            'is_intra_page': False,
            'header_level': None,
            'secondary_headings': _find_secondary_headings(text),
        }
        segments.append(seg)
        active_segment = seg

    _attach_pdf_outline_metadata(segments, pages)
    return segments


def _find_all_headers_with_position(text: str) -> List[Dict[str, Any]]:
    """Find all record headers in text with their positions.

    IMPROVED:
    - Searches entire text, not just first 20 lines
    - Allows multiple header types per line
    - Handles level-1 headers that appear mid-line after OCR has joined lines

    Returns:
        List of {'type': str, 'line_num': int, 'line': str, 'char_offset': int}
        Sorted by position.
    """
    headers = []
    lines = text.split('\n')

    has_progress_parent = False
    for i, line in enumerate(lines):
        line_stripped = line.strip()
        if not line_stripped or len(line_stripped) < 3:
            continue

        # Check only level-1 document titles. A level-2 heading is metadata,
        # never a document boundary.
        matches_in_line = []
        for doc_type, pattern in DOCUMENT_HEADERS.items():
            if re.search(pattern, line_stripped):
                matches_in_line.append(doc_type)

        # If multiple patterns match, prefer more specific ones
        # (e.g., ward_round over progress_note if both match)
        if matches_in_line:
            # Use the first match (ordering in DOCUMENT_HEADERS dict)
            doc_type = matches_in_line[0]
            # “首次病程记录/日常病程记录” is a child record when the
            # page already contains the parent “病程记录” title. Keep it as
            # metadata instead of creating a second document boundary.
            if (doc_type == 'progress_note' and
                    re.search(r'(?:首次|日常|阶段性|术后)病程记录', line_stripped) and
                    has_progress_parent):
                continue
            if doc_type == 'progress_note' and not re.search(
                    r'(?:首次|日常|阶段性|术后)病程记录', line_stripped):
                has_progress_parent = True
            char_offset = sum(len(l) + 1 for l in lines[:i])
            headers.append({
                'type': doc_type,
                'line_num': i,
                'line': line_stripped,
                'char_offset': char_offset
            })

    return headers


def _split_intra_page_records(page: Dict[str, Any],
                              headers: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Split a page with multiple records into separate segments."""
    segments = []
    text = page['text']
    lines = text.split('\n')
    page_num = page['page']

    for i, header in enumerate(headers):
        # Keep the preamble with the first level-1 document. It often contains
        # the date/patient context that OCR placed before the title.
        start_line = 0 if i == 0 else header['line_num']
        end_line = headers[i + 1]['line_num'] if i + 1 < len(headers) else len(lines)

        # Extract text for this segment
        segment_lines = lines[start_line:end_line]
        segment_text = '\n'.join(segment_lines)

        segment = {
            'segment_id': _generate_segment_id(page, header, intra_index=i),
            'document_type': header['type'],
            'case_id': page.get('case_id'),
            'document_id': page.get('document_id'),
            'start_page': page_num,
            'end_page': page_num,
            'start_line': start_line,
            'end_line': end_line,
            'text': segment_text,
            'pages': [page_num],
            'headers_found': [header],
            'boundary_confidence': 0.85,  # Slightly lower than cross-page
            'is_intra_page': True,
            'header_level': 1,
            'secondary_headings': _find_secondary_headings(segment_text),
        }

        segments.append(segment)

    return segments


def _find_secondary_headings(text: str, parent_title: str = None) -> List[Dict[str, Any]]:
    """Return rule-based level-2 headings without treating them as boundaries."""
    patterns = list(SECONDARY_HEADINGS.items())
    normalized_parent = _normalise_outline_text(parent_title)
    for title, parent_patterns in SECONDARY_HEADINGS_BY_OUTLINE.items():
        if normalized_parent and (_normalise_outline_text(title) in normalized_parent or
                                  normalized_parent in _normalise_outline_text(title)):
            patterns.extend(parent_patterns.items())
            break
    found = []
    seen = set()
    for i, line in enumerate(text.split('\n')):
        stripped = line.strip()
        if not stripped:
            continue
        for name, pattern in patterns:
            if re.search(pattern, stripped):
                key = (name, i, stripped)
                if key in seen:
                    continue
                seen.add(key)
                found.append({'type': name, 'line_num': i, 'line': stripped,
                              'level': 2, 'parent_title': parent_title})
    return found


def _can_merge_continuation(active: Dict[str, Any], page: Dict[str, Any],
                            page_category: str, max_segment_pages: int) -> bool:
    """Conservative continuation test for pages without a level-1 title."""
    if len(active.get('pages', [])) >= max_segment_pages:
        return False
    if page_category in {'cover', 'diagnosis', 'order_table', 'surgery_table',
                         'exam_report', 'nursing_eval', 'monitor_record',
                         'surgical_materials', 'assessment_scale', 'billing'}:
        return False
    # A page with a level-2 heading matching the active document is a strong
    # continuation signal. Unknown/progress pages are allowed as narrative
    # continuations because OCR frequently drops the repeated title.
    secondary = _find_secondary_headings(page.get('text', ''))
    if active.get('document_type') == 'ward_round' and secondary:
        return True
    return page_category in {'unknown', 'progress', 'summary'}


def _append_page_to_segment(segment: Dict[str, Any], page: Dict[str, Any]) -> None:
    """Append a continuation page while preserving the original metadata."""
    segment['end_page'] = page['page']
    segment['end_line'] = len(page.get('text', '').split('\n'))
    segment['pages'].append(page['page'])
    segment['text'] = segment.get('text', '') + '\n' + page.get('text', '')
    segment.setdefault('continuation_pages', []).append(page['page'])
    segment['secondary_headings'] = _find_secondary_headings(segment['text'])
    segment['boundary_confidence'] = min(segment.get('boundary_confidence', 0.8), 0.88)


def _create_segment_from_header(page: Dict[str, Any],
                                header: Dict[str, Any],
                                is_intra_page: bool = False) -> Dict[str, Any]:
    """Create a segment from a single header."""
    page_num = page['page']
    text = page['text']

    return {
        'segment_id': _generate_segment_id(page, header),
        'document_type': header['type'],
        'case_id': page.get('case_id'),
        'document_id': page.get('document_id'),
        'start_page': page_num,
        'end_page': page_num,
        'start_line': 0,
        'end_line': len(text.split('\n')),
        'text': text,
        'pages': [page_num],
        'headers_found': [header],
        'boundary_confidence': 0.9,
        'is_intra_page': is_intra_page,
        'header_level': 1,
        'secondary_headings': _find_secondary_headings(text),
    }


def _find_headers(text: str) -> List[Dict[str, Any]]:
    """Find all document headers in text.

    Returns:
        List of {'type': str, 'match': str, 'position': int, 'line': str}
    """
    headers = []
    lines = text.split('\n')

    for i, line in enumerate(lines[:20]):  # Only check first 20 lines
        line_stripped = line.strip()
        if not line_stripped or len(line_stripped) < 3:
            continue

        for doc_type, pattern in DOCUMENT_HEADERS.items():
            if re.search(pattern, line_stripped):
                headers.append({
                    'type': doc_type,
                    'match': pattern,
                    'position': i,
                    'line': line_stripped
                })

    return headers


def _classify_page(text: str) -> str:
    """Classify page when no clear document header is found.

    IMPROVED:
    - Searches entire text (not just first 500 chars)
    - Checks multiple patterns for better coverage
    """
    # Search full text for better accuracy
    for category, pattern in PAGE_CATEGORIES.items():
        if re.search(pattern, text, re.MULTILINE):
            return category

    return 'unknown'


def _calculate_confidence(segment: Dict[str, Any]) -> float:
    """Calculate boundary confidence for a segment.

    High confidence (0.8-1.0): Clear header, reasonable length
    Medium confidence (0.5-0.8): Header but very long or very short
    Low confidence (0.0-0.5): No header or suspicious patterns
    """
    confidence = 0.5  # baseline

    # Has clear header
    if segment.get('headers_found'):
        confidence += 0.3

    # Reasonable length (1-20 pages)
    num_pages = len(segment.get('pages', []))
    if 1 <= num_pages <= 20:
        confidence += 0.2
    elif num_pages > 30:
        confidence -= 0.2

    # Has expected content markers
    text = segment.get('text', '')
    if segment['document_type'] == 'ward_round':
        if re.search(r'患者.*查体', text):
            confidence += 0.1
    elif segment['document_type'] == 'consultation':
        if re.search(r'会诊意见', text):
            confidence += 0.1

    return max(0.0, min(1.0, confidence))


def _generate_segment_id(page: Dict[str, Any], header: Dict[str, Any], intra_index: int = 0) -> str:
    """Generate unique segment ID."""
    content = json.dumps({
        'document_id': page.get('document_id'),
        'page': page['page'],
        'type': header.get('type', 'unknown'),
        'intra_index': intra_index,  # For multiple records on same page
    }, ensure_ascii=False)
    return 'seg_' + hashlib.sha256(content.encode()).hexdigest()[:16]


def get_segment_for_page(segments: List[Dict[str, Any]],
                        page_number: int) -> Optional[Dict[str, Any]]:
    """Get the segment that contains a specific page."""
    for seg in segments:
        if seg['start_page'] <= page_number <= seg['end_page']:
            return seg
    return None


def filter_segments_by_type(segments: List[Dict[str, Any]],
                           doc_types: List[str]) -> List[Dict[str, Any]]:
    """Filter segments by document type."""
    return [seg for seg in segments if seg['document_type'] in doc_types]


def segment_statistics(segments: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Calculate statistics about segmentation results."""
    from collections import Counter

    type_counts = Counter(seg['document_type'] for seg in segments)
    confidence_dist = {
        'high (>0.8)': sum(1 for s in segments if s['boundary_confidence'] > 0.8),
        'medium (0.5-0.8)': sum(1 for s in segments if 0.5 <= s['boundary_confidence'] <= 0.8),
        'low (<0.5)': sum(1 for s in segments if s['boundary_confidence'] < 0.5),
    }

    return {
        'total_segments': len(segments),
        'by_type': dict(type_counts),
        'confidence_distribution': confidence_dist,
        'avg_pages_per_segment': sum(len(s['pages']) for s in segments) / len(segments) if segments else 0,
        'multi_page_segments': sum(1 for s in segments if len(s['pages']) > 1),
    }


def page_hierarchy_rows(pages, segments=None):
    """Return one normalized hierarchy row for every OCR page."""
    index = _build_pdf_outline_index(pages)
    if segments is None:
        segments = segment_documents(pages)
    rows = []
    for page in pages:
        document_id = page.get('document_id') or page.get('path')
        page_number = int(page.get('page', 0) or 0)
        entries = index.get(document_id, [])
        primary = _outline_for_page(entries, page_number)
        # When several bookmarks start on the same page, prefer a title that
        # is actually present in the page header area.
        header_text = _normalise_outline_text((page.get('text') or '')[:1200])
        for entry in [item for item in entries if item['start_page'] == page_number]:
            title = _normalise_outline_text(entry['title'])
            if title and title in header_text:
                primary = entry
                break

        page_segments = [
            segment for segment in segments
            if segment.get('document_id') == page.get('document_id')
            and segment.get('start_page', 0) <= page_number <= segment.get('end_page', 0)
        ]
        parent_title = primary.get('title') if primary else None
        allowed_types = _DIRECTORY_SECONDARY_TYPES.get(
            _normalise_outline_text(parent_title), set())
        child_titles = []
        child_types = []
        for heading in _find_secondary_headings(page.get('text', ''), parent_title):
            if heading['type'] not in allowed_types:
                continue
            title = _directory_heading_title(heading)
            if title not in child_titles:
                child_titles.append(title)
                child_types.append(heading['type'])

        rows.append({
            'document_id': page.get('document_id'),
            'case_id': page.get('case_id'),
            'page': page_number,
            'page_status': page.get('stage') or 'unknown',
            'level_1_title': primary.get('title') if primary else None,
            'level_1_type': primary.get('outline_type') if primary else None,
            'level_1_start_page': primary.get('start_page') if primary else None,
            'level_1_end_page': primary.get('end_page') if primary else None,
            'level_1_source': primary.get('source') if primary else None,
            'level_2_titles': child_titles,
            'level_2_types': child_types,
            'segment_ids': [segment.get('segment_id') for segment in page_segments],
            'segment_types': [segment.get('document_type') for segment in page_segments],
            'segment_titles': [_display_title(segment) for segment in page_segments],
            'text_char_count': len(page.get('text') or ''),
            'text_preview': ' '.join((page.get('text') or '').split())[:120],
        })
    return rows


_DISPLAY_TITLES = {
    'cover': '病案首页',
    'discharge': '出院记录',
    'admission': '入院记录',
    'progress_note': '病程记录',
    'ward_round': '查房记录',
    'consultation': '会诊记录',
    'discussion': '讨论记录',
    'consent': '知情同意书',
    'nursing': '护理记录',
    'surgery': '手术记录',
    'anesthesia': '麻醉记录',
    'long_order_sheet': '长期医嘱单',
    'temporary_order_sheet': '临时医嘱单',
    'order_sheet': '医嘱单',
    'medication': '用药记录',
    'summary': '出院小结',
    'order_table': '医嘱表',
    'surgery_table': '手术及操作人员记录',
    'physical_exam': '体格检查',
    'lab_report': '化验报告',
    'assessment_scale': '评估量表',
    'billing': '费用记录',
    'consent_form': '知情同意书',
    'unknown': '未分类文书',
    'exam_report': '检查报告',
    'lab_report': '化验报告',
    'diagnosis': '诊断记录',
    'nursing_eval': '护理评估',
    'monitor_record': '监护记录',
    'surgical_materials': '手术耗材记录',
    'anesthesia_summary': '麻醉小结',
    'surgery_info': '手术信息',
}

_ENTRY_HEADING_TYPES = {
    'progress_subrecord',
    'ward_round_instruction',
    'consultation_opinion',
    'discussion_conclusion',
    'ward_round_record',
    'discussion_record',
    'admission_section',
    'surgery_section',
    'order_section',
    'nursing_section',
}


def _display_title(segment: Dict[str, Any]) -> str:
    """Return a stable human-readable title for table/export output."""
    doc_type = segment.get('document_type', 'unknown')
    if doc_type in _DISPLAY_TITLES:
        return _DISPLAY_TITLES[doc_type]
    headers = segment.get('headers_found') or []
    if headers:
        return headers[0].get('line') or doc_type
    return doc_type


def segment_table_rows(segments: List[Dict[str, Any]], facts: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    """Build the compact table shown in the review console.

    ``entry_count`` counts facts assigned to a segment when facts are supplied.
    Without facts, it counts the parent title plus meaningful level-2 headings,
    with a minimum of one. The detailed OCR text remains in the segment JSON.
    """
    fact_counts = {}
    for fact in facts or []:
        sid = fact.get('segment_id')
        if sid:
            fact_counts[sid] = fact_counts.get(sid, 0) + 1

    rows = []
    for segment in segments:
        sid = segment.get('segment_id')
        child_count = sum(
            1 for heading in segment.get('secondary_headings', [])
            if heading.get('type') in _ENTRY_HEADING_TYPES
        )
        entry_count = fact_counts.get(sid, 0) if facts is not None else max(1, child_count + 1)
        rows.append({
            'segment_id': sid,
            'type': segment.get('document_type'),
            'title': _display_title(segment),
            'start_page': segment.get('start_page'),
            'end_page': segment.get('end_page'),
            'page_count': len(segment.get('pages') or []),
            'entry_count': entry_count,
        })
    return rows
