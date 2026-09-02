"""T4：运行时功能开关。

通过环境变量 ``AGENT_HUB_FEATURES`` 控制功能启用，逗号分隔：
- 未设置 → 全部启用（默认，向后兼容）。
- ``write,cancel`` → 仅启用 write 与 cancel。
- ``-merge`` → 禁用 merge（减号前缀）。

示例：
    AGENT_HUB_FEATURES=-merge  python -m orchestrator status
"""

from __future__ import annotations

import os


def feature_enabled(name: str) -> bool:
    raw = os.environ.get("AGENT_HUB_FEATURES", "")
    if not raw.strip():
        return True
    tokens = [token.strip() for token in raw.split(",") if token.strip()]
    disabled = {token[1:] for token in tokens if token.startswith("-")}
    if name in disabled:
        return False
    allowlist = {token for token in tokens if not token.startswith("-")}
    if allowlist:
        return name in allowlist
    return True


def enabled_features() -> tuple[str, ...]:
    """返回当前启用的功能集合（含全部默认项时返回空元组）。"""
    raw = os.environ.get("AGENT_HUB_FEATURES", "")
    if not raw.strip():
        return ()
    tokens = [token.strip() for token in raw.split(",") if token.strip()]
    disabled = {token[1:] for token in tokens if token.startswith("-")}
    allowlist = {token for token in tokens if not token.startswith("-")}
    if allowlist:
        return tuple(sorted(allowlist))
    return tuple()
