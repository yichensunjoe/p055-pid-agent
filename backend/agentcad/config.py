from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

DeploymentMode = Literal["local", "shared"]


def _env(primary: str, legacy: str, default: str) -> str:
    return os.getenv(primary, os.getenv(legacy, default))


def _csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _int_env(primary: str, legacy: str, default: int) -> int:
    raw = _env(primary, legacy, str(default))
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{primary} must be an integer") from exc


def _float_env(primary: str, legacy: str, default: float) -> float:
    raw = _env(primary, legacy, str(default))
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{primary} must be a number") from exc


def _optional_float_env(primary: str, legacy: str, default: float | None = None) -> float | None:
    raw = os.getenv(primary, os.getenv(legacy, ""))
    if not raw.strip() or raw.strip().lower() in {"0", "none", "unlimited", "inf"}:
        return default
    try:
        val = float(raw)
        return val if val > 0 else None
    except ValueError as exc:
        raise ValueError(f"{primary} must be a number") from exc


@dataclass(frozen=True)
class Settings:
    database_path: Path
    cors_origins: list[str]
    frontend_dist: Path
    diagnostics_path: Path | None = None
    deployment_mode: DeploymentMode = "local"
    api_token: str | None = None
    #: M9-WS1: the trusted local operator identity. Human review decisions exist
    #: only when this is set on a local deployment; shared deployments fail closed.
    operator_identity: str | None = None
    provider_allow_hosts: tuple[str, ...] = field(default_factory=tuple)
    provider_allow_cidrs: tuple[str, ...] = field(default_factory=tuple)
    max_json_body_bytes: int = 2 * 1024 * 1024
    max_agent_body_bytes: int = 24 * 1024 * 1024
    max_import_body_bytes: int = 25 * 1024 * 1024
    provider_max_response_bytes: int = 4 * 1024 * 1024
    max_concurrent_requests: int = 32
    agent_timeout_seconds: float | None = None

    @classmethod
    def from_env(cls) -> Settings:
        root = Path(__file__).resolve().parents[2]
        database_path = Path(
            _env(
                "PID_AGENT_DATABASE_PATH",
                "AGENTCAD_DATABASE_PATH",
                str(root / "data" / "pid-agent.db"),
            )
        )
        origins = _env(
            "PID_AGENT_CORS_ORIGINS",
            "AGENTCAD_CORS_ORIGINS",
            "http://localhost:5173,http://127.0.0.1:5173",
        )
        frontend_dist = Path(
            _env(
                "PID_AGENT_FRONTEND_DIST",
                "AGENTCAD_FRONTEND_DIST",
                str(root / "frontend" / "dist"),
            )
        )
        default_diagnostics = database_path.with_suffix(".diagnostics.jsonl")
        diagnostics_path = Path(
            _env(
                "PID_AGENT_DIAGNOSTICS_PATH",
                "AGENTCAD_DIAGNOSTICS_PATH",
                str(default_diagnostics),
            )
        )
        mode = _env("PID_AGENT_DEPLOYMENT_MODE", "AGENTCAD_DEPLOYMENT_MODE", "local").lower()
        if mode not in {"local", "shared"}:
            raise ValueError("PID_AGENT_DEPLOYMENT_MODE must be local or shared")
        token = _env("PID_AGENT_API_TOKEN", "AGENTCAD_API_TOKEN", "").strip() or None
        operator = _env("PID_AGENT_OPERATOR_IDENTITY", "AGENTCAD_OPERATOR_IDENTITY", "").strip() or None
        settings = cls(
            database_path=database_path,
            cors_origins=_csv(origins),
            frontend_dist=frontend_dist,
            diagnostics_path=diagnostics_path,
            deployment_mode=mode,  # type: ignore[arg-type]
            api_token=token,
            operator_identity=operator,
            provider_allow_hosts=tuple(
                item.lower().rstrip(".")
                for item in _csv(
                    _env(
                        "PID_AGENT_PROVIDER_ALLOW_HOSTS",
                        "AGENTCAD_PROVIDER_ALLOW_HOSTS",
                        "",
                    )
                )
            ),
            provider_allow_cidrs=tuple(
                _csv(
                    _env(
                        "PID_AGENT_PROVIDER_ALLOW_CIDRS",
                        "AGENTCAD_PROVIDER_ALLOW_CIDRS",
                        "",
                    )
                )
            ),
            max_json_body_bytes=_int_env(
                "PID_AGENT_MAX_JSON_BODY_BYTES", "AGENTCAD_MAX_JSON_BODY_BYTES", 2 * 1024 * 1024
            ),
            max_agent_body_bytes=_int_env(
                "PID_AGENT_MAX_AGENT_BODY_BYTES",
                "AGENTCAD_MAX_AGENT_BODY_BYTES",
                24 * 1024 * 1024,
            ),
            max_import_body_bytes=_int_env(
                "PID_AGENT_MAX_IMPORT_BODY_BYTES",
                "AGENTCAD_MAX_IMPORT_BODY_BYTES",
                25 * 1024 * 1024,
            ),
            provider_max_response_bytes=_int_env(
                "PID_AGENT_PROVIDER_MAX_RESPONSE_BYTES",
                "AGENTCAD_PROVIDER_MAX_RESPONSE_BYTES",
                4 * 1024 * 1024,
            ),
            max_concurrent_requests=_int_env(
                "PID_AGENT_MAX_CONCURRENT_REQUESTS",
                "AGENTCAD_MAX_CONCURRENT_REQUESTS",
                32,
            ),
            agent_timeout_seconds=_optional_float_env(
                "PID_AGENT_AGENT_TIMEOUT_SECONDS",
                "AGENTCAD_AGENT_TIMEOUT_SECONDS",
                None,
            ),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        if self.deployment_mode not in {"local", "shared"}:
            raise ValueError("deployment_mode must be local or shared")
        if self.deployment_mode == "shared" and not self.api_token:
            raise ValueError("shared deployment requires PID_AGENT_API_TOKEN")
        if self.deployment_mode == "shared":
            if not self.cors_origins:
                raise ValueError("shared deployment requires explicit PID_AGENT_CORS_ORIGINS")
            for origin in self.cors_origins:
                if origin == "*":
                    raise ValueError("shared deployment forbids wildcard CORS origins")
                parsed = urlsplit(origin)
                if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                    raise ValueError(f"invalid shared CORS origin: {origin}")
                if parsed.path not in {"", "/"} or parsed.query or parsed.fragment or parsed.username:
                    raise ValueError(f"shared CORS origin must be an origin only: {origin}")
        for name, value in {
            "max_json_body_bytes": self.max_json_body_bytes,
            "max_agent_body_bytes": self.max_agent_body_bytes,
            "max_import_body_bytes": self.max_import_body_bytes,
            "provider_max_response_bytes": self.provider_max_response_bytes,
            "max_concurrent_requests": self.max_concurrent_requests,
        }.items():
            if value <= 0:
                raise ValueError(f"{name} must be greater than zero")
        if self.agent_timeout_seconds is not None and self.agent_timeout_seconds <= 0:
            raise ValueError("agent_timeout_seconds must be greater than zero")
