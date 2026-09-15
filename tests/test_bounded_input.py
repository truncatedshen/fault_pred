"""P1 bounded input: column projection, row limits, Parquet and honest warnings."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from fault_platform.runtime import ExecutionContext
from fault_platform.workspace import FaultWorkspace


@pytest.fixture
def wide_csv(tmp_path):
    frame = pd.DataFrame(
        {
            **{f"c{index}": np.arange(500.0) + index for index in range(6)},
            "equipment": np.arange(500) // 50,
            "label": (np.arange(500) // 50) % 2,
        }
    )
    frame.to_csv(tmp_path / "wide.csv", index=False)
    return frame


def test_csv_projection_and_row_limit(registry, tmp_path, wide_csv):
    context = ExecutionContext(FaultWorkspace("p"), tmp_path)
    component = registry.create(
        "data.input",
        parameters={"path": "wide.csv", "columns": ["c0", "label"], "max_rows": 120},
    )
    component.validate()
    component.preflight(context)
    result = component.execute({}, context)
    frame = result.outputs["dataset"]
    assert list(frame.columns) == ["c0", "label"] and len(frame) == 120
    assert frame.attrs["source_projection"]["max_rows"] == 120
    assert any("limited" in warning for warning in result.warnings)
    assert any("limited" in warning for warning in frame.attrs["evaluation_warnings"])
    assert frame.attrs["source_id"]  # content fingerprint still recorded


def test_unlimited_read_has_no_warning(registry, tmp_path, wide_csv):
    context = ExecutionContext(FaultWorkspace("p"), tmp_path)
    component = registry.create("data.input", parameters={"path": "wide.csv"})
    result = component.execute({}, context)
    assert len(result.outputs["dataset"]) == 500 and not result.warnings
    assert "evaluation_warnings" not in result.outputs["dataset"].attrs


def test_parquet_projection_filter_and_limit(registry, tmp_path):
    pytest.importorskip("pyarrow")
    frame = pd.DataFrame(
        {
            "equipment": np.repeat(np.arange(20), 50),
            "signal": np.random.default_rng(0).normal(size=1000),
            "noise": np.random.default_rng(1).normal(size=1000),
        }
    )
    frame.to_parquet(tmp_path / "data.parquet", index=False)
    context = ExecutionContext(FaultWorkspace("p"), tmp_path)
    component = registry.create(
        "data.input",
        parameters={
            "path": "data.parquet",
            "columns": ["signal"],
            "filters": [["equipment", ">=", 10]],
            "max_rows": 100,
        },
    )
    component.validate()
    component.preflight(context)
    result = component.execute({}, context)
    dataset = result.outputs["dataset"]
    assert list(dataset.columns) == ["signal"] and len(dataset) == 100
    assert result.warnings


def test_unsupported_suffix_is_rejected(registry, tmp_path):
    (tmp_path / "notes.txt").write_text("hello", encoding="utf-8")
    context = ExecutionContext(FaultWorkspace("p"), tmp_path)
    component = registry.create("data.input", parameters={"path": "notes.txt"})
    with pytest.raises(ValueError, match="Unsupported data source"):
        component.preflight(context)


def test_parquet_without_pyarrow_reports_install_hint(registry, tmp_path, monkeypatch):
    (tmp_path / "data.parquet").write_bytes(b"not really parquet")
    context = ExecutionContext(FaultWorkspace("p"), tmp_path)
    component = registry.create("data.input", parameters={"path": "data.parquet"})
    if __import__("importlib").util.find_spec("pyarrow"):  # pragma: no cover - depends on extras
        pytest.skip("pyarrow is installed; the hint is only shown without it")
    with pytest.raises(ValueError, match="pyarrow"):
        component.preflight(context)
