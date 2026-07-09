"""Adapter Loader — reads ERP-specific YAML adapters, joins, and meta files."""
from __future__ import annotations

from pathlib import Path

import yaml

from app.core.logging import get_logger

log = get_logger(__name__)


def load_adapters(adapter_dir: Path) -> dict[str, dict]:
    """Load all non-private *.yaml files from the ERP adapter directory.

    Returns a mapping of concept name → adapter data dict.
    Private files (prefixed with '_') such as _meta.yaml are skipped.
    """
    adapters: dict[str, dict] = {}
    if not adapter_dir.exists():
        log.warning("adapter_dir_missing", extra={"path": str(adapter_dir)})
        return adapters

    for f in adapter_dir.glob("*.yaml"):
        if f.name.startswith("_"):
            continue
        try:
            with open(f, encoding="utf-8") as fh:
                data = yaml.safe_load(fh)
            if data and "concept" in data:
                adapters[data["concept"]] = data
        except Exception as exc:  # noqa: BLE001
            log.error("adapter_load_error", extra={"file": str(f), "err": str(exc)})

    return adapters


def load_joins(relationship_file: Path) -> dict:
    """Load the global joins.yaml relationship file for an ERP type."""
    if not relationship_file.exists():
        log.warning("joins_file_missing", extra={"path": str(relationship_file)})
        return {}
    try:
        with open(relationship_file, encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}
    except Exception as exc:  # noqa: BLE001
        log.error("joins_load_error", extra={"file": str(relationship_file), "err": str(exc)})
        return {}


def load_meta(adapter_dir: Path) -> dict:
    """Load _meta.yaml — complete schema inventory and data rules for an ERP adapter."""
    meta_path = adapter_dir / "_meta.yaml"
    if not meta_path.exists():
        return {}
    try:
        with open(meta_path, encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}
    except Exception as exc:  # noqa: BLE001
        log.error("meta_load_error", extra={"file": str(meta_path), "err": str(exc)})
        return {}
