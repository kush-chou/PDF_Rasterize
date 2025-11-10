import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pdf_processor_suite.pdf_split_rasterize import main_entry

# --- Helper to create a test PDF ---
try:
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    REPORTLAB_AVAILABLE = True
except ImportError:
    REPORTLAB_AVAILABLE = False


def create_sample_nested_pdf(path: Path):
    """Creates a 5-page PDF with nested bookmarks for testing."""
    if not REPORTLAB_AVAILABLE:
        raise unittest.SkipTest(
            "reportlab is not installed, skipping integration test."
        )

    c = canvas.Canvas(str(path), pagesize=letter)  # type: ignore

    # Page 1: Chapter 1
    c.drawString(100, 750, "This is page 1.")
    c.bookmarkPage("Chapter 1")
    c.addOutlineEntry("Chapter 1", "Chapter 1", 0, 0)
    c.showPage()

    # Page 2: Section 1.1
    c.drawString(100, 750, "This is page 2.")
    c.bookmarkPage("Section 1.1")
    c.addOutlineEntry("Section 1.1", "Section 1.1", 1, 0)
    c.showPage()

    # Page 3: Chapter 2
    c.drawString(100, 750, "This is page 3.")
    c.bookmarkPage("Chapter 2")
    c.addOutlineEntry("Chapter 2", "Chapter 2", 0, 0)
    c.showPage()

    # Page 4: Section 2.1
    c.drawString(100, 750, "This is page 4.")
    c.bookmarkPage("Section 2.1")
    c.addOutlineEntry("Section 2.1", "Section 2.1", 1, 0)
    c.showPage()

    # Page 5: Section 2.2
    c.drawString(100, 750, "This is page 5.")
    c.bookmarkPage("Section 2.2")
    c.addOutlineEntry("Section 2.2", "Section 2.2", 1, 0)
    c.showPage()

    c.save()


class TestEndToEndWorkflow(unittest.TestCase):
    def setUp(self):
        """Set up a temporary directory and a sample PDF for testing."""
        self.test_dir = Path(tempfile.mkdtemp())
        self.input_pdf = self.test_dir / "nested_bookmarks.pdf"
        self.output_dir = self.test_dir / "output"
        create_sample_nested_pdf(self.input_pdf)

    def tearDown(self):
        """Clean up the temporary directory after tests."""
        shutil.rmtree(self.test_dir)

    @mock.patch("pdf_processor_suite.pdf_split_rasterize._rasterize_single_pdf")
    def test_split_and_rasterize_nested_bookmarks(self, mock_rasterize_single_pdf):
        """
        Tests the full split-and-rasterize workflow with the new directory structure.
        """

        # --- Mocking ---
        def mock_rasterize_success(pdf_path, output_dir, *args, **kwargs):
            # Simulate creating the rasterized file in the correct output directory
            rasterized_path = output_dir / f"{pdf_path.stem}_rasterized.pdf"
            rasterized_path.touch()
            return str(rasterized_path)

        mock_rasterize_single_pdf.side_effect = mock_rasterize_success

        # --- Setup Arguments ---
        class Args:
            pass

        args = Args()
        args.input = self.input_pdf
        args.output = self.output_dir
        args.resolution = 150
        args.keep_originals = False  # Test cleanup
        args.dry_run = False
        args.workers = 1
        args.gs_path = "gs"
        args.magick_path = "magick"
        args.flatten_output = False  # This should be ignored when rasterizing
        args.max_split_level = 0
        args.html_report = None
        args.merge = None

        # --- Run ---
        main_entry(args, rasterize=True)

        # --- Assert ---
        unrasterized_dir = self.output_dir / "unrasterized"
        rasterized_dir = self.output_dir / "rasterized"

        # The unrasterized directory should be gone because keep_originals is False
        self.assertFalse(
            unrasterized_dir.exists(), "The 'unrasterized' directory should be removed."
        )

        # The rasterized directory should exist and contain the final files
        self.assertTrue(rasterized_dir.is_dir())
        self.assertTrue((rasterized_dir / "Chapter 1_rasterized.pdf").exists())
        self.assertTrue((rasterized_dir / "Section 1.1_rasterized.pdf").exists())
        self.assertTrue((rasterized_dir / "Chapter 2_rasterized.pdf").exists())

    @mock.patch("pdf_processor_suite.pdf_split_rasterize._rasterize_single_pdf")
    def test_split_with_keep_originals(self, mock_rasterize_single_pdf):
        """
        Tests that the 'unrasterized' directory is kept when keep_originals is True.
        """

        # --- Mocking ---
        def mock_rasterize_success(pdf_path, output_dir, *args, **kwargs):
            rasterized_path = output_dir / f"{pdf_path.stem}_rasterized.pdf"
            rasterized_path.touch()
            return str(rasterized_path)

        mock_rasterize_single_pdf.side_effect = mock_rasterize_success

        # --- Setup Arguments ---
        class Args:
            pass

        args = Args()
        args.input = self.input_pdf
        args.output = self.output_dir
        args.resolution = 150
        args.keep_originals = True  # Keep the intermediate files
        args.dry_run = False
        args.workers = 1
        args.gs_path = "gs"
        args.magick_path = "magick"
        args.flatten_output = (
            True  # This should be respected for the unrasterized output
        )
        args.max_split_level = 0
        args.html_report = None
        args.merge = None

        # --- Run ---
        main_entry(args, rasterize=True)

        # --- Assert ---
        unrasterized_dir = self.output_dir / "unrasterized"
        rasterized_dir = self.output_dir / "rasterized"

        # The unrasterized directory should still exist
        self.assertTrue(unrasterized_dir.is_dir())
        self.assertTrue((unrasterized_dir / "Chapter 1.pdf").exists())
        self.assertTrue((unrasterized_dir / "Section 1.1.pdf").exists())

        # The rasterized directory should also exist
        self.assertTrue(rasterized_dir.is_dir())
        self.assertTrue((rasterized_dir / "Chapter 1_rasterized.pdf").exists())

    def test_split_only_no_rasterize(self):
        """
        Tests that splitting without rasterizing places files directly in the output directory.
        """

        # --- Setup Arguments ---
        class Args:
            pass

        args = Args()
        args.input = self.input_pdf
        args.output = self.output_dir
        args.resolution = 150
        args.keep_originals = False
        args.dry_run = False
        args.workers = 1
        args.gs_path = "gs"
        args.magick_path = "magick"
        args.flatten_output = True  # Test flatten output
        args.max_split_level = 0
        args.html_report = None
        args.merge = None

        # --- Run ---
        main_entry(args, rasterize=False)  # Explicitly disable rasterization

        # --- Assert ---
        # No 'unrasterized' or 'rasterized' directories should be created
        self.assertFalse((self.output_dir / "unrasterized").exists())
        self.assertFalse((self.output_dir / "rasterized").exists())

        # Files should be in the root of the output directory
        self.assertTrue((self.output_dir / "Chapter 1.pdf").exists())
        self.assertTrue((self.output_dir / "Section 1.1.pdf").exists())
        self.assertTrue((self.output_dir / "Chapter 2.pdf").exists())
