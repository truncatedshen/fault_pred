"""Runtime data ownership, bounded observations, history and checkpoints."""

from __future__ import annotations

import math
import pickle
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from itertools import count
from pathlib import Path
from threading import RLock
from typing import Any, Callable
from uuid import uuid4

import numpy as np
import pandas as pd

from fault_platform.streaming import StreamedDataset


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def json_safe(value: Any, limit: int = 100, depth: int = 0) -> Any:
    """Convert scientific values to JSON with a per-container preview bound."""
    if depth > 8:
        return "<preview depth limit>"
    if value is None or value is pd.NA or value is pd.NaT:
        return None
    if isinstance(value, (str, bool)):
        return value[:4000] if isinstance(value, str) else value
    if isinstance(value, (int, np.integer)):
        return int(value)
    if isinstance(value, (float, np.floating)):
        return float(value) if math.isfinite(value) else None
    if isinstance(value, dict):
        items = list(value.items())
        result = {str(k): json_safe(v, limit, depth + 1) for k, v in items[:limit]}
        if len(items) > limit:
            result["_truncated"] = len(items) - limit
        return result
    if isinstance(value, (list, tuple, np.ndarray, pd.Index)):
        return [json_safe(v, limit, depth + 1) for v in list(value)[:limit]]
    return str(value)[:500]


def _compact_mapping(value: dict[str, Any], limit: int, include_indices: bool) -> dict[str, Any]:
    """Keep dictionaries readable: long lists become counts plus a short preview."""
    result: dict[str, Any] = {}
    for key, item in value.items():
        if isinstance(item, (list, tuple, pd.Index, np.ndarray)) and len(item) > limit:
            if not include_indices and key.endswith("_indices"):
                result[f"{key}_count"] = len(item)  # e.g. train_indices_count
                continue
            result[key] = {"count": len(item), "preview": json_safe(list(item)[:5])}
        elif isinstance(item, dict):
            result[key] = _compact_mapping(item, limit, include_indices)
        else:
            result[key] = json_safe(item)
    return result


def summarize(value: Any, limit: int = 20, include_indices: bool = False) -> dict[str, Any]:
    limit = max(1, min(limit, 100))
    if isinstance(value, StreamedDataset):
        return {"kind": "streamed", **value.describe()}
    if isinstance(value, pd.DataFrame):
        preview = value.iloc[:limit, :50]
        return {
            "kind": "table",
            "shape": list(value.shape),
            "columns": list(value.columns[:50]),
            "dtypes": value.dtypes.astype(str).iloc[:50].to_dict(),
            # Rates come from the preview rows: computing them over every row would
            # allocate a full boolean frame just to render twenty records.
            "missing_rate": json_safe(preview.isna().mean().to_dict()),
            "missing_rate_rows": len(preview),
            "index": json_safe(preview.index.tolist()),
            "preview": json_safe(preview.to_dict(orient="records"), limit=max(limit, 50)),
            "truncated": len(value) > limit or value.shape[1] > 50,
        }
    if isinstance(value, pd.Series):
        return {
            "kind": "vector",
            "length": len(value),
            "name": value.name,
            "preview": json_safe(value.head(limit).tolist()),
            "truncated": len(value) > limit,
        }
    if isinstance(value, np.ndarray):
        return {"kind": "array", "shape": list(value.shape), "preview": json_safe(value[:limit])}
    if hasattr(value, "predict"):
        return {
            "kind": "model",
            "class": type(value).__name__,
            "features": json_safe(getattr(value, "columns", [])),
            "categorical_encoders": len(getattr(value, "categorical_encoders", [])),
        }
    if hasattr(value, "transform"):
        description = value.describe() if callable(getattr(value, "describe", None)) else {}
        return {"kind": "transformer", "class": type(value).__name__, **json_safe(description)}
    if isinstance(value, dict):
        # Index arrays dominate metric payloads and tell an agent nothing extra, so
        # they collapse into counts unless explicitly requested.
        return {"kind": "object", "value": _compact_mapping(value, limit, include_indices)}
    return {"kind": "object", "value": json_safe(value)}


class PipelineStatus(StrEnum):
    CREATED = "CREATED"
    VALIDATING = "VALIDATING"
    READY = "READY"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class NodeStatus(StrEnum):
    PENDING = "PENDING"
    READY = "READY"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class MemoryArtifactStore:
    """Object store keyed by reference, with an optional byte budget.

    Values are kept **by reference**: the runtime copies an artifact only when a
    component consumes it (`FaultWorkspace.get_output(copy=True)`), so storing and
    previewing never duplicate a large frame. Entries can be pinned (checkpoints).

    When a byte budget is configured, the least recently used unpinned entries are
    moved out of memory: **spilled to disk** when a spill directory is configured
    (the default for the server) or dropped when it is not. Spilling keeps results
    available while bounding RAM; dropping invalidates the owning node instead.
    """

    def __init__(self, max_bytes: int | None = None, spill_dir: str | Path | None = None) -> None:
        self._entries: dict[str, _Entry] = {}
        self._max_bytes = max_bytes if max_bytes and max_bytes > 0 else None
        self._bytes = 0
        self._disk_bytes = 0
        self._spill_dir = Path(spill_dir).resolve() if spill_dir else None
        if self._spill_dir is not None:
            self._spill_dir.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._clock = count(1)
        self.evictions = 0
        self.spills = 0
        self.loads = 0
        self.on_evict: Callable[[str], None] | None = None
        self._suspended = 0

    def put(self, value: Any, owner: str | None = None, copy: bool = False) -> str:
        stored = deepcopy(value) if copy else value
        reference = f"artifact://{uuid4().hex}"
        with self._lock:
            self._entries[reference] = _Entry(stored, estimate_size(stored), owner, 0, next(self._clock))
            self._bytes += self._entries[reference].size
        return reference

    def peek(self, reference: str) -> Any:
        """Read without copying; callers must treat the value as read-only."""
        loaded = False
        with self._lock:
            entry = self._entries[reference]
            if entry.spilled:
                entry.value = self._load(entry)
                self._remove_file(entry.path)  # the payload is back in memory; drop the file
                entry.spilled = False
                entry.path = None
                self._bytes += entry.size
                self._disk_bytes -= entry.size
                self.loads += 1
                loaded = True
            entry.used = next(self._clock)
            value = entry.value
        if loaded:
            # Do not immediately spill the entry we just brought back.
            self.enforce_budget(exclude={reference})
        return value

    def get(self, reference: str, copy: bool = True) -> Any:
        value = self.peek(reference)
        return deepcopy(value) if copy else value

    def delete(self, reference: str) -> None:
        """Drop a reference; a payload still pinned elsewhere (checkpoint) stays alive."""
        with self._lock:
            entry = self._entries.get(reference)
            if entry is None or entry.pins > 0:
                return
            del self._entries[reference]
            if entry.spilled:
                self._disk_bytes -= entry.size
                self._remove_file(entry.path)
            else:
                self._bytes -= entry.size

    def pin(self, references: Any) -> None:
        with self._lock:
            for reference in references:
                entry = self._entries.get(reference)
                if entry is not None:
                    entry.pins += 1

    def unpin(self, references: Any) -> None:
        with self._lock:
            for reference in references:
                entry = self._entries.get(reference)
                if entry is not None:
                    entry.pins = max(0, entry.pins - 1)

    def contains(self, reference: str) -> bool:
        with self._lock:
            return reference in self._entries

    def enforce_budget(self, exclude: Any = ()) -> list[str]:
        """Move least-recently-used unpinned entries out of memory until the budget fits.

        Entries are spilled to disk when a spill directory exists, otherwise they are
        evicted (and their owning node is invalidated by the callback).
        """
        if self._max_bytes is None or self._suspended:
            return []
        evicted: list[str] = []
        excluded = set(exclude)
        with self._lock:
            while self._bytes > self._max_bytes:
                candidates = [
                    ref
                    for ref, entry in self._entries.items()
                    if not entry.pins and not entry.spilled and ref not in excluded
                ]
                if not candidates:
                    break
                oldest = min(candidates, key=lambda ref: self._entries[ref].used)
                if self._spill(oldest):
                    continue
                entry = self._entries.pop(oldest)
                self._bytes -= entry.size
                self.evictions += 1
                evicted.append(oldest)
        for reference in evicted:
            if self.on_evict is not None:
                try:
                    self.on_evict(reference)
                except Exception:  # pragma: no cover - bookkeeping must never break a run
                    pass
        return evicted

    def _spill(self, reference: str) -> bool:
        """Write one payload to disk and release its memory; False when impossible."""
        if self._spill_dir is None:
            return False
        entry = self._entries[reference]
        path = self._spill_dir / f"{reference.rsplit('/', 1)[-1]}.pickle"
        try:
            with path.open("wb") as handle:
                pickle.dump(entry.value, handle, protocol=pickle.HIGHEST_PROTOCOL)
        except Exception:  # pragma: no cover - fall back to eviction
            path.unlink(missing_ok=True)
            return False
        entry.value = None
        entry.spilled = True
        entry.path = str(path)
        self._bytes -= entry.size
        self._disk_bytes += entry.size
        self.spills += 1
        return True

    def _load(self, entry: _Entry) -> Any:
        with Path(entry.path).open("rb") as handle:
            return pickle.load(handle)

    @staticmethod
    def _remove_file(path: str | None) -> None:
        if path:
            Path(path).unlink(missing_ok=True)

    def cleanup(self) -> None:
        """Release every payload, including files this store spilled."""
        with self._lock:
            for entry in self._entries.values():
                if entry.spilled:
                    self._remove_file(entry.path)
            self._entries.clear()
            self._bytes = 0
            self._disk_bytes = 0

    def suspend(self) -> None:
        """Pause eviction: a running DAG still needs every upstream output."""
        with self._lock:
            self._suspended += 1

    def resume(self) -> None:
        with self._lock:
            self._suspended = max(0, self._suspended - 1)
        self.enforce_budget()

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "artifacts": len(self._entries),
                "bytes": self._bytes,
                "max_bytes": self._max_bytes,
                "pinned": sum(1 for entry in self._entries.values() if entry.pins),
                "evictions": self.evictions,
                "suspended": self._suspended > 0,
                "spilled": sum(1 for entry in self._entries.values() if entry.spilled),
                "disk_bytes": self._disk_bytes,
                "spills": self.spills,
                "loads": self.loads,
                "spill_dir": str(self._spill_dir) if self._spill_dir else None,
            }


@dataclass
class _Entry:
    value: Any
    size: int
    owner: str | None = None
    pins: int = 0
    used: int = 0
    spilled: bool = False
    path: str | None = None


def estimate_size(value: Any) -> int:
    """Best-effort in-memory size for cache accounting (never exact, never copies)."""
    if isinstance(value, pd.DataFrame):
        return int(value.memory_usage(deep=True).sum())
    if isinstance(value, pd.Series):
        return int(value.memory_usage(deep=True))
    if isinstance(value, np.ndarray):
        return int(value.nbytes)
    if isinstance(value, dict):
        return sum(estimate_size(item) for item in value.values()) + 256
    if isinstance(value, (list, tuple, set)):
        return sum(estimate_size(item) for item in value) + 64
    if isinstance(value, str):
        return len(value) * 2
    state = getattr(value, "__dict__", None)
    if isinstance(state, dict):
        return sum(estimate_size(item) for item in state.values()) + 256
    return 256


@dataclass
class ExecutionHistory:
    timestamp: str
    node_id: str
    component_type: str
    state_before: str
    state_after: str
    execution_time: float
    input_summary: dict[str, Any]
    output_summary: dict[str, Any]
    success: bool
    error: dict[str, Any] | None = None
    cached: bool = False


class FaultWorkspace:
    def __init__(
        self,
        pipeline_id: str,
        workspace_id: str | None = None,
        artifact_cache_bytes: int | None = None,
        spill_dir: str | Path | None = None,
    ) -> None:
        self.workspace_id = workspace_id or f"ws_{uuid4().hex[:12]}"
        self.pipeline_id = pipeline_id
        self.created_at = now()
        self.updated_at = self.created_at
        self.version = 1
        self.status = PipelineStatus.CREATED
        self.inputs: dict[str, Any] = {}
        self.node_results: dict[str, dict[str, str]] = {}
        self.node_status: dict[str, NodeStatus] = {}
        self.errors: dict[str, dict[str, Any]] = {}
        self.history: list[ExecutionHistory] = []
        self.fingerprints: dict[str, str] = {}
        self.metadata: dict[str, Any] = {}
        self.warnings: list[str] = []
        self.node_warnings: dict[str, list[str]] = {}
        self.artifacts = MemoryArtifactStore(artifact_cache_bytes, spill_dir)
        self.artifacts.on_evict = self._on_artifact_evicted
        self.lock = RLock()

    def cleanup(self) -> None:
        """Release memory and spilled files owned by this workspace."""
        self.artifacts.cleanup()

    def touch(self) -> None:
        self.version += 1
        self.updated_at = now()

    def clear_node(self, node_id: str) -> None:
        with self.lock:
            for reference in self.node_results.pop(node_id, {}).values():
                self.artifacts.delete(reference)
            self.fingerprints.pop(node_id, None)
            self.errors.pop(node_id, None)
            self.node_warnings.pop(node_id, None)
            self.node_status[node_id] = NodeStatus.PENDING
            self.touch()

    def _on_artifact_evicted(self, reference: str) -> None:
        """An evicted artifact invalidates its node, so a later run recomputes it."""
        with self.lock:
            for node_id, ports in list(self.node_results.items()):
                if reference not in ports.values():
                    continue
                kept = {port: ref for port, ref in ports.items() if ref != reference}
                if kept:
                    self.node_results[node_id] = kept
                else:
                    self.node_results.pop(node_id, None)
                    self.fingerprints.pop(node_id, None)
                    self.errors.pop(node_id, None)
                    self.node_warnings.pop(node_id, None)
                    self.node_status[node_id] = NodeStatus.PENDING
                # Results are no longer complete; the pipeline can be run again.
                if self.status == PipelineStatus.SUCCESS:
                    self.status = PipelineStatus.READY
                self.warnings = list(
                    dict.fromkeys(
                        [*self.warnings, f"{node_id}: cached output was evicted; it will recompute."]
                    )
                )
                break

    def store_outputs(self, node_id: str, outputs: dict[str, Any], copy: bool = False) -> None:
        """Store component outputs by reference; components own what they return."""
        with self.lock:
            self.clear_node(node_id)
            self.node_results[node_id] = {
                port: self.artifacts.put(value, owner=node_id, copy=copy) for port, value in outputs.items()
            }
            self.touch()
        # Enforce only after the references are registered, so eviction bookkeeping
        # can always find the owning node.
        self.artifacts.enforce_budget()

    def get_output(self, node_id: str, port: str, copy: bool = True) -> Any:
        """Resolve an upstream output for a consumer.

        Copies by default, which is what keeps one branch from corrupting another's
        cached artifact; pass ``copy=False`` for read-only inspection.
        """
        with self.lock:
            try:
                reference = self.node_results[node_id][port]
            except KeyError as exc:
                raise ValueError(f"Output unavailable: {node_id}.{port}") from exc
        return self.artifacts.get(reference, copy=copy)

    def references(self) -> set[str]:
        with self.lock:
            return {reference for ports in self.node_results.values() for reference in ports.values()}

    def get_node_result(self, node_id: str, limit: int = 20, include_indices: bool = False) -> dict[str, Any]:
        with self.lock:
            return {
                "node_id": node_id,
                "status": self.node_status.get(node_id, NodeStatus.PENDING),
                "outputs": {
                    port: {
                        **summarize(self.artifacts.peek(ref), limit, include_indices),
                        "artifact": ref,
                    }
                    for port, ref in self.node_results.get(node_id, {}).items()
                },
                "error": json_safe(self.errors.get(node_id)),
            }

    def cache_stats(self) -> dict[str, Any]:
        return self.artifacts.stats()

    def get_summary(self) -> dict[str, Any]:
        with self.lock:
            return {
                "workspace_id": self.workspace_id,
                "pipeline_id": self.pipeline_id,
                "status": self.status,
                "created_at": self.created_at,
                "updated_at": self.updated_at,
                "version": self.version,
                "node_status": dict(self.node_status),
                "warnings": list(self.warnings),
                "errors": json_safe(self.errors),
            }

    def snapshot(self) -> dict[str, Any]:
        """Metadata plus artifact *references*; payloads stay in the store and are pinned."""
        with self.lock:
            state = {k: v for k, v in self.__dict__.items() if k not in {"lock", "artifacts"}}
            return deepcopy(state)

    def restore(self, snapshot: dict[str, Any]) -> None:
        with self.lock:
            self.__dict__.update(deepcopy(snapshot))
            self.touch()

    def prune_missing_artifacts(self) -> list[str]:
        """Drop references whose payload is gone (e.g. a checkpoint of a reset workspace)."""
        with self.lock:
            affected = []
            for node_id, ports in list(self.node_results.items()):
                kept = {port: ref for port, ref in ports.items() if self.artifacts.contains(ref)}
                if len(kept) == len(ports):
                    continue
                affected.append(node_id)
                if kept:
                    self.node_results[node_id] = kept
                else:
                    self.node_results.pop(node_id, None)
                    self.fingerprints.pop(node_id, None)
                    self.node_status[node_id] = NodeStatus.PENDING
            if affected:
                self.warnings = list(
                    dict.fromkeys(
                        [*self.warnings, "Checkpoint outputs were released; affected nodes recompute."]
                    )
                )
            return affected


@dataclass
class Checkpoint:
    checkpoint_id: str
    pipeline_id: str
    workspace_id: str
    graph_version: int
    timestamp: str
    completed_nodes: list[str]
    failed_nodes: list[str]
    graph: dict[str, Any] = field(repr=False)
    snapshot: dict[str, Any] = field(repr=False)
    artifact_refs: list[str] = field(default_factory=list, repr=False)

    def summary(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if k not in {"graph", "snapshot", "artifact_refs"}}


class WorkspaceManager:
    def __init__(self, artifact_cache_bytes: int | None = None, spill_root: str | Path | None = None) -> None:
        self.workspaces: dict[str, FaultWorkspace] = {}
        self.checkpoints: dict[str, Checkpoint] = {}
        self.artifact_cache_bytes = artifact_cache_bytes
        self.spill_root = Path(spill_root).resolve() if spill_root else None

    def create_workspace(self, pipeline_id: str) -> FaultWorkspace:
        workspace_id = f"ws_{uuid4().hex[:12]}"
        ws = FaultWorkspace(
            pipeline_id,
            workspace_id,
            artifact_cache_bytes=self.artifact_cache_bytes,
            spill_dir=self._spill_dir(workspace_id),
        )
        self.workspaces[ws.workspace_id] = ws
        return ws

    def _spill_dir(self, workspace_id: str | None) -> Path | None:
        if self.spill_root is None or self.artifact_cache_bytes is None:
            return None
        directory = self.spill_root / (workspace_id or f"ws_{uuid4().hex[:12]}")
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def get_workspace(self, workspace_id: str, pipeline_id: str | None = None) -> FaultWorkspace:
        if workspace_id not in self.workspaces:
            raise ValueError(f"Unknown workspace: {workspace_id}")
        ws = self.workspaces[workspace_id]
        if pipeline_id and ws.pipeline_id != pipeline_id:
            raise ValueError("Workspace belongs to another pipeline")
        return ws

    def delete_workspace(self, workspace_id: str) -> None:
        ws = self.get_workspace(workspace_id)
        if ws.status == PipelineStatus.RUNNING:
            raise ValueError("Cannot delete a running workspace")
        ws.cleanup()
        self._remove_spill_dir(workspace_id)
        del self.workspaces[workspace_id]
        self.checkpoints = {k: cp for k, cp in self.checkpoints.items() if cp.workspace_id != workspace_id}

    def reset_workspace(self, workspace_id: str) -> FaultWorkspace:
        ws = self.get_workspace(workspace_id)
        if ws.status == PipelineStatus.RUNNING:
            raise ValueError("Cannot reset a running workspace")
        ws.cleanup()
        self._remove_spill_dir(workspace_id)
        self.workspaces[workspace_id] = FaultWorkspace(
            ws.pipeline_id,
            workspace_id,
            artifact_cache_bytes=self.artifact_cache_bytes,
            spill_dir=self._spill_dir(workspace_id),
        )
        return self.workspaces[workspace_id]

    def _remove_spill_dir(self, workspace_id: str) -> None:
        if self.spill_root is None:
            return
        directory = (self.spill_root / workspace_id).resolve()
        if not directory.is_relative_to(self.spill_root) or not directory.exists():
            return
        for path in directory.glob("*"):
            path.unlink(missing_ok=True)
        directory.rmdir()

    def cleanup(self) -> None:
        for workspace in self.workspaces.values():
            workspace.cleanup()

    def delete_by_pipeline(self, pipeline_id: str) -> int:
        """Drop every workspace of a pipeline, release its payloads and its spill files."""
        removed = 0
        for workspace_id, workspace in list(self.workspaces.items()):
            if workspace.pipeline_id != pipeline_id:
                continue
            workspace.cleanup()
            self._remove_spill_dir(workspace_id)
            del self.workspaces[workspace_id]
            removed += 1
        self.checkpoints = {
            key: checkpoint
            for key, checkpoint in self.checkpoints.items()
            if checkpoint.pipeline_id != pipeline_id
        }
        return removed

    def cache_stats(self) -> dict[str, Any]:
        stores = [ws.artifacts.stats() for ws in self.workspaces.values()]
        return {
            "workspaces": len(stores),
            "artifacts": sum(store["artifacts"] for store in stores),
            "bytes": sum(store["bytes"] for store in stores),
            "max_bytes": self.artifact_cache_bytes,
            "pinned": sum(store["pinned"] for store in stores),
            "evictions": sum(store["evictions"] for store in stores),
        }

    def save_checkpoint(self, workspace_id: str, graph: dict[str, Any]) -> Checkpoint:
        ws = self.get_workspace(workspace_id, graph["id"])
        if ws.status == PipelineStatus.RUNNING:
            raise ValueError("Wait for a node-boundary-complete run before saving a checkpoint")
        # Payloads are shared by reference and pinned, so a checkpoint costs metadata only.
        snapshot = ws.snapshot()
        references = sorted(ws.references())
        ws.artifacts.pin(references)
        cp = Checkpoint(
            f"cp_{uuid4().hex[:12]}",
            ws.pipeline_id,
            ws.workspace_id,
            graph["version"],
            now(),
            [n for n, status in ws.node_status.items() if status == NodeStatus.SUCCESS],
            [n for n, status in ws.node_status.items() if status == NodeStatus.FAILED],
            deepcopy(graph),
            snapshot,
            references,
        )
        self.checkpoints[cp.checkpoint_id] = cp
        return cp

    def load_checkpoint(self, checkpoint_id: str) -> tuple[FaultWorkspace, dict[str, Any]]:
        if checkpoint_id not in self.checkpoints:
            raise ValueError("Unknown checkpoint")
        cp = self.checkpoints[checkpoint_id]
        ws = self.get_workspace(cp.workspace_id)
        if ws.status == PipelineStatus.RUNNING:
            raise ValueError("Cannot restore a running workspace")
        ws.restore(cp.snapshot)
        ws.prune_missing_artifacts()
        return ws, deepcopy(cp.graph)

    def delete_checkpoint(self, checkpoint_id: str) -> None:
        """Drop a checkpoint and release the artifacts it was keeping alive."""
        checkpoint = self.checkpoints.pop(checkpoint_id, None)
        if checkpoint is None:
            raise ValueError("Unknown checkpoint")
        workspace = self.workspaces.get(checkpoint.workspace_id)
        if workspace is not None:
            workspace.artifacts.unpin(checkpoint.artifact_refs)
            workspace.artifacts.enforce_budget()
