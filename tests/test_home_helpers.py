"""Unit tests for the helpers extracted from the home route handler.

Covers:
- _build_my_booths: live/offline booth status, full dict shape
- _build_event_data: room grouping, live_count, can_interpret delegation
- _can_user_interpret: anonymous, admin, event_owner, interpreter role
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_bm(
    slug="pycon2026",
    room_id=1,
    language_code="en",
    language_name="English",
    event_display="PyCon 2026",
    room_display="Main Hall",
    booth_id=10,
):
    bm = MagicMock()
    bm.booth.event.slug = slug
    bm.booth.event.display_name = event_display
    bm.booth.room.display_name = room_display
    bm.booth.room_id = room_id
    bm.booth.language_code = language_code
    bm.booth.language_name = language_name
    bm.booth_id = booth_id
    return bm


def _make_db_booth(booth_id=10, room_id=1, language_code="en", room_display="Main Hall"):
    b = MagicMock()
    b.id = booth_id
    b.room_id = room_id
    b.language_code = language_code
    b.room.display_name = room_display
    return b


def _make_event(event_id=1, slug="pycon2026"):
    ev = MagicMock()
    ev.id = event_id
    ev.slug = slug
    return ev


# ---------------------------------------------------------------------------
# _build_my_booths
# ---------------------------------------------------------------------------

class TestBuildMyBooths:
    def _call(self, bms, booth_store=None):
        from portal.routers.public import _build_my_booths
        fake_store = booth_store or MagicMock()
        with patch("portal.routers.public.booths", fake_store), \
             patch("portal.routers.public.make_booth_id", return_value="pycon2026:1:en"):
            return _build_my_booths(bms)

    def test_empty_list_returns_empty(self):
        assert self._call([]) == []

    def test_live_booth_sets_is_live_true(self):
        bm = _make_bm()
        mem_booth = MagicMock()
        mem_booth.ingest_status = "connected"
        store = MagicMock()
        store.get_booth_sync.return_value = mem_booth
        result = self._call([bm], booth_store=store)
        assert result[0]["is_live"] is True

    def test_offline_booth_sets_is_live_false(self):
        bm = _make_bm()
        store = MagicMock()
        store.get_booth_sync.return_value = None
        result = self._call([bm], booth_store=store)
        assert result[0]["is_live"] is False

    def test_disconnected_booth_sets_is_live_false(self):
        bm = _make_bm()
        mem_booth = MagicMock()
        mem_booth.ingest_status = "disconnected"
        store = MagicMock()
        store.get_booth_sync.return_value = mem_booth
        result = self._call([bm], booth_store=store)
        assert result[0]["is_live"] is False

    def test_dict_shape_is_correct(self):
        bm = _make_bm()
        store = MagicMock()
        store.get_booth_sync.return_value = None
        result = self._call([bm], booth_store=store)
        row = result[0]
        assert row["booth_id"] == "pycon2026:1:en"
        assert row["event_name"] == "PyCon 2026"
        assert row["room_name"] == "Main Hall"
        assert row["language_name"] == "English"
        assert row["language_code"] == "en"
        assert row["event_slug"] == "pycon2026"
        assert row["membership"] is bm

    def test_multiple_bms_preserves_order(self):
        bms = [_make_bm(language_code=lc) for lc in ("en", "fr", "de")]
        store = MagicMock()
        store.get_booth_sync.return_value = None
        with patch("portal.routers.public.booths", store), \
             patch("portal.routers.public.make_booth_id", side_effect=lambda s, r, lc: f"id:{lc}"):
            from portal.routers.public import _build_my_booths
            result = _build_my_booths(bms)
        assert [r["booth_id"] for r in result] == ["id:en", "id:fr", "id:de"]


# ---------------------------------------------------------------------------
# _can_user_interpret
# ---------------------------------------------------------------------------

class TestCanUserInterpret:
    def _call(self, current_user, event_id=1, booth_id=10, event_roles=None, booth_roles=None):
        from portal.routers.public import _can_user_interpret
        return _can_user_interpret(current_user, event_id, booth_id, event_roles or {}, booth_roles or {})

    def test_anonymous_cannot_interpret(self):
        assert self._call(None) is False

    def test_admin_can_always_interpret(self):
        assert self._call({"is_admin": True}) is True

    def test_event_owner_can_interpret(self):
        assert self._call({"is_admin": False}, event_roles={1: "event_owner"}) is True

    def test_interpreter_role_can_interpret(self):
        assert self._call({"is_admin": False}, booth_roles={10: "interpreter"}) is True

    def test_non_matching_roles_cannot_interpret(self):
        assert self._call({"is_admin": False}, event_roles={1: "viewer"}, booth_roles={10: "observer"}) is False

    def test_missing_is_admin_key_treated_as_false(self):
        assert self._call({}) is False


# ---------------------------------------------------------------------------
# _build_event_data
# ---------------------------------------------------------------------------

class TestBuildEventData:
    def _call(self, events, booths_by_event, current_user=None, event_roles=None, booth_roles=None):
        from portal.routers.public import _build_event_data
        store = MagicMock()
        store.get_booth_sync.return_value = None
        with patch("portal.routers.public.booths", store), \
             patch("portal.routers.public.make_booth_id", side_effect=lambda s, r, lc: f"{s}:{r}:{lc}"):
            return _build_event_data(events, booths_by_event, current_user, event_roles or {}, booth_roles or {})

    def test_no_events_returns_empty(self):
        assert self._call([], {}) == []

    def test_event_with_no_booths(self):
        ev = _make_event()
        result = self._call([ev], {1: []})
        assert result[0]["rooms"] == []
        assert result[0]["live_count"] == 0

    def test_booths_grouped_by_room(self):
        ev = _make_event()
        b1 = _make_db_booth(booth_id=1, room_id=10, language_code="en")
        b2 = _make_db_booth(booth_id=2, room_id=10, language_code="fr")
        b3 = _make_db_booth(booth_id=3, room_id=20, language_code="de")
        result = self._call([ev], {1: [b1, b2, b3]})
        rooms = result[0]["rooms"]
        assert len(rooms) == 2
        assert sorted(len(r["booths"]) for r in rooms) == [1, 2]

    def test_live_count_counts_only_connected_booths(self):
        ev = _make_event()
        b1 = _make_db_booth(booth_id=1, room_id=1, language_code="en")
        b2 = _make_db_booth(booth_id=2, room_id=1, language_code="fr")
        live = MagicMock()
        live.ingest_status = "connected"
        store = MagicMock()
        store.get_booth_sync.side_effect = lambda bid: live if "en" in bid else None
        from portal.routers.public import _build_event_data
        with patch("portal.routers.public.booths", store), \
             patch("portal.routers.public.make_booth_id", side_effect=lambda s, r, lc: f"{s}:{r}:{lc}"):
            result = _build_event_data([ev], {1: [b1, b2]}, None, {}, {})
        assert result[0]["live_count"] == 1

    def test_anonymous_cannot_interpret(self):
        ev = _make_event()
        b = _make_db_booth()
        result = self._call([ev], {1: [b]}, current_user=None)
        assert result[0]["booths"][0]["can_interpret"] is False

    def test_admin_can_interpret_every_booth(self):
        ev = _make_event()
        b = _make_db_booth()
        result = self._call([ev], {1: [b]}, current_user={"is_admin": True})
        assert result[0]["booths"][0]["can_interpret"] is True
