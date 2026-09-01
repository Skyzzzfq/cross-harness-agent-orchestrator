"""Canonical workspace and write-scope boundary policy (P0-03).

The policy answers two questions the SQLite store alone cannot answer safely:

1. Is a task's ``cwd`` a location we are allowed to touch? Write tasks must
   live inside a registered managed worktree; read tasks may also run from the
   project root.
2. Is a task's ``write_scope`` a well-formed, canonical, in-bounds relative
   path set? It must be non-empty, relative, free of ``..``/absolute paths/
   ``.git`` and symlink escapes, and conflict detection must be
   case-insensitive and containment-aware (a directory scope covers all of its
   children).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


def _canonical(path: str | Path) -> Path:
    """Resolve the path and normalize for the host filesystem's case rules."""
    return Path(os.path.normcase(os.path.realpath(os.fspath(path))))


@dataclass(frozen=True)
class WorkspacePolicy:
    """Boundary rules for task cwd and write_scope validation.

    ``project_root`` is the protected repository root. ``managed_worktrees``
    are the registered, resolved worktree directories that write tasks may
    target. When a policy is attached to the store, task creation and dispatch
    validate against these rules; without a policy the store stays permissive
    (used by component tests that exercise scheduling without a real
    workspace).
    """

    project_root: str | Path
    worktrees_root: str | Path | None = None
    managed_worktrees: tuple[str | Path, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "project_root", _canonical(self.project_root))
        if self.worktrees_root is not None:
            object.__setattr__(self, "worktrees_root", _canonical(self.worktrees_root))
        object.__setattr__(
            self,
            "managed_worktrees",
            tuple(_canonical(w) for w in self.managed_worktrees),
        )

    def canonical(self, path: str | Path) -> Path:
        return _canonical(path)

    # -- cwd ----------------------------------------------------------------

    def validate_cwd(self, access_mode: str, cwd: str | Path) -> str:
        """Return the canonical cwd if allowed for the access mode.

        Write tasks must land inside a registered managed worktree. Read tasks
        may additionally use the project root (never an arbitrary outside
        path).
        """
        resolved = _canonical(cwd)
        if access_mode == "write":
            for worktree in self.managed_worktrees:
                if resolved == worktree or _is_within(resolved, worktree):
                    return str(resolved)
            raise ValueError(
                f"write task cwd {resolved} is not inside a registered managed worktree"
            )
        if access_mode == "read_only":
            if _is_within(resolved, self.project_root):
                return str(resolved)
            for worktree in self.managed_worktrees:
                if _is_within(resolved, worktree):
                    return str(resolved)
            raise ValueError(
                f"read task cwd {resolved} is outside the allowed project root"
            )
        raise ValueError(f"unsupported access_mode: {access_mode}")

    # -- write_scope --------------------------------------------------------

    def validate_write_scope(
        self, scope: Iterable[str], *, base: str | Path | None = None
    ) -> tuple[str, ...]:
        """Canonicalize and validate a write scope.

        Rules: non-empty; each entry relative, no ``..``, not absolute, no
        ``.git`` segment, and (when ``base`` is given) must not escape ``base``
        via symlinks or other aliases. Returns normalized entries (forward
        slashes, trailing slash preserved for directory scopes).
        """
        entries = tuple(str(item) for item in scope)
        if not entries:
            raise ValueError("write_scope must not be empty")
        base_path = _canonical(base) if base is not None else None
        normalized = list(validate_write_scope_static(entries))
        for entry in normalized:
            if base_path is not None:
                candidate = _canonical(base_path / entry)
                if not _is_within(candidate, base_path):
                    raise ValueError(
                        f"write_scope entry {entry!r} escapes the worktree base"
                    )
        return tuple(normalized)

    def scopes_conflict(self, left: Iterable[str], right: Iterable[str]) -> bool:
        """Containment- and case-aware write_scope overlap detection."""
        left_parts = [_scope_parts(item) for item in left]
        right_parts = [_scope_parts(item) for item in right]
        for a in left_parts:
            for b in right_parts:
                if _parts_overlap(a, b):
                    return True
        return False


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def validate_write_scope_static(scope: Iterable[str]) -> tuple[str, ...]:
    """静态 write_scope 校验（不依赖 base，不检测 symlink escape）。

    任何 Task 创建都必须通过这里：非空、相对、无 ``..``、无绝对路径、
    无 ``.git``。完整校验（含 symlink escape）由 WorkspacePolicy 在带
    ``base`` 时执行。
    """
    entries = tuple(str(item) for item in scope)
    if not entries:
        raise ValueError("write_scope must not be empty")
    normalized: list[str] = []
    for entry in entries:
        _validate_scope_entry(entry)
        normalized.append(_normalize_scope_entry(entry))
    return tuple(normalized)


def _validate_scope_entry(entry: str) -> None:
    if not entry.strip():
        raise ValueError("write_scope entry must not be empty")
    if "\\" in entry:
        raise ValueError("write_scope entry must use forward slashes")
    parts = entry.split("/")
    for part in parts:
        if part in ("..",):
            raise ValueError("write_scope entry must not contain '..'")
        if part in (".git",):
            raise ValueError("write_scope entry must not target .git")
    if os.path.isabs(entry):
        raise ValueError("write_scope entry must be relative")
    # 归一化后仍是绝对路径（如 C:/...）
    if re.match(r"^[A-Za-z]:", entry):
        raise ValueError("write_scope entry must be relative")


def _normalize_scope_entry(entry: str) -> str:
    # 折叠 "./" 前缀与内部 "a//b"，保留目录尾部斜杠
    trailing = entry.endswith("/")
    collapsed = "/".join(part for part in entry.split("/") if part and part != ".")
    return (collapsed + "/") if trailing else collapsed


def _scope_parts(entry: str) -> tuple[str, ...]:
    normalized = _normalize_scope_entry(entry)
    parts = tuple(
        os.path.normcase(part) for part in normalized.strip("/").split("/")
    )
    return parts


def _parts_overlap(a: tuple[str, ...], b: tuple[str, ...]) -> bool:
    """一个 scope 若是另一个的前缀（目录包含），即冲突；大小写已由 normcase 归一。"""
    if a == b:
        return True
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    if longer[: len(shorter)] == shorter:
        return True
    return False
