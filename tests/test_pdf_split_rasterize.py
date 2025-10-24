import unittest
import sys
from pathlib import Path

from pdf_processor_suite.pdf_split_rasterize import sanitize_filename, _calculate_end_pages_recursively

class TestSanitizeFilename(unittest.TestCase):
    """Tests for the filename sanitization function."""

    def test_removes_invalid_chars(self):
        self.assertEqual(sanitize_filename('file/with\invalid:chars*?<|"">'), 'file-with-invalid-chars')

    def test_strips_whitespace(self):
        self.assertEqual(sanitize_filename('  leading and trailing  '), 'leading and trailing')

    def test_handles_long_names(self):
        long_name = 'a' * 250
        self.assertEqual(len(sanitize_filename(long_name)), 200)

    def test_handles_unicode_quotes(self):
        self.assertEqual(sanitize_filename("‘smart’ “quotes”"), "'smart' \"quotes\"")

class TestEndPageCalculation(unittest.TestCase):
    """Tests the recursive logic for calculating bookmark end pages."""

    def test_nested_end_pages(self):
        """
        Simulates a nested bookmark structure to verify correct page range calculation.
        - Chapter 1 (p1-19)
          - Section 1.1 (p1-9)
          - Section 1.2 (p10-19)
        - Chapter 2 (p20-29)
        """
        bookmarks = [
            {
                "title": "Chapter 1", "level": 1, "page_index": 0,
                "children": [
                    {"title": "Section 1.1", "level": 2, "page_index": 0, "children": []},
                    {"title": "Section 1.2", "level": 2, "page_index": 9, "children": []}
                ]
            },
            {
                "title": "Chapter 2", "level": 1, "page_index": 19, "children": []
            }
        ]
        
        # Total pages in the document
        num_pages = 30

        # Run the function to calculate end pages
        _calculate_end_pages_recursively(bookmarks, parent_end_page_boundary=num_pages, num_pages=num_pages)

        # --- Assertions ---
        
        # Chapter 1 should end right before Chapter 2 starts
        self.assertEqual(bookmarks[0]['end_page_index'], 18) # page 19

        # Section 1.1 should end right before Section 1.2 starts
        self.assertEqual(bookmarks[0]['children'][0]['end_page_index'], 8) # page 9

        # Section 1.2 is the last child, its end should be the same as its parent's
        self.assertEqual(bookmarks[0]['children'][1]['end_page_index'], 18) # page 19

        # Chapter 2 is the last top-level bookmark, it should go to the end of the document
        self.assertEqual(bookmarks[1]['end_page_index'], 29) # page 30

    def test_single_level_bookmarks(self):
        """Tests a simple, flat bookmark structure."""
        bookmarks = [
            {"title": "Intro", "level": 1, "page_index": 0, "children": []},
            {"title": "Body", "level": 1, "page_index": 4, "children": []},
            {"title": "Conclusion", "level": 1, "page_index": 9, "children": []}
        ]
        num_pages = 15
        _calculate_end_pages_recursively(bookmarks, parent_end_page_boundary=num_pages, num_pages=num_pages)

        self.assertEqual(bookmarks[0]['end_page_index'], 3) # p4
        self.assertEqual(bookmarks[1]['end_page_index'], 8) # p9
        self.assertEqual(bookmarks[2]['end_page_index'], 14) # p15

if __name__ == '__main__':
    unittest.main()
