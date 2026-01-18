import aiosqlite
import asyncio

async def main():
    db = await aiosqlite.connect('/app/data/flow.db')
    cursor = await db.execute('SELECT id, SUBSTR(at, 1, 16) as account_id FROM tokens')
    rows = await cursor.fetchall()
    print('Tokens:')
    for r in rows:
        print(f'  ID={r[0]}, account_id={r[1]}')
    await db.close()

asyncio.run(main())
