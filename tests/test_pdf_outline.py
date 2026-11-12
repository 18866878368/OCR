import unittest
from unittest.mock import patch

from medocr.segmentation import (build_hierarchical_outline, page_hierarchy_rows,
                                  segment_documents)


OUTLINE = [
    {'level': 1, 'title': '病案首页', 'start_page': 1, 'page_count': 3},
    {'level': 1, 'title': '病程记录', 'start_page': 2, 'page_count': 3},
]


class PdfOutlineTests(unittest.TestCase):
    def pages(self):
        return [
            {'document_id': 'doc', 'path': 'case.pdf', 'page': 1,
             'text': '病案首页\n患者信息'},
            {'document_id': 'doc', 'path': 'case.pdf', 'page': 2,
             'text': '病程记录\n首次病程记录\n患者情况'},
            {'document_id': 'doc', 'path': 'case.pdf', 'page': 3,
             'text': '日常病程记录\n主任医师查房记录\n诊疗意见'},
        ]

    @patch('medocr.segmentation.read_pdf_outline', return_value=OUTLINE)
    def test_embedded_bookmark_is_attached_to_segment(self, _read):
        segments = segment_documents(self.pages())
        progress = [segment for segment in segments if segment.get('outline_title') == '病程记录']
        self.assertTrue(progress)
        self.assertEqual(progress[0]['outline_source'], 'embedded_pdf_bookmark')
        self.assertTrue(any(item['type'] == 'progress_subrecord'
                            for item in progress[0]['secondary_headings']))

    @patch('medocr.segmentation.read_pdf_outline', return_value=OUTLINE)
    def test_hierarchy_has_rule_based_children(self, _read):
        hierarchy = build_hierarchical_outline(self.pages())
        progress = [item for item in hierarchy['documents'] if item['title'] == '病程记录'][0]
        titles = [item['title'] for item in progress['children']]
        self.assertIn('首次病程记录', titles)
        self.assertIn('主任医师查房记录', titles)
        self.assertEqual(progress['source'], 'embedded_pdf_bookmark')


    @patch('medocr.segmentation.read_pdf_outline', return_value=OUTLINE)
    def test_page_rows_keep_pages_without_level_two_headings(self, _read):
        rows = page_hierarchy_rows(self.pages())
        self.assertEqual(len(rows), 3)
        self.assertEqual([row['page'] for row in rows], [1, 2, 3])
        self.assertEqual(rows[0]['level_2_titles'], [])
        self.assertIn('首次病程记录', rows[1]['level_2_titles'])
        self.assertEqual(rows[1]['text_char_count'], len(self.pages()[1]['text']))


if __name__ == '__main__':
    unittest.main()
