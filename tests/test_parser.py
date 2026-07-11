"""Task 4 gate tests: PyMuPDF parser → immutable IR (invariants 2 and 3)."""

from app.parsers.pymupdf_parser import PyMuPDFParser
from app.schemas.ir import PageIR
from tests.pdf_utils import make_pdf, make_table_pdf, make_two_column_pdf

parser = PyMuPDFParser()


def _all_text(page: PageIR) -> str:
    return " ".join(block.text for block in page.blocks)


def test_page_numbers_and_normalized_bboxes_on_large_pdf() -> None:
    # the Task 4 gate: a 100+ page document, spot-checked
    data = make_pdf(pages=120)
    pages = parser.parse_pages(data, [1, 60, 120])
    assert [p.page for p in pages] == [1, 60, 120]
    for page in pages:
        assert page.blocks, f"page {page.page} parsed to no blocks"
        assert f"Page {page.page}." in _all_text(page)
        for block in page.blocks:
            x0, y0, x1, y1 = block.bbox
            assert 0.0 <= x0 <= x1 <= 1.0, block.bbox
            assert 0.0 <= y0 <= y1 <= 1.0, block.bbox


def test_reading_order_is_dense_and_matches_index() -> None:
    (page,) = parser.parse_pages(make_pdf(pages=1), [1])
    assert [b.reading_order for b in page.blocks] == list(range(len(page.blocks)))
    assert all(b.id == f"b-1-{b.reading_order}" for b in page.blocks)


def test_two_column_reading_order() -> None:
    (page,) = parser.parse_pages(make_two_column_pdf(), [1])
    text_sequence = [block.text.split()[0] for block in page.blocks]
    assert text_sequence == ["TITLE", "L1-alpha", "L2-bravo", "R1-charlie", "R2-delta"]


def test_table_is_one_atomic_block() -> None:
    (page,) = parser.parse_pages(make_table_pdf(), [1])
    tables = [block for block in page.blocks if block.type == "table"]
    assert len(tables) == 1
    table = tables[0]
    # every cell in ONE block — the chunker can never split it
    for marker in ("R1C1", "R2C2", "R3C3"):
        assert marker in table.text
    # cell text is not duplicated into overlapping text blocks
    other_text = " ".join(b.text for b in page.blocks if b.type != "table")
    assert "R1C1" not in other_text
    assert "Intro paragraph" in other_text
    assert "Closing paragraph" in other_text


def test_font_metadata_captured() -> None:
    (page,) = parser.parse_pages(make_pdf(pages=1), [1])
    block = page.blocks[0]
    assert block.font_size is not None and block.font_size > 0
    assert block.font_name


def test_ir_is_frozen() -> None:
    import pytest
    from pydantic import ValidationError

    (page,) = parser.parse_pages(make_pdf(pages=1), [1])
    with pytest.raises(ValidationError):
        page.blocks[0].text = "mutated"  # type: ignore[misc]
