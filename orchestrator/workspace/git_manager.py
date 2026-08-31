from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class GitCommandError(RuntimeError):
    def __init__(self, args: tuple[str, ...], returncode: int, stderr: str) -> None:
        super().__init__(
            f"git command failed ({returncode}): {' '.join(args)}: {stderr.strip()}"
        )
        self.args_run = args
        self.returncode = returncode
        self.stderr = stderr


@dataclass(frozen=True)
class CheckoutFingerprint:
    head: str
    status_porcelain: str
    workspace_sha256: str

    @property
    def status_sha256(self) -> str:
        return hashlib.sha256(self.status_porcelain.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "head": self.head,
            "status_porcelain": self.status_porcelain,
            "status_sha256": self.status_sha256,
            "workspace_sha256": self.workspace_sha256,
        }


@dataclass(frozen=True)
class IntegrationResult:
    applied: bool
    commit: str
    result_commit: str | None = None
    conflicts: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "applied": self.applied,
            "commit": self.commit,
            "result_commit": self.result_commit,
            "conflicts": list(self.conflicts),
        }


def _git_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for name in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
    ):
        environment.pop(name, None)
    environment.update(
        {
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_PAGER": "cat",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "core.hooksPath",
            "GIT_CONFIG_VALUE_0": "NUL" if os.name == "nt" else "/dev/null",
        }
    )
    return environment


def _git(
    cwd: Path,
    *args: str,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        ("git", *args),
        cwd=cwd,
        env=_git_environment(),
        text=True,
        capture_output=True,
        check=False,
    )
    if check and completed.returncode != 0:
        raise GitCommandError(tuple(args), completed.returncode, completed.stderr)
    return completed


def fingerprint_checkout(cwd: Path) -> CheckoutFingerprint:
    head_result = _git(cwd, "rev-parse", "--verify", "HEAD", check=False)
    head = head_result.stdout.strip() if head_result.returncode == 0 else "UNBORN"
    status = _git(cwd, "status", "--porcelain=v1", "--untracked-files=all").stdout
    digest = hashlib.sha256()
    for path in sorted(cwd.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(cwd)
        if relative.parts[0] in {".git", ".agent-hub"}:
            continue
        digest.update(relative.as_posix().encode("utf-8"))
        if path.is_symlink():
            digest.update(b"SYMLINK")
            digest.update(os.readlink(path).encode("utf-8"))
        elif path.is_file():
            digest.update(b"FILE")
            digest.update(path.read_bytes())
        elif path.is_dir():
            digest.update(b"DIR")
    return CheckoutFingerprint(
        head=head,
        status_porcelain=status,
        workspace_sha256=digest.hexdigest(),
    )


class GitWorkspaceManager:
    def __init__(self, repository: Path, worktrees_root: Path) -> None:
        self.repository = repository
        self.worktrees_root = worktrees_root
        self._managed_worktrees: set[Path] = set()

    def initialize_repository(self) -> str:
        self.repository.mkdir(parents=True, exist_ok=False)
        self.worktrees_root.mkdir(parents=True, exist_ok=False)
        _git(self.repository, "init", "--initial-branch=main")
        _git(self.repository, "config", "user.name", "Agent Hub PoC")
        _git(self.repository, "config", "user.email", "agent-hub-poc@invalid.local")
        _git(self.repository, "config", "agenthub.managed", "true")
        demo = self.repository / "demo"
        demo.mkdir()
        (demo / "a.txt").write_text("base-a\n", encoding="utf-8")
        (demo / "b.txt").write_text("base-b\n", encoding="utf-8")
        (demo / "conflict.txt").write_text("base-conflict\n", encoding="utf-8")
        (self.repository / "AGENTS.md").write_text(
            "# Managed Stage 1 workspace\n\n"
            "- Follow only the orchestrator's current assignment.\n"
            "- Do not access paths outside this managed worktree.\n"
            "- Do not use shell, network, or external tools.\n",
            encoding="utf-8",
        )
        _git(self.repository, "add", "demo", "AGENTS.md")
        _git(self.repository, "commit", "-m", "poc: seed isolated workspace")
        return self.head(self.repository)

    def create_worktree(self, name: str, base_commit: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", name):
            raise ValueError("invalid worktree name")
        target = self.worktrees_root / name
        resolved_root = self.worktrees_root.resolve()
        resolved_target = target.resolve()
        try:
            resolved_target.relative_to(resolved_root)
        except ValueError as exc:
            raise ValueError("worktree target is outside managed root") from exc
        try:
            _git(
                self.repository,
                "worktree",
                "add",
                "-b",
                f"poc-{name}",
                str(target),
                base_commit,
            )
        except BaseException:
            # 半创建 worktree：清理残留目录后重新抛出，不留无引用 worktree。
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
            _git(
                self.repository,
                "worktree",
                "prune",
                check=False,
            )
            raise
        self._managed_worktrees.add(resolved_target)
        return resolved_target

    def commit_file(
        self,
        worktree: Path,
        relative_path: str,
        content: str,
        message: str,
    ) -> str:
        worktree_root = worktree.resolve()
        self._assert_managed_worktree(worktree_root)
        relative = Path(relative_path)
        if relative.is_absolute() or any(
            part.casefold() == ".git" for part in relative.parts
        ):
            raise ValueError("write target is reserved or absolute")
        target = (worktree_root / relative).resolve()
        try:
            target.relative_to(worktree_root)
        except ValueError as exc:
            raise ValueError("write target is outside worktree") from exc
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        _git(worktree, "add", "--", relative_path)
        _git(worktree, "commit", "-m", message)
        return self.head(worktree)

    def commit_managed_changes(
        self,
        worktree: Path,
        expected_paths: tuple[str, ...],
        message: str,
    ) -> str:
        worktree_root = worktree.resolve()
        self._assert_managed_worktree(worktree_root)
        if not expected_paths:
            raise ValueError("expected_paths must not be empty")
        normalized: list[str] = []
        for item in expected_paths:
            relative = Path(item)
            if relative.is_absolute() or ".." in relative.parts or any(
                part.casefold() == ".git" for part in relative.parts
            ):
                raise ValueError("expected path is outside or reserved")
            target = (worktree_root / relative).resolve()
            try:
                target.relative_to(worktree_root)
            except ValueError as exc:
                raise ValueError("expected path resolves outside worktree") from exc
            if target.is_symlink():
                raise ValueError("symlink changes are not accepted in the PoC")
            normalized.append(relative.as_posix())
        status_lines = _git(
            worktree_root, "status", "--porcelain=v1", "--untracked-files=all"
        ).stdout.splitlines()
        changed_paths = {
            line[3:].strip().replace("\\", "/")
            for line in status_lines
            if len(line) >= 4
        }
        if changed_paths != set(normalized):
            raise ValueError(
                f"worktree changes do not match declared scope: {sorted(changed_paths)}"
            )
        _git(worktree_root, "add", "--", *normalized)
        _git(worktree_root, "commit", "-m", message)
        if not self.is_clean(worktree_root):
            raise RuntimeError("worker commit left worktree dirty")
        return self.head(worktree_root)

    def integrate(self, commit: str) -> IntegrationResult:
        self._assert_managed_repository()
        if not self.is_clean():
            raise RuntimeError("integration repository must be clean")
        _git(self.repository, "cat-file", "-e", f"{commit}^{{commit}}")
        if self.result_commit_in_integration(commit):
            # 该 result commit 的补丁已落在集成分支：幂等成功，不重复 cherry-pick。
            return IntegrationResult(
                applied=True,
                commit=commit,
                result_commit=commit,
            )
        before_head = self.head(self.repository)
        result = _git(self.repository, "cherry-pick", commit, check=False)
        if result.returncode == 0:
            if not self.is_clean():
                raise RuntimeError("successful integration left a dirty repository")
            return IntegrationResult(
                applied=True,
                commit=commit,
                result_commit=self.head(self.repository),
            )
        conflicts = tuple(
            line.strip()
            for line in _git(
                self.repository,
                "diff",
                "--name-only",
                "--diff-filter=U",
                check=False,
            ).stdout.splitlines()
            if line.strip()
        )
        in_progress = (
            _git(
                self.repository,
                "rev-parse",
                "-q",
                "--verify",
                "CHERRY_PICK_HEAD",
                check=False,
            ).returncode
            == 0
        )
        if in_progress:
            _git(self.repository, "cherry-pick", "--abort")
        if not conflicts:
            raise GitCommandError(("cherry-pick", commit), result.returncode, result.stderr)
        if self.head(self.repository) != before_head or not self.is_clean():
            raise RuntimeError("failed integration was not fully rolled back")
        return IntegrationResult(
            applied=False,
            commit=commit,
            result_commit=None,
            conflicts=conflicts,
        )

    @staticmethod
    def head(cwd: Path) -> str:
        return _git(cwd, "rev-parse", "HEAD").stdout.strip()

    def is_clean(self, cwd: Path | None = None) -> bool:
        target = cwd or self.repository
        return _git(target, "status", "--porcelain=v1").stdout == ""

    def assert_clean_for_write(self, cwd: Path | None = None) -> None:
        target = cwd or self.repository
        if not self.is_clean(target):
            raise RuntimeError(
                f"checkout is dirty and cannot be used for writes: {target}"
            )

    def result_commit_in_integration(self, result_commit: str) -> bool:
        """True if the result commit's patch already landed on the integration
        branch. Uses git cherry (patch equivalence) because a cherry-pick
        creates a new commit that is not an ancestor of the result commit."""
        self._assert_managed_repository()
        _git(self.repository, "cat-file", "-e", f"{result_commit}^{{commit}}")
        if self.is_ancestor(result_commit, "HEAD"):
            return True
        output = _git(
            self.repository,
            "cherry",
            "-v",
            "HEAD",
            result_commit,
            check=False,
        )
        return any(
            line.startswith("- ")
            for line in output.stdout.splitlines()
            if line.strip()
        )

    def safe_write_text(self, target: Path, content: str) -> None:
        """Write text atomically, never leaving a partial file on failure.

        The parent directory must already exist; missing parents, a full disk,
        or a locked file surface as an OSError and the temp file is removed.
        """
        target = target.resolve()
        if not target.parent.is_dir():
            raise FileNotFoundError(
                f"parent directory does not exist: {target.parent}"
            )
        temporary = target.parent / f".{target.name}.tmp-{uuid.uuid4().hex[:8]}"
        try:
            temporary.write_text(content, encoding="utf-8")
            os.replace(temporary, target)
        except BaseException:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    def parent_of(self, commit: str) -> str:
        return _git(self.repository, "rev-parse", f"{commit}^").stdout.strip()

    def is_ancestor(self, commit: str, descendant: str = "HEAD") -> bool:
        return (
            _git(
                self.repository,
                "merge-base",
                "--is-ancestor",
                commit,
                descendant,
                check=False,
            ).returncode
            == 0
        )

    def read_blob(self, commit: str, relative_path: str) -> tuple[str, bytes]:
        self._assert_managed_repository()
        relative = Path(relative_path)
        if relative.is_absolute() or ".." in relative.parts or any(
            part.casefold() == ".git" for part in relative.parts
        ):
            raise ValueError("blob path is outside or reserved")
        blob_oid = _git(
            self.repository,
            "rev-parse",
            f"{commit}:{relative.as_posix()}",
        ).stdout.strip()
        completed = subprocess.run(
            ("git", "cat-file", "blob", blob_oid),
            cwd=self.repository,
            env=_git_environment(),
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            raise GitCommandError(
                ("cat-file", "blob", blob_oid),
                completed.returncode,
                completed.stderr.decode("utf-8", errors="replace"),
            )
        return blob_oid, completed.stdout

    def _assert_managed_repository(self) -> None:
        marker = _git(
            self.repository, "config", "--get", "agenthub.managed", check=False
        )
        if marker.returncode != 0 or marker.stdout.strip() != "true":
            raise ValueError("repository is not managed by Agent Hub")

    def _assert_managed_worktree(self, worktree: Path) -> None:
        if worktree not in self._managed_worktrees:
            raise ValueError("worktree was not created by this manager")
        toplevel = Path(
            _git(worktree, "rev-parse", "--show-toplevel").stdout.strip()
        ).resolve()
        if toplevel != worktree:
            raise ValueError("worktree registration mismatch")
        common_dir_raw = _git(worktree, "rev-parse", "--git-common-dir").stdout.strip()
        common_dir = Path(common_dir_raw)
        if not common_dir.is_absolute():
            common_dir = worktree / common_dir
        if common_dir.resolve() != (self.repository / ".git").resolve():
            raise ValueError("worktree belongs to a different repository")
