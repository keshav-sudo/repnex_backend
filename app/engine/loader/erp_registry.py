"""ERP Registry — resolves V2 YAML knowledge-graph paths per ERP type."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# Resolve v2/ directory: packaged inside repnex_backend_complete, or one level up
_HERE = Path(__file__).resolve()
_V2_CANDIDATE_A = _HERE.parents[3] / "v2"   # repnex_backend_complete/v2
_V2_CANDIDATE_B = _HERE.parents[4] / "v2"   # parent of repnex_backend_complete/v2

V2_DIR: Path = _V2_CANDIDATE_A if _V2_CANDIDATE_A.exists() else _V2_CANDIDATE_B


@dataclass(frozen=True)
class ERPPaths:
    erp_type: str
    ontology_dir: Path
    adapter_dir: Path
    relationship_file: Path


def get_erp_paths(erp_type: str) -> ERPPaths:
    """Return resolved filesystem paths for a given ERP type."""
    erp = erp_type.lower().strip()
    return ERPPaths(
        erp_type=erp,
        ontology_dir=V2_DIR / "ontology",
        adapter_dir=V2_DIR / "adapters" / erp,
        relationship_file=V2_DIR / "relationships" / erp / "joins.yaml",
    )
