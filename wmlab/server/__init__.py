"""HTTP backend of the desktop app (ARCHITECTURE.md §9).

Submodules are imported explicitly (``wmlab.server.app`` needs FastAPI, while
``wmlab.server.indexer`` only needs the standard library and the core package).
"""
