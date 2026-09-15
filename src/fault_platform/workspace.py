"""Runtime data ownership, bounded observations, history and checkpoints.

workspace 是"运行产物与运行状态的家"：图（:mod:`fault_platform.graph`）只有配置，
数据、模型、指标、节点状态、执行历史、检查点全在这里，按 ``pipeline_id`` 关联。

本文件承担的三件硬事：

1. **产物按引用保存**（:class:`MemoryArtifactStore`）。存与预览都不复制对象，
   只有在组件真正消费时才 ``deepcopy`` 一次；因此 2 GB 的输入不会因为"存一份、预览一份、
   检查点再一份"而把内存翻几倍。
2. **观测必须有界**（:func:`summarize` / :func:`json_safe`）。任何通过 MCP/HTTP 返回的对象
   都会先被摘要化：长数组折叠成计数、表格只给前若干行、字符串截断，
   避免 Agent 的上下文被 8000 行索引淹没。
3. **预算与淘汰**。配置了缓存预算时按 LRU 把未固定的产物移出内存：
   有溢写目录就写盘（默认，服务端开启），否则丢弃并**让对应节点失效**（状态回到 PENDING，
   执行状态退回 READY），下次运行重算——宁可重算，也不返回过期结果。
"""

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
    """统一的时间戳：UTC ISO 8601 字符串（跨机器可比较、可排序）。"""
    return datetime.now(timezone.utc).isoformat()


def json_safe(value: Any, limit: int = 100, depth: int = 0) -> Any:
    """把科学计算对象转成可 JSON 序列化的形式，并对每个容器做长度/深度限制。

    * NaN/inf 转成 None（JSON 没有这两个值，直接序列化会得到非法 JSON）；
    * 字典与序列只保留前 ``limit`` 项，字典额外标 ``_truncated`` 告知还有多少；
    * 递归深度上限 8，超深结构折叠成占位字符串（防止自引用或超深嵌套）。
    """
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
    """让字典保持可读：超长列表变成"计数 + 前 5 项预览"。

    特别处理 ``*_indices`` 字段：指标里 ``train_indices``/``test_indices`` 动辄几千条，
    它们对理解结论几乎无用，因此默认直接折叠成 ``train_indices_count`` 这样的计数，
    只有显式要求（``include_indices=True``）时才展开。
    """
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
    """把任意运行产物摘要成有界的字典，供 MCP/HTTP 返回与前端展示。

    按类型分支返回不同 ``kind``（``table``/``vector``/``array``/``streamed``/``model``/
    ``transformer``/``object``），调用方据此渲染；无论哪种，返回体积都与输入规模无关。
    表格只取前 ``limit`` 行、前 50 列，缺失率也只按这批预览行统计
    （对全表统计等于为了展示 20 行而遍历整张表）。
    """
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
    """方案（一次运行）的状态：CREATED → VALIDATING → READY/RUNNING → SUCCESS/FAILED/CANCELLED。"""

    CREATED = "CREATED"
    VALIDATING = "VALIDATING"
    READY = "READY"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class NodeStatus(StrEnum):
    """节点状态：PENDING（待算）/READY/RUNNING/SUCCESS/FAILED/SKIPPED（上游不可用）。"""

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

    关键语义（中文）：

    * ``put`` 默认**不复制**（``copy=False``）——产物按引用入库；
    * ``peek`` 返回对象本身，调用者必须只读（预览路径用它，避免整表拷贝）；
    * ``get`` 默认复制，防止一个分支的原地修改污染别的分支读到的缓存产物；
    * ``pin``/``unpin`` 给检查点用：被固定的条目不会被淘汰；
    * ``suspend``/``resume`` 用于 DAG 执行期间暂停淘汰（执行中每个上游产物都可能还需要）。
    """

    def __init__(self, max_bytes: int | None = None, spill_dir: str | Path | None = None) -> None:
        """``max_bytes=None`` 表示不限预算；有 ``spill_dir`` 时超预算优先写盘而不是丢弃。"""
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
        """保存一个产物，返回 ``artifact://…`` 引用；``owner`` 用于淘汰时反查节点。"""
        stored = deepcopy(value) if copy else value
        reference = f"artifact://{uuid4().hex}"
        with self._lock:
            self._entries[reference] = _Entry(stored, estimate_size(stored), owner, 0, next(self._clock))
            self._bytes += self._entries[reference].size
        return reference

    def peek(self, reference: str) -> Any:
        """不复制地读取（调用方必须视作只读）。

        如果该条目已被溢写到磁盘，这里会把它读回内存并删除磁盘副本
        （"回内存"要减少一次多余的解码，同时把磁盘占用释放掉）。
        """
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
            # 刚读回来的条目要排除在淘汰候选之外，否则会在下一轮立刻又被写回磁盘。
            self.enforce_budget(exclude={reference})
        return value

    def get(self, reference: str, copy: bool = True) -> Any:
        """按引用取产物；默认返回深拷贝，隔离消费方与缓存。"""
        value = self.peek(reference)
        return deepcopy(value) if copy else value

    def delete(self, reference: str) -> None:
        """删除一个引用；若该载荷仍被检查点固定（``pins > 0``）则什么也不做。"""
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
        """固定若干引用（检查点），固定期间不会被 LRU 淘汰。"""
        with self._lock:
            for reference in references:
                entry = self._entries.get(reference)
                if entry is not None:
                    entry.pins += 1

    def unpin(self, references: Any) -> None:
        """解除固定；计数不会低于 0。"""
        with self._lock:
            for reference in references:
                entry = self._entries.get(reference)
                if entry is not None:
                    entry.pins = max(0, entry.pins - 1)

    def contains(self, reference: str) -> bool:
        """该引用当前是否还在存储里（检查点恢复时用来发现载荷已丢失）。"""
        with self._lock:
            return reference in self._entries

    def enforce_budget(self, exclude: Any = ()) -> list[str]:
        """Move least-recently-used unpinned entries out of memory until the budget fits.

        Entries are spilled to disk when a spill directory exists, otherwise they are
        evicted (and their owning node is invalidated by the callback).

        循环取出"未固定、未溢写、且不在 exclude 里"的最久未使用者；
        能写盘就写盘（内存立即下降），不能写盘就真淘汰并触发 ``on_evict`` 回调
        （由 workspace 把对应节点打回 PENDING）。每次调用返回被真正淘汰的引用列表。
        """
        if self._max_bytes is None or self._suspended:
            return []
        evicted: list[str] = []
        excluded = set(exclude)
        with self._lock:
            while self._bytes > self._max_bytes:
                # 每轮重新挑最旧的候选：一次写入/淘汰就可能改变剩余项的相对顺序。
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
        """把一个载荷 pickle 到磁盘并释放内存；无法溢写（无目录/不可序列化）时返回 False。"""
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
        """从磁盘读回一个溢写条目。"""
        with Path(entry.path).open("rb") as handle:
            return pickle.load(handle)

    @staticmethod
    def _remove_file(path: str | None) -> None:
        """删除溢写文件（不存在也算成功）。"""
        if path:
            Path(path).unlink(missing_ok=True)

    def cleanup(self) -> None:
        """释放全部载荷，包括已经写到磁盘的溢写文件（服务/workspace 关闭时调用）。"""
        with self._lock:
            for entry in self._entries.values():
                if entry.spilled:
                    self._remove_file(entry.path)
            self._entries.clear()
            self._bytes = 0
            self._disk_bytes = 0

    def suspend(self) -> None:
        """暂停淘汰（可重入计数）：DAG 执行期间所有上游产物都可能还被需要。"""
        with self._lock:
            self._suspended += 1

    def resume(self) -> None:
        """恢复淘汰；计数归零时立刻补一次预算检查。"""
        with self._lock:
            self._suspended = max(0, self._suspended - 1)
        self.enforce_budget()

    def stats(self) -> dict[str, Any]:
        """缓存统计：条目数、字节数、固定数、溢写/读回/淘汰次数与目录。

        这些数字是**如实上报**的：任务结束时如果发生过溢写或淘汰，报告里要写出来。
        """
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
    """存储中的一条记录：载荷、估算大小、属主节点、固定计数、LRU 时间戳与溢写状态。"""

    value: Any
    size: int
    owner: str | None = None
    pins: int = 0
    used: int = 0
    spilled: bool = False
    path: str | None = None


def estimate_size(value: Any) -> int:
    """尽最大努力估算内存占用（不精确、绝不复制数据）。

    只用于"预算是否超了"的近似判断：DataFrame/Series 用 pandas 的深估算，
    ndarray 用 ``nbytes``，容器递归求和，其它对象按 ``__dict__`` 递归或给个固定下界。
    """
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
    """一条执行历史：状态迁移、耗时、输入/输出摘要、是否缓存命中、失败详情。"""

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
        """创建一个 workspace（通常由 :class:`WorkspaceManager` 负责创建与登记）。"""
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
        """释放本 workspace 占用的内存与磁盘溢写文件。"""
        self.artifacts.cleanup()

    def touch(self) -> None:
        """版本号 +1 并更新时间戳（任何状态变化都会调用它，供前端判断是否需要重画）。"""
        self.version += 1
        self.updated_at = now()

    def clear_node(self, node_id: str) -> None:
        """清空某节点的全部痕迹：产物引用、指纹、错误、警告，状态回到 PENDING。"""
        with self.lock:
            for reference in self.node_results.pop(node_id, {}).values():
                self.artifacts.delete(reference)
            self.fingerprints.pop(node_id, None)
            self.errors.pop(node_id, None)
            self.node_warnings.pop(node_id, None)
            self.node_status[node_id] = NodeStatus.PENDING
            self.touch()

    def _on_artifact_evicted(self, reference: str) -> None:
        """产物被淘汰时的回调：让属主节点失效，下次运行重算。

        这是"诚实优先"的取舍：被淘汰的结果不再可用，于是该节点回到 PENDING、
        方案状态从 SUCCESS 退回 READY，并追加一条可见的警告——
        绝不留下"看起来还有结果、其实已经没了"的假象。
        """
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
        """按端口保存组件产物（默认按引用，不复制）。"""
        with self.lock:
            self.clear_node(node_id)
            self.node_results[node_id] = {
                port: self.artifacts.put(value, owner=node_id, copy=copy) for port, value in outputs.items()
            }
            self.touch()
        # 先登记引用再检查预算：否则淘汰回调可能找不到属主节点，导致状态与产物不一致。
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
        """当前结果引用的全部 artifact 引用（保存检查点时用来固定它们）。"""
        with self.lock:
            return {reference for ports in self.node_results.values() for reference in ports.values()}

    def get_node_result(self, node_id: str, limit: int = 20, include_indices: bool = False) -> dict[str, Any]:
        """返回某节点的状态、各端口产物的摘要与错误信息（产物本体不外传）。"""
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
        """本 workspace 的缓存统计（见 :meth:`MemoryArtifactStore.stats`）。"""
        return self.artifacts.stats()

    def get_summary(self) -> dict[str, Any]:
        """方案级别的摘要：状态、版本、各节点状态、警告与错误（不含产物）。"""
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
        """快照 = 元数据 + artifact **引用**；载荷留在存储里，由调用方负责 pin。"""
        with self.lock:
            # lock 与 artifacts 不能进快照：前者不可复制，后者由 WorkspaceManager 管理。
            state = {k: v for k, v in self.__dict__.items() if k not in {"lock", "artifacts"}}
            return deepcopy(state)

    def restore(self, snapshot: dict[str, Any]) -> None:
        """从快照恢复元数据（恢复后记得调 ``prune_missing_artifacts`` 清理已释放的引用）。"""
        with self.lock:
            self.__dict__.update(deepcopy(snapshot))
            self.touch()

    def prune_missing_artifacts(self) -> list[str]:
        """剔除载荷已经不存在的引用（例如恢复的是"已被重置过的 workspace"的检查点）。

        完全失去产物的节点回到 PENDING，并追加一条"检查点输出已释放"的警告，
        让使用者知道必须重算，而不是拿到空结果。
        """
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
    """一次检查点：图 + workspace 快照 + 被固定住的产物引用。

    真正的数据不复制：检查点"固定"引用，使这些产物不会被 LRU 淘汰；
    因此保存检查点的成本只是元数据（这也是它能随手保存的原因）。
    """

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
        """对外的检查点摘要：不含图、快照与引用（那三者体积大且不适合直接返回）。"""
        return {k: v for k, v in self.__dict__.items() if k not in {"graph", "snapshot", "artifact_refs"}}


class WorkspaceManager:
    def __init__(self, artifact_cache_bytes: int | None = None, spill_root: str | Path | None = None) -> None:
        """管理所有 workspace 与检查点；``spill_root`` 为空表示不落盘（只淘汰）。"""
        self.workspaces: dict[str, FaultWorkspace] = {}
        self.checkpoints: dict[str, Checkpoint] = {}
        self.artifact_cache_bytes = artifact_cache_bytes
        self.spill_root = Path(spill_root).resolve() if spill_root else None

    def create_workspace(self, pipeline_id: str) -> FaultWorkspace:
        """新建一个 workspace，并给它分配独立的溢写子目录。"""
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
        """每个 workspace 一个溢写目录；未配置预算或未配置目录时返回 None（即只淘汰）。"""
        if self.spill_root is None or self.artifact_cache_bytes is None:
            return None
        directory = self.spill_root / (workspace_id or f"ws_{uuid4().hex[:12]}")
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def get_workspace(self, workspace_id: str, pipeline_id: str | None = None) -> FaultWorkspace:
        """按 id 取 workspace；给了 ``pipeline_id`` 时同时校验归属，防止读错方案的结果。"""
        if workspace_id not in self.workspaces:
            raise ValueError(f"Unknown workspace: {workspace_id}")
        ws = self.workspaces[workspace_id]
        if pipeline_id and ws.pipeline_id != pipeline_id:
            raise ValueError("Workspace belongs to another pipeline")
        return ws

    def delete_workspace(self, workspace_id: str) -> None:
        """删除 workspace：释放产物、清理溢写目录，并顺带删掉指向它的检查点。"""
        ws = self.get_workspace(workspace_id)
        if ws.status == PipelineStatus.RUNNING:
            # 运行中删除会让执行线程访问到已释放的对象，因此拒绝。
            raise ValueError("Cannot delete a running workspace")
        ws.cleanup()
        self._remove_spill_dir(workspace_id)
        del self.workspaces[workspace_id]
        self.checkpoints = {k: cp for k, cp in self.checkpoints.items() if cp.workspace_id != workspace_id}

    def reset_workspace(self, workspace_id: str) -> FaultWorkspace:
        """重置 workspace：清空所有产物与状态，但保留同一个 workspace id。"""
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
        """删除某个 workspace 的溢写目录（连同目录内的文件）。"""
        if self.spill_root is None:
            return
        directory = (self.spill_root / workspace_id).resolve()
        # 防御性检查：只允许删除溢写根目录之下的路径。
        if not directory.is_relative_to(self.spill_root) or not directory.exists():
            return
        for path in directory.glob("*"):
            path.unlink(missing_ok=True)
        directory.rmdir()

    def cleanup(self) -> None:
        """释放全部 workspace（服务关闭时调用）。"""
        for workspace in self.workspaces.values():
            workspace.cleanup()

    def delete_by_pipeline(self, pipeline_id: str) -> int:
        """删除某个方案的全部 workspace 与其检查点，返回删除的 workspace 数量。

        这是 MCP ``delete_pipeline`` 的落点：失败尝试留下的内存与磁盘占用会被彻底释放。
        """
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
        """全部 workspace 的缓存汇总（服务信息接口返回它）。"""
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
        """保存检查点：快照元数据并**固定**当前的全部产物引用。

        运行中不允许保存（结果可能过期）；被固定的产物不会被缓存预算淘汰，
        因此检查点的有效期与进程一致。
        """
        ws = self.get_workspace(workspace_id, graph["id"])
        if ws.status == PipelineStatus.RUNNING:
            raise ValueError("Wait for a node-boundary-complete run before saving a checkpoint")
        # 载荷按引用共享并被 pin，因此保存检查点的成本只有元数据。
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
        """恢复检查点：把 workspace 元数据还原成当时的状态，并返回当时的图。

        恢复后立即做一次 :meth:`FaultWorkspace.prune_missing_artifacts`：
        若某些产物已经不在存储里（例如 workspace 被重置过），相关节点回到 PENDING 并给出警告。
        """
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
        """删除检查点，并解除对产物的固定（释放后可能立刻被预算淘汰）。"""
        checkpoint = self.checkpoints.pop(checkpoint_id, None)
        if checkpoint is None:
            raise ValueError("Unknown checkpoint")
        workspace = self.workspaces.get(checkpoint.workspace_id)
        if workspace is not None:
            workspace.artifacts.unpin(checkpoint.artifact_refs)
            workspace.artifacts.enforce_budget()
