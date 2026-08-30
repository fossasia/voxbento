import asyncio

from sqlalchemy import select

from portal.database import get_session
from portal.models import DBBooth, Event


async def main():
    async with get_session() as db_session:
        stmt = select(Event.slug, DBBooth.room_id, DBBooth.language_code, DBBooth.language_name).join(Event)
        res = await db_session.execute(stmt)
        for r in res.fetchall():
            print(r)


if __name__ == "__main__":
    asyncio.run(main())
