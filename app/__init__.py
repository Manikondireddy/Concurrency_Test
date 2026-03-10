"""Shim package.

This project’s actual FastAPI code lives under `wallets_data/app/`.
Creating a tiny top-level `app` package allows running:

    uvicorn app.main:app --reload

from the `Assignment4` directory without having to `cd wallets_data`.
"""

