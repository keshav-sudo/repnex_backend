import json
import os
import uuid
import yaml
from datetime import datetime, UTC
from pathlib import Path
from typing import Any, Dict, List
from motor.motor_asyncio import AsyncIOMotorDatabase
from app.core.config import get_settings
from app.core.logging import get_logger
from app.llm.client import get_llm
from app.services.connection_service import get_connection
from app.services import vector_store_service
from app.engine.loader.erp_registry import V2_DIR

log = get_logger(__name__)

async def generate_and_index_adapters(
    db: AsyncIOMotorDatabase, current: Any, connection_id: uuid.UUID
) -> Dict[str, Any]:
    """
    Extracts schema, chunks tables/collections, generates V2 Ontology/Adapter YAML files,
    writes them to the local v2 folder, and uploads vector embeddings to Pinecone.
    """
    # 1. Fetch connection details and schema
    conn = await get_connection(db, current, connection_id)
    if not conn.schema_info or "tables" not in conn.schema_info:
        raise ValueError("Schema not synced. Please sync database schema first.")
        
    tables = conn.schema_info["tables"]
    log.info("start_adapter_generation", extra={"conn_id": str(connection_id), "tables_count": len(tables)})
    
    # 2. Chunk tables into batches of 20
    chunk_size = 20
    chunks = [tables[i:i + chunk_size] for i in range(0, len(tables), chunk_size)]
    
    llm = get_llm()
    
    all_concepts = []
    all_joins = []
    
    # 3. Process each chunk via LLM mapping prompt
    for idx, chunk in enumerate(chunks):
        # Format chunk tables for the prompt
        chunk_text = []
        for t in chunk:
            cols = [f"{c['name']} ({c.get('type', 'string')})" for c in t.get("columns", [])]
            chunk_text.append(f"Table/Collection: {t['name']}\nFields: {', '.join(cols)}")
        schema_chunk_text = "\n\n".join(chunk_text)
        
        system_prompt = "You are an expert database architect and ontologist mapping physical schemas to logical business concepts."
        user_prompt = f"""
Analyze the following database tables/collections and map them to standard business entities (e.g. customer, supplier, invoice, inventory_item, warehouse, sales_order, purchase_order, general_ledger, WIP, etc.).

Subset of tables to map:
{schema_chunk_text}

Produce a JSON response with the following schema:
{{
  "concepts": [
    {{
      "concept": "concept_name", // snake_case, e.g. customer, supplier, inventory_item, sales_order, sales_order_line
      "module": "sales|purchases|inventory|finance|wip|other",
      "description": "Short description of what this concept represents.",
      "synonyms": ["synonym1", "synonym2"],
      "adapter": {{
        "concept": "concept_name",
        "table": "physical_table_name_from_schema",
        "alias": "short_unique_alias", // e.g. c, s, m, so, sol
        "columns": {{
          "physical_column_name_1": {{
            "name": "semantic_field_name", // snake_case, e.g. customer_code, balance, creation_date
            "type": "string|integer|double|boolean|datetime",
            "description": "Short description of the field"
          }}
        }}
      }}
    }}
  ],
  "joins": [
    {{
      "join_type": "LEFT|INNER",
      "from_concept": "concept_name_1",
      "to_concept": "concept_name_2",
      "condition": "alias1.physical_column_A = alias2.physical_column_B"
    }}
  ]
}}

CRITICAL RULES:
1. ONLY map columns and tables that are present in the provided schema. Do not invent columns.
2. Maintain standard snake_case for all concept names and semantic field names.
3. Suggest joins only if there are columns in the tables that represent logical keys (e.g. customer_code, item_code, order_number).
4. Output ONLY valid raw JSON matching the schema. No markdown formatting or extra text.
"""
        try:
            res = await llm.chat_json(system=system_prompt, user=user_prompt)
            if "concepts" in res:
                all_concepts.extend(res["concepts"])
            if "joins" in res:
                all_joins.extend(res["joins"])
        except Exception as e:
            log.error("llm_chunk_mapping_failed", extra={"chunk_index": idx, "error": str(e)})
            
    # 4. Prepare local folders under v2 directory or save to DB if UUID
    conn_str = str(connection_id)
    is_uuid = False
    try:
        uuid.UUID(conn_str)
        is_uuid = True
    except ValueError:
        pass

    if is_uuid:
        # Clear existing configs in DB
        await db["semantic_configs"].delete_many({"connection_id": conn_str})
        
        configs_to_insert = []
        meta_tables = {}
        default_aliases = {}
        
        for item in all_concepts:
            concept_name = item.get("concept")
            adapter = item.get("adapter", {})
            if not concept_name or not adapter:
                continue
                
            # Ontology JSON
            ont_data = {
                "concept": concept_name,
                "module": item.get("module", "other"),
                "description": item.get("description", ""),
                "synonyms": item.get("synonyms", [])
            }
            configs_to_insert.append({
                "connection_id": conn_str,
                "type": "ontology",
                "concept": concept_name,
                "data": ont_data,
                "updated_at": datetime.now(UTC)
            })
            
            # Adapter JSON
            configs_to_insert.append({
                "connection_id": conn_str,
                "type": "adapter",
                "concept": concept_name,
                "data": adapter,
                "updated_at": datetime.now(UTC)
            })
            
            # Add to meta inventory
            tbl_name = adapter.get("table")
            alias = adapter.get("alias", "t")
            if tbl_name:
                meta_tables[tbl_name] = {
                    "alias": alias,
                    "columns": list(adapter.get("columns", {}).keys()),
                    "notes": item.get("description", "")
                }
                default_aliases[concept_name] = alias
                
        # Joins JSON
        joins_data = {"relationships": all_joins}
        configs_to_insert.append({
            "connection_id": conn_str,
            "type": "joins",
            "concept": None,
            "data": joins_data,
            "updated_at": datetime.now(UTC)
        })
        
        # Meta JSON
        meta_data = {
            "tables": meta_tables,
            "default_aliases": default_aliases,
            "data_rules": {
                "max_rows_per_query": 500,
                "default_date_range_days": 30
            }
        }
        configs_to_insert.append({
            "connection_id": conn_str,
            "type": "meta",
            "concept": None,
            "data": meta_data,
            "updated_at": datetime.now(UTC)
        })
        
        if configs_to_insert:
            await db["semantic_configs"].insert_many(configs_to_insert)
            
    else:
        adapters_dir = V2_DIR / "adapters" / conn_str
        ontology_dir = V2_DIR / "ontology" / conn_str
        relationships_dir = V2_DIR / "relationships" / conn_str
        
        os.makedirs(adapters_dir, exist_ok=True)
        os.makedirs(ontology_dir, exist_ok=True)
        os.makedirs(relationships_dir, exist_ok=True)
        
        # 5. Write Ontology and Adapter YAML files
        meta_tables = {}
        default_aliases = {}
        
        for item in all_concepts:
            concept_name = item.get("concept")
            adapter = item.get("adapter", {})
            if not concept_name or not adapter:
                continue
                
            # Write Ontology YAML
            ont_data = {
                "concept": concept_name,
                "module": item.get("module", "other"),
                "description": item.get("description", ""),
                "synonyms": item.get("synonyms", [])
            }
            with open(ontology_dir / f"{concept_name}.yaml", "w", encoding="utf-8") as f:
                yaml.dump(ont_data, f, sort_keys=False)
                
            # Write Adapter YAML
            with open(adapters_dir / f"{concept_name}.yaml", "w", encoding="utf-8") as f:
                yaml.dump(adapter, f, sort_keys=False)
                
            # Add to meta inventory
            tbl_name = adapter.get("table")
            alias = adapter.get("alias", "t")
            if tbl_name:
                meta_tables[tbl_name] = {
                    "alias": alias,
                    "columns": list(adapter.get("columns", {}).keys()),
                    "notes": item.get("description", "")
                }
                default_aliases[concept_name] = alias
                
        # Write Joins YAML
        joins_data = {"relationships": all_joins}
        with open(relationships_dir / "joins.yaml", "w", encoding="utf-8") as f:
            yaml.dump(joins_data, f, sort_keys=False)
            
        # Write _meta.yaml
        meta_data = {
            "tables": meta_tables,
            "default_aliases": default_aliases,
            "data_rules": {
                "max_rows_per_query": 500,
                "default_date_range_days": 30
            }
        }
        with open(adapters_dir / "_meta.yaml", "w", encoding="utf-8") as f:
            yaml.dump(meta_data, f, sort_keys=False)
        
    # 6. Build Pinecone vectors
    vectors_to_upsert = []
    
    # Tables vectors
    for item in all_concepts:
        concept_name = item.get("concept")
        adapter = item.get("adapter", {})
        if not concept_name or not adapter:
            continue
            
        tbl_name = adapter.get("table")
        columns_list = list(adapter.get("columns", {}).keys())
        desc = item.get("description", "")
        
        # Unique vector id
        vec_id = f"{conn_str}_tbl_{tbl_name}"
        text_content = f"Table: {tbl_name}. Concept: {concept_name}. Description: {desc}. Columns: {', '.join(columns_list)}"
        
        vectors_to_upsert.append({
            "id": vec_id,
            "metadata": {
                "table_name": tbl_name,
                "concept": concept_name,
                "type": "table",
                "text_content": text_content
            }
        })
        
        # Columns vectors
        for col_name, col_info in adapter.get("columns", {}).items():
            col_id = f"{conn_str}_col_{tbl_name}_{col_name}"
            col_desc = col_info.get("description", "")
            col_type = col_info.get("type", "string")
            col_semantic = col_info.get("name", "")
            
            text_content_col = f"Column: {col_name} in Table: {tbl_name}. Semantic Field: {col_semantic}. Type: {col_type}. Description: {col_desc}"
            vectors_to_upsert.append({
                "id": col_id,
                "metadata": {
                    "table_name": tbl_name,
                    "column_name": col_name,
                    "concept": concept_name,
                    "type": "column",
                    "text_content": text_content_col
                }
            })
            
    # Joins vectors
    for idx, join in enumerate(all_joins):
        join_id = f"{conn_str}_join_{idx}"
        text_content_join = f"Join {join.get('from_concept')} to {join.get('to_concept')} on condition {join.get('condition')}"
        vectors_to_upsert.append({
            "id": join_id,
            "metadata": {
                "type": "join",
                "text_content": text_content_join
            }
        })
        
    # 7. Clear old vectors and upsert new ones
    await vector_store_service.delete_vectors_by_connection(conn_str)
    if vectors_to_upsert:
        await vector_store_service.upsert_vectors(conn_str, vectors_to_upsert)
        
    return {
        "status": "success",
        "concepts_count": len(all_concepts),
        "joins_count": len(all_joins),
        "vectors_indexed": len(vectors_to_upsert)
    }
