#!/usr/bin/env python3
import os
import subprocess
import shutil
import logging
import tempfile
import argparse
import time
import json
import concurrent.futures
import threading
from pathlib import Path
from typing import Any # Using Any for complex bookmark structures for now

# PyPDF2 is used for reading/writing PDFs and handling bookmarks
from PyPDF2 import PdfReader, PdfWriter
from PyPDF2.errors import PdfReadError
# Import Destination for type checking bookmarks
from PyPDF2.generic import Destination
import sys

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
    name = name.replace('/', '-').replace('\\', '-').replace(':', '-')
    # Replace other potentially problematic characters
    invalid_chars = '<>""|?*'
    for char in invalid_chars:
        name = name.replace(char, '')

    # --- Extended Sanitization ---
    # Unicode mapping (example: replace smart quotes with standard quotes)
    unicode_map = {
        '“': '"', '”': '"',  # Double quotes
        "’": "'", "‘": "'"   # Single quotes
    }
    for uchar, replacement in unicode_map.items():
        name = name.replace(uchar, replacement)

    # OS-reserved name check (Windows example)
    if sys.platform == 'win32':
        reserved_names = ["CON", "PRN", "AUX", "NUL", "COM1", "COM2", "COM3", "COM4",
                          "COM5", "COM6", "COM7", "COM8", "COM9", "LPT1", "LPT2",
                          "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9"]
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
            next_sibling_start_page = bookmarks[i+1]["page_index"]
        else:
            # This is the last item in the list, so its boundary is the parent's boundary.
            next_sibling_start_page = parent_end_page_boundary

        # If the bookmark has children, we must process them first to determine their ranges.
        # The children's world is bounded by this bookmark's next sibling.
        if bookmark.get("children"):
            _calculate_end_pages_recursively(bookmark["children"], next_sibling_start_page, num_pages)
            # After recursion, the children's end pages are calculated.
            # The parent's end page must be at least the end page of its last child.
            last_child_end_page = bookmark["children"][-1].get("end_page_index", bookmark["page_index"])
        else:
            last_child_end_page = -1 # No children, so no child range to consider.

        # The end page is one less than the start of the next section.
        end_page = next_sibling_start_page - 1

        # A parent's range should encompass its children's ranges.
        final_end_page = max(end_page, last_child_end_page)

        # Final sanity checks: end page cannot be before the start page or after the end of the document.
        bookmark["end_page_index"] = min(max(bookmark["page_index"], final_end_page), num_pages - 1)


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
                logging.error(f"PDF is encrypted and could not be decrypted: {decrypt_error}")
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
            page_ref = getattr(page_obj, 'indirect_reference', page_obj)
            obj_id = id(page_ref)

            if obj_id not in memo:
                try:
                    memo[obj_id] = reader.get_page_number(page_obj)
                    logging.debug(f"Memoizing page number for object ID {obj_id}: {memo[obj_id]}")
                except Exception as e:
                    logging.warning(f"Could not get page number for page object (type: {type(page_obj)}, repr: {repr(page_obj)}): {e}")
                    memo[obj_id] = None
            return memo[obj_id]

        def process_outline_recursive(items: list[Destination | list], level: int = 1) -> list[Bookmark]:
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
                         logging.debug(f"Assigning {len(children_from_list)} children found in list to parent '{last_valid_bookmark_node['title']}'")
                         last_valid_bookmark_node["children"].extend(children_from_list)
                    else:
                         logging.debug(f"Found {len(children_from_list)} children in a list, but no immediate parent bookmark node. Adding to current list.")
                         nested_list.extend(children_from_list)

                elif isinstance(item, Destination) and hasattr(item, 'title'):
                    try:
                        title = sanitize_filename(str(item.title))
                        page_index = None
                        if hasattr(item, 'page'):
                             page_index = get_page_idx(item.page)
                        else:
                             logging.warning(f"Bookmark '{title}' has no 'page' attribute.")

                        if page_index is not None:
                            bookmark_data = {
                                "title": title,
                                "level": level,
                                "page_index": page_index,
                                "end_page_index": num_pages - 1,
                                "children": []
                            }
                            logging.debug(f"Processed bookmark: L{level} '{title}' at page index {page_index}")
                            nested_list.append(bookmark_data)
                            last_valid_bookmark_node = bookmark_data
                        else:
                             logging.warning(f"Could not resolve page index for bookmark '{title}'. Skipping.")

                    except Exception as e:
                        title_str = getattr(item, 'title', 'Unknown Title')
                        logging.warning(f"Skipping bookmark '{title_str}' due to error during processing: {e}")
                else:
                    logging.warning(f"Skipping unexpected item type in outline: {type(item)}")

            return nested_list

        processed_bookmarks = process_outline_recursive(outlines)

        logging.info("Calculating end pages for all bookmarks...")
        _calculate_end_pages_recursively(processed_bookmarks, parent_end_page_boundary=num_pages, num_pages=num_pages)

        def log_final_ranges(items, level=1):
            for item in items:
                start_idx = item.get('page_index', -1)
                end_idx = item.get('end_page_index', -1)
                logging.debug(f"{'  ' * (level-1)}L{level} '{item['title']}' -> Pages {start_idx + 1} to {end_idx + 1}")
                if item.get("children"):
                    log_final_ranges(item["children"], level + 1)

        logging.debug("--- Final Calculated Page Ranges ---")
        log_final_ranges(processed_bookmarks)
        return processed_bookmarks

    except PdfReadError as e:
        logging.error(f"Failed to read PDF: {e}. The file may be corrupt or not a valid PDF.")
        return []
    except Exception as e:
        logging.error(f"An unexpected error occurred in get_bookmark_structure_nested: {e}")
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
        logging.info(f"[DRY RUN] Would create {output_path} for pages {start_page + 1}-{end_page + 1}")
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
        logging.error(f"Page index out of range for {output_path}. Start: {start_page}, End: {end_page}, Total Pages: {len(reader.pages)}")
    except Exception as e:
        logging.error(f"Failed to write PDF {output_path}: {e}")


def split_pdf_by_bookmarks(
    pdf_path: Path,
    output_dir: Path,
    max_level: int = 0,
    flatten_output: bool = False,
    dry_run: bool = False,
    progress_callback=None,
    cancel_event=None,
    pause_event=None,
) -> tuple[list[str], list[str]]:
    """
    Splits a PDF based on its bookmark structure and saves the smaller PDFs.
    Returns lists of successfully created and failed files.
    """
    if not pdf_path.exists():
        logging.error(f"Input PDF not found: {pdf_path}")
        return [], []

    # --- Get Bookmark Structure ---
    bookmarks = get_bookmark_structure_nested(pdf_path)
    if not bookmarks:
        logging.warning("No bookmarks found or an error occurred while reading them.")
        return [], []

    # --- Prepare for Splitting ---
    try:
        reader = PdfReader(pdf_path)
        if reader.is_encrypted:
            reader.decrypt('')
    except Exception as e:
        logging.error(f"Could not read the source PDF: {e}")
        return [], []

    successful_files: list[str] = []
    failed_files: list[str] = []
    total_bookmarks_to_process = [0] # Use a list to make it mutable inside the closure

    def count_bookmarks(items):
        for item in items:
            if max_level == 0 or item["level"] <= max_level:
                total_bookmarks_to_process[0] += 1
            if item.get("children"):
                count_bookmarks(item["children"])

    count_bookmarks(bookmarks)
    processed_count = 0

    # --- Recursive Splitting Function ---
    def process_level(
        items: list[Bookmark],
        current_path: Path,
    ):
        nonlocal processed_count
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
                sanitized_title = f"Untitled_Bookmark_{i+1}"

            # Determine the output path for this bookmark
            if flatten_output:
                # All files go into the root output directory
                file_path = output_dir / f"{sanitized_title}.pdf"
            else:
                # Files are nested according to bookmark structure
                file_path = current_path / f"{sanitized_title}.pdf"

            # --- Save the PDF ---
            start_page = bookmark["page_index"]
            end_page = bookmark["end_page_index"]

            try:
                _save_split_pdf(reader, file_path, start_page, end_page, dry_run)
                successful_files.append(str(file_path))
            except Exception as e:
                logging.error(f"Failed to process bookmark '{bookmark['title']}': {e}")
                failed_files.append(bookmark["title"])

            # --- Update Progress ---
            processed_count += 1
            if progress_callback:
                progress = int((processed_count / total_bookmarks_to_process[0]) * 100)
                progress_callback(progress, f"Processing: {bookmark['title']}")

            # --- Recurse into Children ---
            if bookmark.get("children"):
                next_path = current_path / sanitize_filename(bookmark["title"])
                if flatten_output:
                    # If flattening, the path for children remains the root
                    process_level(bookmark["children"], current_path)
                else:
                    # Otherwise, descend into a new subdirectory
                    process_level(bookmark["children"], next_path)

    # --- Start Processing ---
    logging.info(f"Starting PDF split for {pdf_path.name}...")
    if not dry_run:
        output_dir.mkdir(parents=True, exist_ok=True)

    process_level(bookmarks, output_dir)

    # --- Final Progress Update ---
    if progress_callback:
        progress_callback(100, "Splitting complete.")

    return successful_files, failed_files


def _rasterize_single_pdf(
    pdf_path: Path,
    resolution: int,
    gs_path: str,
    magick_path: str,
    dry_run: bool = False,
    cancel_event: threading.Event | None = None,
    pause_event: threading.Event | None = None,
) -> str:
    """
    Rasterizes a single PDF file to a new PDF with embedded images using Ghostscript.
    Returns the path to the rasterized PDF.
    """
    if cancel_event and cancel_event.is_set():
        raise InterruptedError("Rasterization canceled.")

    output_pdf_path = pdf_path.with_name(f"{pdf_path.stem}_rasterized.pdf")

    if dry_run:
        logging.info(f"[DRY RUN] Would rasterize {pdf_path} to {output_pdf_path}")
        return str(output_pdf_path)

    # Using a temporary directory for intermediate image files
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_dir_path = Path(temp_dir)
        image_pattern = temp_dir_path / "page_%04d.jpg"

        # --- Ghostscript command to convert PDF to images ---
        gs_command = [
            gs_path,
            "-dSAFER",
            "-dBATCH",
            "-dNOPAUSE",
            "-sDEVICE=jpeg",
            f"-r{resolution}",
            f'-sOutputFile={image_pattern}',
            str(pdf_path),
        ]

        logging.debug(f"Executing Ghostscript: {' '.join(gs_command)}")
        try:
            subprocess.run(gs_command, check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as e:
            logging.error(f"Ghostscript failed for {pdf_path}. Error: {e.stderr}")
            raise

        # --- ImageMagick command to merge images back into a PDF ---
        image_files = sorted(temp_dir_path.glob("page_*.jpg"))
        if not image_files:
            logging.warning(f"No images generated by Ghostscript for {pdf_path}. Skipping PDF creation.")
            return ""

        magick_command = [
            magick_path,
            "convert",
        ] + [str(f) for f in image_files] + [
            str(output_pdf_path),
        ]

        logging.debug(f"Executing ImageMagick: {' '.join(magick_command)}")
        try:
            subprocess.run(magick_command, check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as e:
            logging.error(f"ImageMagick failed for {pdf_path}. Error: {e.stderr}")
            raise

    logging.info(f"Successfully rasterized {pdf_path} to {output_pdf_path}")
    return str(output_pdf_path)


def rasterize_pdf(
    pdf_files: list[str],
    resolution: int,
    workers: int,
    gs_path: str,
    magick_path: str,
    dry_run: bool = False,
    progress_callback=None,
    cancel_event: threading.Event | None = None,
    pause_event: threading.Event | None = None,
) -> tuple[list[str], list[str]]:
    """
    Rasterizes a list of PDF files in parallel.
    """
    successful_rasterizations = []
    failed_rasterizations = []
    total_files = len(pdf_files)
    processed_count = 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        future_to_pdf = {
            executor.submit(
                _rasterize_single_pdf,
                Path(pdf_file),
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
            
            processed_count += 1
            if progress_callback:
                progress = int((processed_count / total_files) * 100)
                progress_callback(progress, f"Rasterizing: {Path(pdf_file).name}")

    return successful_rasterizations, failed_rasterizations


def main_entry(args, progress_callback=None, cancel_event=None, pause_event=None, rasterize=True):
    """Main entry point for GUI or direct script calls."""
    # --- Setup Logging ---
    log_level = logging.DEBUG if getattr(args, 'verbose', False) else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=[logging.StreamHandler()],
    )

    # --- Argument Validation and Defaulting ---
    if hasattr(args, 'merge') and args.merge:
        if hasattr(args, 'input') and args.input:
            logging.error("Cannot use --merge with an input PDF file.")
            return [], [], {}
        args.input = None  # Ensure input is None for merge mode
    elif not hasattr(args, 'input') or not args.input:
        logging.error("An input PDF file is required unless --merge is used.")
        return [], [], {}

    if (not hasattr(args, 'output') or not args.output) and hasattr(args, 'input') and args.input:
        args.output = Path(f"{args.input.stem}_output")

    if getattr(args, 'dry_run', False):
        logging.info("--- DRY RUN MODE --- No files will be written.")

    # --- Execute Main Logic ---
    start_time = time.time()
    success_files, failed_files, report_data = [], [], {}

    try:
        if hasattr(args, 'merge') and args.merge:
            # --- MERGE MODE ---
            logging.info(f"Starting merge operation on directory: {args.merge}")
            success_files, failed_files, report_data = merge_pdfs(
                args.merge,
                output_pdf_path=getattr(args, 'merge_output', None),
                recreate_bookmarks=not getattr(args, 'no_recreate_bookmarks', False),
                dry_run=getattr(args, 'dry_run', False),
            )
        else:
            # --- SPLIT/RASTERIZE MODE ---
            logging.info(f"Starting split operation for: {args.input}")
            split_files, failed_splits = split_pdf_by_bookmarks(
                pdf_path=args.input,
                output_dir=args.output,
                max_level=getattr(args, 'max_split_level', 0),
                flatten_output=getattr(args, 'flatten_output', False),
                dry_run=getattr(args, 'dry_run', False),
                progress_callback=progress_callback, # Pass callbacks
                cancel_event=cancel_event,
                pause_event=pause_event,
            )
            success_files.extend(split_files)
            failed_files.extend(failed_splits)

            if rasterize and not getattr(args, 'dry_run', False):
                logging.info("Starting rasterization...")
                rasterized_files, failed_rasterizations = rasterize_pdf(
                    split_files,
                    resolution=getattr(args, 'resolution', RASTERIZE_RESOLUTION),
                    workers=getattr(args, 'workers', os.cpu_count()),
                    gs_path=getattr(args, 'gs_path', 'gs'),
                    magick_path=getattr(args, 'magick_path', 'magick'),
                    dry_run=getattr(args, 'dry_run', False),
                    progress_callback=progress_callback, # Pass callbacks
                    cancel_event=cancel_event,
                    pause_event=pause_event,
                )
                success_files.extend(rasterized_files)
                failed_files.extend(failed_rasterizations)

                if not getattr(args, 'keep_originals', False):
                    logging.info("Cleaning up original split PDFs...")
                    for pdf_file in split_files:
                        if cancel_event and cancel_event.is_set(): break
                        try:
                            os.remove(pdf_file)
                            logging.debug(f"Removed {pdf_file}")
                        except OSError as e:
                            logging.warning(f"Could not remove {pdf_file}: {e}")

            # Prepare report data for split/rasterize
            report_data = {
                "operation_type": "split_rasterize" if rasterize else "split_only",
                "input_file": str(args.input),
                "output_directory": str(args.output),
                "total_files_processed": len(success_files) + len(failed_files),
                "success": [str(f) for f in success_files],
                "failures": [str(f) for f in failed_files],
                "start_time": start_time,
            }

    except Exception as e:
        logging.critical(f"A critical error occurred: {e}", exc_info=True)

    finally:
        # --- Generate Report ---
        end_time = time.time()
        duration = end_time - start_time
        logging.info(f"Operation finished in {duration:.2f} seconds.")
        logging.info(f"  - Success: {len(success_files)} files")
        logging.info(f"  - Failures: {len(failed_files)} files")

        if hasattr(args, 'html_report') and args.html_report:
            report_data["duration"] = duration
            generate_html_report(report_data, args.html_report)

    return success_files, failed_files, report_data

def main():
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
        default=os.cpu_count(),
        help="Number of worker processes for parallel tasks. Defaults to the number of CPU cores.",
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
    main()
