"""Loader package — YAML knowledge graph ingestion for the semantic engine."""
from app.engine.loader.adapter_loader import load_adapters, load_joins, load_meta
from app.engine.loader.erp_registry import ERPPaths, get_erp_paths
from app.engine.loader.ontology_loader import load_ontology

__all__ = [
    "load_ontology",
    "load_adapters",
    "load_joins",
    "load_meta",
    "get_erp_paths",
    "ERPPaths",
]
