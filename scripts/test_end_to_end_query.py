import requests
import json
import sys
import uuid

URL = "http://localhost:8000/v1"
CREDENTIALS = [
    {"email": "thesharmakeshav@gmail.com", "password": "Secret123!"},
    {"email": "testuser@example.com", "password": "Secret123!"}
]

def test_flow():
    token = None
    user_info = None
    for creds in CREDENTIALS:
        try:
            print(f"Attempting login for: {creds['email']} ...")
            response = requests.post(f"{URL}/auth/login", json=creds)
            if response.status_code == 200:
                data = response.json()
                token = data["tokens"]["access_token"]
                user_info = data["user"]
                print(f"✅ Login successful! User: {user_info['email']}, Org ID: {user_info['org_id']}")
                break
            else:
                print(f"❌ Login failed ({response.status_code}): {response.text}")
        except Exception as e:
            print(f"⚠️ Error connecting to server: {e}")

    if not token:
        print("🔴 Could not log in with any credentials. Is the backend server running on port 8000?")
        sys.exit(1)

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }

    # Get connection ID
    print("\n🔍 Fetching active database connections ...")
    conn_resp = requests.get(f"{URL}/connections", headers=headers)
    connections = conn_resp.json()
    
    connection_id = None
    if isinstance(connections, list) and len(connections) > 0:
        connection_id = connections[0]["id"]
        print(f"Found active connection: {connections[0]['name']} (ID: {connection_id})")
    else:
        print("No connections found. Creating a test PostgreSQL connection...")
        # Let's create one
        create_payload = {
            "name": "Test Neon Postgres",
            "db_type": "postgres",
            "host": "ep-orange-bread-a5x2mlow-pooler.us-east-2.aws.neon.tech",
            "port": 5432,
            "db_name": "neondb",
            "username": "neondb_owner",
            "password": "npg_nl8WPyAp0OVv",
            "ssl_enabled": True
        }
        create_resp = requests.post(f"{URL}/connections", json=create_payload, headers=headers)
        if create_resp.status_code == 201:
            conn_data = create_resp.json()
            connection_id = conn_data["id"]
            print(f"✅ Created connection: {conn_data['name']} (ID: {connection_id})")
        else:
            print(f"❌ Failed to create connection ({create_resp.status_code}): {create_resp.text}")
            # We can still test intent parsing without connection_id
            print("Will test intent parsing without connection_id.")

    # Test queries
    test_queries = [
        "Generate last 6 months AP invoices",
        "withholding tax deducted on supplier invoices",
        "AP ageing report"
    ]

    for query_text in test_queries:
        print(f"\n💬 Sending query: \"{query_text}\"")
        session_id = str(uuid.uuid4())
        payload = {
            "natural_language": query_text,
            "session_id": session_id
        }
        if connection_id:
            payload["connection_id"] = connection_id

        try:
            response = requests.post(f"{URL}/query/chat", json=payload, headers=headers)
            if response.status_code == 200:
                res_data = response.json()
                print(f"✅ Response type: {res_data.get('type')}")
                print(f"   Message: {res_data.get('message')}")
                if res_data.get("template_id"):
                    print(f"   Matched Template: {res_data.get('template_id')}")
                if res_data.get("extracted_params"):
                    print(f"   Extracted params: {res_data.get('extracted_params')}")
                if res_data.get("missing_params"):
                    print(f"   Missing params: {res_data.get('missing_params')}")
                if res_data.get("sql"):
                    print("   Generated SQL:")
                    print(f"     {res_data.get('sql')[:150]}...")
                if res_data.get("rows") is not None:
                    print(f"   Rows returned: {res_data.get('rows_returned')}")
                    if len(res_data.get("rows")) > 0:
                        print(f"   First row sample: {res_data.get('rows')[0]}")
            else:
                print(f"❌ API Error ({response.status_code}): {response.text}")
        except Exception as e:
            print(f"⚠️ Query request failed: {e}")

if __name__ == "__main__":
    test_flow()
