#!/usr/bin/env python3
"""
pdf_parser.py - High-throughput, agent-friendly PDF parser and inspection engine.

Designed for AI agents and terminal workflows:
- Extracts structured outlines, page counts, metadata
- Pure Python text extraction per page and per bookmark section
- Fast text search across pages with context snippets
- Machine-readable JSON output for automated agent pipelines
- Markdown export for easy LLM ingestion
"""

import sys
import json
import logging
from pathlib import Path
from typing import List, Dict, Any, Optional, Union
from pypdf import PdfReader
from pypdf.errors import PdfReadError
from pypdf.generic import Destination

logger = logging.getLogger("pdf_parser")


def sanitize_title(title: str) -> str:
    """Strip null bytes, excess whitespace, and control characters from bookmark titles."""
    if not title:
        return ""
    title = str(title).replace("\x00", "").strip()
    return title


class PDFParser:
    """Agent-friendly parser for analyzing, outlining, and extracting PDF documents."""

    def __init__(self, pdf_path: Union[str, Path], password: str = ""):
        self.pdf_path = Path(pdf_path).resolve()
        if not self.pdf_path.exists():
            raise FileNotFoundError(f"PDF file does not exist: {self.pdf_path}")
        
        self.password = password
        self._reader: Optional[PdfReader] = None
        self._load_reader()

    def _load_reader(self) -> None:
        try:
            self._reader = PdfReader(str(self.pdf_path))
            if self._reader.is_encrypted:
                try:
                    self._reader.decrypt(self.password)
                except Exception as e:
                    logger.warning(f"Failed to decrypt {self.pdf_path.name}: {e}")
        except Exception as e:
            raise PdfReadError(f"Could not open PDF file {self.pdf_path}: {e}")

    @property
    def reader(self) -> PdfReader:
        if self._reader is None:
            self._load_reader()
        return self._reader

    @property
    def total_pages(self) -> int:
        return len(self.reader.pages)

    def get_metadata(self) -> Dict[str, Any]:
        """Returns structured document metadata and file statistics."""
        meta = self.reader.metadata or {}
        stat = self.pdf_path.stat()
        return {
            "filename": self.pdf_path.name,
            "filepath": str(self.pdf_path),
            "size_bytes": stat.st_size,
            "size_human": f"{stat.st_size / (1024 * 1024):.2f} MB" if stat.st_size >= 1024 * 1024 else f"{stat.st_size / 1024:.1f} KB",
            "page_count": self.total_pages,
            "is_encrypted": self.reader.is_encrypted,
            "title": str(meta.get("/Title") or meta.get("title") or self.pdf_path.stem),
            "author": str(meta.get("/Author") or meta.get("author") or "Unknown"),
            "creator": str(meta.get("/Creator") or meta.get("creator") or "Unknown"),
            "producer": str(meta.get("/Producer") or meta.get("producer") or "Unknown"),
            "creation_date": str(meta.get("/CreationDate") or "Unknown"),
            "has_bookmarks": bool(self.reader.outline),
        }

    def get_outline(self) -> List[Dict[str, Any]]:
        """
        Parses bookmarks/outlines into a clean nested hierarchy with 0-indexed page bounds.
        Each entry has: title, level, start_page (0-indexed), end_page (0-indexed), page_count, children.
        """
        raw_outline = self.reader.outline
        if not raw_outline:
            return []

        memo: Dict[int, Optional[int]] = {}

        def get_page_index(page_obj) -> Optional[int]:
            page_ref = getattr(page_obj, "indirect_reference", page_obj)
            obj_id = id(page_ref)
            if obj_id not in memo:
                try:
                    memo[obj_id] = self.reader.get_page_number(page_obj)
                except Exception:
                    memo[obj_id] = None
            return memo[obj_id]

        def process_items(items: List[Any], level: int = 1) -> List[Dict[str, Any]]:
            result = []
            last_parent = None
            for item in items:
                if isinstance(item, list):
                    children = process_items(item, level + 1)
                    if last_parent:
                        last_parent["children"].extend(children)
                    else:
                        result.extend(children)
                elif isinstance(item, Destination) and hasattr(item, "title"):
                    title = sanitize_title(str(item.title))
                    p_idx = get_page_index(getattr(item, "page", None))
                    node = {
                        "title": title,
                        "level": level,
                        "start_page": p_idx if p_idx is not None else 0,
                        "end_page": self.total_pages - 1,
                        "children": [],
                    }
                    result.append(node)
                    last_parent = node
            return result

        outline_tree = process_items(raw_outline, level=1)
        self._calculate_end_pages(outline_tree, parent_end_boundary=self.total_pages)
        return outline_tree

    def _calculate_end_pages(self, nodes: List[Dict[str, Any]], parent_end_boundary: int) -> None:
        for i, node in enumerate(nodes):
            if i + 1 < len(nodes):
                next_start = nodes[i + 1]["start_page"]
            else:
                next_start = parent_end_boundary

            if node.get("children"):
                self._calculate_end_pages(node["children"], next_start)
                last_child_end = node["children"][-1].get("end_page", node["start_page"])
            else:
                last_child_end = -1

            calc_end = max(next_start - 1, last_child_end)
            node["end_page"] = min(max(node["start_page"], calc_end), self.total_pages - 1)
            node["page_count"] = node["end_page"] - node["start_page"] + 1

    def extract_text(self, pages: Optional[List[int]] = None) -> Dict[int, str]:
        """
        Extracts plain text for specified 0-indexed page numbers.
        If pages is None, extracts for all pages.
        """
        if pages is None:
            pages = list(range(self.total_pages))
        
        extracted = {}
        for p in pages:
            if 0 <= p < self.total_pages:
                try:
                    text = self.reader.pages[p].extract_text() or ""
                    extracted[p] = text
                except Exception as e:
                    logger.warning(f"Could not extract text from page {p + 1}: {e}")
                    extracted[p] = ""
        return extracted

    def search_text(self, query: str, case_sensitive: bool = False, max_results: int = 50) -> List[Dict[str, Any]]:
        """Searches across pages and returns matching locations with context snippets."""
        matches = []
        q = query if case_sensitive else query.lower()
        for idx in range(self.total_pages):
            try:
                page_text = self.reader.pages[idx].extract_text() or ""
            except Exception:
                continue

            cmp_text = page_text if case_sensitive else page_text.lower()
            start = 0
            while len(matches) < max_results:
                pos = cmp_text.find(q, start)
                if pos == -1:
                    break
                snippet_start = max(0, pos - 40)
                snippet_end = min(len(page_text), pos + len(query) + 40)
                snippet = page_text[snippet_start:snippet_end].replace("\n", " ")
                matches.append({
                    "page": idx + 1,
                    "page_index": idx,
                    "snippet": f"...{snippet}...",
                    "position": pos,
                })
                start = pos + len(q)
            if len(matches) >= max_results:
                break
        return matches

    def to_dict(self, include_text: bool = False) -> Dict[str, Any]:
        """Full serializable dictionary for agent consumption."""
        data = {
            "metadata": self.get_metadata(),
            "outline": self.get_outline(),
        }
        if include_text:
            data["pages"] = {str(k + 1): v for k, v in self.extract_text().items()}
        return data

    def to_markdown(self, include_preview_pages: int = 3) -> str:
        """Renders a clean Markdown summary for LLM context windows."""
        meta = self.get_metadata()
        lines = [
            f"# {meta['title']}",
            f"- **File**: `{meta['filename']}` ({meta['size_human']})",
            f"- **Author**: {meta['author']}",
            f"- **Total Pages**: {meta['page_count']}",
            f"- **Encrypted**: {meta['is_encrypted']}",
            "",
            "## Table of Contents",
        ]
        outline = self.get_outline()
        if outline:
            def render_nodes(nodes, indent=0):
                for n in nodes:
                    prefix = "  " * indent + "-"
                    lines.append(f"{prefix} **{n['title']}** (pp. {n['start_page'] + 1}–{n['end_page'] + 1})")
                    if n.get("children"):
                        render_nodes(n["children"], indent + 1)
            render_nodes(outline)
        else:
            lines.append("*(No bookmarks/outline found in document)*")

        if include_preview_pages > 0:
            lines.extend(["", "## Content Preview"])
            preview_count = min(include_preview_pages, self.total_pages)
            for p in range(preview_count):
                lines.append(f"### Page {p + 1}")
                text = self.reader.pages[p].extract_text() or ""
                lines.append(text.strip()[:1000] if text.strip() else "*(Empty or image-only page)*")
                lines.append("")

        return "\n".join(lines)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="PDF Parser & Inspection Utility for AI Agents")
    parser.add_argument("pdf_path", type=Path, help="Target PDF file to parse")
    parser.add_argument("--json", action="store_true", help="Output machine-readable JSON")
    parser.add_argument("--outline", action="store_true", help="Dump outline/bookmarks hierarchy")
    parser.add_argument("--metadata", action="store_true", help="Dump document metadata")
    parser.add_argument("--extract-text", action="store_true", help="Extract text from pages")
    parser.add_argument("--pages", type=str, help="Comma-separated or range of 1-based page numbers (e.g. 1-5,8,10)")
    parser.add_argument("--search", type=str, help="Search query to locate across document pages")
    parser.add_argument("--markdown", action="store_true", help="Render Markdown overview for LLMs")
    parser.add_argument("--output", type=Path, help="Write output to file instead of stdout")

    args = parser.parse_args()
    try:
        p = PDFParser(args.pdf_path)
    except Exception as e:
        sys.stderr.write(f"Error opening PDF: {e}\n")
        sys.exit(1)

    result_output = ""

    if args.search:
        results = p.search_text(args.search)
        if args.json:
            result_output = json.dumps({"query": args.search, "matches": results, "count": len(results)}, indent=2)
        else:
            result_output = f"Search '{args.search}' found {len(results)} matches:\n" + "\n".join(
                f"  Page {m['page']}: {m['snippet']}" for m in results
            )
    elif args.outline:
        outline = p.get_outline()
        if args.json:
            result_output = json.dumps(outline, indent=2)
        else:
            lines = [f"Outline for {args.pdf_path.name} ({len(outline)} top-level sections):"]
            def print_tree(nodes, depth=0):
                for node in nodes:
                    lines.append(f"{'  ' * depth}• {node['title']} (p. {node['start_page'] + 1}–{node['end_page'] + 1})")
                    if node.get("children"):
                        print_tree(node["children"], depth + 1)
            print_tree(outline)
            result_output = "\n".join(lines)
    elif args.markdown:
        result_output = p.to_markdown()
    elif args.extract_text:
        target_pages = None
        if args.pages:
            indices = []
            for part in args.pages.split(","):
                part = part.strip()
                if "-" in part:
                    start_str, end_str = part.split("-", 1)
                    s, e = int(start_str), int(end_str)
                    indices.extend(range(s - 1, e))
                else:
                    indices.append(int(part) - 1)
            target_pages = sorted(list(set(indices)))
        
        extracted = p.extract_text(target_pages)
        if args.json:
            result_output = json.dumps({str(k + 1): v for k, v in extracted.items()}, indent=2)
        else:
            result_output = "\n\n".join(f"--- Page {p_num + 1} ---\n{text}" for p_num, text in extracted.items())
    elif args.metadata or args.json:
        data = p.to_dict(include_text=False)
        result_output = json.dumps(data, indent=2) if args.json else json.dumps(p.get_metadata(), indent=2)
    else:
        # Default agent overview
        result_output = p.to_markdown()

    if args.output:
        args.output.write_text(result_output, encoding="utf-8")
        print(f"Output saved to {args.output}")
    else:
        print(result_output)


if __name__ == "__main__":
    main()
