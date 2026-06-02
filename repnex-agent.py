#!/usr/bin/env python3
import asyncio
import json
import argparse
import logging
import sys
import websockets

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("repnex-agent")

def run_mssql_query(sql, params, db_name, args):
    import pymssql
    logger.info(f"Executing MSSQL query on database '{db_name}'...")
    
    # pymssql expects positional parameters in tuple or dict
    # If the backend passes dict params, we can pass them directly to pymssql execute.
    # Convert parameters to appropriate formats if needed.
    try:
        conn = pymssql.connect(
            server=args.db_host,
            port=int(args.db_port),
            user=args.db_user,
            password=args.db_password,
            database=db_name,
            login_timeout=10,
            timeout=30
        )
        with conn.cursor(as_dict=True) as cursor:
            cursor.execute(sql, params)
            # Fetch all rows
            rows = cursor.fetchall()
            # Convert any non-serializable objects (datetime, decimal) to strings
            for r in rows:
                for k, v in r.items():
                    if hasattr(v, 'isoformat'): # datetime/date
                        r[k] = v.isoformat()
                    elif hasattr(v, 'to_eng_string'): # decimal
                        r[k] = str(v)
                    elif hasattr(v, '__str__') and type(v).__name__ in ('Decimal', 'UUID'):
                        r[k] = str(v)
            return rows
    except Exception as e:
        logger.error(f"MSSQL Execution error: {e}")
        raise e
    finally:
        if 'conn' in locals():
            conn.close()

async def run_postgres_query(sql, params, db_name, args):
    import asyncpg
    logger.info(f"Executing Postgres query on database '{db_name}'...")
    try:
        conn = await asyncpg.connect(
            host=args.db_host,
            port=int(args.db_port),
            user=args.db_user,
            password=args.db_password,
            database=db_name,
            timeout=30
        )
        
        # Convert sql named params to positional parameters ($1, $2)
        # In target_pool, queries are converted. But just in case, we support simple execution.
        # Postgres expects positional arguments. Let's convert named placeholders (e.g. :param) to positional ($1) if present.
        # Since target_pool already does binding or raw string queries, let's execute.
        # If there are params, we pass them.
        try:
            # Simple named parameter converter for standard SQL templates
            import re
            param_names = re.findall(r":([a-zA-Z0-9_]+)", sql)
            bound_values = []
            for name in param_names:
                bound_values.append(params.get(name))
                
            # Replace :name with $1, $2
            for i, name in enumerate(param_names):
                sql = sql.replace(f":{name}", f"${i+1}")
                
            rows = await conn.fetch(sql, *bound_values)
            result = []
            for row in rows:
                d = dict(row)
                for k, v in d.items():
                    if hasattr(v, 'isoformat'):
                        d[k] = v.isoformat()
                    elif hasattr(v, '__str__') and type(v).__name__ in ('Decimal', 'UUID'):
                        d[k] = str(v)
                result.append(d)
            return result
        finally:
            await conn.close()
    except Exception as e:
        logger.error(f"Postgres Execution error: {e}")
        raise e

async def handle_query(payload, args):
    query_id = payload.get("query_id")
    sql = payload.get("sql")
    params = payload.get("params", {})
    db_name = payload.get("db_name")
    db_type = payload.get("db_type")

    try:
        if db_type == "mssql":
            # Run MSSQL in thread executor to not block async loop
            loop = asyncio.get_running_loop()
            data = await loop.run_in_executor(None, run_mssql_query, sql, params, db_name, args)
        elif db_type == "postgres":
            data = await run_postgres_query(sql, params, db_name, args)
        else:
            raise NotImplementedError(f"Database type '{db_type}' not supported by agent.")

        return {
            "action": "query_response",
            "query_id": query_id,
            "status": "success",
            "data": data
        }
    except Exception as e:
        return {
            "action": "query_response",
            "query_id": query_id,
            "status": "error",
            "error": str(e)
        }

async def agent_loop(args):
    uri = f"{args.server}/ws/gateway?token={args.token}&agent_name={args.agent_name}"
    logger.info(f"Connecting to Repnex Backend Gateway at: {args.server}...")
    logger.info(f"Agent Name: '{args.agent_name}'")
    logger.info(f"Target DB Host: {args.db_host}:{args.db_port} ({args.db_type.upper()})")

    while True:
        try:
            async with websockets.connect(uri, ping_interval=20, ping_timeout=20) as websocket:
                logger.info("✅ Connection established! Registered with cloud.")
                
                while True:
                    message_raw = await websocket.recv()
                    payload = json.loads(message_raw)
                    
                    if payload.get("action") == "query":
                        logger.info(f"Received query request: {payload.get('query_id')}")
                        response = await handle_query(payload, args)
                        await websocket.send(json.dumps(response))
                        logger.info(f"Sent query response for: {payload.get('query_id')}")
                        
        except (websockets.exceptions.ConnectionClosed, OSError) as e:
            logger.warning(f"Connection lost: {e}. Retrying in 5 seconds...")
            await asyncio.sleep(5)
        except Exception as e:
            logger.error(f"Unexpected error: {e}. Retrying in 5 seconds...")
            await asyncio.sleep(5)

def main():
    parser = argparse.ArgumentParser(description="Repnex Secure Data Gateway Agent")
    parser.add_argument("--token", required=True, help="Your Repnex User JWT Token")
    parser.add_argument("--agent-name", default="my-laptop", help="Unique name for this gateway agent")
    parser.add_argument("--server", default="ws://localhost:8000", help="Repnex Backend Server address (ws/wss)")
    
    # Target database parameters
    parser.add_argument("--db-type", choices=["mssql", "postgres"], default="mssql", help="Target database type")
    parser.add_argument("--db-host", default="localhost", help="Host of the database relative to this agent")
    parser.add_argument("--db-port", default="1433", help="Port of the database relative to this agent")
    parser.add_argument("--db-user", default="sa", help="Database username")
    parser.add_argument("--db-password", default="", help="Database password")

    args = parser.parse_args()
    
    try:
        asyncio.run(agent_loop(args))
    except KeyboardInterrupt:
        logger.info("Agent stopped by user.")

if __name__ == "__main__":
    main()
