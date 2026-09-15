"""Repeatable memory benchmark for large inputs and the artifact cache.

It writes a synthetic CSV, then runs the same three-node pipeline in three modes and
reports the peak working set of each child process:

* ``full``    - read every row and column
* ``bounded`` - read a column projection with a row limit
* ``stream``  - keep rows on disk and extract features chunk by chunk

    .\\.venv\\Scripts\\python.exe scripts\\memory_bench.py --sizes 250000,1000000,2000000
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path

import numpy as np
import pandas as pd

CHILD = textwrap.dedent(
    """
    import ctypes, ctypes.wintypes as wt, sys, time
    from pathlib import Path

    kernel32 = ctypes.WinDLL("kernel32"); psapi = ctypes.WinDLL("psapi")

    class PMC(ctypes.Structure):
        _fields_ = [("cb", wt.DWORD), ("PageFaultCount", wt.DWORD), ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t), ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t), ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t), ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t)]

    kernel32.GetCurrentProcess.restype = wt.HANDLE
    psapi.GetProcessMemoryInfo.argtypes = [wt.HANDLE, ctypes.POINTER(PMC), wt.DWORD]

    def peak_mb():
        counters = PMC(); counters.cb = ctypes.sizeof(counters)
        psapi.GetProcessMemoryInfo(kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb)
        return counters.PeakWorkingSetSize / 1048576

    from fault_platform.graph import ComponentGraph
    from fault_platform.registry import default_registry
    from fault_platform.runtime import ExecutionContext, ExecutionEngine
    from fault_platform.workspace import FaultWorkspace

    root, mode, budget_mb, limit, chunk = (Path(sys.argv[1]), sys.argv[2], int(sys.argv[3]),
                                           int(sys.argv[4]), int(sys.argv[5]))
    parameters = {"path": "big.csv"}
    if mode == "bounded":
        parameters.update({"columns": ["s0", "s1", "s2", "equipment", "label"], "max_rows": limit})
    if mode == "stream":
        parameters.update({"streaming": True, "chunk_rows": chunk})

    graph = ComponentGraph(default_registry(), mode, "memory_bench")
    graph.add_node("data.input", "source", parameters)
    graph.add_node("feature.statistical", "stats", {
        "columns": ["s0", "s1", "s2"], "group_column": "equipment", "time_column": "time",
        "label_column": "label", "window_size": 64})
    graph.add_node("validation.random_forest", "model", {"n_estimators": 20, "split_method": "group"})
    graph.connect("source", "dataset", "stats", "dataset")
    graph.connect("stats", "features", "model", "features")
    graph.connect("stats", "labels", "model", "labels")

    workspace = FaultWorkspace(graph.pipeline_id, artifact_cache_bytes=budget_mb * 1024 * 1024,
                               spill_dir=root / f"spill-{mode}")
    started = time.perf_counter()
    ExecutionEngine().execute(graph, ExecutionContext(workspace, root))
    elapsed = time.perf_counter() - started
    stats = workspace.artifacts.stats()
    source = workspace.get_output("source", "dataset")
    rows = source.total_rows if hasattr(source, "total_rows") else len(source)
    metrics = workspace.get_output("model", "metrics") if workspace.node_status.get("model") == "SUCCESS" else {}
    print(
        f"PEAK={peak_mb():.0f}MB status={workspace.status} rows={rows} "
        f"spills={stats['spills']} disk={stats['disk_bytes']/1048576:.0f}MB "
        f"ram={stats['bytes']/1048576:.0f}MB evictions={stats['evictions']} "
        f"accuracy={metrics.get('accuracy')} seconds={elapsed:.1f}"
    )
"""
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Memory benchmark for large inputs")
    parser.add_argument("--rows", type=int, default=None, help="single input size (shorthand for --sizes)")
    parser.add_argument("--sizes", default="250000,1000000,2000000", help="comma separated row counts")
    parser.add_argument("--columns", type=int, default=20)
    parser.add_argument("--limit", type=int, default=200_000, help="row limit used by the bounded mode")
    parser.add_argument("--budget-mb", type=int, default=32, help="artifact cache budget")
    parser.add_argument("--chunk-rows", type=int, default=100_000, help="rows per streamed chunk")
    parser.add_argument("--keep", action="store_true", help="keep the generated files")
    arguments = parser.parse_args()

    root = Path(tempfile.mkdtemp(prefix="fault-memory-bench-"))
    helper = root / "child.py"
    helper.write_text(CHILD, encoding="utf-8")
    sizes = [arguments.rows] if arguments.rows else [int(v) for v in arguments.sizes.split(",") if v]
    for rows in sizes:
        rng = np.random.default_rng(0)
        frame = pd.DataFrame(
            rng.normal(size=(rows, arguments.columns)).astype("float32"),
            columns=[f"s{index}" for index in range(arguments.columns)],
        )
        frame["equipment"] = np.arange(rows) // 64
        frame["time"] = np.arange(rows) % 64
        frame["label"] = (np.arange(rows) // 64) % 3
        csv = root / f"big-{rows}.csv"
        started = time.perf_counter()
        frame.to_csv(csv, index=False, float_format="%.4f")
        print(
            f"\n=== {rows:,} rows | csv {csv.stat().st_size / 1048576:.0f} MB | "
            f"in-memory {frame.memory_usage(deep=True).sum() / 1048576:.0f} MB | "
            f"written in {time.perf_counter() - started:.0f}s ==="
        )
        del frame
        (root / "big.csv").unlink(missing_ok=True)
        csv.replace(root / "big.csv")
        for mode in ("full", "stream"):
            started = time.perf_counter()
            completed = subprocess.run(
                [
                    sys.executable,
                    str(helper),
                    str(root),
                    mode,
                    str(arguments.budget_mb),
                    str(arguments.limit),
                    str(arguments.chunk_rows),
                ],
                capture_output=True,
                text=True,
            )
            output = completed.stdout.strip() or completed.stderr.strip()[-400:]
            print(f"{mode:>8} (wall {time.perf_counter() - started:>3.0f}s): {output}")
    if arguments.keep:
        print("kept:", root)


if __name__ == "__main__":
    main()
