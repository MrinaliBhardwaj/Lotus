"""Vendor abstraction for LLM + embeddings (hard invariant #4).

No module outside this package may import a vendor SDK (`anthropic`, `openai`).
Concrete providers are selected via settings in :mod:`app.core.deps`.
"""
