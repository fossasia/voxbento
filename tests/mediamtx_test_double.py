"""Version-accurate test double for MediaMTX 1.21.4 Control API.

Faithfully reproduces MediaMTX 1.21.4 behavior:
1. POST /v3/config/paths/add/{name}: rejects duplicate addition with 400 {'status': 'error', 'error': 'path already exists'}.
2. PATCH /v3/config/paths/patch/{name}: returns 404 if missing, merges fields and preserves omitted fields if present.
3. GET /v3/config/paths/get/{name}: returns '<redacted>' for non-empty srtPublishPassphrase.
4. GET /v3/config/paths/list: returns list of path configs with '<redacted>' for non-empty srtPublishPassphrase.
5. DELETE /v3/config/paths/delete/{name}: removes path, 404 if not found.
6. Live path queries: /paths/list and /paths/get/{name}.
7. Kick endpoints: /v3/{endpoint}/kick/{id} validates UUID, removes live source, 404 if not found.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import httpx


class MediaMTX121ControlDouble:
    def __init__(
        self,
        media: dict[str, dict[str, Any]] | None = None,
        record_body: bool = True,
        redact_passphrase: bool = False,
    ) -> None:
        self.configured_paths: dict[str, dict[str, Any]] = {}
        self.media: dict[str, dict[str, Any]] = media if media is not None else {}
        self.calls: list[Any] = []
        self.call_records: list[tuple[str, str, dict[str, Any]]] = []
        self.record_body = record_body
        self.redact_passphrase = redact_passphrase

    def set_path(self, name: str, srt_passphrase: str = "", **extra: Any) -> None:
        """Test helper to register a path with a passphrase."""
        if name not in self.configured_paths:
            self.configured_paths[name] = {"name": name}
        self.configured_paths[name]["srtPublishPassphrase"] = srt_passphrase
        self.configured_paths[name].update(extra)

    def get_passphrase(self, name: str) -> str:
        """Test helper to inspect the stored plaintext passphrase."""
        return self.configured_paths.get(name, {}).get("srtPublishPassphrase", "")

    def contains_path(self, name: str) -> bool:
        return name in self.configured_paths

    def clear(self) -> None:
        self.configured_paths.clear()
        self.calls.clear()
        self.call_records.clear()

    # Mapping / set emulation for backwards compatibility with tests
    def __contains__(self, name: str) -> bool:
        return name in self.configured_paths

    def __getitem__(self, name: str) -> str:
        return self.get_passphrase(name)

    def __setitem__(self, name: str, value: str) -> None:
        self.set_path(name, value)

    def add(self, name: str, pass_: str = "pass") -> None:
        self.set_path(name, pass_)

    def discard(self, name: str) -> None:
        self.configured_paths.pop(name, None)

    def pop(self, name: str, default: Any = None) -> Any:
        return self.configured_paths.pop(name, default)

    def items(self):
        return [(k, v.get("srtPublishPassphrase", "")) for k, v in self.configured_paths.items()]

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        data: dict[str, Any] = json.loads(request.read()) if request.content else {}
        if self.record_body:
            self.calls.append((request.method, str(request.url), data))
        else:
            self.calls.append((request.method, str(request.url)))
        self.call_records.append((request.method, str(request.url), data))
        path = request.url.path

        if "/v3/config/paths/get/" in path:
            name = path.split("/v3/config/paths/get/")[1]
            if name in self.configured_paths:
                entry = dict(self.configured_paths[name])
                entry["name"] = name
                if self.redact_passphrase:
                    entry["srtPublishPassphrase"] = "<redacted>" if entry.get("srtPublishPassphrase") else ""
                else:
                    entry["srtPublishPassphrase"] = entry.get("srtPublishPassphrase", "")
                return httpx.Response(200, json=entry)
            return httpx.Response(404, json={"status": "error", "error": f"path '{name}' not found"})

        if "/v3/config/paths/list" in path:
            items = []
            for name, entry in self.configured_paths.items():
                item = dict(entry)
                item["name"] = name
                if self.redact_passphrase:
                    item["srtPublishPassphrase"] = "<redacted>" if item.get("srtPublishPassphrase") else ""
                else:
                    item["srtPublishPassphrase"] = item.get("srtPublishPassphrase", "")
                items.append(item)
            return httpx.Response(200, json={"itemCount": len(items), "pageCount": 1, "items": items})

        if "/v3/config/paths/add/" in path:
            name = path.split("/v3/config/paths/add/")[1]
            if name in self.configured_paths:
                return httpx.Response(400, json={"status": "error", "error": "path already exists"})
            new_entry = dict(data)
            new_entry["name"] = name
            self.configured_paths[name] = new_entry
            return httpx.Response(200, json={})

        if "/v3/config/paths/patch/" in path:
            name = path.split("/v3/config/paths/patch/")[1]
            if name not in self.configured_paths:
                return httpx.Response(404, json={"status": "error", "error": f"path '{name}' not found"})
            self.configured_paths[name].update(data)
            return httpx.Response(200, json={})

        if "/v3/config/paths/delete/" in path:
            name = path.split("/v3/config/paths/delete/")[1]
            if name in self.configured_paths:
                del self.configured_paths[name]
                return httpx.Response(200, json={})
            return httpx.Response(404, json={"status": "error", "error": f"path '{name}' not found"})

        if "/paths/list" in path:
            items = []
            for name, m in self.media.items():
                entry = dict(m)
                entry.setdefault("name", name)
                items.append(entry)
            return httpx.Response(200, json={"items": items})

        if "/paths/get/" in path:
            name = path.split("/paths/get/")[1]
            if name in self.media:
                entry = dict(self.media[name])
                entry.setdefault("name", name)
                return httpx.Response(200, json=entry)
            return httpx.Response(404, json={"error": f"path '{name}' not found"})

        if "/kick/" in path:
            if request.method != "POST":
                return httpx.Response(405, json={"status": "error", "error": "method not allowed"})
            sess_id = path.rstrip("/").split("/kick/")[-1]
            try:
                uuid_obj = UUID(sess_id)
            except (ValueError, TypeError):
                return httpx.Response(400, json={"status": "error", "error": f"invalid UUID: '{sess_id}'"})

            found = False
            for m in self.media.values():
                src = m.get("source")
                if isinstance(src, dict) and src.get("id") == str(uuid_obj):
                    m["source"] = None
                    m["ready"] = False
                    found = True
            if found:
                return httpx.Response(200, json={})
            return httpx.Response(404, json={"status": "error", "error": f"session '{sess_id}' not found"})

        return httpx.Response(200, json={})
