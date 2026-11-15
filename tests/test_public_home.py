from __future__ import annotations

from types import SimpleNamespace

import pytest

from portal.roles import EVENT_OWNER, ROOM_COORDINATOR
from portal.routers.public import _management_home


@pytest.mark.parametrize(
    ("current_user", "db_user", "event_memberships", "room_memberships", "booth_memberships", "expected"),
    [
        ({"is_admin": True}, None, (), (), (), "/admin/"),
        ({"is_admin": False}, SimpleNamespace(can_host=True), (), (), (), "/workspace/"),
        ({"is_admin": False}, None, (SimpleNamespace(role=EVENT_OWNER),), (), (), "/workspace/"),
        ({"is_admin": False}, None, (), (SimpleNamespace(role=ROOM_COORDINATOR),), (), "/mission-control/"),
        ({"is_admin": False}, None, (), (), (SimpleNamespace(),), "/interpreter"),
        ({"is_admin": False}, None, (), (), (), "/account"),
        (None, None, (), (), (), "/account"),
    ],
)
def test_management_home_selects_the_expected_destination(
    current_user,
    db_user,
    event_memberships,
    room_memberships,
    booth_memberships,
    expected,
):
    assert _management_home(current_user, db_user, event_memberships, room_memberships, booth_memberships) == expected


def test_management_home_prefers_higher_priority_memberships():
    assert (
        _management_home(
            {"is_admin": False},
            event_memberships=(SimpleNamespace(role=EVENT_OWNER),),
            room_memberships=(SimpleNamespace(role=ROOM_COORDINATOR),),
            booth_memberships=(SimpleNamespace(),),
        )
        == "/workspace/"
    )
