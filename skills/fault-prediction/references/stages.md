# Stage handbook (the detail behind SKILL.md)

`SKILL.md` keeps the decisions, the invariants and the stage gates. This file keeps the
parameter tables, the worked numbers and the "before moving on" checklists behind each stage.
Read only the section for the stage you are in; a smoke test needs none of it.

## Stage 1 — Workspace and data preparation (§2)

### Formats the platform accepts

Only `.csv`, `.parquet` and `.pq`. Everything else must be converted **before** the platform
sees it, and you must say that you converted it:

| Source | Convert with |
| --- | --- |
| `.mat` (CWRU, SEU) | `scipy.io.loadmat` → DataFrame → `to_parquet` |
| `.txt` (C-MAPSS) | `pandas.read_csv(sep=r"\s+", header=None)` → `to_parquet` |
| MDF/BLF (vehicle logs) | MATLAB `mdfRead`/`blfread`, or Python `asammdf`/`python-can` |
| historian / database | export a columnar slice to Parquet |

While converting, aim for **one row per sample**: `asset_id`, `instance_id`, `time`, the
measurement columns and the label. Windowing assumes that shape.

### Checklist

 - [ ] `data_root` known; the file is visible in `list_datasets` or explicitly requested from the operator
 - [ ] instance / asset / time / label / measurement columns identified (asked, or inferred and the inference stated)
 - [ ] `data.input.path` is relative and parses (run `data.input` alone if unsure)
 - [ ] file above ~1 GB? choose a bounded read (`columns`, `max_rows`, Parquet `filters`) or `streaming`

### Watch out for

 - Guessing the path. Every wrong path is a wasted run; `list_datasets` costs one call.
 - Assuming the file needs no conversion. `.mat`/`.txt`/MDF are not accepted, and a
   half-converted file (a `.mat` renamed to `.csv`) fails later with confusing dtypes.
 - Confusing an instance with an asset. Record both columns now, while the raw data is in
   front of you.

## Stage 2 — Quality pre-check (§3)

### What `data.quality` reports

| Finding | Meaning | Action |
| --- | --- | --- |
| `all_nan_columns` | a column with no data | drop it from `columns` |
| `constant_columns` | constant overall | drop it — zero variance, zero information |
| `groups_with_constant_columns` | constant *inside* an asset while varying across assets | the classic held/quantised point; dropping it is usually right, keeping it is a decision you must state |
| duplicate rows | repeated samples | investigate before trusting metrics — duplicates leak across splits |
| mixed-label windows, label transitions | windows whose label changes inside | tells you whether `label_policy=strict` can survive |
| flat-window ratio per column | fraction of windows with no variation | `feature.spectral` yields NaN there by default; keep the ratio in your report |

Run it with **the same** `columns`, `group_column`, `time_column`, `window_size` and `step` you
will use for window features, so its counts describe the real thing.

### Worked example

On a 3W-style industrial dataset `P-TPT` was 19.4 % flat and `T-TPT` 7.8 % flat **with
`missing_rate = 0`**. `visual.overview` shows nothing; `data.quality` shows it immediately.

### Checklist

 - [ ] `data.quality` ran with the final window parameters
 - [ ] dead/constant/flat columns are dropped, or kept with a stated reason
 - [ ] the intended label policy is consistent with the mixed-label count
 - [ ] the user was told what the pre-check found

### Watch out for

 - `missing_rate = 0` does not mean "usable".
 - Global statistics hide per-group constants: a column can be 0 % missing, non-constant
   overall, and constant inside every well.
 - Skipping this stage does not fail the run; it fails the *conclusion*.

## Stage 3 — Windows, groups and labels (§4)

The window producers are `feature.statistical`, `feature.fitting`, `feature.spectral` and
`feature.entropy`. They share these parameters:

| Parameter | Meaning | Notes |
| --- | --- | --- |
| `columns` | measurement columns | **required**; measurements only — never the id/time/label columns |
| `group_column` | window group (instance or run) | windows never straddle two groups; without it, `window_size=0` means "the whole table" |
| `label_column` | emits labels for the windows | connect that `labels` output to every validator |
| `time_column` | ordering | keep it set whenever rows are time ordered |
| `asset_column` | owning asset (well, machine) | required for `split_method=asset` |
| `window_size` | rows per window | `0` = the entire group (one row out) |
| `step` | stride | `0` = non-overlapping; `step < window_size` = overlapping windows |
| `label_policy` | `strict` (default) / `last` / `mode` | see below |

### `label_policy`

| Policy | Behaviour | Use when |
| --- | --- | --- |
| `strict` | a window whose label changes **inside** it fails the whole run | labels are constant per window by construction |
| `mode` | majority label of the window | onset/degradation data — the normal real case |
| `last` | label at the end of the window | you deliberately predict the state at the window end |

If labels change over time (nearly every real fault dataset), `strict` **will** fail — that is
correct behaviour, not a bug, and it happens exactly at the fault onset, which is usually the
most interesting place in the data. Switch to `mode` (or `last`) and say which you chose.

### Checklist

 - [ ] every window component (and `data.quality`) shares identical `columns`, `group_column`, `time_column`, `window_size`, `step`
 - [ ] every validator is wired to both `features` **and** `labels` from the same window component
 - [ ] `window_size`/`step` chosen against the real sampling rate, not copied from an example
 - [ ] label policy stated with its reason

### Watch out for

 - Row-level labels are not window labels. `data.labels` returns one label per raw row; feeding
   it to a model alongside windowed features mismatches lengths and fails.
 - `window_size=0` silently collapses each group to a single row — right for whole-run
   classification, wrong for onset detection.
 - Fan-out from one window component to several consumers is fine; joining two window
   components back together requires `feature.merge` and matching provenance.

## Stage 4 — Features (§5)

### Which branch answers which question

| Question about the signal | Branch |
| --- | --- |
| level and shape (mean, std, variance, RMS, skewness, kurtosis, quantiles, range, IQR, MAD, peak, crest factor) | `feature.statistical` |
| trend / degradation rate | `feature.fitting` (`linear`, `polynomial` + `degree`, `exponential`) |
| rotation, resonance, periodicity | `feature.spectral` — needs `sampling_rate` and real variation |
| complexity and irregularity | `feature.entropy` (`approximate_entropy`, `information_entropy`) |
| short-term dynamics without windowing | `feature.temporal` (differences, rolling autocorrelation) |
| rolling summary kept row-aligned | `feature.rolling_statistics` |
| discrete attributes (category, mode, grade) | `feature.categorical` → `encoder` port |
| features already computed in the table | `feature.select` |

### Rules that are enforced, not stylistic

 - **Merging requires identical provenance.** `feature.merge` needs exactly equal feature
   indices, identical source rows, identical `groups`/`assets`/`source_path`/`source_id`, and
   **disjoint column names** (overlap fails with `Feature names overlap; rename before
   merging`). Two branches built from the same columns/window/step/group always merge; a branch
   that filtered, resampled or changed the window will not.
 - **NaN features cannot enter a model.** Sources of NaN: flat windows in spectral output
   (`flat_policy=nan`), short groups, missing raw values. Insert `feature.imputation`
   (`mean`, `median`, `zero`, `drop_columns`; `fill_value` for a constant) between the features
   and the model. If you forget, the model error names the offending columns. Report how much
   was filled.
 - **Categorical is a fitted pair.** `feature.categorical` fits and emits an `encoder`; reuse it
   through `feature.categorical_transform` instead of refitting on new data. Validators embed
   upstream categorical encoders in the trained model.
 - **Exploration outputs are terminal.** `visual.*`, `explore.*` and the
   `report`/`plot`/`StatisticsResult`/`CorrelationMatrix` ports are for the human, not model
   inputs.
 - **`feature.score_select` and `feature.pca` are fitted on all rows**, so they see the holdout.
   Two acceptable uses: (a) on an inspection branch, to understand structure; (b) before a
   model, only while reporting the leakage warning as a caveat. Their ranking is never
   validated evidence.
 - **`feature.spectral` needs a known sampling rate.** `sampling_rate` is required and has no
   default; if nobody knows it, ask instead of guessing. A channel that holds or quantises
   values produces flat windows: with `flat_policy=nan` (default) those rows keep alignment and
   get NaN spectra (impute afterwards), `skip` drops the windows (only safe when nothing must
   merge with them), `error` restores the hard failure. Variation that exists only at the window
   edges also counts as flat, because a Hann taper is zero there.

### Checklist

 - [ ] every feature branch shares window parameters and merges without rename conflicts
 - [ ] `feature.imputation` sits before every model whenever a NaN source exists
 - [ ] exploration/visual nodes hang off as terminal branches
 - [ ] you can name the columns the model will actually see

### Inspecting the intermediate table

Feature branches are inspectable in place: `visual.overview` (rows, columns, dtypes, missing
rates), `visual.histogram` (distributions), `visual.line` (a few channels) and
`explore.correlation` (redundancy) all accept a `FeatureDataset` as well as a raw `Dataset`.
Hanging one of them on the branch you actually model costs a single node and turns "the agent
says the features are fine" into something a fault engineer can look at; quote its numbers in
the report.

## Stage 5 — Validation (§6)

`split_method` on `validation.random_forest`, `validation.svm`, `validation.decision_tree` and
`validation.reservoir_classifier` (`stratified` default; `asset` supported). Regression is the
exception: `validation.linear_regression` offers `random` (default), `group` and `temporal` —
there is no asset holdout for it, so state that limitation instead of pretending otherwise:

| Method | Holds out | Use when |
| --- | --- | --- |
| `stratified` | random rows, class-proportional | rows are independent: no windows, no repeated instances |
| `group` | whole values of `group_column` (**instances**) | overlapping windows, or one instance per asset |
| `asset` | whole assets (needs `asset_column` upstream) | "will this work on equipment we have never seen?" |
| `temporal` | later rows, purging training rows that share raw data with test | forecasting and real deployment order |

`stratified` refuses overlapping or repeated data (`Overlapping windows require group or
temporal split`) — a guardrail, not an obstacle.

### Asset-level holdout recipe

1. `data.asset_key(column="<instance key>", target="asset", mode="split", separator="_",
   index=0)` — or `mode="regex"` with one capture group. It derives the asset from the instance
   key (`WELL-00001_20170201…` → `WELL-00001`).
2. Set `asset_column="asset"` on **every** window component and on `data.quality`.
3. Set `split_method="asset"` on every classifier. It requires the asset attribute and at least
   two assets; the error messages name the missing piece.

### Why `group` is not an asset holdout

One asset usually contributes several instances, so a held-out "group" still leaks that asset
behaviour into training. Measured on real 3W data (28 instances, 10 wells, 8 091 windows, 72
features, RF-300):

| split | accuracy | balanced acc. | ROC-AUC | PR-AUC | miss rate | unseen test assets |
| --- | --- | --- | --- | --- | --- | --- |
| `group` (instance) | 0.638 | 0.621 | **0.716** | 0.804 | 0.231 | 0 / 6 |
| `asset` (leave-one-well-out) | 0.606 | 0.568 | **0.528** | 0.611 | 0.270 | 3 / 3 |

The same model and features lose ~0.19 AUC once the split becomes honest. Report coverage that
way; never present the instance-level number as deployment performance.

### Reading a metrics payload

| Key | Why you care |
| --- | --- |
| `accuracy` | headline, misleading under imbalance |
| `balanced_accuracy` | class-size-corrected accuracy — prefer it when faults are rare |
| `roc_auc`, `average_precision` | ranking quality; PR-AUC is the one that matters for rare faults |
| `per_class_recall`, `train_class_counts`, `test_class_counts` | which class is silently missing (original labels, not encoded) |
| `confusion_matrix` | where the errors are |
| `positive_class`, `miss_rate` | miss rate for the fault class; set `positive_class` when the label order is unclear |
| `coverage` | `train_instances`/`test_instances`/`test_instances_unseen` and the asset equivalents |
| `warnings` | leakage, unavailable AUC, flat windows, evictions |

Always fetch `coverage` and quote it next to the score: "3 of 3 test wells were unseen" is the
sentence that makes the number mean something.

`validation.compare` compares up to three metrics payloads on **identical holdout rows** and
refuses mismatched test indices, so only compare runs sharing features, labels, split method,
`test_size` and `random_state`.

### Checklist

 - [ ] the split method matches the data structure (windows → group/asset/temporal)
 - [ ] asset-level question? `asset_column` set everywhere and `split_method=asset`
 - [ ] `coverage` read and quoted
 - [ ] models compared with identical everything except the estimator
 - [ ] warnings surfaced with the numbers

## Stage 6 — Execute and debug (§7)

### Status vocabulary

Pipeline: `CREATED`, `VALIDATING`, `READY`, `RUNNING`, `SUCCESS`, `FAILED`, `CANCELLED`.
Node: `PENDING`, `READY`, `RUNNING`, `SUCCESS`, `FAILED`, `SKIPPED` (skipped = an upstream
result was unavailable).

There are two layers of "success": the *control* call succeeding (`success: true`) and the
*run* succeeding (`status`). Querying a `FAILED` run is a successful query.

### Iterating without recomputing everything

 - `incremental=true` (default) reuses nodes whose fingerprint is unchanged, so editing one
   parameter recomputes only it and its descendants.
 - `retry_node(pipeline_id, node_id)` (= `execute_from_node`) re-runs that node and everything
   downstream, reusing valid upstream artifacts. Use it after fixing the failing node.
 - `execute_node` runs exactly one node.
 - `cancel_pipeline` stops a run; `get_history` lists per-node attempts with timings and the
   `cached` flag.
 - Editing the graph invalidates the workspace: results are dropped and nodes read `PENDING`
   ("Graph changed; run to refresh results").

### Reading a failure

`get_node_result` on the failed node returns `error` with the exception type, message, traceback
lines and a parameter snapshot; the `input_summary` preview shows the first rows the component
actually received. That preview is usually the answer — a constant column, a wrong dtype, a
filter that matched nothing. Fix with `configure_components`, then `retry_node`.

### Checklist

 - [ ] `validate_pipeline` clean before executing
 - [ ] the run reached `SUCCESS`, or you can explain every `SKIPPED`/`FAILED` node
 - [ ] stack traces read, not just re-run

## Stage 7 — Read the results (§8)

Shapes by artifact type:

| `kind` | Contents |
| --- | --- |
| `table` | `shape`, `columns`, `dtypes`, preview rows, `missing_rate` over the preview |
| `vector` | length, name, head |
| `array` | shape, head |
| `streamed` | the streamed dataset description (rows stay on disk) |
| `model` | class name, feature list, number of embedded categorical encoders |
| `transformer` | encoder description |
| `object` | the mapping itself, with long arrays collapsed to `*_count` |

Read metrics from the validator `metrics` port and keep `include_indices=false`. The metrics
payload already carries the confusion matrix, per-class recall and class counts, so you never
need `train_indices`/`test_indices` to describe a result; pulling them is what previously flooded
an agent context with 8 000 row numbers. Expand indices only when the rows themselves are the
deliverable.

## Stage 8 — Persist and hand off (§9)

 - `save_pipeline(pipeline_id, filename="...xml")` writes the graph under `storage_root` (XML
   only, path confined to the storage directory). `get_pipeline_xml` returns the same document
   inline; `load_pipeline(xml)` imports a new pipeline (a fresh id on collision);
   `replace_pipeline(graph, expected_version=...)` overwrites with optimistic concurrency and
   refuses stale versions (`Graph changed in another client; reload before editing`).
 - `save_checkpoint` / `load_checkpoint` / `list_checkpoints` snapshot graph **and** workspace
   state (in memory; gone with the process). Restoring a checkpoint whose payloads were released
   warns and recomputes the affected nodes.
 - `delete_pipeline(pipeline_id)` removes the pipeline, its workspaces, spilled files and
   checkpoints. Use it after abandoning a failed attempt so the service does not accumulate
   state.
 - Hand-off sentence: pipeline id, XML path, data path, split method, label policy, headline
   metrics with coverage.

## Large data and memory (§10)

The workspace stores artifacts **by reference** (storing and previewing do not copy), can spill
to disk under a byte budget, and streams when asked — but the bounds are real:

 - **Bound the read first:** `columns` (projection) and `max_rows`, or Parquet `filters`
   (predicate pushdown, e.g. `[["equipment", ">", 10]]`).
 - **Streaming above ~1 GB:** `data.input.streaming=true` + `chunk_rows`. Rows must be grouped
   **contiguously** (sorted by the group column) and time-ordered inside each group. Exactly
   these components accept a streamed dataset: `feature.statistical`, `feature.fitting`,
   `feature.spectral`, `visual.overview` and `data.materialize`. Everything else — including
   `data.quality`, entropy features, filters and plots — refuses with
   `… cannot consume streamed input; insert data.materialize or turn streaming off on
   data.input`. Plan the quality pre-check **before** switching streaming on, or accept a
   materialized run and keep its warning.
 - A **single huge group** (one well, millions of rows) is still buffered whole. Split it into
   instances or shrink the window.
 - The cache budget may evict results. With a spill directory (the server default) evicted
   payloads are reloaded on demand; without one, the owning node is invalidated and the pipeline
   drops to `READY` — re-run instead of assuming the numbers still exist. `get_server_info`
   reports `evictions` and `spills` honestly.
 - If you analysed a bounded subset, the conclusion is about that subset: name the rows, columns
   and filters that produced the number.
