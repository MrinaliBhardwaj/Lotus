"""The immutable intermediate representation (hard invariant #3).

Parsing happens exactly once; everything downstream (section detection,
chunking, embedding) is a pure transform over these shapes read back from
storage. The models are frozen so no stage can mutate the parse artifact.

Bounding boxes are normalized to 0–1 fractions of page dimensions (hard
invariant #2) the moment they leave PyMuPDF — no raw points anywhere else.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

BlockType = Literal["text", "table", "image"]


class BlockIR(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str  # "b-{page}-{reading_order}" — stable across re-reads of the artifact
    type: BlockType
    text: str
    bbox: list[float] = Field(min_length=4, max_length=4)  # normalized [x0, y0, x1, y1]
    reading_order: int
    font_size: float | None = None  # dominant span size — heading detection input (Task 5)
    font_name: str | None = None
    is_bold: bool = False


class PageIR(BaseModel):
    model_config = ConfigDict(frozen=True)

    page: int  # 1-based
    width: float  # original page size in points, kept for debugging/round-trips
    height: float
    blocks: list[BlockIR]


class LinearizedBlock(BaseModel):
    """One block's slice of the canonical linearized text stream."""

    model_config = ConfigDict(frozen=True)

    id: str
    page: int
    char_start: int
    char_end: int


class LinearizedText(BaseModel):
    """The whole document in reading order — the source of global char offsets
    (chunks' ``char_start``/``char_end`` index into ``text``)."""

    model_config = ConfigDict(frozen=True)

    text: str
    blocks: list[LinearizedBlock]
