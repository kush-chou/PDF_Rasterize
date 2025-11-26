#!/usr/bin/env python3
from __future__ import annotations

import argparse
import concurrent.futures
import json
import logging
import multiprocessing
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any  # Using Any for complex bookmark structures for now

# PyPDF2 is used for reading/writing PDFs and handling bookmarks
from PyPDF2 import PdfMerger, PdfReader, PdfWriter
from PyPDF2.errors import PdfReadError

# Import Destination for type checking bookmarks
from PyPDF2.generic import Destination, Fit

from .subprocess_utils import run_subprocess

# --- Monkey Patch PyPDF2 Fit ---
# Fix for "not enough values to unpack" with malformed /XYZ destinations
_original_fit_init = Fit.__init__


def _patched_fit_init(self, fit_type, fit_args=tuple()):
    if fit_type == "/XYZ" and len(fit_args) < 3:
        fit_args = list(fit_args) + [None] * (3 - len(fit_args))
        fit_args = tuple(fit_args)
    _original_fit_init(self, fit_type, fit_args)


Fit.__init__ = _patched_fit_init
# -------------------------------

# --- Global Configuration ---
RASTERIZE_RESOLUTION = 300  # DPI for rasterization
CLEANUP_ORIGINAL_SPLIT_PDFS = True  # Set to False via --keep-originals flag if needed
# --- Type Aliases ---
FilePath = str | Path
Bookmark = dict[str, Any]


# --- Function to sanitize filenames ---
def sanitize_filename(name: FilePath) -> str:
    """Removes or replaces characters invalid for filenames/paths."""
    name = str(name)  # Ensure it's a string
    # Remove characters that are problematic in paths
    name = name.replace("/", "-").replace("\\", "-").replace(":", "-")
    # Replace other potentially problematic characters
    invalid_chars = '<>""|?*'
    for char in invalid_chars:
        name = name.replace(char, "")

    # --- Extended Sanitization ---
    # Unicode mapping (example: replace smart quotes with standard quotes)
    unicode_map = {
        "“": '"',
        "”": '"',  # Double quotes
        "’": "'",
        "‘": "'",  # Single quotes
    }
    for uchar, replacement in unicode_map.items():
        name = name.replace(uchar, replacement)

    # OS-reserved name check (Windows example)
    if sys.platform == "win32":
        reserved_names = [
            "CON",
            "PRN",
            "AUX",
            "NUL",
            "COM1",
            "COM2",
            "COM3",
            "COM4",
            "COM5",
            "COM6",
            "COM7",
            "COM8",
            "COM9",
            "LPT1",
            "LPT2",
            "LPT3",
            "LPT4",
            "LPT5",
            "LPT6",
            "LPT7",
            "LPT8",
            "LPT9",
        ]
        if name.upper() in reserved_names:
            name = f"{name}_"  # Append underscore if reserved
    # Strip leading/trailing whitespace and limit length
    return name.strip()[:200]


def _calculate_end_pages_recursively(
    bookmarks: list[Bookmark], parent_end_page_boundary: int, num_pages: int
) -> None:
    """
    Recursively traverses the nested bookmark structure to calculate the end page for each entry.
    The end page of a bookmark is determined by the start of its next sibling, or its parent's boundary.
    """
    for i, bookmark in enumerate(bookmarks):
        # The boundary for the current bookmark is the start of its next sibling.
        if i + 1 < len(bookmarks):
            # The next sibling provides the end boundary (exclusive).
            next_sibling_start_page = bookmarks[i + 1]["page_index"]
        else:
            # This is the last item in the list, so its boundary is the parent's boundary.
            next_sibling_start_page = parent_end_page_boundary

        # If the bookmark has children, we must process them first to determine their ranges.
        # The children's world is bounded by this bookmark's next sibling.
        if bookmark.get("children"):
            _calculate_end_pages_recursively(
                bookmark["children"], next_sibling_start_page, num_pages
            )
            # After recursion, the children's end pages are calculated.
            # The parent's end page must be at least the end page of its last child.
            last_child_end_page = bookmark["children"][-1].get(
                "end_page_index", bookmark["page_index"]
            )
        else:
            last_child_end_page = -1  # No children, so no child range to consider.

        # The end page is one less than the start of the next section.
        end_page = next_sibling_start_page - 1

        # A parent's range should encompass its children's ranges.
        final_end_page = max(end_page, last_child_end_page)

        # Final sanity checks: end page cannot be before the start page or after the end of the document.
        bookmark["end_page_index"] = min(
            max(bookmark["page_index"], final_end_page), num_pages - 1
        )


def get_bookmark_structure_nested(pdf_path: Path) -> list[Bookmark]:
    """
    Extracts a nested structure of bookmarks from a PDF, including calculated start and end pages.
    """
    try:
        reader = PdfReader(pdf_path)
        if reader.is_encrypted:
            try:
                reader.decrypt("")
                logging.info("PDF decrypted successfully.")
            except Exception as decrypt_error:
                logging.error(
                    f"PDF is encrypted and could not be decrypted: {decrypt_error}"
                )
                return []

        outlines = reader.outline
        if not outlines:
            logging.warning(f"No bookmarks found in {pdf_path}.")
            return []

        logging.info(f"Found {len(outlines)} top-level bookmark items.")

        num_pages = len(reader.pages)
        processed_bookmarks: list[Bookmark] = []

        memo: dict[int, int | None] = {}

        def get_page_idx(page_obj):
            page_ref = getattr(page_obj, "indirect_reference", page_obj)
            obj_id = id(page_ref)

            if obj_id not in memo:
                try:
                    memo[obj_id] = reader.get_page_number(page_obj)
                    logging.debug(
                        f"Memoizing page number for object ID {obj_id}: {memo[obj_id]}"
                    )
                except Exception as e:
                    logging.warning(
                        f"Could not get page number for page object (type: {type(page_obj)}, repr: {repr(page_obj)}): {e}"
                    )
                    memo[obj_id] = None
            return memo[obj_id]

        def process_outline_recursive(
            items: list[Destination | list], level: int = 1
        ) -> list[Bookmark]:
            nested_list: list[Bookmark] = []
            if not items:
                return nested_list

            last_valid_bookmark_node: Bookmark | None = None

            for item in items:
                bookmark_data = None

                if isinstance(item, list):
                    logging.debug(f"Processing sub-list at level {level}")
                    children_from_list = process_outline_recursive(item, level + 1)
                    if last_valid_bookmark_node:
                        logging.debug(
                            f"Assigning {len(children_from_list)} children found in list to parent '{last_valid_bookmark_node['title']}'"
                        )
                        last_valid_bookmark_node["children"].extend(children_from_list)
                    else:
                        logging.debug(
                            f"Found {len(children_from_list)} children in a list, but no immediate parent bookmark node. Adding to current list."
                        )
                        nested_list.extend(children_from_list)

                elif isinstance(item, Destination) and hasattr(item, "title"):
                    try:
                        title = sanitize_filename(str(item.title))
                        page_index = None
                        if hasattr(item, "page"):
                            page_index = get_page_idx(item.page)
                        else:
                            logging.warning(
                                f"Bookmark '{title}' has no 'page' attribute."
                            )

                        if page_index is not None:
                            bookmark_data = {
                                "title": title,
                                "level": level,
                                "page_index": page_index,
                                "end_page_index": num_pages - 1,
                                "children": [],
                            }
                            logging.debug(
                                f"Processed bookmark: L{level} '{title}' at page index {page_index}"
                            )
                            nested_list.append(bookmark_data)
                            last_valid_bookmark_node = bookmark_data
                        else:
                            logging.warning(
                                f"Could not resolve page index for bookmark '{title}'. Skipping."
                            )

                    except Exception as e:
                        title_str = getattr(item, "title", "Unknown Title")
                        logging.warning(
                            f"Skipping bookmark '{title_str}' due to error during processing: {e}"
                        )
                else:
                    logging.warning(
                        f"Skipping unexpected item type in outline: {type(item)}"
                    )

            return nested_list

        processed_bookmarks = process_outline_recursive(outlines)

        logging.info("Calculating end pages for all bookmarks...")
        _calculate_end_pages_recursively(
            processed_bookmarks, parent_end_page_boundary=num_pages, num_pages=num_pages
        )

        def log_final_ranges(items, level=1):
            for item in items:
                start_idx = item.get("page_index", -1)
                end_idx = item.get("end_page_index", -1)
                logging.debug(
                    f"{'  ' * (level - 1)}L{level} '{item['title']}' -> Pages {start_idx + 1} to {end_idx + 1}"
                )
                if item.get("children"):
                    log_final_ranges(item["children"], level + 1)

        logging.debug("--- Final Calculated Page Ranges ---")
        log_final_ranges(processed_bookmarks)
        return processed_bookmarks

    except PdfReadError as e:
        logging.error(
            f"Failed to read PDF: {e}. The file may be corrupt or not a valid PDF."
        )
        return []
    except Exception as e:
        logging.error(
            f"An unexpected error occurred in get_bookmark_structure_nested: {e}"
        )
        return []


def _save_split_pdf(
    reader: PdfReader,
    output_path: Path,
    start_page: int,
    end_page: int,
    dry_run: bool = False,
) -> None:
    """Saves a page range from the reader to a new PDF file."""
    if dry_run:
        logging.info(
            f"[DRY RUN] Would create {output_path} for pages {start_page + 1}-{end_page + 1}"
        )
        return

    writer = PdfWriter()
    try:
        # Add pages from the specified range
        for i in range(start_page, end_page + 1):
            writer.add_page(reader.pages[i])

        # Create directory if it doesn't exist
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # Write the new PDF to the file
        with open(output_path, "wb") as f:
            writer.write(f)
        logging.info(f"Successfully created {output_path}")
    except IndexError:
        logging.error(
            f"Page index out of range for {output_path}. Start: {start_page}, End: {end_page}, Total Pages: {len(reader.pages)}"
        )
    except Exception as e:
        logging.error(f"Failed to write PDF {output_path}: {e}")


def _check_external_tool(tool_name: str, path: str) -> bool:
    """Checks if an external tool is available in PATH or at the specified path."""
    if shutil.which(path):
        return True
    logging.error(
        f"External tool '{tool_name}' not found. "
        f"Please ensure '{path}' is in your system's PATH or specify its full path in the application settings."
    )
    return False


def _create_output_directories(
    base_output_dir: Path, dry_run: bool
) -> tuple[Path, Path]:
    """
    Creates the main output directory and a subfolder for rasterized files.
    Returns the paths for the main output and the rasterized content directory.
    """
    # The main output directory is now the base directory provided.
    main_output_dir = base_output_dir
    # The rasterized directory is a subfolder within the main output directory.
    rasterized_dir = main_output_dir / "rasterized"

    if not dry_run:
        # Create both directories. `exist_ok=True` prevents errors if they already exist.
        main_output_dir.mkdir(parents=True, exist_ok=True)
        rasterized_dir.mkdir(parents=True, exist_ok=True)
        logging.info(f"Ensured output directory exists: {main_output_dir}")
        logging.info(f"Rasterized files will be saved in: {rasterized_dir}")
    else:
        logging.info(
            f"[DRY RUN] Would ensure output directory exists: {main_output_dir}"
        )
        logging.info(f"[DRY RUN] Would create rasterized directory: {rasterized_dir}")

    return main_output_dir, rasterized_dir


def split_pdf_by_bookmarks(
    pdf_path: Path,
    output_dir: Path,
    max_level: int = 0,
    flatten_output: bool = False,
    dry_run: bool = False,
    progress_callback=None,
    cancel_event=None,
    pause_event=None,
    create_parent_splits: bool = True,
) -> tuple[list[str], list[str]]:
    """
    Splits a PDF based on its bookmark structure and saves the smaller PDFs.
    Returns lists of successfully created and failed files.
    """
    if not pdf_path.exists():
        logging.error(f"Input PDF not found: {pdf_path}")
        return [], []

    # --- Create Output Directories ---
    main_output_dir, rasterized_output_dir = _create_output_directories(
        output_dir, dry_run
    )

    # --- Get Bookmark Structure ---
    bookmarks = get_bookmark_structure_nested(pdf_path)
    if not bookmarks:
        logging.warning("No bookmarks found or an error occurred while reading them.")
        return [], []

    # Save bookmarks to JSON for potential merging later
    # This file should always be saved in the main_output_dir, regardless of flatten_output
    bookmarks_file = main_output_dir / "_bookmarks.json"
    if not dry_run:
        try:
            # The parent directory (split_output_base_dir) is already created by _create_output_directories
            with open(bookmarks_file, "w", encoding="utf-8") as f:
                json.dump(bookmarks, f, indent=4, ensure_ascii=False)
            logging.info(f"Saved bookmark structure to {bookmarks_file}")
        except Exception as e:
            logging.error(f"Failed to save bookmarks file: {e}")

    # --- Prepare for Splitting ---
    try:
        reader = PdfReader(pdf_path)
        if reader.is_encrypted:
            reader.decrypt("")
    except Exception as e:
        logging.error(f"Could not read the source PDF: {e}")
        return [], []

    successful_files: list[str] = []
    failed_files: list[str] = []

    total_pages_to_process = [0]

    def count_pages(items):
        for item in items:
            if max_level == 0 or item["level"] <= max_level:
                total_pages_to_process[0] += (
                    item["end_page_index"] - item["page_index"] + 1
                )
            if item.get("children"):
                count_pages(item["children"])

    count_pages(bookmarks)
    processed_pages = 0

    # --- Recursive Splitting Function ---
    def process_level(
        items: list[Bookmark],
        current_path: Path,
    ):
        nonlocal processed_pages
        for i, bookmark in enumerate(items):
            # --- GUI Event Handling ---
            if cancel_event and cancel_event.is_set():
                logging.info("Cancellation requested. Stopping split.")
                return
            while pause_event and pause_event.is_set():
                time.sleep(0.1)

            # --- Level & Path Logic ---
            if max_level != 0 and bookmark["level"] > max_level:
                continue

            # Sanitize title for use in filename
            sanitized_title = sanitize_filename(bookmark["title"])
            if not sanitized_title:
                sanitized_title = f"Untitled_Bookmark_{i + 1}"

            # Determine the output path for this bookmark
            if flatten_output:
                # All files go into the main output directory
                file_path = main_output_dir / f"{sanitized_title}.pdf"
            else:
                # Files are nested according to bookmark structure within main_output_dir
                file_path = current_path / f"{sanitized_title}.pdf"

            # --- Save the PDF ---
            start_page = bookmark["page_index"]
            end_page = bookmark["end_page_index"]
            num_pages_in_split = end_page - start_page + 1

            # Determine if we should create a split for this parent bookmark.
            should_create_split = True
            if (
                not create_parent_splits
                and bookmark.get("children")
                and (max_level == 0 or bookmark["level"] < max_level)
            ):
                # Skip creating a PDF for parent bookmark when configured to do so
                should_create_split = False

            if should_create_split:
                try:
                    _save_split_pdf(reader, file_path, start_page, end_page, dry_run)
                    successful_files.append(str(file_path))
                    processed_pages += num_pages_in_split
                except Exception as e:
                    logging.error(
                        f"Failed to process bookmark '{bookmark['title']}': {e}"
                    )
                    failed_files.append(bookmark["title"])

            # --- Update Progress ---
            if progress_callback:
                progress = (
                    int((processed_pages / total_pages_to_process[0]) * 100)
                    if total_pages_to_process[0] > 0
                    else 0
                )
                progress_callback(progress, f"Processing: {bookmark['title']}")

            # --- Recurse into Children ---
            if bookmark.get("children"):
                next_path = (
                    current_path  # This is correct: if flatten, current_path is always unrasterized_output_dir
                    if flatten_output
                    else current_path / sanitize_filename(bookmark["title"])
                )
                process_level(bookmark["children"], next_path)

    # --- Start Processing ---
    logging.info(f"Starting PDF split for {pdf_path.name}...")
    if not dry_run:
        # The output directories are already created by _create_output_directories
        pass  # Remove redundant output_dir.mkdir() call

    process_level(bookmarks, main_output_dir)  # Start with main_output_dir

    # --- Final Progress Update ---
    if progress_callback:
        progress_callback(100, "Splitting complete.")

    return successful_files, failed_files


def _rasterize_single_pdf(
    pdf_path: Path,
    output_dir: Path,
    split_dir: Path,
    resolution: int,
    gs_path: str,
    magick_path: str,
    dry_run: bool = False,
    cancel_event: Any = None,
    pause_event: Any = None,
) -> str:
    """
    Rasterizes a single PDF file to a new PDF with embedded images using Ghostscript.
    Returns the path to the rasterized PDF.
    """
    if cancel_event and cancel_event.is_set():
        raise InterruptedError("Rasterization canceled.")
    while pause_event and pause_event.is_set():
        time.sleep(0.1)

    # Determine output path, preserving relative structure
    relative_path = pdf_path.relative_to(split_dir)
    output_pdf_path = (
        output_dir / relative_path.parent / f"{relative_path.stem}_rasterized.pdf"
    )

    if dry_run:
        logging.info(f"[DRY RUN] Would rasterize {pdf_path} to {output_pdf_path}")
        return str(output_pdf_path)

    # Create the specific output directory if it doesn't exist
    output_pdf_path.parent.mkdir(parents=True, exist_ok=True)

    # Using a temporary directory for intermediate image files
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_dir_path = Path(temp_dir)
        image_pattern = temp_dir_path / "page_%04d.jpg"

        # --- Ghostscript command to convert PDF to images ---
        gs_command = [
            gs_path,
            "-dQUIET",
            "-dSAFER",
            "-dBATCH",
            "-dNOPAUSE",
            "-sDEVICE=jpeg",
            f"-r{resolution}",
            f"-sOutputFile={image_pattern}",
            str(pdf_path),
        ]

        logging.debug(f"Executing Ghostscript: {' '.join(gs_command)}")
        try:
            return_code, stdout, stderr = run_subprocess(
                gs_command, timeout=600, cancel_event=cancel_event
            )
            if return_code != 0:
                raise subprocess.CalledProcessError(
                    return_code, gs_command, output=stdout, stderr=stderr
                )
        except subprocess.TimeoutExpired as e:
            error_msg = f"Ghostscript timed out for {pdf_path} after {e.timeout} seconds. Error: {e.stderr.strip() if e.stderr else ''}"
            logging.error(error_msg)
            raise RuntimeError(f"Ghostscript Timeout: {error_msg}")
        except subprocess.CalledProcessError as e:
            error_msg = f"Ghostscript failed for {pdf_path}. Error: {e.stderr.strip()}"
            logging.error(error_msg)
            raise RuntimeError(f"Ghostscript Error: {error_msg}")
        except FileNotFoundError:
            error_msg = f"Ghostscript executable not found at '{gs_path}'. Ensure it's installed and accessible."
            logging.error(error_msg)
            raise RuntimeError(f"Ghostscript Not Found: {error_msg}")
        except Exception as e:
            error_msg = f"An unexpected error occurred during Ghostscript processing for {pdf_path}: {e}"
            logging.error(error_msg)
            raise RuntimeError(f"Ghostscript General Error: {error_msg}")

        # --- ImageMagick command to merge images back into a PDF ---
        image_files = sorted(temp_dir_path.glob("page_*.jpg"))
        if not image_files:
            logging.warning(
                f"No images generated by Ghostscript for {pdf_path}. Skipping PDF creation."
            )
            return ""

        magick_command = (
            [
                magick_path,
                "convert",
            ]
            + [str(f) for f in image_files]
            + [
                str(output_pdf_path),
            ]
        )

        logging.debug(f"Executing ImageMagick: {' '.join(magick_command)}")
        try:
            return_code, stdout, stderr = run_subprocess(
                magick_command, timeout=600, cancel_event=cancel_event
            )
            if return_code != 0:
                raise subprocess.CalledProcessError(
                    return_code, magick_command, output=stdout, stderr=stderr
                )
        except subprocess.TimeoutExpired as e:
            error_msg = f"ImageMagick timed out for {pdf_path} after {e.timeout} seconds. Error: {e.stderr.strip() if e.stderr else ''}"
            logging.error(error_msg)
            raise RuntimeError(f"ImageMagick Timeout: {error_msg}")
        except subprocess.CalledProcessError as e:
            error_msg = f"ImageMagick failed for {pdf_path}. Error: {e.stderr.strip()}"
            logging.error(error_msg)
            raise RuntimeError(f"ImageMagick Error: {error_msg}")
        except FileNotFoundError:
            error_msg = f"ImageMagick executable not found at '{magick_path}'. Ensure it's installed and accessible."
            logging.error(error_msg)
            raise RuntimeError(f"ImageMagick Not Found: {error_msg}")
        except Exception as e:
            error_msg = f"An unexpected error occurred during ImageMagick processing for {pdf_path}: {e}"
            logging.error(error_msg)
            raise RuntimeError(f"ImageMagick General Error: {error_msg}")

    logging.info(f"Successfully rasterized {pdf_path} to {output_pdf_path}")
    return str(output_pdf_path)


def rasterize_pdf(
    pdf_files: list[str],
    output_dir: Path,
    split_dir: Path,
    resolution: int,
    workers: int,
    gs_path: str,
    magick_path: str,
    dry_run: bool = False,
    progress_callback=None,
    cancel_event: Any = None,
    pause_event: Any = None,
) -> tuple[list[str], list[str]]:
    """
    Rasterizes a list of PDF files in parallel.
    """
    successful_rasterizations = []
    failed_rasterizations = []

    total_pages = 0
    pages_per_file = {}
    for pdf_file in pdf_files:
        try:
            reader = PdfReader(pdf_file)
            num_pages = len(reader.pages)
            total_pages += num_pages
            pages_per_file[pdf_file] = num_pages
        except Exception as e:
            logging.warning(f"Could not read {pdf_file} to get page count: {e}")

    processed_pages = 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        future_to_pdf = {
            executor.submit(
                _rasterize_single_pdf,
                Path(pdf_file),
                output_dir,
                split_dir,
                resolution,
                gs_path,
                magick_path,
                dry_run,
                cancel_event,
                pause_event,
            ): pdf_file
            for pdf_file in pdf_files
        }

        for future in concurrent.futures.as_completed(future_to_pdf):
            pdf_file = future_to_pdf[future]
            if cancel_event and cancel_event.is_set():
                # Cancel remaining futures
                for f in future_to_pdf:
                    f.cancel()
                break

            try:
                result_path = future.result()
                if result_path:
                    successful_rasterizations.append(result_path)
            except Exception as e:
                logging.error(f"Rasterization failed for {pdf_file}: {e}")
                failed_rasterizations.append(pdf_file)

            if pdf_file in pages_per_file:
                processed_pages += pages_per_file[pdf_file]

            if progress_callback and total_pages > 0:
                progress = int((processed_pages / total_pages) * 100)
                progress_callback(progress, f"Rasterizing: {Path(pdf_file).name}")

    return successful_rasterizations, failed_rasterizations


def merge_pdfs(
    merge_dir: Path,
    output_pdf_path: Path | None = None,
    recreate_bookmarks: bool = True,
    dry_run: bool = False,
    progress_callback=None,
) -> tuple[list[str], list[str], dict]:
    """
    Merges all PDF files in a directory into a single PDF, with bookmark recreation,
    prioritizing order from _bookmarks.json if available.
    """
    if not output_pdf_path:
        output_pdf_path = merge_dir / "merged_output.pdf"

    # --- Load Bookmarks if requested ---
    bookmarks_structure = None
    bookmarks_file = merge_dir / "_bookmarks.json"
    if recreate_bookmarks and bookmarks_file.exists():
        try:
            with open(bookmarks_file, "r", encoding="utf-8") as f:
                bookmarks_structure = json.load(f)
            logging.info(f"Loaded bookmark structure from {bookmarks_file}")
        except Exception as e:
            logging.error(f"Failed to load bookmarks file {bookmarks_file}: {e}")
            recreate_bookmarks = False  # Disable bookmark recreation if load fails
    elif recreate_bookmarks:
        logging.warning(
            "'_bookmarks.json' not found. Cannot recreate bookmarks during merge."
        )
        recreate_bookmarks = False

    # --- Determine files to merge and their order ---
    files_to_merge_info: list[
        tuple[Path, str]
    ] = []  # (pdf_path, original_bookmark_title_for_offset)

    # Collect all PDF files in the directory for efficient lookup
    all_pdfs_in_merge_dir: dict[str, Path] = {}  # Map sanitized_title -> actual_path
    for p in merge_dir.rglob("*.pdf"):
        if not p.name.startswith("."):
            stem = p.stem.replace("_rasterized", "")
            all_pdfs_in_merge_dir[stem] = p

    if bookmarks_structure:
        # If bookmarks are present, use their order to determine merge sequence
        def collect_files_from_bookmarks(bookmarks_list):
            for bookmark in bookmarks_list:
                sanitized_title = sanitize_filename(bookmark["title"])
                # Try to find the file using the sanitized title (unrasterized or rasterized)
                found_path = all_pdfs_in_merge_dir.get(sanitized_title)
                if found_path:
                    files_to_merge_info.append((found_path, bookmark["title"]))
                else:
                    logging.warning(
                        f"PDF file for bookmark '{bookmark['title']}' (sanitized: '{sanitized_title}') not found in {merge_dir}. Skipping."
                    )
                if bookmark.get("children"):
                    collect_files_from_bookmarks(bookmark["children"])

        collect_files_from_bookmarks(bookmarks_structure)

        if not files_to_merge_info:
            logging.warning(
                "No PDF files found matching bookmarks. Merging all PDFs alphabetically."
            )
            # Fallback to alphabetical if bookmarks didn't yield any files
            for p in sorted(
                [p for p in merge_dir.rglob("*.pdf") if not p.name.startswith(".")]
            ):
                stem = p.stem.replace("_rasterized", "")
                files_to_merge_info.append(
                    (p, stem)
                )  # Use stem as "title" for offset if no bookmark
            recreate_bookmarks = False  # Cannot recreate bookmarks without structure
    else:
        # If no bookmarks or recreation is disabled, merge all PDFs alphabetically
        for p in sorted(
            [p for p in merge_dir.rglob("*.pdf") if not p.name.startswith(".")]
        ):
            stem = p.stem.replace("_rasterized", "")
            files_to_merge_info.append(
                (p, stem)
            )  # Use stem as "title" for offset if no bookmark
        recreate_bookmarks = (
            False  # Explicitly disable if not using bookmarks for order
        )

    if not files_to_merge_info:
        logging.warning(f"No PDF files found to merge in {merge_dir}")
        return [], [], {}

    if dry_run:
        logging.info(
            f"[DRY RUN] Would merge {len(files_to_merge_info)} PDFs into {output_pdf_path}"
        )
        for pdf_path, _ in files_to_merge_info:
            logging.info(f"  - {pdf_path}")
        return [str(p) for p, _ in files_to_merge_info], [], {}

    merger = PdfMerger()
    failed_files = []
    successful_files = []
    page_offsets = {}
    current_offset = 0

    total_pages_to_merge = 0
    for pdf_path, _ in files_to_merge_info:
        try:
            reader = PdfReader(pdf_path)
            total_pages_to_merge += len(reader.pages)
        except Exception:
            pass  # ignore if a file can't be read, it will fail later anyway

    processed_pages = 0

    for i, (pdf_path, original_bookmark_title) in enumerate(files_to_merge_info):
        try:
            # Get page count for bookmark offset calculation
            reader = PdfReader(pdf_path)
            num_pages = len(reader.pages)

            # Key page_offsets by the original bookmark title (sanitized)
            # This is crucial for matching with the bookmark structure later
            offset_key = sanitize_filename(original_bookmark_title)
            page_offsets[offset_key] = current_offset
            current_offset += num_pages

            # Append to merger
            merger.append(str(pdf_path))
            successful_files.append(str(pdf_path))
            processed_pages += num_pages

            if progress_callback and total_pages_to_merge > 0:
                progress = int((processed_pages / total_pages_to_merge) * 100)
                progress_callback(progress, f"Merging: {pdf_path.name}")

        except Exception as e:
            logging.error(f"Failed to process {pdf_path} for merging: {e}")
            failed_files.append(str(pdf_path))

    # --- Bookmark Recreation ---
    if recreate_bookmarks and bookmarks_structure:
        logging.info("Recreating bookmarks...")

        def add_bookmarks_recursive(bookmarks, parent=None):
            for b in bookmarks:
                sanitized_title = sanitize_filename(b["title"])

                # Ensure the sanitized title was actually merged and has an offset
                if sanitized_title in page_offsets:
                    page_num = page_offsets[sanitized_title]
                    new_bookmark = merger.add_bookmark(b["title"], page_num, parent)

                    if b.get("children"):
                        add_bookmarks_recursive(b["children"], new_bookmark)
                else:
                    logging.warning(
                        f"Could not find a merged PDF for bookmark: '{b['title']}' (Sanitized: '{sanitized_title}'). Skipping bookmark."
                    )

        add_bookmarks_recursive(bookmarks_structure)
    elif (
        recreate_bookmarks
    ):  # Should not happen if logic is correct, but as a safeguard
        logging.warning(
            "Bookmark recreation requested but _bookmarks.json was not loaded or found."
        )
    else:
        logging.info("Bookmark recreation not enabled or not possible.")

    # --- Write Final PDF ---

    try:
        with open(output_pdf_path, "wb") as f:
            merger.write(f)

        logging.info(
            f"Successfully merged {len(successful_files)} PDFs into {output_pdf_path}"
        )

    except Exception as e:
        logging.error(f"Failed to write merged PDF {output_pdf_path}: {e}")

        failed_files.extend([s for s in successful_files if s not in failed_files])

        successful_files = []

    finally:
        merger.close()

    report_data = {
        "operation_type": "merge",
        "output_file": str(output_pdf_path),
        "total_files_merged": len(successful_files),
        "success": [str(output_pdf_path)] if successful_files else [],
        "failures": failed_files,
    }

    return [str(output_pdf_path)] if successful_files else [], failed_files, report_data


def main_entry(
    args, progress_callback=None, cancel_event=None, pause_event=None, rasterize=True
):
    """Main entry point for GUI or direct script calls."""
    # --- Setup Logging ---
    log_level = logging.DEBUG if getattr(args, "verbose", False) else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=[logging.StreamHandler()],
    )

    # --- Argument Validation and Defaulting ---
    if hasattr(args, "merge") and args.merge:
        if hasattr(args, "input") and args.input:
            logging.error("Cannot use --merge with an input PDF file.")
            return [], [], {}
        args.input = None  # Ensure input is None for merge mode
    elif not hasattr(args, "input") or not args.input:
        logging.error("An input PDF file is required unless --merge is used.")
        return [], [], {}

    if (
        (not hasattr(args, "output") or not args.output)
        and hasattr(args, "input")
        and args.input
    ):
        args.output = Path(f"{args.input.stem}_output")

    if getattr(args, "dry_run", False):
        logging.info("--- DRY RUN MODE --- No files will be written.")

    # --- Execute Main Logic ---
    start_time = time.time()
    success_files, failed_files, report_data = [], [], {}

    try:
        if hasattr(args, "merge") and args.merge:
            # --- MERGE MODE ---
            logging.info(f"Starting merge operation on directory: {args.merge}")
            report_data["operation_type"] = "merge"
            success_files, failed_files, merge_report = merge_pdfs(
                args.merge,
                output_pdf_path=getattr(args, "merge_output", None),
                recreate_bookmarks=not getattr(args, "no_recreate_bookmarks", False),
                dry_run=getattr(args, "dry_run", False),
                progress_callback=progress_callback,
            )
            report_data.update(merge_report)  # Merge merge_pdfs's report data
        else:
            # --- SPLIT/RASTERIZE MODE ---
            if args.input is None:
                logging.error(
                    "Input PDF file is required for split/rasterize operation when --merge is not used."
                )
                report_data["error"] = "Input PDF file is required."
                report_data["operation_type"] = "split_rasterize"
                return [], [], report_data
            logging.info(f"Starting split operation for: {args.input}")
            report_data["operation_type"] = (
                "split_and_rasterize" if rasterize else "split"
            )

            # The base output directory is now used directly for splitting.
            output_for_splitting = args.output

            # The flatten_output flag from arguments is used for the *unrasterized* split files.
            flatten_output_for_splitting = getattr(args, "flatten_output", False)

            split_files, failed_splits = split_pdf_by_bookmarks(
                pdf_path=args.input,
                output_dir=output_for_splitting,
                max_level=getattr(args, "max_split_level", 0),
                flatten_output=flatten_output_for_splitting,
                dry_run=getattr(args, "dry_run", False),
                progress_callback=progress_callback,
                cancel_event=cancel_event,
                pause_event=pause_event,
                create_parent_splits=getattr(args, "create_parent_splits", True),
            )
            success_files.extend(split_files)
            failed_files.extend(failed_splits)

            # After splitting, the output directories are known directly.
            main_output_dir = output_for_splitting
            rasterized_dir = main_output_dir / "rasterized"

            if rasterize:
                # --- External Tool Checks for Rasterization ---
                gs_path = getattr(args, "gs_path", "gs")
                magick_path = getattr(args, "magick_path", "magick")
                if not _check_external_tool("Ghostscript", gs_path):
                    report_data["error"] = (
                        f"Ghostscript executable not found at '{gs_path}'."
                    )
                    report_data["failures"] = (
                        failed_splits  # Include any previous split failures
                    )
                    return [], [], report_data
                if not _check_external_tool("ImageMagick", magick_path):
                    report_data["error"] = (
                        f"ImageMagick executable not found at '{magick_path}'."
                    )
                    report_data["failures"] = (
                        failed_splits  # Include any previous split failures
                    )
                    return [], [], report_data

                # This block handles operations required when rasterization is active.

                # Copy bookmarks file to rasterized dir if it exists
                # This should always happen if rasterize is true, to preserve bookmarks for merged rasterized PDF.
                bookmarks_file_source = main_output_dir / "_bookmarks.json"
                if bookmarks_file_source.exists() and not getattr(
                    args, "dry_run", False
                ):
                    rasterized_dir.mkdir(
                        parents=True, exist_ok=True
                    )  # Ensure it exists if not dry_run
                    shutil.copy(
                        bookmarks_file_source, rasterized_dir / "_bookmarks.json"
                    )
                    logging.info(f"Copied _bookmarks.json to {rasterized_dir}")

                if not getattr(args, "dry_run", False):
                    # Determine workers, ensuring it's an integer
                    num_workers = getattr(args, "workers", os.cpu_count())
                    if num_workers is None:
                        num_workers = 1  # Fallback if os.cpu_count() returns None

                    logging.info("Starting rasterization...")
                    rasterized_files, failed_rasterizations = rasterize_pdf(
                        pdf_files=split_files,
                        output_dir=rasterized_dir,  # Rasterized PDFs go here
                        split_dir=main_output_dir,  # Unrasterized source PDFs are here
                        resolution=getattr(args, "resolution", RASTERIZE_RESOLUTION),
                        workers=num_workers,  # Now explicitly an int
                        gs_path=gs_path,  # Pass the checked path
                        magick_path=magick_path,  # Pass the checked path
                        dry_run=getattr(args, "dry_run", False),
                        progress_callback=progress_callback,
                        cancel_event=cancel_event,
                        pause_event=pause_event,
                    )
                    success_files.extend(rasterized_files)
                    failed_files.extend(failed_rasterizations)

                    # Cleanup for individual unrasterized PDFs if keep_originals is False
                    if not getattr(args, "keep_originals", False):
                        logging.info(
                            "Cleaning up original split PDFs after rasterization..."
                        )
                        for pdf_file_path_str in split_files:
                            try:
                                Path(pdf_file_path_str).unlink(missing_ok=True)
                                logging.debug(
                                    f"Removed original split PDF: {pdf_file_path_str}"
                                )
                            except OSError as e:
                                logging.warning(
                                    f"Could not remove original split PDF {pdf_file_path_str}: {e}"
                                )
                else:  # Dry run for rasterization
                    logging.info("Dry run: Would start rasterization for split PDFs.")
            else:  # Not rasterizing, just splitting
                logging.info(
                    f"PDF split complete. Unrasterized PDFs are in: {main_output_dir}"
                )
                # No specific actions needed here as split_pdf_by_bookmarks already handled creation.
                # The 'split_files' list already contains the paths to the unrasterized PDFs.

            # Prepare report data for split/rasterize
            report_data["output_file"] = (
                str(main_output_dir) if not rasterize else str(rasterized_dir)
            )
            report_data["total_files_processed"] = len(success_files) + len(
                failed_files
            )
            report_data["success"] = success_files
            report_data["failures"] = failed_files

    except Exception as e:
        logging.error(f"An unexpected error occurred: {e}", exc_info=True)
        report_data["error"] = str(e)
        report_data["operation_type"] = (
            "merge" if hasattr(args, "merge") and args.merge else "split_rasterize"
        )

    finally:
        # --- Generate Report ---
        end_time = time.time()
        duration = end_time - start_time
        logging.info(f"Operation finished in {duration:.2f} seconds.")
        logging.info(f"  - Success: {len(success_files)} files")
        logging.info(f"  - Failures: {len(failed_files)} files")

        if hasattr(args, "html_report") and args.html_report:
            report_data["duration"] = duration
            # generate_html_report(report_data, args.html_report) # Function not found, commented out
        # Attach timing information to report_data for GUI consumption.
        try:
            report_data.setdefault("start_time", start_time)
            report_data.setdefault("end_time", end_time)
            # prefer explicit duration key if already present, otherwise set elapsed_time
            report_data.setdefault("elapsed_time", float(duration))
        except Exception:
            # Defensive: if conversion fails, still return report_data without timing
            pass
    return success_files, failed_files, report_data


def main_cli():
    """Main function to parse arguments and orchestrate the PDF processing."""
    parser = argparse.ArgumentParser(
        description="Split a PDF by its bookmarks and optionally rasterize the output.",
        formatter_class=argparse.RawTextHelpFormatter,
    )

    # --- Input/Output Arguments ---
    parser.add_argument(
        "input",
        type=Path,
        nargs="?",
        help="Path to the input PDF file. Required unless --merge is used.",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Path to the output directory. Defaults to a folder named after the PDF.",
    )

    # --- Splitting Arguments ---
    split_group = parser.add_argument_group("Splitting Options")
    split_group.add_argument(
        "--max-split-level",
        type=int,
        default=0,
        help="Maximum bookmark depth to split. 0 for unlimited. Default is 0.",
    )
    split_group.add_argument(
        "--flatten-output",
        action="store_true",
        help="Store all output files in a single directory, ignoring the bookmark hierarchy.",
    )

    # --- Rasterization Arguments ---
    raster_group = parser.add_argument_group("Rasterization Options")
    raster_group.add_argument(
        "-r",
        "--rasterize",
        action="store_true",
        help="Rasterize the split PDF files into images (e.g., JPG).",
    )
    raster_group.add_argument(
        "--resolution",
        type=int,
        default=RASTERIZE_RESOLUTION,
        help=f"DPI resolution for rasterization. Default is {RASTERIZE_RESOLUTION}.",
    )
    raster_group.add_argument(
        "--keep-originals",
        action="store_true",
        help="Do not delete the intermediate split PDF files after rasterization.",
    )

    # --- Merging Arguments ---
    merge_group = parser.add_argument_group("Merging Options")
    merge_group.add_argument(
        "-m",
        "--merge",
        type=Path,
        metavar="MERGE_DIR",
        help="Instead of splitting, merge rasterized images from a directory back into a PDF.",
    )
    merge_group.add_argument(
        "--merge-output",
        type=Path,
        help="Filename for the merged PDF. Defaults to 'merged_output.pdf' in the merge directory.",
    )
    merge_group.add_argument(
        "--no-recreate-bookmarks",
        action="store_true",
        help="Do not recreate bookmarks from the _bookmarks.json file during merge.",
    )

    # --- General Arguments ---
    general_group = parser.add_argument_group("General Options")
    general_group.add_argument(
        "-w",
        "--workers",
        type=int,
        default=os.cpu_count()
        if os.cpu_count() is not None
        else 1,  # Ensure default is an int
        help="Number of worker processes for parallel tasks. Defaults to the number of CPU cores, or 1 if not detectable.",
    )
    general_group.add_argument(
        "--gs-path",
        type=str,
        default="gs",
        help="Path to the Ghostscript executable (gs).",
    )
    general_group.add_argument(
        "--magick-path",
        type=str,
        default="magick",
        help="Path to the ImageMagick executable (magick).",
    )
    general_group.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate the process without creating or modifying files.",
    )
    general_group.add_argument(
        "-v", "--verbose", action="store_true", help="Enable verbose logging output."
    )
    general_group.add_argument(
        "--html-report",
        type=Path,
        metavar="REPORT_PATH",
        help="Generate an HTML report of the operation at the specified path.",
    )

    args = parser.parse_args()
    main_entry(args, rasterize=args.rasterize)


if __name__ == "__main__":
    main_cli()
