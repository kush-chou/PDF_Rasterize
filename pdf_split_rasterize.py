#!/usr/bin/env python3
import os
import subprocess
import shutil # Added for shutil.which
import logging
import tempfile
import argparse
import time
import json
import concurrent.futures
import threading # Added for threading.Event type hint
from pathlib import Path
from typing import List, Dict, Optional, Union, Tuple # Added for type hints
# pypdf is used for reading/writing PDFs and handling bookmarks
from pypdf import PdfReader, PdfWriter
from pypdf.errors import PdfReadError
# Import Destination for type checking bookmarks
from pypdf.generic import Destination
import sys # Added for sys.platform

# --- Global Configuration ---
RASTERIZE_RESOLUTION = 300 # DPI for rasterization
CLEANUP_ORIGINAL_SPLIT_PDFS = True # Set to False via --keep-originals flag if needed
# --- Type Aliases ---
FilePath = Union[str, Path]
Bookmark = Dict[str, Union[str, int, List['Bookmark']]]
# --- Function to sanitize filenames ---
def sanitize_filename(name: Union[str, Path]) -> str:
    """Removes or replaces characters invalid for filenames/paths."""
    name = str(name) # Ensure it's a string
    # Remove characters that are problematic in paths
    name = name.replace('/', '-').replace('\\', '-').replace(':', '-')
    # Replace other potentially problematic characters
    invalid_chars = '<>"|?*'
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
    if sys.platform == 'win32':  # Check if OS is Windows
        reserved_names = ["CON", "PRN", "AUX", "NUL", "COM1", "COM2", "COM3", "COM4", 
                          "COM5", "COM6", "COM7", "COM8", "COM9", "LPT1", "LPT2", 
                          "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9"]
        if name.upper() in reserved_names:
            name = f"{name}_"  # Append underscore if reserved
    # Strip leading/trailing whitespace and limit length
    return name.strip()[:200]

def _calculate_end_pages_recursively(bookmarks: List[Dict], parent_end_page_boundary: int, num_pages: int) -> None:
    """
    Recursively traverses the nested bookmark structure to calculate the end page for each entry.
    The end page of a bookmark is determined by the start of its next sibling, or its parent's boundary.
    A parent's range must encompass all its children.
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

        # The final end page is the greater of the calculated end page (from sibling) and the end page of its last child.
        final_end_page = max(end_page, last_child_end_page)

        # Final sanity checks: end page cannot be before the start page or after the end of the document.
        bookmark["end_page_index"] = min(max(bookmark["page_index"], final_end_page), num_pages - 1)

def get_bookmark_structure_nested(pdf_path: Path) -> List[Dict]:
    """
    Reads the PDF outlines (bookmarks) and returns a NESTED list structure
    preserving the hierarchy. Each item contains:
    title, level, start_page_index, end_page_index, children (list).
    """
    logging.info(f"Attempting to read bookmarks from: {pdf_path}")
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
            logging.warning(f"No bookmarks found in {pdf_path}")
            return []
        logging.info(f"Found {len(outlines)} top-level bookmark items.")

        num_pages = len(reader.pages)
        processed_bookmarks = [] # To store the final nested structure

        # --- Helper to get page index ---
        memo: Dict[int, Optional[int]] = {} # Memoization cache for page objects to page numbers
        def get_page_idx(page_obj):
            # Use object's indirect reference if available, otherwise object itself
            # This can help if pypdf gives indirect objects for pages sometimes
            page_ref = getattr(page_obj, 'indirect_reference', page_obj)
            obj_id = id(page_ref) # Use ID of the reference or the object

            if obj_id not in memo:
                try:
                    # Pass the original page_obj to get_page_number
                    memo[obj_id] = reader.get_page_number(page_obj)
                    logging.debug(f"Memoizing page number for object ID {obj_id}: {memo[obj_id]}")
                except Exception as e:
                    # Log the type of object and its representation if possible
                    logging.warning(f"Could not get page number for page object (type: {type(page_obj)}, repr: {repr(page_obj)}): {e}")
                    memo[obj_id] = None
            return memo[obj_id]

        # --- Recursive helper to process outlines ---
        def process_outline_recursive(items: List[Union[Destination, List]], level: int = 1) -> List[Dict]:
            nested_list = []
            if not items:
                return nested_list

            # Track the parent bookmark's data to potentially assign children later
            # This structure might need adjustment based on pypdf's exact outline format
            last_valid_bookmark_node = None

            for item in items:
                bookmark_data = None
                children_from_list = [] # Children found in a sub-list

                # Case 1: Item is a list -> contains children/siblings
                if isinstance(item, list):
                    logging.debug(f"Processing sub-list at level {level}")
                    # Process these children recursively. They belong to the *previous* valid bookmark found at this level.
                    children_from_list = process_outline_recursive(item, level + 1) # Children are one level deeper
                    # Assign these children to the last valid bookmark node found at the current level
                    if last_valid_bookmark_node:
                         logging.debug(f"Assigning {len(children_from_list)} children found in list to parent '{last_valid_bookmark_node['title']}'")
                         last_valid_bookmark_node["children"].extend(children_from_list)
                    else:
                         # This case might happen if a list appears before any valid bookmark at this level
                         # Or if the structure is [Destination, [child1, child2], Destination2]
                         # We might need to add them directly to the current nested_list if they are top-level orphans
                         logging.debug(f"Found {len(children_from_list)} children in a list, but no immediate parent bookmark node at this level. Adding to current list.")
                         nested_list.extend(children_from_list) # Add as siblings if no parent context

                # Case 2: Item is a Destination -> a bookmark entry
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
                                "level": level, # Actual level in the hierarchy
                                "page_index": page_index,
                                "end_page_index": num_pages - 1, # Default end page
                                "children": [] # Placeholder for potential children from subsequent lists
                            }
                            logging.debug(f"Processed bookmark: L{level} '{title}' at page index {page_index}")
                            nested_list.append(bookmark_data)
                            last_valid_bookmark_node = bookmark_data # Remember this node for potential children
                        else:
                             logging.warning(f"Could not resolve page index for bookmark '{title}'. Skipping.")

                    except Exception as e:
                        title_str = getattr(item, 'title', 'Unknown Title')
                        logging.warning(f"Skipping bookmark '{title_str}' due to error during processing: {e}")
                # Case 3: Unexpected item type
                else:
                    logging.warning(f"Skipping unexpected item type in outline: {type(item)}")

            # Note: End page calculation is deferred until the full tree is built

            return nested_list

        # --- Process the entire outline ---
        logging.debug("Starting recursive outline processing...")
        processed_bookmarks = process_outline_recursive(outlines, level=1)
        logging.debug("Finished recursive outline processing.")
        # Optional: Pretty print the raw nested structure for debugging
        # logging.debug(f"Raw nested structure:\n{pprint.pformat(processed_bookmarks)}")

        # --- Hierarchical End Page Calculation ---
        logging.info("Calculating end pages for all bookmarks...")
        _calculate_end_pages_recursively(processed_bookmarks, parent_end_page_boundary=num_pages, num_pages=num_pages)
        # --- Log the final calculated ranges for debugging ---
        def log_final_ranges(items, level=1):
            for item in items:
                start_idx = item.get('page_index', -1)
                end_idx = item.get('end_page_index', -1)
                logging.debug(f"{'  ' * (level-1)}L{level} '{item['title']}' -> Pages {start_idx + 1} to {end_idx + 1}")
                if item.get("children"):
                    log_final_ranges(item["children"], level + 1)
        
        logging.debug("--- Final Calculated Page Ranges ---")
        log_final_ranges(processed_bookmarks)
        logging.debug("--- End Final Calculated Page Ranges ---")

        # Count the total number of bookmarks processed
        final_bookmark_count = 0
        def count_bookmarks(items):
            nonlocal final_bookmark_count
            for item in items:
                final_bookmark_count += 1
                if item.get("children"):
                    count_bookmarks(item["children"])
        count_bookmarks(processed_bookmarks)

        logging.info(f"Processed {final_bookmark_count} valid bookmarks into nested structure.")
        return processed_bookmarks

    except PdfReadError as e:
        logging.error(f"Error reading PDF {pdf_path}: {e}")
        return []
    except Exception as e:
        logging.error(f"An unexpected error occurred while processing bookmarks for {pdf_path}: {e}", exc_info=True) # Add traceback
        return []


def split_pdf_recursive(reader, bookmarks, current_path, dry_run=False, cancel_event=None, pause_event=None, max_split_level=0, create_parent_pdfs=False):
    """
    Recursively splits the PDF based on the NESTED bookmark structure.
    If max_split_level is set, it rasterizes bookmarks at that level, or any bookmark
    at a lower level that doesn't have children. It skips creating PDFs for parent
    bookmarks that have children, processing them recursively instead.
    """
    num_pages_total = len(reader.pages)

    if not bookmarks:
        logging.debug(f"No bookmarks to process at path: {current_path}")
        return

    for bookmark in bookmarks:
        if pause_event:
            pause_event.wait()

        if cancel_event and cancel_event.is_set():
            logging.warning("Cancellation detected in split_pdf_recursive. Aborting split.")
            return

        level = bookmark["level"]
        title = bookmark["title"]
        start_page = bookmark["page_index"]
        end_page = bookmark.get("end_page_index", start_page)
        children = bookmark.get("children", [])

        # Stop processing if the current level exceeds the max split level
        if max_split_level > 0 and level > max_split_level:
            logging.info(f"Skipping bookmark '{title}' at level {level} (max level is {max_split_level}).")
            continue

        logging.debug(f"Processing in split_pdf_recursive: L{level} '{title}' at path '{current_path}'")

        if not title:
            logging.warning(f"Skipping bookmark with empty title at level {level}, page {start_page + 1}")
            continue

        safe_title = sanitize_filename(title)
        output_pdf_path = current_path / f"{safe_title}.pdf"
        output_dir_path = current_path / safe_title

        # Determine if a PDF should be created for the current bookmark
        is_valid_range = not (start_page < 0 or end_page >= num_pages_total or start_page > end_page)
        create_pdf_for_this_bookmark = False

        if is_valid_range:
            if create_parent_pdfs:
                # In parent-inclusive mode, always create a PDF if the page range is valid.
                create_pdf_for_this_bookmark = True
            else:
                # In leaf-only mode, only create a PDF for leaves or at the max split level.
                create_pdf_for_this_bookmark = not children or (max_split_level > 0 and level == max_split_level)

        if create_pdf_for_this_bookmark:
            if dry_run:
                logging.info(f"[DRY RUN] Would extract pages {start_page + 1} to {end_page + 1} for L{level} PDF: {output_pdf_path}")
            else:
                logging.info(f"  Extracting pages {start_page + 1} to {end_page + 1} for L{level} PDF: {output_pdf_path}")
                writer = PdfWriter()
                pages_added_count = 0
                try:
                    for page_num in range(start_page, end_page + 1):
                        writer.add_page(reader.pages[page_num])
                        pages_added_count += 1

                    if pages_added_count > 0:
                        output_pdf_path.parent.mkdir(parents=True, exist_ok=True)
                        with open(output_pdf_path, "wb") as f_out:
                            writer.write(f_out)
                        logging.info(f"    Successfully created PDF: {output_pdf_path}")
                    else:
                        logging.warning(f"    No pages added for '{title}' (Range: {start_page+1}-{end_page+1}).")
                except Exception as e:
                    logging.error(f"    Error creating PDF for '{title}': {e}")

        # --- Recursive Step ---
        # If the bookmark has children AND we have NOT reached the max_split_level,
        # then we need to go deeper. We create a directory for its children and recurse.
        # We do NOT create a PDF for this parent bookmark.
        if children and (max_split_level == 0 or level < max_split_level):
            if dry_run:
                logging.info(f"[DRY RUN] Would create directory for children: {output_dir_path}")
            else:
                try:
                    output_dir_path.mkdir(parents=True, exist_ok=True)
                    logging.info(f"Created directory for children: {output_dir_path}")
                except Exception as e:
                    logging.error(f"    Failed to create directory {output_dir_path}: {e}")
            
            # Recurse into the children
            split_pdf_recursive(reader, children, output_dir_path, dry_run=dry_run, cancel_event=cancel_event, pause_event=pause_event, max_split_level=max_split_level, create_parent_pdfs=create_parent_pdfs)



def rasterize_pdf(input_pdf, output_pdf, resolution=300, gs_executable="gs"):
    """Rasterizes a single PDF file using Ghostscript."""
    # --- Check if input PDF exists and is not empty ---
    if not input_pdf.is_file():
        logging.error(f"Rasterize input file not found: {input_pdf}")
        return False
    if input_pdf.stat().st_size == 0:
        logging.warning(f"Rasterize input file is empty: {input_pdf}")
        # Optionally delete the empty file? For now, just report failure.
        return False

    logging.info(f"Rasterizing: {input_pdf} -> {output_pdf}")

    gs_command = [
        gs_executable,
        '-sDEVICE=pdfwrite',
        '-dCompatibilityLevel=1.4',
        f'-r{resolution}',
        '-dDownsampleColorImages=true',
        f'-dColorImageResolution={resolution}',
        '-dNOPAUSE',
        '-dQUIET',
        '-dBATCH',
        '-sOutputFile=' + str(output_pdf),
        str(input_pdf)
    ]

    try:
        # Step 1: Convert PDF to PNGs using Ghostscript
        logging.debug(f"Running Ghostscript: {' '.join(gs_command)}")
        gs_result = subprocess.run(gs_command, capture_output=True, text=True, check=False, timeout=300) # Add timeout (e.g., 5 minutes)
        if gs_result.returncode != 0:
            error_msg = f"Ghostscript failed for {input_pdf}. RC: {gs_result.returncode}"
            # Include first 500 chars of stderr in the main error message
            if gs_result.stderr:
                error_msg += f". Stderr: {gs_result.stderr[:500]}"
            else:
                error_msg += ". No stderr output."

            logging.error(error_msg)
            # Log stdout as well, might contain useful info despite QUIET
            logging.debug(f"Ghostscript stdout: {gs_result.stdout[:500]}")
            return False
        logging.debug("Ghostscript finished.")

        # Final check: Ensure output PDF exists and has content
        if not output_pdf.exists() or output_pdf.stat().st_size == 0:
            logging.error(f"Rasterized PDF {output_pdf} was not created or is empty after Ghostscript step.")
            # Log gs output again for debugging this specific case
            logging.debug(f"Ghostscript stdout: {gs_result.stdout[:500]}")
            logging.debug(f"Ghostscript stderr: {gs_result.stderr[:500]}")
            return False

        # If we reach here, rasterization was successful
        return True

    except FileNotFoundError as e:
        # Check specifically for gs or magick and give a clearer message
        missing_cmd = gs_executable
        logging.error(f"Command '{missing_cmd}' not found: {e}. Make sure Ghostscript is installed and in your system's PATH.")
        return False
    except subprocess.TimeoutExpired as e:
         # Be more specific about which command timed out
         logging.error(f"Rasterization step '{e.cmd}' timed out for {input_pdf}. The file might be too complex or large.")
         return False
    except Exception as e:
        logging.error(f"An unexpected error occurred during rasterization of {input_pdf}: {e}", exc_info=True)
        return False

def rasterize_worker(split_pdf_path_str, rasterize_resolution, cleanup_original_splits, dry_run=False, gs_executable="gs"):
    """
    Worker function for parallel rasterization. Takes string paths to be pickle-able.
    Returns a tuple (success: bool, original_path: str).
    """
    split_pdf_path = Path(split_pdf_path_str)
    # Define output path for the rasterized version
    rasterized_pdf_path = split_pdf_path.with_name(f"{split_pdf_path.stem}_rasterized.pdf")
    
    if dry_run:
        logging.info(f"[DRY RUN] Would rasterize: {split_pdf_path} -> {rasterized_pdf_path}")
        if cleanup_original_splits:
            logging.info(f"[DRY RUN] Would delete original split PDF: {split_pdf_path}")
        return (True, str(split_pdf_path)) # Simulate success in dry run

    # Actual rasterization
    success = rasterize_pdf(split_pdf_path, rasterized_pdf_path, resolution=rasterize_resolution, gs_executable=gs_executable)
    if success:
        if cleanup_original_splits:
            try:
                split_pdf_path.unlink()
                # The log from the worker will show up in the main console.
                # It might be interleaved with other logs, but it's useful.
                logging.info(f"  Deleted original split PDF: {split_pdf_path}")
            except Exception as e:
                logging.error(f"  Failed to delete {split_pdf_path}: {e}")
        return (True, str(split_pdf_path))
    else:
        # The failure is already logged by rasterize_pdf
        return (False, str(split_pdf_path))

def save_bookmarks_to_json(bookmarks, json_path, dry_run=False):
    """Saves the nested bookmark structure to a JSON file."""
    if dry_run:
        logging.info(f"[DRY RUN] Would save bookmark structure to {json_path}")
        return
    try:
        logging.info(f"Saving bookmark structure to {json_path}")
        with open(json_path, 'w', encoding='utf-8') as f:
            json.dump(bookmarks, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logging.error(f"Failed to save bookmarks to JSON: {e}")

def find_first_page_of_descendants(bookmark, file_page_map, base_dir, current_path_parts):
    """Recursively find the start page of the first valid PDF in a bookmark's descendants."""
    # Check self first
    safe_title = sanitize_filename(bookmark['title'])
    # The PDF for a bookmark is at the current path level
    pdf_path_for_bookmark = base_dir.joinpath(*current_path_parts, safe_title).with_suffix('.pdf')
    page_info = file_page_map.get(pdf_path_for_bookmark)
    if page_info:
        return page_info['start_page']

    # If not found, check children
    if bookmark.get("children"):
        # Children are in a subdirectory named after the parent
        new_path_parts = current_path_parts + [safe_title]
        for child in bookmark["children"]:
            page = find_first_page_of_descendants(child, file_page_map, base_dir, new_path_parts)
            if page is not None:
                return page  # Found the first one, return it

    return None  # No page found in this branch

def recreate_bookmarks_recursive(writer, bookmarks, parent, file_page_map, base_dir, current_path_parts=[]):
    """Recursively adds bookmarks from the saved structure to the new PDF."""
    for bookmark in bookmarks:
        page_num = find_first_page_of_descendants(bookmark, file_page_map, base_dir, current_path_parts)

        if page_num is not None:
            logging.debug(f"Adding bookmark '{bookmark['title']}' pointing to page {page_num + 1}")
            new_parent = writer.add_outline_item(bookmark['title'], page_num, parent=parent)

            if bookmark.get("children"):
                safe_title = sanitize_filename(bookmark['title'])
                new_path_parts = current_path_parts + [safe_title]
                recreate_bookmarks_recursive(writer, bookmark["children"], new_parent, file_page_map, base_dir, new_path_parts)
        else:
            logging.warning(f"Could not find any content pages for bookmark tree starting with '{bookmark['title']}'. Skipping this branch.")


def merge_rasterized_pdfs(directory, output_file, recreate_bookmarks=True, dry_run=False, 
                          progress_callback=None, cancel_event=None, pause_event=None, report_data=None):
    """Finds all '*_rasterized.pdf' files in a directory, sorts them, and merges them."""
    logging.info(f"Starting merge process for directory: {directory}")
    
    if not directory.is_dir():
        logging.error(f"Merge directory does not exist: {directory}")
        return False
        
    # Use rglob to find files in all subdirectories and sort them
    rasterized_files = sorted(list(directory.rglob("*_rasterized.pdf")))
    if report_data:
        report_data['total_files_processed'] = len(rasterized_files)
    
    if not rasterized_files:
        logging.warning(f"No '*_rasterized.pdf' files found in {directory}.")
        return True # Not an error, just nothing to do.

    logging.info(f"Found {len(rasterized_files)} rasterized PDFs to merge.")
    
    if dry_run:
        logging.info(f"[DRY RUN] Would merge {len(rasterized_files)} files into: {output_file}")
        for f in rasterized_files:
            logging.info(f"[DRY RUN]   - Would include: {f.relative_to(directory)}")
        return True

    merger = PdfWriter()
    total_files = len(rasterized_files)

    # --- Bookmark Recreation Setup ---
    loaded_bookmarks = None
    file_page_map = {}
    page_offset = 0
    bookmarks_json_path = directory / "_bookmarks.json"

    if recreate_bookmarks:
        if bookmarks_json_path.exists():
            try:
                logging.info(f"Found bookmark structure at {bookmarks_json_path}. Will re-create bookmarks.")
                with open(bookmarks_json_path, 'r', encoding='utf-8') as f:
                    loaded_bookmarks = json.load(f)
            except Exception as e:
                logging.error(f"Failed to load or parse {bookmarks_json_path}: {e}")
                loaded_bookmarks = None # Ensure it's None on failure
        else:
            logging.warning(f"Bookmark recreation requested, but {bookmarks_json_path} not found.")

    try:
        for i, pdf_path in enumerate(rasterized_files):
            if pause_event:
                pause_event.wait()

            if cancel_event and cancel_event.is_set():
                logging.warning("Cancellation detected during merge. Aborting.")
                merger.close()
                return False

            logging.info(f"Merging ({i+1}/{total_files}): {pdf_path.name}")
            if progress_callback:
                progress_callback(i, total_files)
            
            if loaded_bookmarks:
                # We need to map the original split path to its page count and offset
                original_split_path = pdf_path.with_name(pdf_path.stem.replace('_rasterized', '')).with_suffix('.pdf')
                try:
                    reader = PdfReader(pdf_path)
                    num_pages = len(reader.pages)
                    file_page_map[original_split_path] = {'start_page': page_offset, 'num_pages': num_pages}
                    page_offset += num_pages
                except Exception as e:
                    logging.error(f"Could not read {pdf_path} to get page count for bookmark mapping: {e}")

            merger.append(str(pdf_path))
            if report_data:
                report_data['success'].append(pdf_path.name)
        
        if progress_callback:
            progress_callback(total_files, total_files)

        # --- Add Bookmarks Before Writing ---
        if loaded_bookmarks:
            logging.info("Re-creating bookmark structure in the merged PDF...")
            recreate_bookmarks_recursive(writer=merger, bookmarks=loaded_bookmarks, parent=None, file_page_map=file_page_map, base_dir=directory)

        logging.info(f"Writing final merged PDF to: {output_file}")
        with open(output_file, "wb") as f_out:
            merger.write(f_out)
        merger.close()
        return True
    except Exception as e:
        if report_data:
            report_data['failures'].append(f"An error occurred during the merge process: {e}")
        logging.error(f"An error occurred during the merge process: {e}", exc_info=True)
        return False

def _flatten_directory(parent_dir: Path):
    """Moves all files from subdirectories into the parent directory and removes empty subdirectories."""
    logging.info(f"Flattening output directory: {parent_dir}")
    moved_count = 0
    error_count = 0
    
    # Walk through all subdirectories from the bottom up
    for root, dirs, files in os.walk(parent_dir, topdown=False):
        current_dir = Path(root)
        
        # Skip the parent directory itself for file moving
        if current_dir == parent_dir:
            continue
            
        for file in files:
            source = current_dir / file
            destination = parent_dir / file
            
            # Handle filename conflicts by appending a counter
            if destination.exists():
                name, ext = os.path.splitext(file)
                counter = 1
                while destination.exists():
                    new_name = f"{name}_{counter}{ext}"
                    destination = parent_dir / new_name
                    counter += 1
            
            try:
                shutil.move(str(source), str(destination))
                moved_count += 1
                logging.info(f"  Moved: {source.relative_to(parent_dir)} -> {destination.name}")
            except Exception as e:
                error_count += 1
                logging.error(f"  Error moving {source}: {e}")
        
        # After moving files, if the directory is now empty, remove it
        if not any(current_dir.iterdir()):
            try:
                current_dir.rmdir()
                logging.info(f"  Removed empty directory: {current_dir.relative_to(parent_dir)}")
            except OSError as e:
                logging.warning(f"  Could not remove directory {current_dir}: {e}")

    logging.info(f"Flattening complete. Files moved: {moved_count}, Errors: {error_count}")
    return error_count == 0

def generate_html_report(report_data, output_path):
    """Generates an HTML summary report of the processing run."""
    start_time = report_data.get('start_time', 0)
    end_time = time.time()
    duration = end_time - start_time

    html = f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <title>Processing Report</title>
        <style>
            body {{ font-family: sans-serif; margin: 2em; }}
            h1, h2 {{ color: #333; }}
            .summary {{ background-color: #f2f2f2; padding: 1em; border-radius: 5px; }}
            .file-list {{ list-style-type: none; padding-left: 0; }}
            .file-list li {{ background-color: #fafafa; margin-bottom: 5px; padding: 8px; border-left: 3px solid #ccc; }}
            .success {{ border-left-color: #27ae60; }}
            .failure {{ border-left-color: #c0392b; }}
        </style>
    </head>
    <body>
        <h1>PDF Processing Report</h1>
        <div class="summary">
            <p><strong>Input PDF:</strong> {report_data.get('input_pdf', 'N/A')}</p>
            <p><strong>Output Directory:</strong> {report_data.get('output_dir', 'N/A')}</p>
            <p><strong>Completed in:</strong> {duration:.2f} seconds</p>
            <p><strong>Successful Splits/Rasterizations:</strong> {len(report_data.get('success', []))}</p>
            <p><strong>Failures:</strong> {len(report_data.get('failures', []))}</p>
        </div>

        <h2>Failures ({len(report_data.get('failures', []))})</h2>
        <ul class="file-list">
            {''.join(f'<li class="failure">{item}</li>' for item in report_data.get('failures', ['None']))}
        </ul>

        <h2>Successful Files ({len(report_data.get('success', []))})</h2>
        <ul class="file-list">
            {''.join(f'<li class="success">{item}</li>' for item in report_data.get('success', ['None']))}
        </ul>
    </body>
    </html>
    """)
    try:
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(html)
        logging.info(f"HTML report generated at: {output_path}")
    except Exception as e:
        logging.error(f"Failed to write HTML report: {e}")

def run_split_rasterize(input_pdf_path, output_base_dir, rasterize_resolution, cleanup_original_splits, num_workers, gs_path, max_split_level, flatten_output, html_report, dry_run, create_parent_pdfs=False, progress_callback=None, cancel_event=None, pause_event=None):
    errors = []
    report_data = {
        'operation_type': 'split_rasterize',
        'start_time': time.time(),
        'input_pdf': str(input_pdf_path),
        'output_dir': str(output_base_dir),
        'success': [],
        'failures': [],
        'total_files_processed': 0,
        'gs_path': gs_path
    }

    if not input_pdf_path.is_file():
        msg = f"Input PDF not found: {input_pdf_path}"
        logging.error(msg)
        errors.append(msg)
        report_data['failures'].append(msg)
        return False, errors, report_data

    if not shutil.which(gs_path):
        msg = f"Ghostscript executable not found at: {gs_path}. Please check your settings or PATH."
        logging.error(msg)
        errors.append(msg)
        report_data['failures'].append(msg)
        return False, errors, report_data

    output_base_dir.mkdir(parents=True, exist_ok=True)

    logging.info(f"Processing PDF: {input_pdf_path}")
    logging.info("Extracting nested bookmark structure...")
    nested_bookmarks = get_bookmark_structure_nested(input_pdf_path)

    if not nested_bookmarks:
        msg = "Could not extract bookmarks or PDF is invalid. Exiting."
        logging.error(msg)
        errors.append(msg)
        report_data['failures'].append(msg)
        return False, errors, report_data

    save_bookmarks_to_json(nested_bookmarks, output_base_dir / "_bookmarks.json", dry_run)

    logging.info(f"Splitting PDF into directory structure under: {output_base_dir}")
    try:
        reader = PdfReader(input_pdf_path)
        if reader.is_encrypted:
            try:
                reader.decrypt("")
                logging.info("Reader decrypted for splitting.")
            except Exception as decrypt_err:
                msg = f"Could not decrypt PDF for splitting: {decrypt_err}"
                logging.error(msg)
                errors.append(msg)
                report_data['failures'].append(msg)
                return False, errors, report_data

        if cancel_event and cancel_event.is_set(): return False, errors, report_data

        split_pdf_recursive(reader, nested_bookmarks, output_base_dir, dry_run=dry_run, cancel_event=cancel_event, pause_event=pause_event, max_split_level=max_split_level, create_parent_pdfs=create_parent_pdfs)

    except Exception as e:
        msg = f"Error during PDF splitting: {e}"
        logging.error(msg, exc_info=True)
        errors.append(msg)
        report_data['failures'].append(msg)
        return False, errors, report_data

    if cancel_event and cancel_event.is_set():
        logging.warning("Cancellation detected after splitting, before rasterization. Aborting.")
        return False, errors, report_data

    logging.info("Starting rasterization process...")
    all_pdfs_found = list(output_base_dir.rglob("*.pdf"))
    split_pdfs_to_process = [p for p in all_pdfs_found if not p.stem.endswith("_rasterized")]

    if not split_pdfs_to_process:
        logging.warning("No split PDFs found in the output directory to rasterize.")
        return True, errors, report_data

    logging.info(f"Found {len(split_pdfs_to_process)} split PDFs to rasterize. Starting parallel processing...")
    success_count, fail_count = 0, 0
    report_data['total_files_processed'] = len(split_pdfs_to_process)

    with concurrent.futures.ProcessPoolExecutor(max_workers=num_workers) as executor:
        future_to_pdf = {
            executor.submit(rasterize_worker, str(pdf_path), rasterize_resolution, cleanup_original_splits, dry_run, gs_executable=gs_path): pdf_path
            for pdf_path in split_pdfs_to_process
        }
        total_tasks = len(future_to_pdf)
        completed_tasks = 0
        for future in concurrent.futures.as_completed(future_to_pdf):
            pdf_path = future_to_pdf[future]
            completed_tasks += 1
            if progress_callback:
                progress_callback(completed_tasks, total_tasks)
            if pause_event:
                pause_event.wait()
            if cancel_event and cancel_event.is_set():
                logging.warning("Cancellation detected during rasterization. Shutting down workers.")
                executor.shutdown(wait=False)
                return False, errors, report_data
            try:
                success, original_path_str = future.result()
                if success:
                    success_count += 1
                    report_data['success'].append(Path(original_path_str).name)
                else:
                    fail_count += 1
                    msg = f"Rasterization failed for {Path(original_path_str).name}"
                    errors.append(msg)
                    report_data['failures'].append(msg)
            except Exception as exc:
                msg = f"'{pdf_path.name}' generated an exception during rasterization: {exc}"
                logging.error(msg, exc_info=True)
                errors.append(msg)
                report_data['failures'].append(msg)
                fail_count += 1

    logging.info(f"Rasterization complete. Success: {success_count}, Failed: {fail_count}")

    if flatten_output:
        if dry_run:
            logging.info(f"[DRY RUN] Would flatten the output directory: {output_base_dir}")
        elif fail_count == 0:
            _flatten_directory(output_base_dir)
        else:
            logging.warning("Skipping directory flattening due to rasterization failures.")

    if html_report:
        generate_html_report(report_data, html_report)

    return fail_count == 0, errors, report_data

def run_merge(merge_dir, output_file, recreate_bookmarks, dry_run, progress_callback=None, cancel_event=None, pause_event=None):
    report_data = {
        'operation_type': 'merge',
        'start_time': time.time(),
        'input_dir': str(merge_dir),
        'output_file': str(output_file),
        'success': [],
        'failures': [],
        'total_files_processed': 0
    }
    success = merge_rasterized_pdfs(
        directory=merge_dir,
        output_file=output_file,
        dry_run=dry_run,
        recreate_bookmarks=recreate_bookmarks,
        progress_callback=progress_callback,
        cancel_event=cancel_event,
        pause_event=pause_event,
        report_data=report_data
    )
    return success, report_data.get('failures', []), report_data

def main_entry(args, progress_callback=None, cancel_event=None, pause_event=None) -> tuple[bool, list, dict]:
    """
    Main entry point that can be called from the GUI. 
    
    Args:
        args: Should have attributes: input, output, resolution, keep_originals
        progress_callback: Optional function to report progress (value, total)
        cancel_event: Optional threading.Event to signal cancellation.
    Returns:
        A tuple of (overall_success, list_of_errors, report_data_dict)
    """
    dry_run = getattr(args, 'dry_run', False)

    if getattr(args, 'merge', None):
        merge_dir = Path(args.merge)
        output_file = merge_dir.parent / f"{merge_dir.name}_merged.pdf"
        if getattr(args, 'merge_output', None):
            output_file = Path(args.merge_output)
        recreate_bmarks = not getattr(args, 'no_recreate_bookmarks', False)
        
        return run_merge(
            merge_dir=merge_dir,
            output_file=output_file,
            recreate_bookmarks=recreate_bmarks,
            dry_run=dry_run,
            progress_callback=progress_callback,
            cancel_event=cancel_event,
            pause_event=pause_event
        )
    else:
        return run_split_rasterize(
            input_pdf_path=args.input,
            output_base_dir=args.output,
            rasterize_resolution=args.resolution,
            cleanup_original_splits=not args.keep_originals,
            num_workers=getattr(args, 'workers', None),
            gs_path=getattr(args, 'gs_path', 'gs'),
            max_split_level=getattr(args, 'max_split_level', 0),
            flatten_output=getattr(args, 'flatten_output', False),
            html_report=getattr(args, 'html_report', None),
            dry_run=dry_run,
            create_parent_pdfs=getattr(args, 'create_parent_pdfs', False),
            progress_callback=progress_callback,
            cancel_event=cancel_event,
            pause_event=pause_event
        )

def main_cli():
    parser = argparse.ArgumentParser(description="Split, rasterize, and merge PDFs based on bookmarks.")

    # --- Logging Configuration for CLI ---
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

    
    # --- Operation Mode Arguments ---
    mode_group = parser.add_mutually_exclusive_group(required=True)
    mode_group.add_argument("--input", type=Path, help="Input PDF file to split and rasterize.")
    mode_group.add_argument("--merge", type=Path, help="Directory containing rasterized PDFs to merge.")

    # --- Arguments for Split & Rasterize Mode ---
    split_group = parser.add_argument_group('Splitting & Rasterizing Options')
    split_group.add_argument("-o", "--output", type=Path, help="Base output directory for split files (required with --input).")
    split_group.add_argument("-r", "--resolution", type=int, default=RASTERIZE_RESOLUTION, help=f"Rasterization DPI (default: {RASTERIZE_RESOLUTION}).")
    split_group.add_argument("--keep-originals", action="store_true", help="Keep original split PDFs after rasterization.")
    split_group.add_argument("-w", "--workers", type=int, default=None, help="Number of parallel processes for rasterization (default: all CPU cores).")
    split_group.add_argument("--flatten-output", action="store_true", help="Move all output files into a single flat directory.")
    split_group.add_argument("--max-split-level", type=int, default=0, help="Maximum bookmark level to split. 0 for unlimited (default).")
    split_group.add_argument("--create-parent-pdfs", action="store_true", help="Also create PDFs for parent bookmarks that contain children.")

    # --- Arguments for Merge Mode ---
    merge_group = parser.add_argument_group('Merging Options')
    merge_group.add_argument("--merge-output", type=Path, help="Optional: Specify the full output path for the merged PDF.")
    merge_group.add_argument("--no-recreate-bookmarks", action="store_true", help="Do not attempt to re-create bookmarks in the merged PDF.")

    # --- General Arguments ---

    general_group = parser.add_argument_group('General Options')
    general_group.add_argument("--dry-run", action="store_true", help="Show what would happen without making changes.")
    general_group.add_argument("--gs-path", type=str, default="gs", help="Path to the Ghostscript (gs) executable.")
    general_group.add_argument("--html-report", type=Path, help="Generate a final HTML summary report at the specified path.")

    args = parser.parse_args()
    
    # Validate arguments
    if args.input and not args.output:
        parser.error("--output is required when using --input.")

    # Call the main_entry function with the parsed arguments
    overall_success, collected_errors, _ = main_entry(args)

    if collected_errors:
        logging.error("\n--- Operation finished with errors ---")
        for i, err in enumerate(collected_errors):
            logging.error(f"  {i+1}. {err}")
    
    if not overall_success:
        # Use sys.exit to return a non-zero status code on failure
        sys.exit(1)

if __name__ == "__main__":
    main_cli()