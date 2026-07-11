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
