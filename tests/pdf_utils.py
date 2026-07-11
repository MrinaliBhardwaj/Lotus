"""Programmatic PDF fixtures — no binary files checked into the repo."""

import fitz

BODY_TEXT = (
    "This Agreement is entered into by and between the parties. "
    "The obligations herein survive termination and assignment."
)


def make_pdf(
    pages: int = 3,
    *,
    text: str | None = None,
    encrypted: bool = False,
    scanned_pages: set[int] | None = None,
) -> bytes:
    """A synthetic PDF: text pages by default; ``scanned_pages`` (1-based)
    become image-only pages with no text layer."""
    doc = fitz.open()
    for index in range(pages):
        page = doc.new_page(width=612, height=792)
        if scanned_pages and (index + 1) in scanned_pages:
            pixmap = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 40, 40))
            pixmap.clear_with(120)
            page.insert_image(fitz.Rect(36, 36, 576, 756), pixmap=pixmap)
        else:
            page.insert_text(fitz.Point(72, 96), text or f"Page {index + 1}. {BODY_TEXT}")
    save_kwargs: dict[str, object] = {}
    if encrypted:
        save_kwargs = {
            "encryption": fitz.PDF_ENCRYPT_AES_256,
            "user_pw": "secret",
            "owner_pw": "secret",
        }
    data = doc.tobytes(**save_kwargs)
    doc.close()
    return bytes(data)


def make_two_column_pdf() -> bytes:
    """A page with a full-width title and two columns of two blocks each.
    Correct reading order: TITLE, L1, L2, R1, R2."""
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)  # midline at x=306
    page.insert_textbox(fitz.Rect(72, 40, 540, 70), "TITLE spanning the whole page width")
    page.insert_textbox(fitz.Rect(50, 120, 290, 180), "L1-alpha left column first block")
    page.insert_textbox(fitz.Rect(50, 320, 290, 380), "L2-bravo left column second block")
    page.insert_textbox(fitz.Rect(322, 120, 562, 180), "R1-charlie right column first block")
    page.insert_textbox(fitz.Rect(322, 320, 562, 380), "R2-delta right column second block")
    data = doc.tobytes()
    doc.close()
    return bytes(data)


def make_contract_pdf(section_count: int = 3, paragraphs_per_section: int = 5) -> bytes:
    """A contract-shaped PDF: numbered headings (16pt bold), numbered
    subsections (13pt bold), and 11pt body paragraphs, flowing across pages."""
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    y = 72.0

    def put_heading(content: str, size: float) -> None:
        nonlocal page, y
        if y + size * 2 > 720:
            page = doc.new_page(width=612, height=792)
            y = 72.0
        page.insert_text(fitz.Point(72, y + size), content, fontsize=size, fontname="hebo")
        y += size * 2

    def put_body(content: str, height: float = 120.0) -> None:
        nonlocal page, y
        if y + height > 720:
            page = doc.new_page(width=612, height=792)
            y = 72.0
        page.insert_textbox(fitz.Rect(72, y, 540, y + height), content, fontsize=11)
        y += height + 14

    paragraph = (BODY_TEXT + " ") * 4
    for s in range(1, section_count + 1):
        put_heading(f"{s}. Section {s} Heading About Provisions", size=16)
        for p in range(paragraphs_per_section):
            put_body(f"S{s}P{p} {paragraph}")
        put_heading(f"{s}.1 Subsection Detail And Carve-Outs", size=13)
        for p in range(2):
            put_body(f"S{s}sub{p} {paragraph}")
    data = doc.tobytes()
    doc.close()
    return bytes(data)


def make_table_pdf(rows: int = 3, cols: int = 3) -> bytes:
    """A page with body text plus a ruled table PyMuPDF's find_tables detects."""
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text(fitz.Point(72, 80), f"Intro paragraph before the table. {BODY_TEXT}")

    x0, y0, col_width, row_height = 72.0, 120.0, 120.0, 36.0
    x1, y1 = x0 + cols * col_width, y0 + rows * row_height
    for r in range(rows + 1):
        page.draw_line(fitz.Point(x0, y0 + r * row_height), fitz.Point(x1, y0 + r * row_height))
    for c in range(cols + 1):
        page.draw_line(fitz.Point(x0 + c * col_width, y0), fitz.Point(x0 + c * col_width, y1))
    for r in range(rows):
        for c in range(cols):
            page.insert_text(
                fitz.Point(x0 + c * col_width + 8, y0 + r * row_height + 22), f"R{r + 1}C{c + 1}"
            )

    page.insert_text(fitz.Point(72, y1 + 60), f"Closing paragraph after the table. {BODY_TEXT}")
    data = doc.tobytes()
    doc.close()
    return bytes(data)
