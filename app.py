"""Flask application — deprecated in Phase 1C.

All Socket.IO events and REST endpoints have migrated to FastAPI.
Start the server with:

    uvicorn fastapi_app:app --host 0.0.0.0 --port 8001 --reload

This file is retained as a skeleton until Phase 1D removes Flask entirely.
"""
from __future__ import annotations

from flask import Flask

from portal.config import settings

app = Flask(__name__)
app.config['SECRET_KEY'] = settings.secret_key


def main() -> None:
    raise SystemExit(
        'Flask is deprecated in Phase 1C.\n'
        'Run the server with: uvicorn fastapi_app:app --port 8001 --reload'
    )


if __name__ == '__main__':
    main()
