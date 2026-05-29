import asyncio
import traceback
from datetime import date, timedelta
from app.core.database.target_pool import init_target_pool_registry

async def main():
    pool_registry = init_target_pool_registry()
    from app.core.database.session import init_engine, get_db
    init_engine()
    
    async for db in get_db():
        from sqlalchemy import select
        from app.core.database.models import DBConnection
        res = await db.execute(select(DBConnection).filter(DBConnection.name == "Test Neon Postgres"))
        conn = res.scalars().first()
        if not conn:
            print("No connection found in database!")
            return
            
        pool = await pool_registry.get_pool(conn)
        
        async with pool._pool.acquire() as pg_conn:
            print("Creating Syspro ERP mock tables...")
            await pg_conn.execute("""
                DROP TABLE IF EXISTS ApWithholdingTax;
                DROP TABLE IF EXISTS AdmWithholdingTax;
                DROP TABLE IF EXISTS ApInvoice;
                DROP TABLE IF EXISTS ApSupplier;
            """)
            
            await pg_conn.execute("""
                CREATE TABLE ApSupplier (
                    Supplier VARCHAR(50) PRIMARY KEY,
                    SupplierName VARCHAR(255),
                    SupplierClass VARCHAR(50)
                );
                
                CREATE TABLE ApInvoice (
                    Supplier VARCHAR(50) REFERENCES ApSupplier(Supplier),
                    Invoice VARCHAR(50),
                    Branch VARCHAR(50),
                    InvoiceDate DATE,
                    DueDate DATE,
                    Reference VARCHAR(255),
                    OrigInvValue NUMERIC(18,2),
                    MthInvBal1 NUMERIC(18,2),
                    Currency VARCHAR(10),
                    InvoiceStatus VARCHAR(10)
                );
                
                CREATE TABLE AdmWithholdingTax (
                    WithTaxCode VARCHAR(50) PRIMARY KEY,
                    Description VARCHAR(255),
                    CurTaxRate NUMERIC(5,2)
                );
                
                CREATE TABLE ApWithholdingTax (
                    WhtYear INT,
                    Period INT,
                    Supplier VARCHAR(50) REFERENCES ApSupplier(Supplier),
                    Invoice VARCHAR(50),
                    InvoiceDate DATE,
                    WithTaxCode VARCHAR(50) REFERENCES AdmWithholdingTax(WithTaxCode),
                    WithTaxRate NUMERIC(5,2),
                    WithTaxableValue NUMERIC(18,2),
                    WithTaxValue NUMERIC(18,2),
                    InvoiceValue NUMERIC(18,2),
                    Status VARCHAR(10)
                );
            """)
            print("Tables created successfully! Seeding mock data...")
            
            # Seed Suppliers
            await pg_conn.execute("""
                INSERT INTO ApSupplier (Supplier, SupplierName, SupplierClass) VALUES
                ('SUP001', 'Acme Supplier Corp', 'DOMESTIC'),
                ('SUP002', 'Global Office Supplies', 'DOMESTIC'),
                ('SUP003', 'Prime Logistical Services', 'FOREIGN'),
                ('SUP004', 'Apex Technology Solutions', 'SERVICES'),
                ('SUP005', 'Delta Consulting Group', 'FOREIGN')
            """)
            
            # Seed Invoices
            today = date.today()
            invoices = [
                # Supplier, Invoice, Branch, InvoiceDate, DueDate, Reference, OrigInvValue, MthInvBal1, Currency, InvoiceStatus
                ('SUP001', 'INV-1001', 'BR01', today - timedelta(days=15), today + timedelta(days=15), 'Ref Acme 1', 12500.00, 12500.00, 'USD', 'O'),
                ('SUP001', 'INV-1002', 'BR01', today - timedelta(days=45), today - timedelta(days=15), 'Ref Acme 2', 8400.00, 8400.00, 'USD', 'O'),
                ('SUP002', 'INV-2001', 'BR02', today - timedelta(days=5), today + timedelta(days=25), 'Ref Global 1', 3400.50, 0.00, 'USD', 'P'),
                ('SUP002', 'INV-2002', 'BR02', today - timedelta(days=75), today - timedelta(days=45), 'Ref Global 2', 9600.00, 9600.00, 'USD', 'O'),
                ('SUP003', 'INV-3001', 'BR01', today - timedelta(days=120), today - timedelta(days=90), 'Ref Prime 1', 15000.00, 15000.00, 'USD', 'O'),
                ('SUP004', 'INV-4001', 'BR03', today - timedelta(days=8), today + timedelta(days=22), 'Ref Apex 1', 54200.00, 54200.00, 'USD', 'O'),
                ('SUP005', 'INV-5001', 'BR01', today - timedelta(days=150), today - timedelta(days=120), 'Ref Delta 1', 22000.00, 22000.00, 'USD', 'O')
            ]
            
            for inv in invoices:
                await pg_conn.execute(
                    "INSERT INTO ApInvoice VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)",
                    *inv
                )
                
            # Seed Withholding Tax Codes
            await pg_conn.execute("""
                INSERT INTO AdmWithholdingTax (WithTaxCode, Description, CurTaxRate) VALUES
                ('WHT01', 'Standard 10% Withholding Tax', 10.00),
                ('WHT02', 'Reduced 5% Withholding Tax', 5.00)
            """)
            
            # Seed Withholding Tax deductions
            wht_records = [
                # WhtYear, Period, Supplier, Invoice, InvoiceDate, WithTaxCode, WithTaxRate, WithTaxableValue, WithTaxValue, InvoiceValue, Status
                (today.year, today.month, 'SUP001', 'INV-1001', today - timedelta(days=15), 'WHT01', 10.00, 12500.00, 1250.00, 12500.00, 'O'),
                (today.year, today.month, 'SUP004', 'INV-4001', today - timedelta(days=8), 'WHT02', 5.00, 54200.00, 2710.00, 54200.00, 'O')
            ]
            for w in wht_records:
                await pg_conn.execute(
                    "INSERT INTO ApWithholdingTax VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)",
                    *w
                )
                
            print("Successfully seeded all Syspro ERP mock tables!")

if __name__ == "__main__":
    asyncio.run(main())
