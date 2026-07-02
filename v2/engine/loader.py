"""V2 semantic layer loader.

Loads universal ontology concepts, ERP adapters and join relationships from
the v2/ YAML tree and validates them for structural correctness so the
resolver can rely on clean, consistent definitions.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

import yaml

VALID_TYPES = {"string", "date", "decimal", "integer"}
VALID_CARDINALITIES = {"many_to_one", "one_to_many", "one_to_one"}

V2_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@dataclass
class Concept:
    name: str
    display_name: str
    module: str
    description: str
    synonyms: list[str]
    header_attributes: dict[str, dict[str, Any]]
    detail_attributes: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass
class Adapter:
    concept: str
    erp: str
    table: str
    alias: str | None
    fields: dict[str, str]
    detail_table: str | None = None
    detail_fields: dict[str, str] = field(default_factory=dict)
    balance_mapping: dict[str, Any] | None = None
    calculated_fields: dict[str, str] = field(default_factory=dict)
    default_filters: dict[str, str] = field(default_factory=dict)

    def all_fields(self) -> dict[str, str]:
        """Union of direct, calculated and balance-mapped field expressions."""
        out = dict(self.fields)
        out.update(self.calculated_fields)
        if self.balance_mapping:
            out.update(self.balance_mapping.get("fields", {}))
        out.update(self.detail_fields)
        return out


@dataclass
class Relationship:
    from_concept: str
    to_concept: str
    cardinality: str
    condition: str
    join_type: str = "LEFT"


class SemanticLayerError(ValueError):
    """Raised when the YAML tree is structurally invalid."""


def _load_yaml(path: str) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise SemanticLayerError(f"{path}: top level must be a mapping")
    return data


def load_ontology(root: str = V2_ROOT) -> dict[str, Concept]:
    concepts: dict[str, Concept] = {}
    onto_dir = os.path.join(root, "ontology")
    for fname in sorted(os.listdir(onto_dir)):
        if not fname.endswith((".yaml", ".yml")):
            continue
        path = os.path.join(onto_dir, fname)
        data = _load_yaml(path)
        name = data.get("concept")
        if not name or not isinstance(name, str) or name != name.lower():
            raise SemanticLayerError(f"{path}: 'concept' must be lowercase snake_case")
        for section in ("header_attributes", "detail_attributes"):
            for attr, spec in (data.get(section) or {}).items():
                if attr != attr.lower():
                    raise SemanticLayerError(f"{path}: attribute '{attr}' must be snake_case")
                if spec.get("type") not in VALID_TYPES:
                    raise SemanticLayerError(
                        f"{path}: attribute '{attr}' has invalid type {spec.get('type')!r}"
                    )
                if not isinstance(spec.get("required"), bool):
                    raise SemanticLayerError(f"{path}: attribute '{attr}' missing boolean 'required'")
        concepts[name] = Concept(
            name=name,
            display_name=data.get("display_name", name),
            module=data.get("module", ""),
            description=(data.get("description") or "").strip(),
            synonyms=data.get("synonyms") or [],
            header_attributes=data.get("header_attributes") or {},
            detail_attributes=data.get("detail_attributes") or {},
        )
    if not concepts:
        raise SemanticLayerError(f"No ontology concepts found under {onto_dir}")
    return concepts


def load_adapters(erp: str, root: str = V2_ROOT) -> dict[str, Adapter]:
    adapters: dict[str, Adapter] = {}
    adapter_dir = os.path.join(root, "adapters", erp)
    if not os.path.isdir(adapter_dir):
        raise SemanticLayerError(f"No adapter directory for ERP {erp!r}: {adapter_dir}")
    for fname in sorted(os.listdir(adapter_dir)):
        if fname.startswith("_") or not fname.endswith((".yaml", ".yml")):
            continue
        path = os.path.join(adapter_dir, fname)
        data = _load_yaml(path)
        concept = data.get("concept")
        table = data.get("table") or data.get("header_table")
        if not concept or not table:
            raise SemanticLayerError(f"{path}: 'concept' and 'table' are required")
        adapters[concept] = Adapter(
            concept=concept,
            erp=erp,
            table=table,
            alias=data.get("alias"),
            fields=data.get("fields") or {},
            detail_table=data.get("detail_table"),
            detail_fields=data.get("detail_fields") or {},
            balance_mapping=data.get("balance_mapping"),
            calculated_fields=data.get("calculated_fields") or {},
            default_filters=data.get("default_filters") or {},
        )
    return adapters


def load_relationships(erp: str, root: str = V2_ROOT) -> list[Relationship]:
    path = os.path.join(root, "relationships", erp, "joins.yaml")
    if not os.path.exists(path):
        return []
    data = _load_yaml(path)
    rels: list[Relationship] = []
    for item in data.get("relationships") or []:
        card = item.get("cardinality")
        if card not in VALID_CARDINALITIES:
            raise SemanticLayerError(f"{path}: invalid cardinality {card!r}")
        if not item.get("condition"):
            raise SemanticLayerError(f"{path}: relationship missing 'condition'")
        rels.append(
            Relationship(
                from_concept=item["from_concept"],
                to_concept=item["to_concept"],
                cardinality=card,
                condition=item["condition"],
                join_type=item.get("join_type", "LEFT"),
            )
        )
    return rels


def cross_validate(
    concepts: dict[str, Concept],
    adapters: dict[str, Adapter],
) -> list[str]:
    """Return a list of human-readable consistency errors (empty = clean)."""
    errors: list[str] = []
    for cname, adapter in adapters.items():
        concept = concepts.get(cname)
        if concept is None:
            errors.append(f"adapter '{cname}': no matching ontology concept")
            continue
        known = set(concept.header_attributes) | set(concept.detail_attributes)
        for attr in adapter.all_fields():
            if attr not in known:
                errors.append(
                    f"adapter '{cname}': field '{attr}' not declared in ontology"
                )
    return errors
