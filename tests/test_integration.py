import unittest
import tempfile
import shutil
from pathlib import Path
import sys
from unittest import mock

from pdf_processor_suite.pdf_split_rasterize import main_entry

# --- Helper to create a test PDF ---
try:
    from reportlab.pdfgen import canvas
    from reportlab.lib.pagesizes import letter
    REPORTLAB_AVAILABLE = True
except ImportError:
    REPORTLAB_AVAILABLE = False

def create_sample_nested_pdf(path: Path):
    """Creates a 5-page PDF with nested bookmarks for testing."""
    if not REPORTLAB_AVAILABLE:
        raise unittest.SkipTest("reportlab is not installed, skipping integration test.")

    c = canvas.Canvas(str(path), pagesize=letter) # type: ignore

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

    @mock.patch('pdf_processor_suite.pdf_split_rasterize.rasterize_pdf') # Patch the underlying function
    def test_split_and_rasterize_nested_bookmarks(self, mock_rasterize_pdf):
        """
        Tests the full split-and-rasterize workflow with nested bookmarks.
        The rasterization step is mocked to speed up the test and avoid
        dependencies on Ghostscript/ImageMagick.
        """
        # Configure the mock for rasterize_pdf
        # This function simulates the behavior of rasterize_pdf
        def mock_rasterize_pdf_success(input_pdf, output_pdf, *args, **kwargs):
            """Simulates a successful rasterization by rasterize_pdf."""
            output_pdf.touch() # Simulate creating the rasterized file
            return True # Simulate success

        mock_rasterize_pdf.side_effect = mock_rasterize_pdf_success

        # --- Setup Arguments for main_entry ---
        # We create a simple object to mimic the argparse result
        class Args:
            def __init__(self):
                self.input = None
                self.output = None
                self.resolution = None
                self.keep_originals = None
                self.dry_run = None
                self.workers = None
                self.gs_path = None
                self.magick_path = None
                self.html_report = None
                self.flatten_output = False

        args = Args()
        args.input = self.input_pdf # type: ignore
        args.output = self.output_dir # type: ignore
        args.resolution = 150 # type: ignore # This value is passed but not used by the mock
        args.keep_originals = False # type: ignore # Test the cleanup logic
        args.dry_run = False # type: ignore
        args.workers = 2 # type: ignore
        args.gs_path = "gs" # type: ignore # Mocked, so path doesn't matter
        args.magick_path = "magick" # type: ignore # Mocked, so path doesn't matter
        args.html_report = None

        # --- Run the main logic ---
        success, errors, report = main_entry(args)

        # --- Assert the results ---
        self.assertTrue(success, "The main process should report success.")
        self.assertEqual(len(errors), 0, "There should be no errors reported.")

        # Check that the bookmark JSON file was created
        self.assertTrue((self.output_dir / "_bookmarks.json").exists())

        # Check for expected directory structure
        self.assertTrue((self.output_dir / "Chapter 1").is_dir())
        self.assertTrue((self.output_dir / "Chapter 2").is_dir())

        # Check that the final rasterized files exist
        self.assertTrue((self.output_dir / "Chapter 1" / "Section 1.1_rasterized.pdf").exists())
        self.assertTrue((self.output_dir / "Chapter 2" / "Section 2.1_rasterized.pdf").exists())
        self.assertTrue((self.output_dir / "Chapter 2" / "Section 2.2_rasterized.pdf").exists())

        # Check that the original split PDFs were cleaned up
        self.assertFalse((self.output_dir / "Chapter 1" / "Section 1.1.pdf").exists())

    @mock.patch('pdf_processor_suite.pdf_split_rasterize.rasterize_pdf') # Patch the underlying function
    def test_split_and_flatten_output(self, mock_rasterize_pdf):
        """
        Tests the workflow with the --flatten-output option enabled.
        """
        
        # Configure the mock for rasterize_pdf
        def mock_rasterize_pdf_success(input_pdf, output_pdf, *args, **kwargs):
            """Simulates a successful rasterization by rasterize_pdf."""
            output_pdf.touch() # Simulate creating the rasterized file
            return True # Simulate success

        mock_rasterize_pdf.side_effect = mock_rasterize_pdf_success
        # --- Setup Arguments for main_entry ---
        class Args:
            def __init__(self):
                self.input = None
                self.output = None
                self.resolution = 150
                self.keep_originals = False
                self.dry_run = False
                self.workers = 2
                self.gs_path = "gs"
                self.magick_path = "magick"
                self.html_report = None
                self.flatten_output = True # Enable the feature we are testing

        args = Args()
        args.input = self.input_pdf # type: ignore
        args.output = self.output_dir # type: ignore

        # --- Run the main logic ---
        success, errors, report = main_entry(args)

        # --- Assert the results ---
        self.assertTrue(success, "The main process should report success.")
        self.assertEqual(len(errors), 0, "There should be no errors reported.")

        # Check that the bookmark JSON file still exists in the root of the output
        self.assertTrue((self.output_dir / "_bookmarks.json").exists())

        # Check that the final rasterized files exist in the FLAT output directory
        self.assertTrue((self.output_dir / "Section 1.1_rasterized.pdf").exists())
        self.assertTrue((self.output_dir / "Section 2.1_rasterized.pdf").exists())
        self.assertTrue((self.output_dir / "Section 2.2_rasterized.pdf").exists())

        # Check that the nested directories were removed
        self.assertFalse((self.output_dir / "Chapter 1").exists(), "Nested directory 'Chapter 1' should have been removed.")
        self.assertFalse((self.output_dir / "Chapter 2").exists(), "Nested directory 'Chapter 2' should have been removed.")

    @mock.patch('pdf_processor_suite.pdf_split_rasterize.rasterize_pdf')
    def test_rasterization_failure_handling(self, mock_rasterize_pdf):
        """
        Tests that the system correctly handles a failure during the rasterization of one file.
        """
        # Configure the mock to fail for a specific file
        def mock_rasterize_side_effect(input_pdf, output_pdf, *args, **kwargs):
            # Let's make 'Section 2.1.pdf' fail
            if "Section 2.1" in str(input_pdf):
                # Don't create the output file to simulate a hard failure
                return False # Signal failure
            else:
                # Succeed for all other files
                output_pdf.touch()
                return True

        mock_rasterize_pdf.side_effect = mock_rasterize_side_effect

        # --- Setup Arguments for main_entry ---
        class Args:
            def __init__(self):
                self.input = self.input_pdf # type: ignore
                self.output = self.output_dir # type: ignore
                self.resolution = 150
                self.keep_originals = False
                self.dry_run = False
                self.workers = 2
                self.gs_path = "gs"
                self.magick_path = "magick"
                self.html_report = None
                self.flatten_output = False

        args = Args()

        # --- Run the main logic ---
        success, errors, report = main_entry(args)

        # --- Assert the results ---
        self.assertFalse(success, "The main process should report failure.")
        self.assertEqual(len(errors), 1, "There should be one error reported.")
        self.assertIn("Rasterization failed for Section 2.1.pdf", errors[0])

        # Check that the successful files were still created and their originals cleaned up
        self.assertTrue((self.output_dir / "Chapter 1" / "Section 1.1_rasterized.pdf").exists())
        self.assertFalse((self.output_dir / "Chapter 1" / "Section 1.1.pdf").exists())

        # Check that the failed file was NOT created, and its original was NOT deleted
        self.assertFalse((self.output_dir / "Chapter 2" / "Section 2.1_rasterized.pdf").exists())
        self.assertTrue((self.output_dir / "Chapter 2" / "Section 2.1.pdf").exists(), "Original split file for the failed rasterization should be kept.")
