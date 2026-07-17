from __future__ import annotations

import pytest


@pytest.mark.anyio
class TestTranslationWorkerSharedClient:
    """The translation batch must share a single HTTP client across all target languages."""

    @pytest.fixture(autouse=True)
    async def setup_db(self):
        from portal.database import configure, dispose, init_db

        configure("sqlite+aiosqlite://")
        await init_db()
        yield
        await dispose()

    async def _seed_floor_room(self, languages):
        """Seed a floor-translation room (provider 'local', so no API key is needed) plus one segment."""
        from portal.database import create_event, create_room, get_session
        from portal.models import RoomTranslationLanguage, TranscriptSegment

        async with get_session() as s:
            event = await create_event(s, slug="tcon", display_name="T Con")
            room = await create_room(s, event_id=event.id, display_name="Hall")
            room.floor_translation_enabled = True
            room.floor_translation_provider = "local"
            room.floor_translation_model = "local-model"
            for code, name in languages:
                s.add(RoomTranslationLanguage(room_id=room.id, language_code=code, language_name=name, enabled=True))
            segment = TranscriptSegment(room_id=room.id, booth_id=None, language_code="en", text="Hello world.")
            s.add(segment)
            await s.commit()
            return room.id, segment.id

    async def _seed_booth(self, languages):
        """Seed a booth-translation booth (provider 'local') plus one segment tied to that booth."""
        from portal.database import create_event, create_room, get_session
        from portal.models import BoothTranslationLanguage, DBBooth, TranscriptSegment

        async with get_session() as s:
            event = await create_event(s, slug="bcon", display_name="B Con")
            room = await create_room(s, event_id=event.id, display_name="Hall")
            booth = DBBooth(
                event_id=event.id,
                room_id=room.id,
                language_code="en",
                language_name="English",
                translation_enabled=True,
                translation_provider="local",
                translation_model="local-model",
            )
            s.add(booth)
            await s.flush()
            for code, name in languages:
                s.add(BoothTranslationLanguage(booth_id=booth.id, language_code=code, language_name=name, enabled=True))
            segment = TranscriptSegment(room_id=room.id, booth_id=booth.id, language_code="en", text="Hello world.")
            s.add(segment)
            await s.commit()
            return room.id, segment.id

    def _worker_with_spies(self, monkeypatch):
        """Spy on httpx.AsyncClient construction and _call_llm; return (worker, created, received, broadcasts)."""
        from portal.translations.worker import TranslationWorker

        created_clients = []

        class SpyClient:
            def __init__(self, *args, **kwargs):
                created_clients.append(self)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        monkeypatch.setattr("portal.translations.worker.httpx.AsyncClient", SpyClient)

        # Capture the client instance each language's _call_llm receives.
        received_clients = []

        async def fake_call_llm(self, client, provider, model, api_key, text, target_lang_name):
            received_clients.append(client)
            return f"[{target_lang_name}] {text}"

        monkeypatch.setattr(TranslationWorker, "_call_llm", fake_call_llm)

        broadcasts = []

        async def broadcast(booth_id_str, payload):
            broadcasts.append((booth_id_str, payload))

        return TranslationWorker(broadcast), created_clients, received_clients, broadcasts

    async def test_one_shared_client_threaded_to_every_language(self, monkeypatch):
        languages = [("fr", "French"), ("de", "German"), ("es", "Spanish")]
        room_id, segment_id = await self._seed_floor_room(languages)
        worker, created, received, broadcasts = self._worker_with_spies(monkeypatch)

        await worker.handle_translation(
            room_id=room_id, segment_id=segment_id, text="Hello world.", booth_id_str="floor-1"
        )

        # Exactly one client for the whole batch...
        assert len(created) == 1
        # ...consumed once per target language...
        assert len(received) == len(languages)
        # ...and every call received that same shared instance.
        assert all(c is created[0] for c in received)
        # Sanity: all languages were translated and broadcast.
        assert len(broadcasts) == len(languages)

    async def test_booth_path_shares_one_client(self, monkeypatch):
        """The booth (non-floor) branch resolves config from the booth and must share one client too."""
        languages = [("fr", "French"), ("de", "German")]
        room_id, segment_id = await self._seed_booth(languages)
        worker, created, received, broadcasts = self._worker_with_spies(monkeypatch)

        await worker.handle_translation(
            room_id=room_id, segment_id=segment_id, text="Hello world.", booth_id_str="booth-en"
        )

        assert len(created) == 1
        assert len(received) == len(languages)
        assert all(c is created[0] for c in received)
        assert len(broadcasts) == len(languages)
