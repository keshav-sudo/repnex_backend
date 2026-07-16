"""Ontology Loader — reads universal business concept YAML files from v2/ontology/."""
from __future__ import annotations

from pathlib import Path

import yaml

from app.core.logging import get_logger

log = get_logger(__name__)


def load_ontology(ontology_dir: Path, connection_id: str | None = None) -> dict[str, dict]:
    """Load all *.yaml files from the ontology directory.

    Returns a mapping of concept name → ontology data dict.
    """
    ontology: dict[str, dict] = {}
    if not ontology_dir.exists():
        log.warning("ontology_dir_missing", extra={"path": str(ontology_dir)})
        return ontology

    for f in ontology_dir.glob("*.yaml"):
        if f.is_dir():
            continue
        try:
            with open(f, encoding="utf-8") as fh:
                data = yaml.safe_load(fh)
            if data and "concept" in data:
                ontology[data["concept"]] = data
        except Exception as exc:  # noqa: BLE001
            log.error("ontology_load_error", extra={"file": str(f), "err": str(exc)})

    if connection_id:
        conn_dir = ontology_dir / connection_id
        if conn_dir.exists() and conn_dir.is_dir():
            for f in conn_dir.glob("*.yaml"):
                try:
                    with open(f, encoding="utf-8") as fh:
                        data = yaml.safe_load(fh)
                    if data and "concept" in data:
                        ontology[data["concept"]] = data
                except Exception as exc:  # noqa: BLE001
                    log.error("ontology_load_error", extra={"file": str(f), "err": str(exc)})

    return ontology
