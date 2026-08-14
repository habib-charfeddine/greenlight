"""Tenant policy packs + global settings.

Policy is data, not code: each tenant is one YAML file in ``config/tenants/``.
``EffectivePolicy`` merges global thresholds from ``config/settings.yaml`` with
the tenant's overrides. ``policy_version`` is stamped on every finding.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from .models import Finding

#: tenant `media:` block key -> global `thresholds:` key
_MEDIA_KEY_MAP = {
    "min_width": "image_min_width",
    "min_height": "image_min_height",
    "blur_laplacian_min": "blur_laplacian_min",
    "video_max_seconds": "video_max_seconds",
}


class TenantPolicy(BaseModel):
    model_config = ConfigDict(extra="allow")

    policy_version: str = "unversioned"
    tenant_id: str
    tenant_name: str = ""
    locale: str = "en-GB"
    tone_rules: str = ""
    banned_terms: list[str] = Field(default_factory=list)
    conventions: str = ""
    max_emoji: int = 3
    require_action: bool = False
    allowed_domains: list[str] = Field(default_factory=list)
    deny_url_shorteners: bool = True
    deny_raw_ip_urls: bool = True
    visual_rules: str = ""
    media: dict[str, Any] = Field(default_factory=dict)
    vision_sample_rate: float | None = None   # None -> global default
    burn_in: bool = False
    gate_mode: str = "advisory"               # advisory | hold_red | hold_amber
    disabled_checks: list[str] = Field(default_factory=list)
    severity_overrides: dict[str, str] = Field(default_factory=dict)


class EffectivePolicy:
    """One tenant's policy merged over the global settings."""

    def __init__(self, settings: dict, tenant: TenantPolicy):
        self.settings = settings
        self.tenant = tenant

    @property
    def policy_version(self) -> str:
        return self.tenant.policy_version

    @property
    def tenant_id(self) -> str:
        return self.tenant.tenant_id

    @property
    def vision_sample_rate(self) -> float:
        if self.tenant.vision_sample_rate is not None:
            return self.tenant.vision_sample_rate
        return float(self.settings.get("vision_sample_rate", 0.15))

    def threshold(self, key: str) -> Any:
        """Global threshold, overridable by the tenant's ``media:`` block.

        Tenant media keys use short names (``min_width``); globals use the
        prefixed names (``image_min_width``) — the map bridges the two.
        """
        for media_key, global_key in _MEDIA_KEY_MAP.items():
            if global_key == key and media_key in self.tenant.media:
                return self.tenant.media[media_key]
        return self.settings.get("thresholds", {}).get(key)


def load_settings(path: str | Path = "config/settings.yaml") -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_tenants(directory: str | Path = "config/tenants") -> dict[str, TenantPolicy]:
    """All tenant policy packs, keyed by tenant_id."""
    tenants: dict[str, TenantPolicy] = {}
    for p in sorted(Path(directory).glob("*.yaml")):
        with open(p, encoding="utf-8") as f:
            tenant = TenantPolicy.model_validate(yaml.safe_load(f))
        tenants[tenant.tenant_id] = tenant
    return tenants


def apply_overrides(findings: list[Finding], policy: EffectivePolicy) -> list[Finding]:
    """Per-tenant kill switch + severity overrides (03_CHECK_CATALOG header)."""
    out: list[Finding] = []
    for f in findings:
        if f.check_id in policy.tenant.disabled_checks:
            continue
        override = policy.tenant.severity_overrides.get(f.check_id)
        if override in ("P0", "P1", "P2", "P3"):
            f = f.model_copy(update={"severity": override})
        out.append(f)
    return out
