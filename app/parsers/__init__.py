"""PDF parsing behind an interface → immutable IR (hard invariant #3).

The parser interface and PyMuPDF implementation land in Task 4. Kept as a
top-level package (not under services/) so parsing can never fold into the
chunker.
"""
