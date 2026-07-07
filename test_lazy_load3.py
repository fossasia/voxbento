import asyncio
from portal.database import get_session, get_user_by_email, set_room_membership, list_room_memberships_for_user
from portal.models import Room
from sqlalchemy import select

async def main():
    async with get_session() as session:
        user = await get_user_by_email(session, "arnav@gmail.com")
        room = (await session.execute(select(Room))).scalars().first()
        if user and room:
            await set_room_membership(session, user_id=user.id, room_id=room.id, role="coordinator")
            await session.commit()
            
    async with get_session() as session2:
        user = await get_user_by_email(session2, "arnav@gmail.com")
        rms = await list_room_memberships_for_user(session2, user.id)
        
    for m in rms:
        print("Room:", m.room)
        print("Event:", m.room.event)

asyncio.run(main())
