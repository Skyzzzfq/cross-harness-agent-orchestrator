"""Transactional merge and outbox consumers for the resident loop (P0-02).

These execute the real side effects that the store alone must not fake:

- ``MergeExecutor`` claims a PENDING merge, runs a real Git integrate, and
  settles the merge record *only after* the result commit is provably on the
  integration branch (the store enforces this via ``is_integrated``).
- ``OutboxDispatcher`` claims a PENDING outbox intent, performs the external
  delivery hook, and marks it sent/failed.

Both are idempotent across restart: crash-after-claim is reconciled by
``reconcile_merge_with_git`` / ``reconcile_merge_queue``, and a repeated
``finish_merge`` is refused by the strict APPLYING guard.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from orchestrator.core.models import AuthorityToken, ControllerToken
from orchestrator.storage.sqlite_store import SQLiteStateStore
from orchestrator.workspace.git_manager import GitWorkspaceManager


class MergeExecutor:
    """Consume the persistent merge queue with real Git integration."""

    def __init__(self, store: SQLiteStateStore, manager: GitWorkspaceManager) -> None:
        self.store = store
        self.manager = manager

    def run_merge_once(
        self,
        run_id: str,
        controller: ControllerToken,
        authority: AuthorityToken,
    ) -> dict[str, Any]:
        claim = self.store.claim_merge_queue(run_id, controller, authority=authority)
        if claim is None:
            return {"status": "idle"}
        merge_id = str(claim["merge_id"])
        result_commit = str(claim["result_commit"])
        try:
            result = self.manager.integrate(result_commit)
        except Exception as exc:  # noqa: BLE001 - Git failure surfaces as merge failed
            self.store.finish_merge(
                merge_id,
                "failed",
                controller,
                authority=authority,
                issue_kind="unexpected",
                issue_detail={"error": type(exc).__name__, "message": str(exc)[:300]},
            )
            return {"status": "failed", "merge_id": merge_id, "error": type(exc).__name__}
        if result.applied:
            # P0-02：数据库只在 Git 对账成功后才标记 APPLIED/COMPLETED，
            # 且 merge 业务状态与 Outbox intent 同一事务。
            self.store.finish_merge(
                merge_id,
                "applied",
                controller,
                authority=authority,
                result_commit=result_commit,
                is_integrated=lambda commit: self.manager.result_commit_in_integration(
                    commit
                ),
                outbox_payload={"result_commit": result_commit},
            )
            return {"status": "applied", "merge_id": merge_id}
        conflicts = list(getattr(result, "conflicts", ()) or ())
        self.store.finish_merge(
            merge_id,
            "conflict",
            controller,
            authority=authority,
            issue_kind="content_conflict",
            issue_detail={"conflicts": conflicts},
        )
        return {"status": "conflict", "merge_id": merge_id, "conflicts": conflicts}

    def reconcile_once(
        self,
        run_id: str,
        controller: ControllerToken,
        authority: AuthorityToken,
    ) -> dict[str, Any]:
        """Crash recovery: mark already-integrated merges applied, requeue the rest."""
        return self.store.reconcile_merge_with_git(
            run_id,
            controller,
            authority=authority,
            is_applied=lambda commit: self.manager.result_commit_in_integration(commit),
        )


class OutboxDispatcher:
    """Consume the transactional outbox with an external delivery hook."""

    def __init__(
        self,
        store: SQLiteStateStore,
        deliver: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.store = store
        self.deliver = deliver

    def run_once(
        self,
        run_id: str,
        controller: ControllerToken,
        authority: AuthorityToken,
    ) -> dict[str, Any]:
        intent = self.store.claim_outbox(run_id, controller)
        if intent is None:
            return {"status": "idle"}
        outbox_id = str(intent["outbox_id"])
        try:
            if self.deliver is not None:
                self.deliver(intent)
            self.store.finish_outbox(outbox_id, "sent", controller)
            return {"status": "sent", "outbox_id": outbox_id}
        except Exception as exc:  # noqa: BLE001 - delivery failure marks outbox FAILED
            self.store.finish_outbox(outbox_id, "failed", controller)
            return {
                "status": "failed",
                "outbox_id": outbox_id,
                "error": type(exc).__name__,
            }
