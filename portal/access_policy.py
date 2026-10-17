from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select

from portal.models import Room
from portal.roles import _ROLE_RANK, ROLE_PERMISSIONS, Permission


@dataclass
class AccessPolicy:
    """Authorization policy built from current database memberships."""

    is_super_admin: bool = False
    event_roles: dict[int, str] = field(default_factory=dict)
    room_roles: dict[int, str] = field(default_factory=dict)
    room_events: dict[int, int] = field(default_factory=dict)
    booth_roles: dict[int, str] = field(default_factory=dict)
    booth_scopes: dict[int, tuple[int, int]] = field(default_factory=dict)

    @classmethod
    async def from_session(cls, session, user) -> AccessPolicy:
        """Build a policy from the user's current database memberships."""
        from portal.database import (
            list_booth_memberships_for_user,
            list_memberships_for_user,
            list_room_memberships_for_user,
        )

        event_memberships = await list_memberships_for_user(session, user.id)
        room_memberships = await list_room_memberships_for_user(session, user.id)
        booth_memberships = await list_booth_memberships_for_user(session, user.id)

        policy = cls.from_memberships(
            user,
            event_memberships,
            room_memberships,
            booth_memberships,
        )

        owned_event_ids = [membership.event_id for membership in event_memberships if membership.role == "event_owner"]

        if owned_event_ids:
            result = await session.execute(select(Room.id, Room.event_id).where(Room.event_id.in_(owned_event_ids)))
            policy.room_events.update(dict(result.all()))

        return policy

    @classmethod
    def from_memberships(
        cls,
        user,
        event_memberships,
        room_memberships,
        booth_memberships,
    ) -> AccessPolicy:
        policy = cls(is_super_admin=bool(user.is_admin))

        for membership in event_memberships:
            policy.event_roles[membership.event_id] = membership.role

        for membership in room_memberships:
            room = membership.room
            if room is not None:
                policy.room_roles[membership.room_id] = membership.role
                policy.room_events[membership.room_id] = room.event_id

        for membership in booth_memberships:
            booth = membership.booth
            if booth is not None:
                policy.booth_roles[membership.booth_id] = membership.role
                policy.booth_scopes[membership.booth_id] = (
                    booth.room_id,
                    booth.event_id,
                )
                policy.room_events.setdefault(booth.room_id, booth.event_id)

        return policy

    @staticmethod
    def highest_ranked_role(roles: list[str]) -> str | None:
        return max(roles, key=lambda role: _ROLE_RANK.get(role, 0), default=None)

    @staticmethod
    def can_perform_role(
        granted_role: str | None,
        requested_role: str,
    ) -> bool:
        if granted_role is None:
            return False
        return _ROLE_RANK.get(granted_role, -1) >= _ROLE_RANK.get(requested_role, 0)

    def _roles_for_scope(
        self,
        *,
        event_id: int | None = None,
        room_id: int | None = None,
        booth_id: int | None = None,
    ) -> list[str]:
        roles: list[str] = []

        if booth_id is not None:
            scope = self.booth_scopes.get(booth_id)
            if scope is None:
                return []

            scoped_room_id, scoped_event_id = scope
            roles.extend(
                [
                    self.booth_roles.get(booth_id),
                    self.room_roles.get(scoped_room_id),
                    self.event_roles.get(scoped_event_id),
                ]
            )
        elif room_id is not None:
            event_id = self.room_events.get(room_id)
            if event_id is None:
                return []

            roles.append(self.room_roles.get(room_id))

            event_role = self.event_roles.get(event_id)
            if event_role == "event_owner":
                roles.append(event_role)

        elif event_id is not None:
            roles.append(self.event_roles.get(event_id))
        else:
            roles.extend(self.event_roles.values())
            roles.extend(self.room_roles.values())
            roles.extend(self.booth_roles.values())

        return [role for role in roles if role in ROLE_PERMISSIONS]

    def role_for(
        self,
        *,
        event_id: int | None = None,
        room_id: int | None = None,
        booth_id: int | None = None,
    ) -> str | None:
        if self.is_super_admin:
            return "super_admin"

        roles = self._roles_for_scope(
            event_id=event_id,
            room_id=room_id,
            booth_id=booth_id,
        )
        return max(roles, key=lambda role: _ROLE_RANK[role], default=None)

    def can(
        self,
        permission: Permission,
        *,
        event_id: int | None = None,
        room_id: int | None = None,
        booth_id: int | None = None,
    ) -> bool:
        if self.is_super_admin:
            return True

        return any(
            permission in ROLE_PERMISSIONS[role]
            for role in self._roles_for_scope(
                event_id=event_id,
                room_id=room_id,
                booth_id=booth_id,
            )
        )

    def has_role(
        self,
        role: str,
        *,
        event_id: int | None = None,
        room_id: int | None = None,
        booth_id: int | None = None,
    ) -> bool:
        if self.is_super_admin:
            return role == "super_admin"

        return role in self._roles_for_scope(
            event_id=event_id,
            room_id=room_id,
            booth_id=booth_id,
        )

    def has_room_coordinator_in_event(self, event_id: int) -> bool:
        return any(
            role == "room_coordinator" and self.room_events.get(room_id) == event_id
            for room_id, role in self.room_roles.items()
        )

    def has_any_role(self, role: str) -> bool:
        return (
            role in self.event_roles.values() or role in self.room_roles.values() or role in self.booth_roles.values()
        )


@dataclass(frozen=True)
class Principal:
    """An active, database-verified user and their authorization policy."""

    user_id: int
    policy: AccessPolicy
    is_active: bool = True

    @property
    def is_global_admin(self) -> bool:
        return self.is_active and self.policy.is_super_admin

    def can(
        self,
        permission: Permission,
        *,
        event_id: int | None = None,
        room_id: int | None = None,
        booth_id: int | None = None,
    ) -> bool:
        return self.is_active and self.policy.can(
            permission,
            event_id=event_id,
            room_id=room_id,
            booth_id=booth_id,
        )

    def role_for(
        self,
        *,
        event_id: int | None = None,
        room_id: int | None = None,
        booth_id: int | None = None,
    ) -> str | None:
        if not self.is_active:
            return None
        return self.policy.role_for(
            event_id=event_id,
            room_id=room_id,
            booth_id=booth_id,
        )

    def has_role(
        self,
        role: str,
        *,
        event_id: int | None = None,
        room_id: int | None = None,
        booth_id: int | None = None,
    ) -> bool:
        return self.is_active and self.policy.has_role(
            role,
            event_id=event_id,
            room_id=room_id,
            booth_id=booth_id,
        )

    def has_any_role(self, role: str) -> bool:
        return self.is_active and self.policy.has_any_role(role)

    def has_room_coordinator_in_event(self, event_id: int) -> bool:
        return self.is_active and self.policy.has_room_coordinator_in_event(event_id)
