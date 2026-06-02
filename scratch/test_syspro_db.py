import asyncio
import pymssql
import sys

def test_conn():
    server = '192.168.31.64'
    user = 'syspro'
    password = 'syspr0'
    print(f"Attempting to connect to SQL Server at {server}:1433 as user '{user}'...")
    try:
        conn = pymssql.connect(
            server=server,
            port=1433,
            user=user,
            password=password,
            database='master',
            login_timeout=5,
            timeout=5
        )
        print("✅ Connection to master database successful!")
        with conn.cursor() as cur:
            cur.execute("SELECT name FROM sys.databases WHERE name NOT IN ('tempdb', 'model', 'msdb') AND state_desc = 'ONLINE'")
            dbs = [row[0] for row in cur.fetchall()]
            print("Available Databases:", dbs)
        conn.close()
    except Exception as e:
        print(f"❌ Connection failed: {e.__class__.__name__}: {e}")

if __name__ == "__main__":
    test_conn()
