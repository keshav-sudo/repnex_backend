import asyncio
import traceback
from app.core.database.models import DBConnection
from app.core.database.target_pool import init_target_pool_registry
from app.query_engine.template_loader import init_template_registry
from app.query_engine.parameter_binder import bind

async def main():
    pool_registry = init_target_pool_registry()
    from app.core.database.session import init_engine, get_db
    init_engine()
    
    async for db in get_db():
        from sqlalchemy import select
        res = await db.execute(select(DBConnection).filter(DBConnection.name == "Test Neon Postgres"))
        conn = res.scalars().first()
        if not conn:
            print("No connection found in database!")
            return
            
        print(f"Testing execution with connection: {conn.name}")
        pool = await pool_registry.get_pool(conn)
        
        reg = init_template_registry()
        t = reg.get("ap_invoices_by_date_range")
        bound = bind(t, {"start_date": "last 6 months"}, db_type="postgres")
        
        try:
            print("Fetching stream...")
            async for batch in pool.fetch_stream(bound.sql, bound.params, batch_size=10, timeout=10):
                print(f"Batch returned: {batch}")
            print("Stream fetched successfully!")
        except Exception:
            traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(main())
