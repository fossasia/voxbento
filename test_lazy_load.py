import asyncio
from portal.database import get_session, get_user_by_email, set_booth_membership, list_booth_memberships_for_user
from portal.models import DBBooth
from sqlalchemy import select

async def main():
    async with get_session() as session:
        user = await get_user_by_email(session, "arnav@gmail.com")
        bms = await list_booth_memberships_for_user(session, user.id)
        
    for m in bms:
        try:
            print("Accessing room...")
            print("Room:", m.booth.room.name)
        except Exception as e:
            print("Exception accessing room:", type(e))
        try:
            print("Accessing event...")
            print("Event:", m.booth.event.display_name)
        except Exception as e:
            print("Exception accessing event:", type(e))

asyncio.run(main())
