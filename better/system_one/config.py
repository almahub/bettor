"""Environment configuration independent of application/domain settings."""

import os
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator


class SystemOneSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = False
    provider: str = "rizzo_flow"
    shadow_mode: bool = True
    pre_screen_enabled: bool = True
    routing_enabled: bool = True
    consistency_enabled: bool = True
    escalation_enabled: bool = True
    rizzo_flow_base_url: str = "http://127.0.0.1:8017"
    rizzo_flow_model: str = ""
    rizzo_flow_model_fingerprint: str | None = None
    rizzo_flow_timeout_ms: int = Field(default=3000, gt=0)
    rizzo_flow_max_retries: int = Field(default=1, ge=0, le=5)
    rizzo_flow_api_key: SecretStr = SecretStr("")
    circuit_failure_threshold: int = Field(default=3, gt=0)
    circuit_reset_seconds: float = Field(default=30, gt=0)
    cache_ttl_seconds: float = Field(default=60, ge=0)
    cache_max_entries: int = Field(default=256, gt=0)
    threshold_version: str = "thresholds_v1_uncalibrated"

    @model_validator(mode="after")
    def shadow_only(self) -> Self:
        if not self.shadow_mode:
            raise ValueError(
                "Active System One requires a documented benchmark promotion; only shadow mode is implemented"
            )
        if not self.rizzo_flow_base_url.startswith(("http://", "https://")):
            raise ValueError("Rizzo Flow base URL must use HTTP or HTTPS")
        return self

    @classmethod
    def from_env(cls) -> Self:
        mapping = {
            "enabled": "SYSTEM_ONE_ENABLED",
            "provider": "SYSTEM_ONE_PROVIDER",
            "shadow_mode": "SYSTEM_ONE_SHADOW_MODE",
            "pre_screen_enabled": "SYSTEM_ONE_PRE_SCREEN_ENABLED",
            "routing_enabled": "SYSTEM_ONE_ROUTING_ENABLED",
            "consistency_enabled": "SYSTEM_ONE_CONSISTENCY_ENABLED",
            "escalation_enabled": "SYSTEM_ONE_ESCALATION_ENABLED",
            "threshold_version": "SYSTEM_ONE_THRESHOLD_VERSION",
            "circuit_failure_threshold": "SYSTEM_ONE_CIRCUIT_FAILURE_THRESHOLD",
            "circuit_reset_seconds": "SYSTEM_ONE_CIRCUIT_RESET_SECONDS",
            "cache_ttl_seconds": "SYSTEM_ONE_CACHE_TTL_SECONDS",
            "cache_max_entries": "SYSTEM_ONE_CACHE_MAX_ENTRIES",
        }
        for field in cls.model_fields:
            if field.startswith("rizzo_flow_"):
                mapping[field] = field.upper()
        return cls.model_validate(
            {field: os.environ[key] for field, key in mapping.items() if key in os.environ}
        )
