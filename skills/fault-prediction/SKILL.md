---
name: fault-prediction
description: Build, run and revise fault prediction component graphs on the Fault Prediction Component Platform through its MCP tools — workspace and data preparation, quality pre-checks, aligned window features, and comparable holdout validation.
---

# Fault Prediction Component Platform — Agent guide

The HTTP service must be running before the MCP bridge; both edit the same graph as the visual designer. Work in this order: **workspace → quality → windows/labels → features → validation → inspect → persist**. Skipping the first two steps is what breaks real datasets.

## 0. Work efficiently (token discipline)

- Assemble in bulk: `add_components` (list of `{component_type, node_id, parameters, position}`) then `connect_many` (list of `{source_node, source_port, target_node, target_port}`), and `configure_components` for later tweaks. A 9-node graph is 3–4 calls, not 26.
- Every edit accepts `include_graph=false`. Bulk operations already default to it: you get `{pipeline_id, version, node_count, edge_count, added}` instead of the whole graph. Call `get_pipeline` once when you actually need to reason about structure.
- `get_node_result` is **compact by default**: long arrays such as `train_indices`/`test_indices` collapse into `*_count`. Pass `include_indices=true` only when you genuinely need the rows.
- `execute_pipeline` is asynchronous; call `wait_for_pipeline(pipeline_id, timeout_seconds=...)` instead of sleeping and polling. It returns the status summary plus `timed_out`.
- `delete_pipeline` removes a pipeline, its workspaces, spilled files and checkpoints — use it after a failed attempt so the server does not accumulate state.
- `get_server_info` answers "what is this server configured to do"; `list_datasets` lists readable files. If your client shows duplicated tool names (`list_pipelines_1`, `save_pipeline_2`) that is a client-side discovery alias — use the unsuffixed name.

## 1. Workspace and data preparation (do this first)

- `get_server_info` reports `data_root`, `storage_root`, the artifact cache budget and how many data files exist. `list_datasets` lists them with sizes.
- `data.input.path` is **relative to `data_root`**; absolute paths and `..` escapes are rejected. Put your file in `data_root` (the web UI's upload button writes to `data_root/uploads/`).
- Changing `data_root` (or the cache budget) means restarting the service with `--data-root ...`; there is no tool that moves it at runtime. Ask the operator instead of guessing.
- Supported formats are **CSV and Parquet** (`.csv`, `.parquet`, `.pq`). Everything else must be converted before the platform sees it:

| Source | Convert with |
| --- | --- |
| `.mat` (CWRU, SEU) | `scipy.io.loadmat` → DataFrame → `to_csv` / `to_parquet` |
| `.txt` (C-MAPSS) | `pandas.read_csv(sep=r"\s+", header=None)` → `to_parquet` |
| MDF/BLF (vehicle logs) | `mdfRead` / `blfread` in MATLAB, or `asammdf` / `python-can` in Python |
| Database / historian | export a columnar slice to Parquet |

  When you convert, keep one row per sample with `asset_id` / `instance_id`, `time`, the measurement columns and the label. State the conversion in your answer.
- Large inputs: `columns` (projection) + `max_rows` bound a CSV read; Parquet adds `filters` (predicate pushdown). Above roughly 1 GB use `streaming=true` with `chunk_rows` — see §6.

## 2. Quality pre-check (before any feature work)

Run `data.quality` on the raw frame before features. It reports what a missing-rate column cannot show:

| Finding | Why it matters |
| --- | --- |
| all-NaN / all-zero / constant columns | zero-variance windows, NaN spectra, useless features |
| **per-group constant columns** | held or quantised tags: constant inside one asset while varying across assets — invisible in `visual.overview` |
| flat-window ratio per column | a channel that carries no spectrum; `feature.spectral` returns NaN there by default |
| duplicate rows | duplicated samples bias validation |
| windows with mixed labels, and label-change count | tells you whether `label_policy=strict` can survive |

Always run it with the same `group_column`, `label_column`, `time_column`, `window_size`, `step` you will use for features. Report its findings before modelling; drop the dead channels. `missing_rate = 0` does not mean a column is usable.

## 3. Windows, groups and labels

- Window components (`feature.statistical`, `feature.fitting`, `feature.spectral`) and `data.quality` must share identical `columns` (measurements only), `group_column`, `time_column`, `window_size`, `step`; `feature.merge` requires that provenance to match exactly.
- `group_column` is the **window group** (an instance/run). Keep the asset id as a normal column — see §5.
- Set `label_column` on the window component and connect its `labels` output to every validator: row-level labels are not aligned with aggregated windows.
- `label_policy`:

| Policy | Meaning | Use when |
| --- | --- | --- |
| `strict` (default) | refuses any window whose label changes **inside** it — the whole run fails | labels are constant per window by construction |
| `mode` | majority label of the window | **onset/degradation data** — the usual real case |
| `last` | label at the end of the window | you deliberately predict the state at the window end |

  Onset data almost always needs `mode` (or `last`). Say which policy you used and why; `strict` failing on a window that straddles a fault start is expected behaviour, not a bug.

## 4. Feature engineering

- Start with `feature.statistical`; add `feature.fitting` for trend/degradation, `feature.spectral` only when the signal really is a waveform with a **known** sampling rate, and `feature.categorical` for discrete attributes. The categorical component fits and emits an `encoder`; reuse it through `feature.categorical_transform` for inference instead of fitting on new data. Its fixed output schema handles unseen values according to `handle_unknown`, and validators embed upstream categorical encoders in the trained model.
- Spectral features need variation: `flat_policy=nan` (default) keeps row alignment and reports NaN where a window is constant; `skip` drops such windows (only safe if no other branch must merge); `error` restores the hard failure. NaN features reach the model as missing values — say so in the report.
- `feature.select` converts existing columns to `FeatureDataset`; `data.labels` extracts row-aligned labels.
- `feature.score_select` and `feature.pca` are **fitted on every row**, so they are exploratory: their scores are not independent of the holdout split. Two acceptable uses: (a) put them on an inspection branch to understand the data, or (b) keep them before the model and report the leakage warning as exploratory. Never present their ranking as validated evidence.
- Exploration outputs (`visual.overview`, `explore.*`, `visual.*`) are terminal branches, not model inputs. Full-data scaling/encoding belongs on an exploration branch; the SVM validator fits scaling and calibration inside training only.

## 5. Validation and splits

- `split_method=group` splits by `group_column`, i.e. by **instance**. That is not automatically an asset-level holdout: when one asset contributes several instances, windows from a held-out asset can still appear in training.
  - For asset-level holdout, either aggregate/one-row-per-asset before windowing, or filter to one instance per asset, or group by an asset column that you added to the features.
  - After validating, check coverage: `metrics["train_indices"]`/`test_indices` (or their counts) let you map windows back to assets and state how many test assets were unseen in training.
- Overlapping windows cannot use a random split — use `group` or `temporal`. `temporal` splits in feature-row order and purges training windows that share raw rows with the test windows; the order must express the intended chronology.
- Compare Random Forest, SVM and optionally XGBoost with identical features, labels, split method, test size and random state; `validation.compare` refuses mismatched test indices.
- Report holdout size, split method, label policy and every warning with the metrics. Synthetic-data scores demonstrate the implementation, not industrial performance.

## 6. Large inputs

- Bounded read first: `columns` + `max_rows`, or Parquet with `filters`.
- Above ~1 GB: `data.input.streaming=true` (+ `chunk_rows`) keeps rows on disk. Rows must be grouped **contiguously** (sorted by `group_column`) and ordered by time inside each group; window features and `visual.overview` stream, everything else fails with a hint to insert `data.materialize` (which restores full-memory behaviour, with a warning).
- A single huge group (e.g. one asset with millions of rows) is still buffered whole — split it into instances or reduce the window.
- The artifact cache budget may evict results: those nodes return to `PENDING` with a warning and the pipeline drops to `READY`. Re-run instead of assuming old results exist.

## 7. Execute, inspect, iterate, persist

- `create_pipeline` → `add_components` → `connect_many` → `validate_pipeline` → `execute_pipeline` → `wait_for_pipeline`.
- On failure read the node's `error` (type, message, stack, parameter snapshot, input preview) and `get_history`; fix with `configure_components` then `retry_node` / `execute_from_node`, which reuse valid upstream outputs.
- Inspect with compact `get_node_result`; use `include_indices=true` sparingly. Warnings carry leakage, eviction, flat-window and subset notices — surface them.
- Persist with `save_pipeline` / `get_pipeline_xml`; `save_checkpoint` snapshots graph + workspace (in memory; it ends with the process).
- Clean up with `delete_pipeline`; state the pipeline id and XML path in your report.

## 8. When to leave MCP

MCP is for assembling, executing, persisting and inspecting. For bulk metric extraction, cross-run comparisons or offline analysis, use the platform's own entry points instead of pulling large payloads through tools:

- `python -m fault_platform run examples/example_pipeline.xml --data-root <dir>` prints a JSON summary without UI or MCP.
- `python scripts/mcp_smoke.py --from-config` and `python scripts/verify_deploy.py --from-config` are ready-made end-to-end checks.
- The Python API (`fault_platform.graph` / `runtime` / `workspace`) runs a graph in-process; `fault_core` alone is enough for feature/quality analysis.
- A human can open the same graph at http://127.0.0.1:8765 — changes made from MCP appear there live.
