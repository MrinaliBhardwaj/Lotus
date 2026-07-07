"""Object-storage abstraction (raw PDFs, parsed IR JSON, page images).

All access goes through :class:`app.storage.base.ObjectStorage`; the concrete
backend (S3/MinIO or local filesystem) is selected by configuration.
"""
