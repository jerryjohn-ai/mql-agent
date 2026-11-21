"""Runtime settings from environment (.env) plus versioned config files."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name, default)
    return value if value not in ("", None) else default


@dataclass(frozen=True)
class Settings:
    # Model access: "anthropic" (default) or "bedrock"
    llm_provider: str = field(default_factory=lambda: _env("MQL_LLM_PROVIDER", "anthropic"))
    screen_model: str = field(default_factory=lambda: _env("MQL_SCREEN_MODEL", "claude-haiku-5-5"))
    scoring_model: str = field(default_factory=lambda: _env("MQL_SCORING_MODEL", "claude-sonnet-5-5"))
    aws_region: str = field(default_factory=lambda: _env("AWS_REGION", "eu-west-2"))

    # MCP servers (local, streamable HTTP)
    read_server_url: str = field(default_factory=lambda: _env("MQL_READ_SERVER_URL", "http://127.0.0.1:8101/mcp"))
    write_server_url: str = field(default_factory=lambda: _env("MQL_WRITE_SERVER_URL", "http://127.0.0.1:8102/mcp"))
    read_server_token: str | None = field(default_factory=lambda: _env("MQL_READ_SERVER_TOKEN"))
    write_server_token: str | None = field(default_factory=lambda: _env("MQL_WRITE_SERVER_TOKEN"))

    # Signing key shared only by the router and Server B
    approval_key: str | None = field(default_factory=lambda: _env("MQL_APPROVAL_KEY"))

    # Who may decide human reviews (comma-separated ids)
    approvers: tuple[str, ...] = field(
        default_factory=lambda: tuple(a.strip() for a in (_env("MQL_APPROVERS", "") or "").split(",") if a.strip())
    )

    data_dir: Path = field(default_factory=lambda: Path(_env("MQL_DATA_DIR", str(ROOT / "data"))))
    prompt_version_screen: str = "screen_v1"
    prompt_version_score: str = "score_v1"
    rubric_version: str = "rubric_v1"

    @property
    def checkpoint_db(self) -> Path:
        return self.data_dir / "checkpoints.sqlite"

    @property
    def audit_db(self) -> Path:
        return self.data_dir / "audit.sqlite"


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    s.data_dir.mkdir(parents=True, exist_ok=True)
    return s


@lru_cache
def load_policy(path: str | None = None) -> dict:
    p = Path(path) if path else ROOT / "config" / "policy.yaml"
    with open(p, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_text(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")
