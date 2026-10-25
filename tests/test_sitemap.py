from __future__ import annotations

from xml.etree import ElementTree

import pytest
from httpx import ASGITransport, AsyncClient

from fastapi_app import app
from portal.config import settings

SITEMAP_NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
EXPECTED_PAGES = (
    ("/", "daily", "1.0"),
    ("/research", "weekly", "0.9"),
    ("/research/speech-ai-statistics", "weekly", None),
    ("/research/whisper-benchmark", "weekly", None),
    ("/research/stt-comparison", "weekly", None),
    ("/research/real-time-audio-latency", "weekly", None),
    ("/research/webrtc-vs-rtmp-vs-hls", "weekly", None),
    ("/research/speech-ai-glossary", "weekly", None),
    ("/research/streaming-stt-architecture", "weekly", None),
)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("configured_base", "canonical_base"),
    [
        pytest.param("https://voxbento.com", "https://voxbento.com", id="default-com"),
        pytest.param("https://captions.example.test", "https://captions.example.test", id="custom-domain"),
        pytest.param("https://captions.example.test/", "https://captions.example.test", id="trailing-slash"),
        pytest.param(
            "https://captions.example.test/events/voxbento/",
            "https://captions.example.test/events/voxbento",
            id="deployment-prefix",
        ),
        pytest.param("http://localhost:8000", "http://localhost:8000", id="local-http-port"),
        pytest.param(
            "https://captions.example.test/events/a&b/",
            "https://captions.example.test/events/a&b",
            id="xml-ampersand-round-trip",
        ),
    ],
)
@pytest.mark.parametrize(
    "headers",
    [
        pytest.param({}, id="ordinary-host"),
        pytest.param(
            {"Host": "request.example.test", "X-Forwarded-Host": "forwarded.example.test"},
            id="conflicting-host-headers",
        ),
    ],
)
async def test_sitemap_uses_configured_canonical_base(
    monkeypatch: pytest.MonkeyPatch,
    configured_base: str,
    canonical_base: str,
    headers: dict[str, str],
) -> None:
    """Ensure sitemap URLs use configuration rather than request host headers."""
    monkeypatch.setattr(settings, "public_base_url", configured_base)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver.test") as client:
        response = await client.get("/sitemap.xml", headers=headers)

    assert response.status_code == 200
    assert response.headers["content-type"].split(";", 1)[0] == "application/xml"
    root = ElementTree.fromstring(response.content)
    assert root.tag == f"{SITEMAP_NS}urlset"
    entries = root.findall(f"{SITEMAP_NS}url")
    assert len(entries) == len(root) == 9
    actual = {
        entry.findtext(f"{SITEMAP_NS}loc"): (
            entry.findtext(f"{SITEMAP_NS}changefreq"),
            entry.findtext(f"{SITEMAP_NS}priority"),
        )
        for entry in entries
    }
    assert actual == {
        f"{canonical_base}{path}": (changefreq, priority) for path, changefreq, priority in EXPECTED_PAGES
    }
