"""Provider configuration and persisted AI circuit-breaker state."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

RUNTIME_PATH = Path.home() / ".plugin-psd-render" / "ai-runtime.json"
SCHEMA_VERSION = 1


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def provider_fingerprint(base_url: str, api_key: str) -> str:
    payload = f"{str(base_url or '').strip()}\0{str(api_key or '').strip()}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def legacy_provider(base_url: str, api_key: str, models: list[str]) -> dict:
    return {
        "id": "legacy",
        "name": "主 API",
        "base_url": str(base_url or "").strip(),
        "api_key_env": "PSD_RENDER_AI_API_KEY",
        "api_key": str(api_key or "").strip(),
        "models": [str(model).strip() for model in models if str(model).strip()],
        "enabled": True,
        "failover_on_auth": True,
        "failover_on_quota": True,
    }


def normalize_profiles(raw: Mapping[str, Any] | None, *, legacy: dict | None = None) -> dict:
    raw = raw if isinstance(raw, Mapping) else {}
    providers_raw = raw.get("providers") if isinstance(raw.get("providers"), Mapping) else {}
    providers: dict[str, dict] = {}
    for provider_id, item in providers_raw.items():
        if not isinstance(item, Mapping):
            continue
        pid = str(provider_id or item.get("id") or "").strip()
        if not pid:
            continue
        providers[pid] = {
            "id": pid,
            "name": str(item.get("name") or pid),
            "base_url": str(item.get("base_url") or "").strip(),
            "api_key_env": str(item.get("api_key_env") or "").strip(),
            "api_key": str(item.get("api_key") or "").strip(),
            "models": [str(model).strip() for model in item.get("models") or [] if str(model).strip()],
            "enabled": bool(item.get("enabled", True)),
            "failover_on_auth": bool(item.get("failover_on_auth", True)),
            "failover_on_quota": bool(item.get("failover_on_quota", True)),
        }
    if not providers and legacy and legacy.get("base_url"):
        providers["legacy"] = copy.deepcopy(legacy)
    order = [str(item) for item in raw.get("provider_order") or [] if str(item) in providers]
    order.extend(pid for pid in providers if pid not in order)
    # Secrets are runtime-only and are never returned as persisted metadata.
    for provider in providers.values():
        provider.pop("api_key", None)
    return {"schema_version": SCHEMA_VERSION, "provider_order": order, "providers": providers}


def load_runtime(path: str | Path = RUNTIME_PATH) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {"schema_version": SCHEMA_VERSION, "providers": {}}
    if not isinstance(data, Mapping):
        return {"schema_version": SCHEMA_VERSION, "providers": {}}
    providers = data.get("providers") if isinstance(data.get("providers"), Mapping) else {}
    return {"schema_version": SCHEMA_VERSION, "providers": copy.deepcopy(dict(providers))}


def save_runtime(state: Mapping[str, Any], path: str | Path = RUNTIME_PATH) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="ai-runtime-", dir=str(target.parent), text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"schema_version": SCHEMA_VERSION, **dict(state)}, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if os.name != "nt":
            os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def provider_state(state: dict, provider: Mapping[str, Any], api_key: str) -> dict:
    pid = str(provider.get("id") or "")
    fingerprint = provider_fingerprint(provider.get("base_url", ""), api_key)
    entry = state.setdefault("providers", {}).setdefault(pid, {})
    if entry.get("fingerprint") != fingerprint:
        entry.clear()
        entry.update({"fingerprint": fingerprint, "status": "unknown", "consecutive_failures": 0})
    return entry


def is_blocked(entry: Mapping[str, Any]) -> bool:
    return str(entry.get("status") or "") in {"quota_exhausted", "auth_error", "blocked"}


def mark_failure(state: dict, provider: Mapping[str, Any], api_key: str, kind: str, message: str = "") -> dict:
    entry = provider_state(state, provider, api_key)
    entry["last_failure_at"] = now_iso()
    entry["last_error_kind"] = str(kind or "unknown")
    entry["last_error"] = str(message or "")[:500]
    entry["consecutive_failures"] = int(entry.get("consecutive_failures") or 0) + 1
    if kind == "quota_exhausted":
        entry["status"] = "quota_exhausted"
        entry.pop("opened_until", None)
    elif kind in {"auth", "invalid_config"}:
        entry["status"] = "auth_error" if kind == "auth" else "blocked"
    elif kind in {"rate_limit", "transient_http", "transient_network", "timeout"}:
        entry["status"] = "rate_limited" if kind == "rate_limit" else "degraded"
    else:
        entry["status"] = "degraded"
    return entry


def mark_success(state: dict, provider: Mapping[str, Any], api_key: str) -> dict:
    entry = provider_state(state, provider, api_key)
    entry.update({"status": "healthy", "last_success_at": now_iso(), "consecutive_failures": 0})
    entry.pop("opened_until", None)
    entry.pop("retry_after_at", None)
    return entry


def reset_blocks(path: str | Path = RUNTIME_PATH) -> None:
    """Forget quota/auth blocks, e.g. after the user re-saves AI settings
    following a top-up; the next request re-tests each provider."""
    state = load_runtime(path)
    for entry in (state.get("providers") or {}).values():
        if isinstance(entry, dict):
            entry["status"] = "unknown"
            entry["consecutive_failures"] = 0
            entry.pop("opened_until", None)
    save_runtime(state, path)


__all__ = [
    "reset_blocks",
    "RUNTIME_PATH", "load_runtime", "save_runtime", "normalize_profiles",
    "legacy_provider", "provider_fingerprint", "provider_state", "is_blocked",
    "mark_failure", "mark_success", "now_iso",
]
