#!/usr/bin/env python3
"""
Repnex Secure Data Gateway Agent
---------------------------------
Tunnels queries from the Repnex cloud to your local database over a secure WebSocket.

Usage:
    # Run manually (foreground):
    python3 repnex-agent.py --server "wss://repnex-backend.onrender.com" --token "JWT" \
        --agent-name "my-laptop" --db-type mssql --db-host localhost --db-port 1433 \
        --db-user sa --db-password secret

    # Install as auto-start background service (run once):
    python3 repnex-agent.py --install-service --server "wss://..." --token "JWT" \
        --agent-name "my-laptop" --db-type mssql --db-host localhost --db-port 1433 \
        --db-user sa --db-password secret

    # Remove the background service:
    python3 repnex-agent.py --uninstall-service
"""
import asyncio
import json
import argparse
import logging
import os
import sys
import platform
import subprocess
import textwrap
import websockets

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("repnex-agent")

SCRIPT_PATH = os.path.abspath(__file__)
PYTHON_PATH = sys.executable
SERVICE_NAME = "RepnexGatewayAgent"

# ── Database drivers ──────────────────────────────────────────────────────────

def run_mssql_query(sql, params, db_name, args):
    import pymssql
    logger.info(f"Executing MSSQL query on database '{db_name}'...")
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
            rows = cursor.fetchall()
            for r in rows:
                for k, v in r.items():
                    if hasattr(v, 'isoformat'):
                        r[k] = v.isoformat()
                    elif hasattr(v, 'to_eng_string'):
                        r[k] = str(v)
                    elif hasattr(v, '__str__') and type(v).__name__ in ('Decimal', 'UUID'):
                        r[k] = str(v)
            return rows
    except Exception as e:
        logger.error(f"MSSQL Execution error: {e}")
        raise
    finally:
        if 'conn' in locals():
            conn.close()


async def run_postgres_query(sql, params, db_name, args):
    import asyncpg, re
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
        param_names = re.findall(r":([a-zA-Z0-9_]+)", sql)
        bound_values = [params.get(name) for name in param_names]
        for i, name in enumerate(param_names):
            sql = sql.replace(f":{name}", f"${i+1}")
        rows = await conn.fetch(sql, *bound_values)
        result = []
        for row in rows:
            d = dict(row)
            for k, v in d.items():
                if hasattr(v, 'isoformat'):
                    d[k] = v.isoformat()
                elif type(v).__name__ in ('Decimal', 'UUID'):
                    d[k] = str(v)
            result.append(d)
        return result
    except Exception as e:
        logger.error(f"Postgres Execution error: {e}")
        raise
    finally:
        if 'conn' in locals():
            await conn.close()


# ── Query handler ─────────────────────────────────────────────────────────────

async def handle_query(payload, args):
    query_id = payload.get("query_id")
    sql = payload.get("sql")
    params = payload.get("params", {})
    db_name = payload.get("db_name")
    db_type = payload.get("db_type")
    try:
        if db_type == "mssql":
            loop = asyncio.get_running_loop()
            data = await loop.run_in_executor(None, run_mssql_query, sql, params, db_name, args)
        elif db_type == "postgres":
            data = await run_postgres_query(sql, params, db_name, args)
        else:
            raise NotImplementedError(f"Database type '{db_type}' not supported by agent.")
        return {"action": "query_response", "query_id": query_id, "status": "success", "data": data}
    except Exception as e:
        return {"action": "query_response", "query_id": query_id, "status": "error", "error": str(e)}


# ── WebSocket loop ────────────────────────────────────────────────────────────

async def agent_loop(args):
    uri = f"{args.server}/ws/gateway?token={args.token}&agent_name={args.agent_name}"
    logger.info(f"Connecting to Repnex Backend at: {args.server}")
    logger.info(f"Agent Name  : '{args.agent_name}'")
    logger.info(f"Database    : {args.db_host}:{args.db_port} ({args.db_type.upper()})")

    while True:
        try:
            async with websockets.connect(uri, ping_interval=20, ping_timeout=20) as websocket:
                logger.info("✅ Connected and registered with cloud. Waiting for queries...")
                while True:
                    message_raw = await websocket.recv()
                    payload = json.loads(message_raw)
                    if payload.get("action") == "query":
                        logger.info(f"Query received: {payload.get('query_id')}")
                        response = await handle_query(payload, args)
                        await websocket.send(json.dumps(response))
                        logger.info(f"Query answered: {payload.get('query_id')}")
        except (websockets.exceptions.ConnectionClosed, OSError) as e:
            logger.warning(f"Connection lost: {e}. Retrying in 5 seconds...")
            await asyncio.sleep(5)
        except Exception as e:
            logger.error(f"Unexpected error: {e}. Retrying in 5 seconds...")
            await asyncio.sleep(5)


# ── Service installer helpers ─────────────────────────────────────────────────

def _build_agent_args(args) -> str:
    """Build the argument string for the service command."""
    return (
        f'--server "{args.server}" '
        f'--token "{args.token}" '
        f'--agent-name "{args.agent_name}" '
        f'--db-type "{args.db_type}" '
        f'--db-host "{args.db_host}" '
        f'--db-port "{args.db_port}" '
        f'--db-user "{args.db_user}" '
        f'--db-password "{args.db_password}"'
    )


def install_service_windows(args):
    """Register agent as a Windows Task Scheduler task that starts at system startup."""
    agent_args = _build_agent_args(args)
    task_cmd = f'"{PYTHON_PATH}" "{SCRIPT_PATH}" {agent_args}'

    print(f"\n📦 Installing '{SERVICE_NAME}' as Windows Startup Service...")
    print(f"   Python  : {PYTHON_PATH}")
    print(f"   Script  : {SCRIPT_PATH}")

    # Remove existing task if present
    subprocess.run(
        ["schtasks", "/Delete", "/TN", SERVICE_NAME, "/F"],
        capture_output=True
    )

    # Create task at system startup, run as SYSTEM, highest privileges
    result = subprocess.run(
        [
            "schtasks", "/Create", "/F",
            "/TN", SERVICE_NAME,
            "/TR", task_cmd,
            "/SC", "ONSTART",
            "/DELAY", "0001:00",   # 1 min after startup (wait for network)
            "/RU", "SYSTEM",
            "/RL", "HIGHEST"
        ],
        capture_output=True, text=True
    )

    if result.returncode != 0:
        # Fallback: create task at user logon (doesn't need SYSTEM)
        result = subprocess.run(
            [
                "schtasks", "/Create", "/F",
                "/TN", SERVICE_NAME,
                "/TR", task_cmd,
                "/SC", "ONLOGON",
                "/DELAY", "0000:30",
                "/RL", "HIGHEST"
            ],
            capture_output=True, text=True
        )

    if result.returncode == 0:
        # Start immediately
        run_result = subprocess.run(
            ["schtasks", "/Run", "/TN", SERVICE_NAME],
            capture_output=True, text=True
        )
        if run_result.returncode == 0:
            print(f"\n✅ SUCCESS! '{SERVICE_NAME}' is installed and running.")
            print("   ▸ It will auto-start every time Windows boots.")
            print(f"   ▸ To stop : schtasks /End /TN {SERVICE_NAME}")
            print(f"   ▸ To remove: python repnex-agent.py --uninstall-service")
        else:
            print(f"\n✅ Service registered. Start manually:")
            print(f"   schtasks /Run /TN {SERVICE_NAME}")
    else:
        print(f"\n❌ Failed to register task:\n{result.stderr}")
        print("\n💡 Try running this script as Administrator.")
        sys.exit(1)


def uninstall_service_windows():
    print(f"🗑️  Removing Windows Task: '{SERVICE_NAME}'...")
    result = subprocess.run(
        ["schtasks", "/Delete", "/TN", SERVICE_NAME, "/F"],
        capture_output=True, text=True
    )
    if result.returncode == 0:
        print(f"✅ Removed '{SERVICE_NAME}' from Task Scheduler.")
    else:
        print(f"⚠️  Task not found or already removed: {result.stderr.strip()}")


def install_service_linux(args):
    """Create and enable a systemd service for the agent."""
    agent_args = _build_agent_args(args)
    service_content = textwrap.dedent(f"""\
        [Unit]
        Description=Repnex Secure Data Gateway Agent
        After=network-online.target
        Wants=network-online.target

        [Service]
        Type=simple
        ExecStart={PYTHON_PATH} {SCRIPT_PATH} {agent_args}
        Restart=always
        RestartSec=10
        StandardOutput=journal
        StandardError=journal

        [Install]
        WantedBy=multi-user.target
    """)

    service_path = f"/etc/systemd/system/{SERVICE_NAME}.service"
    print(f"\n📦 Installing '{SERVICE_NAME}' as systemd service...")

    try:
        with open(service_path, "w") as f:
            f.write(service_content)
    except PermissionError:
        # Write locally and give instructions
        local_path = f"./{SERVICE_NAME}.service"
        with open(local_path, "w") as f:
            f.write(service_content)
        print(f"\n⚠️  Run the following as sudo to complete installation:\n")
        print(f"   sudo mv {local_path} {service_path}")
        print(f"   sudo systemctl daemon-reload")
        print(f"   sudo systemctl enable {SERVICE_NAME}")
        print(f"   sudo systemctl start {SERVICE_NAME}")
        return

    os.system("systemctl daemon-reload")
    os.system(f"systemctl enable {SERVICE_NAME}")
    result = os.system(f"systemctl start {SERVICE_NAME}")

    if result == 0:
        print(f"\n✅ SUCCESS! '{SERVICE_NAME}' is installed and running.")
        print("   ▸ It will auto-start every time the server boots.")
        print(f"   ▸ Logs   : journalctl -u {SERVICE_NAME} -f")
        print(f"   ▸ Status : systemctl status {SERVICE_NAME}")
        print(f"   ▸ Remove : python3 repnex-agent.py --uninstall-service")
    else:
        print(f"\n⚠️  Service registered but could not start. Check logs:")
        print(f"   journalctl -u {SERVICE_NAME} -n 50")


def uninstall_service_linux():
    print(f"🗑️  Removing systemd service '{SERVICE_NAME}'...")
    os.system(f"systemctl stop {SERVICE_NAME}")
    os.system(f"systemctl disable {SERVICE_NAME}")
    service_path = f"/etc/systemd/system/{SERVICE_NAME}.service"
    if os.path.exists(service_path):
        os.remove(service_path)
        os.system("systemctl daemon-reload")
        print(f"✅ Removed '{SERVICE_NAME}' service.")
    else:
        print(f"⚠️  Service file not found at {service_path}.")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Repnex Secure Data Gateway Agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Examples:
              # Run manually:
              python3 repnex-agent.py --server "wss://repnex-backend.onrender.com" \\
                --token "JWT_TOKEN" --agent-name "my-laptop" \\
                --db-type mssql --db-host localhost --db-port 1433 \\
                --db-user sa --db-password secret

              # Install as auto-start service (run once):
              python3 repnex-agent.py --install-service --server "wss://..." \\
                --token "JWT_TOKEN" --agent-name "my-laptop" \\
                --db-type mssql --db-host localhost --db-port 1433 \\
                --db-user sa --db-password secret

              # Remove service:
              python3 repnex-agent.py --uninstall-service
        """)
    )

    parser.add_argument("--install-service", action="store_true",
                        help="Install agent as a background auto-start service and exit")
    parser.add_argument("--uninstall-service", action="store_true",
                        help="Remove the background service and exit")

    parser.add_argument("--token", default="", help="Your Repnex User JWT Token")
    parser.add_argument("--agent-name", default="my-laptop", help="Unique name for this gateway agent")
    parser.add_argument("--server", default="ws://localhost:8000",
                        help="Repnex Backend Server address (wss://... for production)")
    parser.add_argument("--db-type", choices=["mssql", "postgres"], default="mssql")
    parser.add_argument("--db-host", default="localhost")
    parser.add_argument("--db-port", default="1433")
    parser.add_argument("--db-user", default="sa")
    parser.add_argument("--db-password", default="")

    args = parser.parse_args()
    os_name = platform.system()

    # ── Service management ────────────────────────────────────────────────────
    if args.uninstall_service:
        if os_name == "Windows":
            uninstall_service_windows()
        else:
            uninstall_service_linux()
        return

    if args.install_service:
        if not args.token:
            print("❌ --token is required for --install-service")
            sys.exit(1)
        if os_name == "Windows":
            install_service_windows(args)
        else:
            install_service_linux(args)
        return

    # ── Normal run ────────────────────────────────────────────────────────────
    if not args.token:
        print("❌ --token is required. Get it from the Repnex UI (Gateway Mode > copy command).")
        sys.exit(1)

    try:
        asyncio.run(agent_loop(args))
    except KeyboardInterrupt:
        logger.info("Agent stopped by user.")


if __name__ == "__main__":
    main()
