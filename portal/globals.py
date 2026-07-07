from __future__ import annotations

import time

from portal.booth_state import BoothRegistry

booths = BoothRegistry()
_JS_CACHE_BUST = str(int(time.time()))
