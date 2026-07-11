"""PyMuPDF (fitz) parser — the primary Phase-1 implementation.

Per page: extract text blocks with dominant font metadata, detect ruled
tables and keep each as ONE atomic block (they must never be split by the
chunker), reconstruct multi-column reading order, and normalize every bbox
to 0–1 fractions of the page size (invariant 2).
"""

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import fitz

from app.parsers.base import PDFParser
from app.schemas.ir import BlockIR, PageIR

_BOLD_FLAG = 1 << 4  # fitz span flag bit for bold
# a block wider than this fraction of the page can't belong to one column
_FULL_WIDTH_FRACTION = 0.6


@dataclass
class _RawBlock:
    type: str  # "text" | "table" | "image"
    text: str
    bbox: tuple[float, float, float, float]  # raw points
    font_size: float | None = None
    font_name: str | None = None
    is_bold: bool = False


class PyMuPDFParser(PDFParser):
    def parse_pages(self, pdf_bytes: bytes, page_numbers: Sequence[int]) -> list[PageIR]:
        results: list[PageIR] = []
        with fitz.open(stream=pdf_bytes, filetype="pdf") as pdf:
            for number in page_numbers:
                page = pdf[number - 1]
                results.append(self._parse_page(page, number))
        return results

    def _parse_page(self, page: fitz.Page, number: int) -> PageIR:
        width, height = float(page.rect.width), float(page.rect.height)
        table_rects, table_blocks = self._extract_tables(page)
        raw_blocks = table_blocks + self._extract_text_blocks(page, table_rects)
        ordered = _reading_order(raw_blocks, width)

        blocks = [
            BlockIR(
                id=f"b-{number}-{index}",
                type=raw.type,
                text=raw.text,
                bbox=_normalize(raw.bbox, width, height),
                reading_order=index,
                font_size=raw.font_size,
                font_name=raw.font_name,
                is_bold=raw.is_bold,
            )
            for index, raw in enumerate(ordered)
        ]
        return PageIR(page=number, width=width, height=height, blocks=blocks)

    def _extract_tables(self, page: fitz.Page) -> tuple[list[fitz.Rect], list[_RawBlock]]:
        rects: list[fitz.Rect] = []
        blocks: list[_RawBlock] = []
        for table in page.find_tables().tables:
            rows = table.extract()
            text = "\n".join(
                " | ".join("" if cell is None else str(cell).strip() for cell in row)
                for row in rows
            ).strip()
            if not text:
                continue
            rect = fitz.Rect(table.bbox)
            rects.append(rect)
            blocks.append(
                _RawBlock(type="table", text=text, bbox=(rect.x0, rect.y0, rect.x1, rect.y1))
            )
        return rects, blocks

    def _extract_text_blocks(
        self, page: fitz.Page, table_rects: list[fitz.Rect]
    ) -> list[_RawBlock]:
        blocks: list[_RawBlock] = []
        for block in page.get_text("dict")["blocks"]:
            if block["type"] != 0:  # images carry no text; skipped in P1 (no OCR)
                continue
            rect = fitz.Rect(block["bbox"])
            # text already captured inside a detected table stays atomic with it
            if any(rect in table_rect or (rect & table_rect).get_area() > 0.5 * rect.get_area()
                   for table_rect in table_rects):
                continue
            spans = [span for line in block["lines"] for span in line["spans"]]
            text = _block_text(block)
            if not text:
                continue
            size, name, bold = _dominant_font(spans)
            blocks.append(
                _RawBlock(
                    type="text",
                    text=text,
                    bbox=(rect.x0, rect.y0, rect.x1, rect.y1),
                    font_size=size,
                    font_name=name,
                    is_bold=bold,
                )
            )
        return blocks


def _block_text(block: dict[str, Any]) -> str:
    lines = ["".join(span["text"] for span in line["spans"]).strip() for line in block["lines"]]
    return "\n".join(line for line in lines if line).strip()


def _dominant_font(spans: list[dict[str, Any]]) -> tuple[float | None, str | None, bool]:
    """Font of the span style carrying the most characters in the block."""
    if not spans:
        return None, None, False
    weights: Counter[tuple[float, str, bool]] = Counter()
    for span in spans:
        style = (round(float(span["size"]), 1), span["font"], bool(span["flags"] & _BOLD_FLAG))
        weights[style] += len(span["text"])
    size, name, bold = weights.most_common(1)[0][0]
    return size, name, bold


def _normalize(
    bbox: tuple[float, float, float, float], width: float, height: float
) -> list[float]:
    x0, y0, x1, y1 = bbox

    def clamp(value: float) -> float:
        return min(max(value, 0.0), 1.0)

    return [clamp(x0 / width), clamp(y0 / height), clamp(x1 / width), clamp(y1 / height)]


def _reading_order(blocks: list[_RawBlock], page_width: float) -> list[_RawBlock]:
    """Multi-column reading-order reconstruction.

    Full-width blocks (titles, tables spanning the page) split the page into
    vertical bands; inside each band, left-column blocks read before
    right-column blocks, each top-to-bottom. Pages that don't look columnar
    fall back to plain top-to-bottom order.
    """
    if not blocks:
        return []
    midline = page_width / 2

    def is_full_width(block: _RawBlock) -> bool:
        x0, _, x1, _ = block.bbox
        return (x1 - x0) > _FULL_WIDTH_FRACTION * page_width or (x0 < midline < x1)

    columnar = [b for b in blocks if not is_full_width(b)]
    left = [b for b in columnar if (b.bbox[0] + b.bbox[2]) / 2 < midline]
    right = [b for b in columnar if (b.bbox[0] + b.bbox[2]) / 2 >= midline]
    if len(left) < 2 or len(right) < 2:  # not a two-column page
        return sorted(blocks, key=lambda b: (b.bbox[1], b.bbox[0]))

    separators = sorted(
        (b for b in blocks if is_full_width(b)), key=lambda b: (b.bbox[1], b.bbox[0])
    )
    boundaries = [s.bbox[1] for s in separators] + [float("inf")]

    ordered: list[_RawBlock] = []
    band_start = float("-inf")
    for separator, band_end in zip([None, *separators], boundaries, strict=True):
        if separator is not None:
            ordered.append(separator)
            band_start = separator.bbox[1]
        for column in (left, right):
            ordered.extend(
                sorted(
                    (b for b in column if band_start <= b.bbox[1] < band_end),
                    key=lambda b: (b.bbox[1], b.bbox[0]),
                )
            )
    return ordered
