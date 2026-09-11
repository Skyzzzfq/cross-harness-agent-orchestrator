"""Model choices used by the local console.

The Codex catalog is queried through the installed Codex SDK. CodeBuddy's
built-in choices come from its local product manifest and ArkCLI-managed
user-level model files; custom providers are read from the console settings
without ever exposing their API key.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from orchestrator.console.settings import load_model_providers


MODEL_HEALTH_FILENAME = "model-health.json"
CODEBUDDY_PROBE_PROMPT = "连接测试：只回复 OK，不调用工具。"


def _option(
    model_id: str,
    label: str,
    *,
    source: str,
    description: str | None = None,
    provider_id: str | None = None,
) -> dict[str, str]:
    item = {"id": model_id, "label": label, "source": source}
    if description:
        item["description"] = description
    if provider_id:
        item["provider_id"] = provider_id
    return item


def _safe_failure(exc: Exception) -> dict[str, Any]:
    # Do not return exception text: SDK errors may contain request metadata.
    return {
        "status": "unavailable",
        "source": "codex",
        "models": [],
        "error": f"Codex model discovery unavailable ({type(exc).__name__})",
    }


def _model_health_path(project_root: Path) -> Path:
    return project_root / ".agent-hub" / MODEL_HEALTH_FILENAME


def _health_scope(provider_id: str | None) -> str:
    return provider_id or "__builtin__"


def _load_model_health(project_root: Path) -> dict[str, Any]:
    path = _model_health_path(project_root)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_model_health(project_root: Path, updates: dict[str, Any]) -> None:
    path = _model_health_path(project_root)
    current = _load_model_health(project_root)
    codebuddy = current.setdefault("codebuddy", {})
    if not isinstance(codebuddy, dict):
        codebuddy = {}
        current["codebuddy"] = codebuddy
    codebuddy.update(updates)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")


def _apply_model_health(
    project_root: Path, catalog: dict[str, Any]
) -> dict[str, Any]:
    """Hide models that failed the last probe when that probe is still current."""
    health = _load_model_health(project_root).get("codebuddy", {})
    if not isinstance(health, dict):
        return catalog
    models = catalog.get("models", [])
    if not isinstance(models, list):
        return catalog

    visible: list[dict[str, Any]] = []
    health_view: dict[str, dict[str, Any]] = {}
    for scope in {_health_scope(item.get("provider_id")) for item in models}:
        record = health.get(scope)
        if not isinstance(record, dict):
            continue
        health_view[scope] = {
            "checked_at": record.get("checked_at"),
            "available": len(record.get("verified_models", [])),
            "failed": len(record.get("failed_models", [])),
        }

    for item in models:
        if not isinstance(item, dict):
            continue
        scope = _health_scope(item.get("provider_id"))
        record = health.get(scope)
        current_ids = sorted(
            str(candidate.get("id"))
            for candidate in models
            if isinstance(candidate, dict)
            and _health_scope(candidate.get("provider_id")) == scope
            and candidate.get("id")
        )
        recorded_ids = (
            sorted(str(value) for value in record.get("catalog_ids", []))
            if isinstance(record, dict)
            else []
        )
        if isinstance(record, dict) and current_ids == recorded_ids:
            verified = {str(value) for value in record.get("verified_models", [])}
            if str(item.get("id")) not in verified:
                continue
        visible.append(item)

    result = dict(catalog)
    result["models"] = visible
    if health_view:
        result["model_health"] = health_view
    return result


def _probe_failure(item: dict[str, Any], error_kind: str) -> dict[str, Any]:
    return {
        "id": str(item.get("id") or ""),
        "provider_id": item.get("provider_id"),
        "ok": False,
        "error_kind": error_kind,
    }


async def _probe_codebuddy_item(
    project_root: Path, item: dict[str, Any]
) -> dict[str, Any]:
    model_id = str(item.get("id") or "").strip()
    provider_id = item.get("provider_id")
    try:
        from codebuddy_agent_sdk import CodeBuddyAgentOptions, ResultMessage, query
        from orchestrator.adapters.codebuddy_config import (
            codebuddy_environment_for_provider,
            preferred_codebuddy_cli,
        )

        cli_path = preferred_codebuddy_cli(project_root)
        if cli_path is None:
            return _probe_failure(item, "cli_unavailable")
        env = codebuddy_environment_for_provider(project_root, provider_id)
        env["CODEBUDDY_SKIP_GIT_BASH_CHECK"] = "1"
        options = CodeBuddyAgentOptions(
            cwd=str(project_root),
            codebuddy_code_path=cli_path,
            max_turns=1,
            model=model_id,
            permission_mode="plan",
            tools=[],
            allowed_tools=[],
            disallowed_tools=[],
            mcp_servers={},
            extra_args={},
            request_timeout_ms=30_000,
            setting_sources=["user", "project"],
            env=env,
            stderr=lambda _line: None,
            persist_session=False,
        )
        async for message in query(prompt=CODEBUDDY_PROBE_PROMPT, options=options):
            if isinstance(message, ResultMessage):
                if message.is_error:
                    return _probe_failure(item, "model_error")
                return {
                    "id": model_id,
                    "provider_id": provider_id,
                    "ok": True,
                }
        return _probe_failure(item, "no_result")
    except Exception as exc:  # SDK/auth/model errors stay inside the probe boundary.
        return _probe_failure(item, type(exc).__name__)


async def _run_codebuddy_probe(
    project_root: Path, items: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    semaphore = asyncio.Semaphore(3)

    async def run_one(item: dict[str, Any]) -> dict[str, Any]:
        async with semaphore:
            try:
                return await asyncio.wait_for(
                    _probe_codebuddy_item(project_root, item), timeout=35
                )
            except asyncio.TimeoutError:
                return _probe_failure(item, "timeout")

    return list(await asyncio.gather(*(run_one(item) for item in items)))


def probe_codebuddy_models(
    project_root: Path, *, provider_id: str | None = None
) -> dict[str, Any]:
    """Call each selected CodeBuddy model once and retain successful choices."""
    base_catalog = discover_codebuddy_models(project_root)
    all_items = [
        item for item in base_catalog.get("models", []) if isinstance(item, dict)
    ]
    target_scope = _health_scope(provider_id)
    items = [
        item
        for item in all_items
        if _health_scope(item.get("provider_id")) == target_scope
    ]
    results = asyncio.run(_run_codebuddy_probe(project_root, items)) if items else []
    checked_at = datetime.now(timezone.utc).isoformat()

    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        grouped.setdefault(_health_scope(item.get("provider_id")), []).append(item)
    updates: dict[str, Any] = {}
    for scope, scope_items in grouped.items():
        scope_results = [
            result
            for result in results
            if _health_scope(result.get("provider_id")) == scope
        ]
        updates[scope] = {
            "checked_at": checked_at,
            "catalog_ids": sorted(str(item["id"]) for item in scope_items),
            "verified_models": sorted(
                str(result["id"]) for result in scope_results if result.get("ok")
            ),
            "failed_models": sorted(
                str(result["id"])
                for result in scope_results
                if not result.get("ok")
            ),
        }
    if updates:
        _save_model_health(project_root, updates)

    result = _apply_model_health(project_root, base_catalog)
    result["probe"] = {
        "checked_at": checked_at,
        "tested": len(results),
        "available": sum(1 for item in results if item.get("ok")),
        "failed": sum(1 for item in results if not item.get("ok")),
        "failed_models": [
            str(item.get("id")) for item in results if not item.get("ok")
        ],
    }
    return result


def discover_codex_models(
    project_root: Path, *, include_hidden: bool = False
) -> dict[str, Any]:
    async def fetch() -> list[dict[str, str]]:
        from openai_codex import AsyncCodex, CodexConfig
        from orchestrator.platform import codex_transport_environment

        codex = AsyncCodex(
            CodexConfig(env=codex_transport_environment(project_root))
        )
        async with codex:
            response = await codex.models(include_hidden=include_hidden)
        models: list[dict[str, str]] = []
        for item in response.data:
            if not include_hidden and bool(getattr(item, "hidden", False)):
                continue
            model_id = str(getattr(item, "model", "") or getattr(item, "id", ""))
            if not model_id.strip():
                continue
            models.append(
                _option(
                    model_id.strip(),
                    str(getattr(item, "display_name", "") or model_id).strip(),
                    source="codex",
                    description=str(getattr(item, "description", "") or "").strip()
                    or None,
                )
            )
        return models

    try:
        models = asyncio.run(fetch())
    except Exception as exc:  # SDK/auth/network errors stay at this boundary.
        return _safe_failure(exc)
    return {"status": "ok", "source": "codex", "models": models}


def _manifest_models(project_root: Path) -> tuple[str, list[dict[str, str]]]:
    base = (
        project_root
        / ".agent-hub"
        / "tools"
        / "node_modules"
        / "@tencent-ai"
        / "codebuddy-code"
    )
    for filename in ("product.internal.json", "product.json"):
        path = base / filename
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        raw_models = data.get("models")
        if not isinstance(raw_models, list):
            continue
        models: list[dict[str, str]] = []
        for raw in raw_models:
            if not isinstance(raw, dict):
                continue
            model_id = str(raw.get("id") or "").strip()
            if not model_id:
                continue
            label = str(raw.get("name") or model_id).strip()
            description = str(
                raw.get("descriptionZh") or raw.get("descriptionEn") or ""
            ).strip()
            models.append(
                _option(
                    model_id,
                    label,
                    source="workbuddy-manifest",
                    description=description or None,
                )
            )
        if models:
            return filename, models
    return "", []


def _arkcli_user_models() -> list[dict[str, str]]:
    """Read model metadata from ArkCLI's supported WorkBuddy user files.

    ArkCLI may write the same model configuration to more than one compatible
    WorkBuddy/CodeBuddy location. Only display metadata is copied into the
    catalog; credentials, URLs, and every other provider setting stay in the
    original file and are never returned by the console API.
    """
    paths = (
        Path.home() / ".workbuddy-ai" / "models.json",
        Path.home() / ".workbuddy" / "models.json",
        Path.home() / ".codebuddy" / "models.json",
    )
    models: list[dict[str, str]] = []
    seen: set[str] = set()
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        raw_models = data.get("models")
        if isinstance(raw_models, dict):
            raw_models = [
                {"id": model_id, **(value if isinstance(value, dict) else {})}
                for model_id, value in raw_models.items()
            ]
        if not isinstance(raw_models, list):
            continue
        available = data.get("availableModels")
        available_ids = (
            {str(value).strip() for value in available if str(value).strip()}
            if isinstance(available, list)
            else None
        )
        for raw in raw_models:
            if not isinstance(raw, dict):
                continue
            model_id = str(raw.get("id") or raw.get("model") or "").strip()
            if not model_id or model_id in seen:
                continue
            if available_ids is not None and model_id not in available_ids:
                continue
            label = str(
                raw.get("name")
                or raw.get("label")
                or raw.get("displayName")
                or model_id
            ).strip()
            seen.add(model_id)
            models.append(_option(model_id, label, source="arkcli-user-config"))
    return models


def discover_codebuddy_models(project_root: Path) -> dict[str, Any]:
    manifest, models = _manifest_models(project_root)
    sources = ["workbuddy-manifest"] if models else []
    arkcli_models = _arkcli_user_models()
    if arkcli_models:
        # User-level WorkBuddy configuration is the effective override for a
        # duplicate built-in id. Keep one option per built-in model id.
        for item in arkcli_models:
            for index, existing in enumerate(models):
                if existing.get("id") == item["id"] and not existing.get("provider_id"):
                    models[index] = item
                    break
            else:
                models.append(item)
        sources.append("arkcli-user-config")
    seen = {(None, item["id"]) for item in models}
    for provider in load_model_providers(project_root):
        provider_id = str(provider["provider_id"])
        for item in provider["models"]:
            key = (provider_id, str(item["id"]))
            if key in seen:
                continue
            seen.add(key)
            models.append(
                _option(
                    str(item["id"]),
                    f"{item['label']} · {provider['label']}",
                    source="configured-provider",
                    provider_id=provider_id,
                )
            )
        sources.append(provider_id)
    return {
        "status": "ok" if models else "unavailable",
        "source": manifest or "configured-provider",
        "sources": sources,
        "models": models,
        **({} if models else {"error": "WorkBuddy model manifest is unavailable"}),
    }


def get_model_catalog(project_root: Path, backend: str) -> dict[str, Any]:
    backend = "codebuddy" if backend == "workbuddy" else backend
    if backend == "codex":
        result = discover_codex_models(project_root)
        configured = [
            item
            for provider in load_model_providers(project_root)
            if provider["backend"] == backend
            for item in provider["models"]
        ]
        if not result["models"] and configured:
            result["models"] = [
                _option(str(item["id"]), str(item["label"]), source="configured-provider")
                for item in configured
            ]
        return result
    if backend == "codebuddy":
        return _apply_model_health(
            project_root, discover_codebuddy_models(project_root)
        )
    if backend == "fake":
        return {
            "status": "ok",
            "source": "built-in",
            "models": [_option("fake-v1", "Fake v1", source="built-in")],
        }
    return {
        "status": "unavailable",
        "source": backend,
        "models": [],
        "error": f"unknown backend: {backend}",
    }
