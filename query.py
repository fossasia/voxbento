import asyncio

from sqlalchemy import select

from portal.database import get_session
from portal.models import BoothMembership, User


async def main():
    async with get_session() as session:
        res = await session.execute(select(User).where(User.email.ilike("arnav@gmail.com")))
        user = res.scalars().first()
        if user:
            print(f"User: {user.email}, is_admin={user.is_admin}")
            bms = await session.execute(select(BoothMembership).where(BoothMembership.user_id == user.id))
            for bm in bms.scalars().all():
                print(f"BoothMembership: role={bm.role}, booth_id={bm.booth_id}")
        else:
            print("User not found")


if __name__ == "__main__":
    asyncio.run(main())
