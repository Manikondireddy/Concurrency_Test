"""Uvicorn entrypoint shim.

Imports and re-exports the FastAPI `app` from the real application package.
"""

from wallets_data.app.main import app  # noqa: F401

