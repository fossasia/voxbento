from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

os.environ["API_KEY_ENCRYPTION_KEY"] = "test-encryption-key-value-for-all-tests"
os.environ["BOOTH_ACCESS_TOKEN"] = ""


ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

pytest_plugins = ("anyio",)


@pytest.fixture(params=["asyncio"])
def anyio_backend(request):
    return request.param
