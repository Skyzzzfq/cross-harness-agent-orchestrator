"""Run-scoped four-zone workspace allocation.

The manager owns paths under ``.agent-hub/runs/<run-id>``.  A manifest is the
small, inspectable source of truth for the zones; Git worktrees remain managed
by :class:`GitWorkspaceManager` and are never created in the user's checkout.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import subprocess
import tarfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orchestrator.core.models import utc_now
from orchestrator.workspace.git_manager import GitCommandError, GitWorkspaceManager


def _safe_component(value: str, *, limit: int = 24) -> str:
    text = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value)).strip(".-") or "item"
    return text[:limit]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


@dataclass(frozen=True)
class WorkspaceManifest:
    run_id: str
    base_commit: str
    root: str
    zones: dict[str, dict[str, Any]]
    resources_version: str
    owner: str
    lifecycle: str
    created_at: str
    legacy: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "manifest_version": 1,
            "run_id": self.run_id,
            "base_commit": self.base_commit,
            "root": self.root,
            "zones": self.zones,
            "resources_version": self.resources_version,
            "owner": self.owner,
            "lifecycle": self.lifecycle,
            "created_at": self.created_at,
            "legacy": self.legacy,
        }


class RunWorkspaceManager:
    """Allocate private scratch, shared, mounts, resources and worktrees."""

    ZONES = {
        "scratch": {"path": "scratch", "owner": "agent", "access": "private"},
        "shared": {"path": "shared", "owner": "hub", "access": "cooperative"},
        "mounts": {"path": "mounts", "owner": "hub", "access": "described"},
        "resources": {"path": "resources", "owner": "hub", "access": "read_only"},
        "worktrees": {"path": "worktrees", "owner": "hub", "access": "task_attempt"},
        "integration": {"path": "integration", "owner": "hub", "access": "serial"},
    }

    def __init__(self, project_root: Path, *, hub_root: Path | None = None) -> None:
        self.project_root = project_root.resolve()
        self.hub_root = (hub_root or self.project_root / ".agent-hub").resolve()
        self.runs_root = self.hub_root / "runs"
        self._git_managers: dict[str, GitWorkspaceManager] = {}

    def run_root(self, run_id: str) -> Path:
        root = self.runs_root / _safe_component(run_id, limit=48)
        root_resolved = root.resolve()
        try:
            root_resolved.relative_to(self.runs_root.resolve())
        except ValueError as exc:
            raise ValueError("run workspace escaped managed root") from exc
        return root_resolved

    def ensure_run(self, run_id: str, *, base_commit: str | None = None) -> WorkspaceManifest:
        root = self.run_root(run_id)
        manifest_path = root / "manifest.json"
        if manifest_path.is_file():
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            if str(data.get("run_id")) != str(run_id):
                raise ValueError("workspace manifest run_id mismatch")
            return WorkspaceManifest(
                run_id=str(data["run_id"]),
                base_commit=str(data["base_commit"]),
                root=str(data["root"]),
                zones=dict(data["zones"]),
                resources_version=str(data.get("resources_version") or "r2-v1"),
                owner=str(data.get("owner") or "agent-hub"),
                lifecycle=str(data.get("lifecycle") or "active"),
                created_at=str(data.get("created_at") or ""),
                legacy=bool(data.get("legacy", False)),
            )
        self._ignore_runtime_root()
        root.mkdir(parents=True, exist_ok=True)
        commit = str(base_commit or self._repository_head())
        zones: dict[str, dict[str, Any]] = {}
        for name, spec in self.ZONES.items():
            zone_path = root / str(spec["path"])
            zone_path.mkdir(parents=True, exist_ok=True)
            zones[name] = {**spec, "absolute_path": str(zone_path)}
        (root / "shared" / "inputs").mkdir(parents=True, exist_ok=True)
        (root / "shared" / "handoffs").mkdir(parents=True, exist_ok=True)
        (root / "shared" / "artifacts").mkdir(parents=True, exist_ok=True)
        (root / "shared" / "evidence").mkdir(parents=True, exist_ok=True)
        (root / "shared" / "progress").mkdir(parents=True, exist_ok=True)
        self._write_json(root / "mounts" / "manifest.json", {"mounts": [], "enabled": False})
        self._write_json(root / "resources" / "manifest.json", {"version": "r2-v1", "entries": []})
        manifest = WorkspaceManifest(
            run_id=str(run_id),
            base_commit=commit,
            root=str(root),
            zones=zones,
            resources_version="r2-v1",
            owner="agent-hub",
            lifecycle="active",
            created_at=utc_now(),
        )
        self._write_json(manifest_path, manifest.to_dict())
        return manifest

    def allocate_scratch(self, run_id: str, agent_id: str, attempt_id: str) -> Path:
        manifest = self.ensure_run(run_id)
        target = Path(manifest.zones["scratch"]["absolute_path"]) / _safe_component(agent_id) / _safe_component(attempt_id)
        self._assert_inside(target, Path(manifest.zones["scratch"]["absolute_path"]))
        target.mkdir(parents=True, exist_ok=True)
        return target

    def allocate_attempt(
        self,
        run_id: str,
        task_id: str,
        attempt_id: str,
        *,
        access_mode: str,
        base_commit: str | None = None,
        agent_id: str = "agent",
    ) -> dict[str, Any]:
        manifest = self.ensure_run(run_id, base_commit=base_commit)
        scratch = self.allocate_scratch(run_id, agent_id, attempt_id)
        if access_mode == "write":
            name = _safe_component(f"{run_id}-{task_id}-{attempt_id}", limit=63)
            manager = self._git_manager_for(run_id, Path(manifest.zones["worktrees"]["absolute_path"]))
            worktree = manager.create_worktree(name, str(base_commit or manifest.base_commit))
            path = worktree
            kind = "git_worktree"
        elif access_mode == "read_only":
            resource = self.snapshot_resources(
                run_id,
                base_commit=base_commit,
                paths=(),
            )
            path = Path(str(resource["path"]))
            kind = "resource_snapshot"
        else:
            raise ValueError(f"unsupported access_mode: {access_mode}")
        return {
            "run_id": run_id,
            "task_id": task_id,
            "attempt_id": attempt_id,
            "path": str(path),
            "workspace_path": str(path),
            "scratch_path": str(scratch),
            "base_commit": str(base_commit or manifest.base_commit),
            "kind": kind,
            "manifest_path": str(self.run_root(run_id) / "manifest.json"),
        }

    def snapshot_resources(
        self,
        run_id: str,
        *,
        base_commit: str | None = None,
        paths: tuple[str, ...] = ("AGENTS.md",),
    ) -> dict[str, Any]:
        """Materialize a small, read-only resource snapshot for a Run."""
        manifest = self.ensure_run(run_id, base_commit=base_commit)
        root = Path(manifest.zones["resources"]["absolute_path"])
        snapshot_root = root / "snapshot"
        snapshot_root.mkdir(parents=True, exist_ok=True)
        commit = str(base_commit or manifest.base_commit)
        copied: list[str] = []
        if commit != "UNVERSIONED":
            result = subprocess.run(
                ("git", "archive", commit, *paths),
                cwd=self.project_root,
                capture_output=True,
                check=False,
            )
            if result.returncode != 0:
                raise ValueError("resource snapshot base commit is unavailable")
            with tarfile.open(fileobj=io.BytesIO(result.stdout), mode="r:") as archive:
                for member in archive.getmembers():
                    if not member.isfile() or member.name.startswith(".git/"):
                        continue
                    target = (snapshot_root / member.name).resolve()
                    self._assert_inside(target, snapshot_root)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    extracted = archive.extractfile(member)
                    if extracted is None:
                        continue
                    target.write_bytes(extracted.read())
                    copied.append(member.name)
        snapshot = {
            "version": f"{commit}:resources-v1",
            "base_commit": commit,
            "paths": copied,
            "read_only": True,
            "created_at": utc_now(),
        }
        self._write_json(root / "snapshot.json", snapshot)
        return {"path": str(snapshot_root), **snapshot}

    def publish_artifact(
        self,
        run_id: str,
        task_id: str,
        attempt_id: str,
        source: Path,
        *,
        candidate_commit: str | None,
        producer: str,
    ) -> dict[str, Any]:
        manifest = self.ensure_run(run_id)
        source_resolved = source.resolve()
        artifact_root = Path(manifest.zones["shared"]["absolute_path"]) / "artifacts" / _safe_component(task_id) / _safe_component(attempt_id)
        self._assert_inside(source_resolved, Path(manifest.root))
        if not source_resolved.is_file():
            raise FileNotFoundError(source)
        artifact_root.mkdir(parents=True, exist_ok=True)
        target = artifact_root / source_resolved.name
        target.write_bytes(source_resolved.read_bytes())
        entry = {
            "artifact_id": f"artifact-{uuid.uuid4().hex[:16]}",
            "run_id": run_id,
            "task_id": task_id,
            "attempt_id": attempt_id,
            "path": str(target),
            "sha256": _sha256(target),
            "candidate_commit": candidate_commit,
            "producer": producer,
            "published_at": utc_now(),
            "version": 1,
        }
        index = self.run_root(run_id) / "shared" / "artifacts" / "manifest.json"
        existing = []
        if index.is_file():
            existing = json.loads(index.read_text(encoding="utf-8")).get("artifacts", [])
        existing.append(entry)
        self._write_json(index, {"manifest_version": 1, "artifacts": existing})
        return entry

    def _git_manager_for(self, run_id: str, worktrees_root: Path) -> GitWorkspaceManager:
        manager = self._git_managers.get(run_id)
        if manager is None:
            manager = GitWorkspaceManager(self.project_root, worktrees_root)
            self._git_managers[run_id] = manager
        return manager

    def _repository_head(self) -> str:
        manager = GitWorkspaceManager(self.project_root, self.hub_root / "_r2_probe_worktrees")
        try:
            return manager.head(self.project_root)
        except GitCommandError:
            # Read-only tasks can still receive a four-zone manifest in a
            # directory that has not been initialized as a Git repository;
            # write attempts fail explicitly when they request a worktree.
            return "UNVERSIONED"

    def _ignore_runtime_root(self) -> None:
        """Keep Hub runtime files out of the user's checkout status."""
        exclude = self.project_root / ".git" / "info" / "exclude"
        try:
            current = exclude.read_text(encoding="utf-8") if exclude.is_file() else ""
            lines = {line.strip() for line in current.splitlines()}
            if ".agent-hub/" not in lines:
                exclude.parent.mkdir(parents=True, exist_ok=True)
                exclude.write_text(
                    (current.rstrip() + "\n" if current.strip() else "") + ".agent-hub/\n",
                    encoding="utf-8",
                )
        except OSError:
            # Runtime state remains usable even when the repository metadata
            # is read-only; callers still get an explicit manifest path.
            return

    @staticmethod
    def _assert_inside(path: Path, parent: Path) -> None:
        try:
            path.resolve().relative_to(parent.resolve())
        except ValueError as exc:
            raise ValueError(f"workspace path escaped zone: {path}") from exc

    @staticmethod
    def _write_json(path: Path, data: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.parent / f".{path.name}.tmp-{uuid.uuid4().hex[:8]}"
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)
