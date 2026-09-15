"""Asset-level holdout: derive assets, carry them through windows, hold them out for real."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from fault_core import assets as asset_tools
from fault_core import features, models
from fault_core.preprocessing import impute_features
from fault_platform.api import create_app
from fault_platform.service import PipelineService


def plant_frame(assets: int = 4, instances_per_asset: int = 3, rows: int = 64) -> pd.DataFrame:
    """Synthetic fleet: each asset owns several instances, labels differ per asset."""
    rng = np.random.default_rng(7)
    records = []
    for asset in range(assets):
        for run in range(instances_per_asset):
            label = (asset + run) % 2
            total = rows
            records.append(
                pd.DataFrame(
                    {
                        "instance": f"WELL-{asset:05d}_{run}",
                        "asset": f"WELL-{asset:05d}",
                        "time_s": np.arange(total),
                        "fault": label,
                        "sensor": rng.normal(2 + label * 3 + asset, 0.4, total),
                        "temperature": rng.normal(30 + label * 5, 1.5, total),
                        "dead": np.zeros(total),
                        "held": np.repeat(np.arange(asset + 1, asset + 2), total),
                    }
                )
            )
    return pd.concat(records, ignore_index=True)


def test_asset_key_from_instance_key():
    frame = plant_frame(assets=2, instances_per_asset=1)
    derived = asset_tools.asset_key(frame.drop(columns=["asset"]), column="instance", target="asset")
    assert set(derived["asset"]) == {"WELL-00000", "WELL-00001"}
    regex = asset_tools.asset_key(
        frame.drop(columns=["asset"]), column="instance", target="well", mode="regex", pattern=r"^(WELL-\d+)"
    )
    assert set(regex["well"]) == {"WELL-00000", "WELL-00001"}
    with pytest.raises(ValueError, match="already exists"):
        asset_tools.asset_key(frame, column="instance", target="asset")
    with pytest.raises(ValueError, match="Missing source column"):
        asset_tools.asset_key(frame, column="nope")
    with pytest.raises(ValueError, match="zero or positive"):
        asset_tools.asset_key(frame, column="instance", target="x", index=-1)


def test_window_features_carry_assets():
    frame = plant_frame()
    kwargs = {
        "columns": ["sensor", "temperature"],
        "group_column": "instance",
        "asset_column": "asset",
        "label_column": "fault",
        "time_column": "time_s",
        "window_size": 16,
    }
    statistical = features.extract_features(frame, **kwargs)
    assert len(statistical["features"].attrs["assets"]) == len(statistical["features"])
    assert statistical["labels"].attrs["assets"] == statistical["features"].attrs["assets"]
    spectral = features.spectral(frame, **dict(kwargs, sampling_rate=64.0, columns=["sensor"]))
    assert len(spectral["features"].attrs["assets"]) == len(spectral["features"])
    streamed = features.extract_features_stream([frame.iloc[:200], frame.iloc[200:]], **kwargs)
    assert streamed["features"].attrs["assets"] == statistical["features"].attrs["assets"]
    with pytest.raises(ValueError, match="Missing asset column"):
        features.extract_features(frame, **{**kwargs, "asset_column": "missing"})


def test_asset_split_holds_out_whole_assets_while_group_split_does_not():
    frame = plant_frame(assets=4, instances_per_asset=3)
    extracted = features.extract_features(
        frame,
        ["sensor", "temperature"],
        group_column="instance",
        asset_column="asset",
        label_column="fault",
        window_size=16,
    )
    by_instance = models.validate_model(**extracted, split_method="group", n_estimators=20)["metrics"]
    by_asset = models.validate_model(**extracted, split_method="asset", n_estimators=20)["metrics"]

    instance_assets = set(extracted["features"].attrs["assets"])
    # The instance split reports both levels, so the coupling is visible instead of hidden.
    assert by_instance["coverage"]["train_instances"] + by_instance["coverage"]["test_instances"] == 12
    assert by_instance["coverage"]["test_assets"] >= 1
    # The asset split answers the real question: no test asset was seen during training.
    assert by_asset["coverage"]["test_assets_unseen"] == by_asset["coverage"]["test_assets"]
    assert by_asset["coverage"]["test_instances_unseen"] >= 1
    assert by_asset["coverage"]["train_assets"] + by_asset["coverage"]["test_assets"] == len(instance_assets)
    assert by_asset["coverage"]["train_assets"] >= 1


def test_asset_split_needs_asset_column_and_two_assets():
    frame = plant_frame(assets=2, instances_per_asset=2)
    without_assets = features.extract_features(
        frame, ["sensor"], group_column="instance", label_column="fault", window_size=16
    )
    with pytest.raises(ValueError, match="Asset split requires asset_column"):
        models.validate_model(**without_assets, split_method="asset")
    single_asset = frame.assign(asset="only")
    extracted = features.extract_features(
        single_asset,
        ["sensor"],
        group_column="instance",
        asset_column="asset",
        label_column="fault",
        window_size=16,
    )
    with pytest.raises(ValueError, match="at least two assets"):
        models.validate_model(**extracted, split_method="asset")


def test_feature_imputation_handles_flat_window_nans():
    frame = plant_frame(assets=2, instances_per_asset=2)
    # Half the instances hold the channel steady, so only some windows lose their spectrum.
    frame["held"] = np.where(frame["instance"].str.endswith("_0"), 1.0, np.arange(len(frame)) % 5.0)
    extracted = features.spectral(
        frame,
        columns=["sensor", "held"],
        sampling_rate=64.0,
        group_column="instance",
        asset_column="asset",
        label_column="fault",
        window_size=16,
        features=["dominant_frequency", "spectral_rms"],
    )
    assert extracted["features"].isna().any().any()  # some windows have no spectrum
    with pytest.raises(ValueError, match="feature.imputation"):
        models.validate_model(**extracted, split_method="asset")

    filled, notes = impute_features(extracted["features"], method="mean")
    assert not filled.isna().any().any() and notes
    metrics = models.validate_model(filled, extracted["labels"], split_method="asset", n_estimators=20)[
        "metrics"
    ]
    assert 0 <= metrics["accuracy"] <= 1

    zeroed, _ = impute_features(extracted["features"], method="zero")
    assert not zeroed.isna().any().any()
    all_flat = frame.assign(dead=0.0)
    dead = features.spectral(
        all_flat,
        columns=["dead"],
        sampling_rate=64.0,
        group_column="instance",
        asset_column="asset",
        label_column="fault",
        window_size=16,
        features=["spectral_rms"],
    )["features"]
    assert dead.isna().all().all()
    with pytest.raises(ValueError, match="entirely NaN"):
        impute_features(dead, method="mean")
    dropped, notes = impute_features(dead, method="drop_columns")
    assert dropped.empty or not dropped.isna().any().any()


def test_metric_semantics_use_original_labels_and_named_positive_class():
    frame = plant_frame(assets=6, instances_per_asset=2)
    extracted = features.extract_features(
        frame,
        ["sensor", "temperature"],
        group_column="instance",
        asset_column="asset",
        label_column="fault",
        window_size=16,
    )
    metrics = models.validate_model(**extracted, split_method="asset", n_estimators=20, positive_class="1")[
        "metrics"
    ]
    assert set(metrics["test_class_counts"]) == {"0", "1"}
    assert set(metrics["per_class_recall"]) == {"0", "1"}
    assert metrics["positive_class"] == "1"
    assert 0.0 <= metrics["miss_rate"] <= 1.0
    assert 0.0 <= metrics["balanced_accuracy"] <= 1.0
    with pytest.raises(ValueError, match="positive_class"):
        models.validate_model(**extracted, split_method="asset", n_estimators=20, positive_class="9")


def test_api_lists_and_accepts_parquet(tmp_path):
    service = PipelineService(tmp_path / "data", tmp_path / "saved")
    try:
        plant_frame(assets=1, instances_per_asset=1, rows=32).to_parquet(
            service.data_root / "fleet.parquet", index=False
        )
        with TestClient(create_app(service=service)) as client:
            listed = client.get("/api/data").json()["datasets"]
            assert [item["path"] for item in listed] == ["fleet.parquet"]
            payload = (service.data_root / "fleet.parquet").read_bytes()
            uploaded = client.post(
                "/api/data/upload", files={"file": ("fleet.parquet", payload, "application/octet-stream")}
            ).json()
            assert uploaded["success"] and uploaded["path"].endswith(".parquet")
            assert "instance" in uploaded["columns"]
            rejected = client.post("/api/data/upload", files={"file": ("notes.txt", b"hello", "text/plain")})
            assert rejected.status_code == 400
    finally:
        service.close()
