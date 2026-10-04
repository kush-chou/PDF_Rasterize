#!/usr/bin/env python3
"""
pdf_tui.py - Terminal User Interface (TUI) for PDF exploration, parsing, and splitting.

Designed for interactive terminal usage and agent diagnostics:
- Works over SSH / headless agent sessions (ANSI-based, no PyQt or X11 required)
- Interactive menu for exploring document structure, metadata, outlines, and text
- Direct shortcuts to export structured JSON/Markdown and trigger splits
- Supports one-shot CLI summary flag (--summary)
"""

import sys
import os
import json
from pathlib import Path
from typing import Optional, List, Dict, Any
from pdf_parser import PDFParser


# ANSI Color Codes
CYAN = "\033[96m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
MAGENTA = "\033[95m"
BLUE = "\033[94m"
RED = "\033[91m"
BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"


def clear_screen():
    if sys.stdout.isatty():
        os.system("cls" if os.name == "nt" else "clear")


def render_banner(title: str = "PDF RASTERIZE & PARSER TUI"):
    print(f"{CYAN}{BOLD}╔{'═' * 62}╗{RESET}")
    print(f"{CYAN}{BOLD}║  {title.center(58)}  ║{RESET}")
    print(f"{CYAN}{BOLD}╚{'═' * 62}╝{RESET}\n")


def print_box(header: str, lines: List[str], color=BLUE):
    width = 62
    print(f"{color}┌─ {BOLD}{header}{RESET}{color} {'─' * max(0, width - len(header) - 4)}┐{RESET}")
    for line in lines:
        stripped_len = len(line.replace(CYAN, "").replace(GREEN, "").replace(YELLOW, "").replace(MAGENTA, "").replace(BLUE, "").replace(RED, "").replace(BOLD, "").replace(DIM, "").replace(RESET, ""))
        padding = max(0, width - stripped_len - 2)
        print(f"{color}│{RESET} {line}{' ' * padding} {color}│{RESET}")
    print(f"{color}└{'─' * width}┘{RESET}")


class PDFTUI:
    def __init__(self, pdf_path: Path):
        self.pdf_path = Path(pdf_path).resolve()
        self.parser = PDFParser(self.pdf_path)
        self.meta = self.parser.get_metadata()
        self.outline = self.parser.get_outline()

    def print_summary(self):
        """Displays high-level document health and properties."""
        lines = [
            f"{BOLD}File:{RESET} {self.meta['filename']}",
            f"{BOLD}Path:{RESET} {DIM}{str(self.meta['filepath'])[:50]}...{RESET}",
            f"{BOLD}Size:{RESET} {self.meta['size_human']} ({self.meta['size_bytes']:,} bytes)",
            f"{BOLD}Pages:{RESET} {GREEN}{self.meta['page_count']}{RESET} total pages",
            f"{BOLD}Title:{RESET} {self.meta['title']}",
            f"{BOLD}Author:{RESET} {self.meta['author']}",
            f"{BOLD}Encrypted:{RESET} {RED if self.meta['is_encrypted'] else GREEN}{self.meta['is_encrypted']}{RESET}",
            f"{BOLD}Bookmarks:{RESET} {GREEN if self.meta['has_bookmarks'] else YELLOW}{'Present' if self.meta['has_bookmarks'] else 'None'}{RESET}",
        ]
        print_box("DOCUMENT OVERVIEW", lines, color=CYAN)

    def view_outline(self):
        """Hierarchical interactive display of bookmarks/outlines."""
        if not self.outline:
            print(f"\n{YELLOW}No bookmarks or outlines found in {self.meta['filename']}.{RESET}\n")
            return

        print(f"\n{BOLD}{CYAN}Document Outline Hierarchy ({len(self.outline)} root nodes):{RESET}")
        print("─" * 64)

        def print_nodes(nodes: List[Dict[str, Any]], depth: int = 0):
            for i, n in enumerate(nodes):
                indent = "  " * depth
                bullet = "├─" if i < len(nodes) - 1 else "└─"
                page_info = f"{YELLOW}[pp. {n['start_page'] + 1}–{n['end_page'] + 1} | {n['page_count']} pages]{RESET}"
                print(f"{DIM}{indent}{bullet}{RESET} {BOLD}{n['title']}{RESET} {page_info}")
                if n.get("children"):
                    print_nodes(n["children"], depth + 1)

        print_nodes(self.outline)
        print("─" * 64)

    def preview_page(self, page_num: Optional[int] = None):
        """Renders text preview of a specified page."""
        if page_num is None:
            prompt = input(f"{BOLD}Enter page number (1–{self.meta['page_count']}): {RESET}").strip()
            try:
                page_num = int(prompt)
            except ValueError:
                print(f"{RED}Invalid page number.{RESET}")
                return

        if not (1 <= page_num <= self.meta['page_count']):
            print(f"{RED}Page {page_num} out of bounds (1–{self.meta['page_count']}).{RESET}")
            return

        text = self.parser.extract_text([page_num - 1]).get(page_num - 1, "")
        print(f"\n{BOLD}{BLUE}=== Page {page_num} of {self.meta['page_count']} ==={RESET}\n")
        if text.strip():
            print(text)
        else:
            print(f"{DIM}(Page {page_num} contains no extractable text / may be a raster scan){RESET}")
        print(f"\n{BLUE}{'═' * 64}{RESET}\n")

    def search_interactive(self):
        """Interactive keyword search across pages."""
        q = input(f"{BOLD}Search query: {RESET}").strip()
        if not q:
            return
        matches = self.parser.search_text(q)
        print(f"\n{BOLD}Found {len(matches)} matches for '{q}':{RESET}\n")
        for m in matches[:20]:
            print(f"  • {GREEN}Page {m['page']}{RESET}: {m['snippet']}")
        if len(matches) > 20:
            print(f"  {DIM}...and {len(matches) - 20} more matches.{RESET}")
        print()

    def export_json(self):
        """Dumps outline and metadata to JSON file."""
        default_name = f"{self.pdf_path.stem}_analysis.json"
        target = input(f"Output filename [{default_name}]: ").strip() or default_name
        data = self.parser.to_dict(include_text=False)
        Path(target).write_text(json.dumps(data, indent=2), encoding="utf-8")
        print(f"{GREEN}Exported metadata & outline to {target}{RESET}")

    def export_markdown(self):
        """Dumps document structure and previews to Markdown file."""
        default_name = f"{self.pdf_path.stem}_overview.md"
        target = input(f"Output filename [{default_name}]: ").strip() or default_name
        md = self.parser.to_markdown()
        Path(target).write_text(md, encoding="utf-8")
        print(f"{GREEN}Exported markdown overview to {target}{RESET}")

    def run_menu(self):
        """Interactive TUI event loop."""
        while True:
            render_banner(f"PDF TUI: {self.meta['filename'][:40]}")
            self.print_summary()
            print()
            menu_options = [
                f"{CYAN}[1]{RESET} View Document Outline / Bookmarks",
                f"{CYAN}[2]{RESET} Preview Page Text",
                f"{CYAN}[3]{RESET} Search Keyword across Pages",
                f"{CYAN}[4]{RESET} Export Outline to JSON",
                f"{CYAN}[5]{RESET} Export Overview to Markdown",
                f"{CYAN}[6]{RESET} Dump Complete Page Text",
                f"{RED}[q]{RESET} Quit",
            ]
            for opt in menu_options:
                print(f"  {opt}")
            print()

            choice = input(f"{BOLD}Select an option: {RESET}").strip().lower()
            if choice == "1":
                self.view_outline()
                input(f"\n{DIM}Press Enter to continue...{RESET}")
            elif choice == "2":
                self.preview_page()
                input(f"\n{DIM}Press Enter to continue...{RESET}")
            elif choice == "3":
                self.search_interactive()
                input(f"\n{DIM}Press Enter to continue...{RESET}")
            elif choice == "4":
                self.export_json()
                input(f"\n{DIM}Press Enter to continue...{RESET}")
            elif choice == "5":
                self.export_markdown()
                input(f"\n{DIM}Press Enter to continue...{RESET}")
            elif choice == "6":
                p_text = self.parser.extract_text()
                out_file = f"{self.pdf_path.stem}_extracted_text.txt"
                Path(out_file).write_text("\n\n".join(f"=== Page {k + 1} ===\n{v}" for k, v in p_text.items()), encoding="utf-8")
                print(f"{GREEN}Extracted all text to {out_file}{RESET}")
                input(f"\n{DIM}Press Enter to continue...{RESET}")
            elif choice in ("q", "quit", "exit"):
                print("Exiting PDF TUI. Goodbye!")
                break
            else:
                print(f"{RED}Unknown option '{choice}'.{RESET}")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Terminal User Interface (TUI) for PDF Analysis")
    parser.add_argument("pdf_path", type=Path, help="Target PDF file to inspect")
    parser.add_argument("--summary", action="store_true", help="Print summary box and exit (non-interactive)")
    parser.add_argument("--outline", action="store_true", help="Print outline hierarchy and exit (non-interactive)")
    parser.add_argument("--preview", type=int, help="Print specified page text and exit (non-interactive)")

    args = parser.parse_args()

    if not args.pdf_path.exists():
        sys.stderr.write(f"Error: File {args.pdf_path} not found.\n")
        sys.exit(1)

    tui = PDFTUI(args.pdf_path)

    if args.summary:
        render_banner()
        tui.print_summary()
    elif args.outline:
        tui.view_outline()
    elif args.preview is not None:
        tui.preview_page(args.preview)
    else:
        tui.run_menu()


if __name__ == "__main__":
    main()
