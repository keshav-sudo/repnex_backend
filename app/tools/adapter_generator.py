import argparse
import asyncio
import json
import uuid
from motor.motor_asyncio import AsyncIOMotorClient
from app.core.config import get_settings
from app.services.connection_service import get_connection_by_id, sync_schema
from app.query_engine.semantic_resolver import SemanticResolver
from app.llm.client import get_llm
from app.core.security.auth import CurrentUser

async def generate_adapters(conn_id_str: str, erp_type: str):
    print(f"[*] Initializing connection for ID: {conn_id_str}...")
    settings = get_settings()
    
    # Establish MongoDB connection
    db_name = "repnex"
    cleaned_url = settings.DATABASE_URL
    if "://" in cleaned_url:
        cleaned_url = cleaned_url.split("://", 1)[1]
    if "/" in cleaned_url:
        parts = cleaned_url.split("/", 1)
        if len(parts) > 1 and parts[1]:
            possible_db = parts[1].split("?")[0]
            if possible_db and "=" not in possible_db and "&" not in possible_db:
                db_name = possible_db

    client = AsyncIOMotorClient(settings.DATABASE_URL)
    db = client[db_name]

    conn_id = uuid.UUID(conn_id_str)
    conn = await get_connection_by_id(db, conn_id)
    
    # Check if schema_info is populated, otherwise sync schema
    if not conn.schema_info or "tables" not in conn.schema_info:
        print("[*] Schema not synced. Syncing schema now (please wait)...")
        # Creating a mock admin current user to satisfy signature
        admin_user = CurrentUser(
            user_id=uuid.uuid4(),
            org_id=uuid.UUID(conn.org_id),
            role="admin",
            email="admin@repnex.com"
        )
        await sync_schema(db, admin_user, conn_id)
        # Fetch updated connection
        conn = await get_connection_by_id(db, conn_id)
        print("[+] Schema synced successfully.")
        
    tables_metadata = conn.schema_info.get("tables", [])
    print(f"[+] Found {len(tables_metadata)} tables in the database schema.")

    # Load local universal concepts
    resolver = SemanticResolver(erp_type="syspro", db=db)
    ontology = await resolver.load_ontology()
    print(f"[+] Loaded {len(ontology)} universal business concepts.")

    # Build schema summary for LLM prompt
    schema_summary = []
    for t in tables_metadata:
        cols = [c["name"] for c in t.get("columns", [])]
        schema_summary.append(f"Table: {t['name']}\nColumns: {', '.join(cols)}")
    schema_context = "\n\n".join(schema_summary)

    llm = get_llm()

    for concept_name, concept_def in ontology.items():
        print(f"[*] Mapping concept: {concept_name}...")
        prompt = f"""
You are an expert database administrator. Your job is to match a universal business concept definition to a physical database schema.

### UNIVERSAL CONCEPT: {concept_name}
Description: {concept_def.get('description')}
Fields: {json.dumps(concept_def.get('fields', {}), indent=2)}
Synonyms: {json.dumps(concept_def.get('synonyms', []), indent=2)}

### PHYSICAL DATABASE SCHEMA:
{schema_context}

### OUTPUT FORMAT:
You MUST output ONLY a valid JSON object matching the following structure. Do not output any other text or markdown blocks.
{{
  "erp": "{erp_type}",
  "concept": "{concept_name}",
  "table": "MatchedTableName",
  "alias": "t",
  "fields": {{
     "universal_field_1": "t.matched_column_1",
     "universal_field_2": "t.matched_column_2"
  }}
}}
"""
        response_text = await llm.chat_text(
            system="You are a precise schema matching assistant. Output ONLY valid raw JSON.",
            user=prompt,
            max_tokens=1000
        )
        
        # Clean up response to get raw JSON
        import re
        json_match = re.search(r"({.*})", response_text.strip(), re.DOTALL)
        if json_match:
            response_text = json_match.group(1)
            
        try:
            adapter_json = json.loads(response_text)
            # Add dynamic ID
            adapter_json["_id"] = f"{erp_type}:{concept_name}"
            # Save to MongoDB v2_adapters
            await db["v2_adapters"].update_one(
                {"_id": adapter_json["_id"]},
                {"$set": adapter_json},
                upsert=True
            )
            print(f"[+] Successfully mapped and pushed adapter for concept '{concept_name}' to MongoDB.")
        except Exception as e:
            print(f"[!] Failed to parse or save LLM response for {concept_name}: {e}")
            print(f"Raw LLM response was:\n{response_text}")

    # Generate Joins relationship dynamically
    print("[*] Generating joins relationships...")
    joins_prompt = f"""
Based on the physical schema and the concepts we mapped, identify join relationships between tables.

### TARGET ERP DATABASE SCHEMA:
{schema_context}

### OUTPUT FORMAT:
You MUST output ONLY a valid JSON object matching the following structure. Do not output any other text or markdown blocks.
{{
  "erp": "{erp_type}",
  "relationships": [
    {{
      "from_concept": "concept_name_1",
      "to_concept": "concept_name_2",
      "cardinality": "many_to_one",
      "join_type": "left",
      "condition": "table1_alias.column_name = table2_alias.column_name"
    }}
  ]
}}
"""
    joins_resp = await llm.chat_text(
        system="You are a precise join relationship mapping assistant. Output ONLY valid raw JSON.",
        user=joins_prompt,
        max_tokens=1000
    )
    
    json_match = re.search(r"({.*})", joins_resp.strip(), re.DOTALL)
    if json_match:
        joins_resp = json_match.group(1)

    try:
        joins_json = json.loads(joins_resp)
        joins_json["_id"] = erp_type
        # Save to MongoDB v2_joins
        await db["v2_joins"].update_one(
            {"_id": erp_type},
            {"$set": joins_json},
            upsert=True
        )
        print(f"[+] Successfully generated and pushed joins relationship for '{erp_type}' to MongoDB.")
    except Exception as e:
        print(f"[!] Failed to parse or save joins relationships: {e}")
        print(f"Raw response: {joins_resp}")

    print("\n[+] AUTO-GENERATION AND DATABASE SYNCHRONIZATION COMPLETED!")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Auto-generate V2 adapters using DB schema and LLM.")
    parser.add_argument("--conn-id", required=True, help="Connection UUID in target database")
    parser.add_argument("--erp-type", required=True, help="Identifier for target custom erp database (e.g. epicor)")
    args = parser.parse_args()

    asyncio.run(generate_adapters(args.conn_id, args.erp_type))
