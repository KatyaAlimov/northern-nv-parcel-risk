"""
Load scoring_config.yaml and expose typed helpers for risk_engine / scripts.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

import yaml

ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = ROOT / "config" / "scoring_config.yaml"


@lru_cache(maxsize=4)
def load_scoring_config(path: str | None = None) -> Dict[str, Any]:
    cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
    if not cfg_path.exists():
        raise FileNotFoundError(f"Scoring config not found: {cfg_path}")
    with cfg_path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError(f"Invalid scoring config structure in {cfg_path}")
    return data


def clear_config_cache() -> None:
    load_scoring_config.cache_clear()


def ahp_matrix(cfg: Dict[str, Any] | None = None) -> Tuple[Tuple[float, ...], ...]:
    cfg = cfg or load_scoring_config()
    matrix = cfg["ahp"]["pairwise_matrix"]
    return tuple(tuple(float(v) for v in row) for row in matrix)


def ahp_criteria(cfg: Dict[str, Any] | None = None) -> Tuple[str, ...]:
    cfg = cfg or load_scoring_config()
    return tuple(str(c) for c in cfg["ahp"]["criteria"])


def ahp_cr_threshold(cfg: Dict[str, Any] | None = None) -> float:
    cfg = cfg or load_scoring_config()
    return float(cfg["ahp"].get("consistency_ratio_threshold", 0.10))


def fault_decay_lambda(cfg: Dict[str, Any] | None = None) -> float:
    cfg = cfg or load_scoring_config()
    return float(cfg["fault"]["decay_lambda"])


def fault_buffers_meters(cfg: Dict[str, Any] | None = None) -> Dict[str, float]:
    cfg = cfg or load_scoring_config()
    return {k: float(v) for k, v in cfg["fault"]["buffers_meters"].items()}


def fault_calibration_targets(cfg: Dict[str, Any] | None = None) -> List[Dict[str, float]]:
    cfg = cfg or load_scoring_config()
    return list(cfg["fault"].get("calibration_targets", []))


def flood_rules(cfg: Dict[str, Any] | None = None) -> Dict[str, Any]:
    cfg = cfg or load_scoring_config()
    return cfg["flood"]


def tier_thresholds(cfg: Dict[str, Any] | None = None) -> Dict[str, float]:
    cfg = cfg or load_scoring_config()
    return {
        "high_min": float(cfg["tiers"]["high_min"]),
        "moderate_min": float(cfg["tiers"]["moderate_min"]),
    }


def citations(cfg: Dict[str, Any] | None = None) -> List[Dict[str, Any]]:
    cfg = cfg or load_scoring_config()
    return list(cfg.get("citations", []))


def disclaimer(cfg: Dict[str, Any] | None = None) -> str:
    cfg = cfg or load_scoring_config()
    return str(cfg.get("meta", {}).get("disclaimer", "")).strip()
