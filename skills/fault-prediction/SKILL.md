---
name: fault-prediction
description: Drive the Fault Prediction Component Platform over its MCP tools — inspect the server, prepare and locate data, pre-check quality, build aligned windows and labels, engineer features, run honest holdout validation (instance / asset / temporal), execute and debug graphs, read results, and persist reusable pipelines. Use for equipment fault detection, degradation/onset modelling, anomaly exploration and model comparison on this platform, including revising an existing pipeline.
---

# Fault Prediction Component Platform — agent playbook

A pipeline here is a **typed component graph**: `data.input → (quality) → windows/labels →
features → validation`, plus exploration and visual branches. You (over MCP), the web designer
and the HTTP API all edit the **same** graph, so a human can watch your nodes appear and run.

Run the loop in order: **recon → data prep → quality → windows → features → validation →
execute → inspect → revise → persist → report**. Each stage consumes the previous stage
contract, and the two failures that hurt most in practice (a wrong data path, dead or flat
channels) are caught in the first two stages for a handful of tool calls.

## 0. Operating rules

### 0.1 Where to look

| You are about to… | Go to |
| --- | --- |
| start any task, or do not know the server | §1 Recon |
| wonder where the data lives, or how to get it in | §2 Workspace and data |
| check whether the data is usable | §3 Quality pre-check |
| cut windows, choose a label policy | §4 Windows, groups, labels |
| add, merge or clean features | §5 Features |
| choose a split, read metrics | §6 Validation |
| debug a failed run, retry a node | §7 Execute and debug |
| read results without dumping arrays | §8 Results |
| hand the pipeline to someone else | §9 Persist and hand off |
| hit out-of-memory, or the file is >1 GB | §10 Large data |
| match an error message to a cause | §11, then `references/troubleshooting.md` |
| wonder whether this stage is missing a capability | the **Stage gate** at the end of §3–§6 |
| need the parameter tables, worked numbers or checklists behind a stage | `references/stages.md` |
| copy a known-good call sequence | `references/recipes.md` |
| know what a component is for, or when not to use it | `references/components.md` |

### 0.2 Invariants (violating these fails immediately)

1. **Ports are typed.** `Dataset`, `FeatureDataset`, `LabelVector`, `FeatureTransformer`,
   `Prediction`, `Model`, `Metrics`, `FeatureImportance`, `StatisticsResult`,
   `CorrelationMatrix`, `Visualization`, `PlotArtifact`. A mismatched connection is refused
   with `Incompatible port types: X -> Y`. Window features are `FeatureDataset`; never wire a
   raw `Dataset` into a model.
2. **One producer per input port.** Fan-out is free (one output → many consumers; that is how
   you branch). Fan-in needs an explicit component — `feature.merge` for features, nothing for
   raw frames. A second connection into an occupied input fails with
   `Target input already connected`.
3. **Cycles are refused** (`Connection creates a cycle`) — the graph is a DAG.
4. **Data paths are relative to the server `data_root`.** Absolute paths and `..` escapes are
   rejected, and `data_root` cannot be changed at runtime.
5. **State lives in the process.** Pipelines, workspaces, artifacts and checkpoints belong to
   the running service; a restart clears them. Only files under `data_root` and XML saved under
   `storage_root` survive. This is a single-user local service.

### 0.3 Token discipline

 - Discover by intent (`retrieve_components`), not by listing the whole catalogue.
 - Assemble in bulk: `add_components` + `connect_many` + `configure_components` — a 10-node
   graph is 3–5 calls. Bulk calls default to `include_graph=false`; keep it that way and read
   `{pipeline_id, version, node_count, edge_count, added}` instead of an echoed graph.
 - Fetch `get_component_schema` only for the components you are about to configure.
 - Keep `get_node_result` compact (`include_indices=false`); long arrays collapse to `*_count`.
 - Wait with `wait_for_pipeline`; never sleep-and-poll.

### 0.4 Honesty rules

 - Warnings are part of the result, not noise. Leakage, flat-window, eviction, subset and
   stale-workspace notices must reach the user **with** the number they qualify.
 - Never present an exploratory score (full-data feature selection or PCA, synthetic data) as
   validated performance. Name the split and the coverage.
 - If you converted, subsampled, dropped or imputed anything, say so and say why.
 - If a stage was skipped (no quality run, no asset holdout), say that too. A missing check is
   a finding.

## 1. Recon — three calls, then a capability shortlist

| Call | What it answers |
| --- | --- |
| `get_server_info()` | platform version, `data_root`, `storage_root`, data file count and the first 50 paths, component count, artifact-cache stats (bytes, evictions, spills, spill dir) |
| `list_datasets()` | every readable `.csv`/`.parquet`/`.pq` under `data_root`, with byte sizes |
| `get_component_facets()` | the categories, subcategories, tags and versions that exist — enough to browse without pulling schemas |

Then discover by intent: `retrieve_components(intent="...", category=..., input_type=...,
output_type=..., source_component_type=..., target_component_type=...)`. It ranks lexical
matches (English and Chinese bigrams) and, given the two `*_component_type` arguments, keeps
only components that can legally sit between two nodes you already have. `list_components`
(`category`, `tags`, `input_type`, `include_schema`, …) and `search_components` cover exhaustive
and plain keyword lookups. **Prefer `retrieve_components`** — listing everything wastes context.

Recon produces two things: the environment facts above, and a 3–6 line **capability shortlist**
for the task at hand. Build the shortlist with `get_component_facets()` plus one
`retrieve_components` call per capability you are unsure this platform has (drift, periodicity,
unsupervised anomaly, categorical encoding, …) — typically one or two calls. Do **not**
enumerate all 56 components: that is a catalogue, not a shortlist, and it costs context without
changing a decision.

`create_example(include_xgboost=false)` builds a working end-to-end pipeline on generated
synthetic data. Use it to learn the expected shape of a graph or to smoke-test the service;
never to answer a question about the user data.

## 2. Stage 1 — Workspace and data preparation

**Goal:** the raw file is inside `data_root`, and you know which columns are the instance, the
asset, the time index, the measurements and the label.

1. `get_server_info()` → note `data_root`; that directory is the sandbox root.
2. `list_datasets()` → is the file already there? If yes, use that exact relative path.
3. If not, it has to get in, and **over MCP you cannot copy files in**. Either the web UI upload
   button (writes to `data_root/uploads/dataset_<hex>.<ext>`, renamed — re-read `list_datasets`
   afterwards; caps at 25 MB, `.csv`/`.parquet`/`.pq` only), or the operator placing the file
   inside `data_root` / restarting the service with `--data-root <dir>`. Ask for the one you
   need, state why, and never guess a path or search the filesystem.
4. Configure `data.input`: `path` (relative, **required**), `format` (`csv` default, or
   `parquet`), plus `separator`/`encoding` for CSV.

Only CSV and Parquet ever reach the platform: `.mat`, `.txt`, MDF/BLF have to be converted
outside it, and you must say that you did it. Conversion recipes and the stage checklist are in
`references/stages.md` §Stage 1. Aim for **one row per sample** (`asset_id`, `instance_id`,
`time`, the measurement columns, the label) — windowing assumes that shape — and record the
instance **and** asset columns now, while the raw data is in front of you.

## 3. Stage 2 — Quality pre-check

**Goal:** know which channels are dead before they become surprising features.

Add `data.quality` on the same `data.input` output, configured with **the same** `columns`,
`group_column`, `time_column`, `window_size` and `step` you will use for window features. It
outputs `Visualization`, so it is a **terminal branch**: it never feeds the model, and that is
why fan-out exists.

Two findings change decisions, and neither is visible in `visual.overview`: a column can be
0 % missing, non-constant overall, and constant **inside every group**; and a held or quantised
channel produces **flat windows**, where `feature.spectral` returns NaN by default. Keep the
per-column flat ratio for your report. The full finding table is in `references/stages.md`
§Stage 2.

**Stage gate — does this stage have a problem?** Ask each question; act only when one is true. A
gate that fires costs one terminal branch, not a redesign, so the bar is "would the answer change
what I do next".

| Self-check | When it fires |
| --- | --- |
| channels look redundant | `explore.correlation` on the same `data.input` output (terminal branch) |
| several periods, batches or assets in one file | `explore.concept_drift` — it takes **two** inputs (`reference`, `current`), so wire early vs late slices from two `data.filter` nodes |
| suspect periodicity or a repeating duty cycle | `explore.periodicity` (if the sampling rate is unknown, ask instead of guessing) |
| asset levels differ a lot | `explore.central_tendency` + `explore.dispersion`, grouped by the asset column |
| no labels, and you only need to know what "unusual" looks like | `explore.anomaly` (outputs `Prediction`, terminal branch) |

**Do not add a node just to use a component.** Everything in that table is a terminal branch: it
serves the human report and never feeds the model.

## 4. Stage 3 — Windows, groups and labels

**Goal:** a `FeatureDataset` whose rows correspond 1:1 with labels, and which validation can
split honestly.

The window producers are `feature.statistical`, `feature.fitting`, `feature.spectral` and
`feature.entropy`. They must agree on `columns`, `group_column`, `time_column`, `asset_column`,
`label_column`, `window_size`, `step` and `label_policy` if you intend to merge their output;
the parameter semantics are in `references/stages.md` §Stage 3.

Two rules decide whether the run works at all:

 - `label_policy=strict` (the default) **fails the whole run** when a label changes inside a
   window, which on real fault data happens exactly at the fault onset. Use `mode` (majority) or
   `last`, and say which you chose and why.
 - `window_size=0` means "the whole group": right for whole-run classification, wrong for onset
   detection. Pick `window_size`/`step` against the real sampling rate, not from an example.

**Stage gate — does this stage have a problem?**

| Self-check | When it fires |
| --- | --- |
| the label changes inside a window | `label_policy=mode`; `strict` fails outright on real fault data |
| the question is *when* degradation starts | window-end labels plus `mode`, and expect a rare positive class |
| you plan to hold out whole assets | wire the asset column now (`data.asset_key`, or an `asset_column` on the windows); `split_method=asset` refuses to run without it |
| a group is shorter than one window | shorten `window_size`, or drop the group back in Stage 2 and say so |

## 5. Stage 4 — Features

**Goal:** one or more aligned `FeatureDataset` branches, merged, cleaned, and free of channels
you already know are dead.

Which branch answers which signal question — level and shape, trend, rotation/resonance,
irregularity, short-term dynamics, discrete attributes — is tabulated in `references/stages.md`
§Stage 4. Pick from it rather than defaulting to the statistical branch, and use `feature.select`
when the features are already in the table.

Rules that are enforced, not stylistic:

 - **Merging requires identical provenance.** `feature.merge` needs equal feature indices,
   identical source rows and groups, and **disjoint column names** (overlap fails with
   `Feature names overlap; rename before merging`). Two branches built from the same
   columns/window/step/group always merge; a filtered or resampled one will not.
 - **NaN features cannot enter a model.** Flat spectral windows, short groups and missing values
   all produce NaN; insert `feature.imputation` between features and model, and report how much
   was filled.
 - **Categorical is a fitted pair.** `feature.categorical` emits an `encoder`; reuse it through
   `feature.categorical_transform` instead of refitting on new data.
 - **`sampling_rate` is required by `feature.spectral`** and has no default: if nobody knows it,
   ask instead of guessing.
 - **`feature.score_select` and `feature.pca` are fitted on all rows**, so they see the holdout:
   exploration only, and the leakage warning is part of the answer.

**Stage gate — are the features enough?**

| Self-check | When it fires |
| --- | --- |
| every column the model sees is a level/shape summary (mean, std, peaks) | walk the table in `references/stages.md` §Stage 4 line by line; a property that holds with no branch built is a capability you dropped |
| a channel is a discrete step or grade being treated as continuous | `feature.categorical` (fits an `encoder`) → reuse it through `feature.categorical_transform` |
| the waveform is spiky or irregularly structured | `feature.entropy` (`methods` is required) |
| you need rate of change without windowing | `feature.temporal` (`columns` required) or `feature.rolling_statistics` |
| an exploration branch produces NaN | `feature.imputation` before any model, and report how much was filled |
| you cannot say what the modelled feature table looks like | hang `visual.overview` on the feature branch — it accepts `FeatureDataset` as well as `Dataset`, so row count, column names, dtypes and missing rates come back as an ordinary result the human can check |

## 6. Stage 5 — Validation

**Goal:** a score that answers the question the user is actually asking.

The default `split_method` for the classifiers is `stratified`, and it refuses overlapping or
repeated data (`Overlapping windows require group or temporal split`) — a guardrail, not an
obstacle. Match the split to the data structure: windows → `group`, whole equipment → `asset`
(needs `asset_column` upstream), deployment order → `temporal`. `validation.linear_regression`
offers `random`/`group`/`temporal` and no asset holdout at all, so state that limitation instead
of pretending otherwise. The method table, the asset-holdout recipe and the metrics dictionary
are in `references/stages.md` §Stage 5.

Two things belong in every answer:

 - **`group` is not an asset holdout.** One asset usually contributes several instances, so a
   held-out group still leaks that asset behaviour into training. On real 3W data the same model
   and features fell from ROC-AUC 0.716 (instance holdout) to 0.528 (leave-one-well-out).
 - **Quote `coverage` next to the score** — "3 of 3 test wells were unseen" is the sentence that
   makes the number mean something. `validation.compare` only accepts runs sharing features,
   labels, split method, `test_size` and `random_state`.

**Stage gate — is the validation answering the question?**

| Self-check | When it fires |
| --- | --- |
| classes are imbalanced | report `balanced_accuracy`, `per_class_recall` and `miss_rate`, never accuracy alone |
| there are no labels to validate against | `validation.knn_detector`, `validation.isolation_forest_detector` or `validation.persistence_detector` — all three take a raw `Dataset` and need no `LabelVector` |
| the question is whether the series is forecastable at all | `validation.arma` (`column` required) as the baseline |
| only one algorithm family is in the comparison | add `validation.decision_tree` or `validation.reservoir_classifier` to `validation.compare` |
| the holdout contains assets the model never saw | quote `coverage.unseen_assets`; that is the only generalisation number worth trusting |

## 7. Stage 6 — Execute and debug

**Goal:** a finished run, or a precise diagnosis of what blocked it.

```
create_pipeline(name)
  → add_components([...])            # bulk
  → connect_many([...])              # bulk
  → validate_pipeline(pipeline_id)   # structural only, cheap
  → execute_pipeline(pipeline_id)    # returns RUNNING
  → wait_for_pipeline(pipeline_id, timeout_seconds=...)
```

`validate_pipeline` catches missing required inputs, unset required parameters, type mismatches,
cycles, multiple producers on one input and unsatisfied `compatibility` constraints without
running anything — always run it first. There are two layers of "success": the control call
(`success: true`) and the run (`status`); querying a `FAILED` run is a successful query.

When a node fails, read its `error` **and** the `input_summary` preview from `get_node_result`:
a constant column, a wrong dtype or a filter that matched nothing is usually visible right there.
Fix with `configure_components`, then `retry_node` — incremental reuse re-runs only that node and
its descendants. Status vocabulary and the rest of the iteration toolkit: `references/stages.md`
§Stage 6.

## 8. Stage 7 — Read the results

`get_node_result(pipeline_id, node_id)` returns `{status, outputs: {port: {kind, …, artifact}},
error, warnings}`; `get_pipeline_result` covers every node at once (limit it on big graphs). Keep
`include_indices=false`: the metrics payload already carries the confusion matrix, per-class
recall and class counts, so you never need `train_indices`/`test_indices` to describe a result —
pulling them is what once flooded a context with 8 000 row numbers. Expand indices only when the
rows themselves are the deliverable, and forward the workspace `warnings`. The table of artifact
shapes by `kind` is in `references/stages.md` §Stage 7.

## 9. Stage 8 — Persist and hand off

`save_pipeline(pipeline_id, filename="...xml")` writes the graph under `storage_root`;
`get_pipeline_xml` returns the same document inline; `load_pipeline(xml)` imports it as a new
pipeline; `replace_pipeline(graph, expected_version=...)` is the optimistic-concurrency update
and refuses a stale version. `save_checkpoint`/`load_checkpoint`/`list_checkpoints` snapshot the
graph **and** the workspace (in memory — gone with the process). `delete_pipeline` removes the
pipeline, its workspaces, spilled files and checkpoints, so use it after abandoning a failed
attempt.

Hand off with: pipeline id, XML path, data path, split method, label policy, and the headline
metrics with coverage. A human can open `http://127.0.0.1:8765` and watch the same graph — your
edits appear live over SSE (`GET /api/events`), including node status during a run.

## 10. Large data and memory

Artifacts are stored **by reference** (storing and previewing do not copy), the cache can spill
to disk under a byte budget, and `data.input.streaming=true` + `chunk_rows` streams above ~1 GB —
but only `feature.statistical`, `feature.fitting`, `feature.spectral`, `visual.overview` and
`data.materialize` accept a streamed dataset. Bound the read first (`columns`, `max_rows`,
Parquet `filters`), and remember that a single huge group is still buffered whole. Full bounds,
the spill/eviction semantics and the "name what your number describes" rule are in
`references/stages.md` §Large data and `references/recipes.md` Recipe D.

## 11. Symptoms → causes → fixes

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| `Incompatible port types: Dataset -> FeatureDataset` | raw data wired where features are expected | insert the window/feature component, or `feature.select` |
| `Overlapping windows require group or temporal split` | `stratified` with repeated groups | change `split_method` |
| `Feature provenance differs: source_rows` | merged branches with different windows or filters | rebuild both branches with identical window parameters |
| model error naming NaN columns | flat windows or missing values upstream | insert `feature.imputation` before the model |
| run fails on one window, the message mentions labels changing | `label_policy=strict` on onset data | switch to `mode`/`last` |

The other guardrails — with their exact messages and the reason each exists — are in
`references/troubleshooting.md`.

## 12. Reporting contract

When you finish a task, the answer must contain:

1. **Data** — which file, which columns (instance/asset/time/label/measurements), and any
   conversion or bounding you performed.
2. **Quality** — dead/flat/constant channels found, and what you did about them.
3. **Graph** — node ids and component types, window parameters, the label policy and why.
4. **Validation** — split method, holdout size, `coverage` including unseen assets, the metrics
   table, and every warning.
5. **Boundaries** — what was exploratory, what was synthetic, what was skipped, what could
   invalidate the number.
6. **Artifacts** — pipeline id, XML path, and the URL the human can open to see it.
7. **Stage checks** — which gates fired and what you did about each; which capability families
   stayed untouched (one line is enough) and why that is acceptable for this task.
8. **Middle evidence** — one concrete look at the data you actually modelled: hang
   `visual.overview` on the feature branch (and `visual.line`/`explore.correlation` when the
   question is about a signal or redundancy) and quote what it returned — rows, columns,
   missing rate — so the human can verify the pipeline without taking your word for it.

Lead with the result, then the caveat that qualifies it. Never lead with the caveat, and never
drop it.

## Appendix A — tool map (38 tools)

| Stage | Tools |
| --- | --- |
| Recon | `get_server_info`, `list_datasets`, `get_component_facets`, `list_components`, `search_components`, `retrieve_components`, `get_component_schema` |
| Pipeline lifecycle | `create_pipeline`, `list_pipelines`, `get_pipeline`, `delete_pipeline`, `replace_pipeline`, `save_pipeline`, `load_pipeline`, `get_pipeline_xml`, `create_example` |
| Graph editing | `add_component`, `add_components`, `remove_component`, `configure_component`, `configure_components`, `connect_components`, `connect_many`, `disconnect_components` |
| Execution | `validate_pipeline`, `execute_pipeline` (`mode=all|node|from`, `incremental`), `execute_node`, `execute_from_node`, `retry_node`, `cancel_pipeline` |
| Observation | `get_pipeline_status`, `wait_for_pipeline`, `get_node_result`, `get_pipeline_result`, `get_history` |
| Persistence | `save_checkpoint`, `load_checkpoint`, `list_checkpoints` |

`wait_for_pipeline` is the only blocking tool and deliberately runs outside the service lock. If a
client shows suffixed duplicate tool names, use the unsuffixed name.

## Appendix B — capability map

Five families, 56 components, and what each one expects on its input port:

| Family | What it is for | Ports |
| --- | --- | --- |
| **Data (16):** | read, filter, reshape, resample, split, scale/encode, derive keys, quality pre-check | `Dataset` |
| **Explore (8):** | question-shaped diagnostics — drift, periodicity, correlation, anomaly; all terminal | `Dataset` |
| **Visual (8):** | plots for the human; all terminal | `Dataset` |
| **Feature (13):** | window producers, merge, clean, select, reduce | `Dataset` → `FeatureDataset` (+ `LabelVector`) |
| **Validation (11):** | supervised classifiers, regression/ARMA baselines, unsupervised detectors, comparison | `FeatureDataset` + `LabelVector`, or raw `Dataset` for the detectors |

`references/components.md` holds the full list — every component with its ports, key parameters
and when not to use it.

This is a **catalogue to look things up in**, not a checklist to weigh item by item. Which family
matters, and when, is decided by the **Stage gate** at the end of each stage (§3–§6): a short list
of questions whose honest answer is usually "no".

## References

 - `references/stages.md` — the detail behind each stage: parameter tables, worked numbers,
   checklists and the watch-outs.
 - `references/recipes.md` — copy-ready call sequences: tabular classification, onset/degradation
   with `mode` labels, asset holdout, unsupervised detection, large-file streaming, revising a
   failed run, exploration before modelling.
 - `references/troubleshooting.md` — the full symptom catalogue, guardrail by guardrail.
 - `references/components.md` — the curated component map: what each one is for, when not to use
   it, and its key parameters.

The authoritative parameter schema is always `get_component_schema(component_type)`; the generated
catalogue `docs/components.md` (from `scripts/export_catalog.py`) is the same data in bulk. If a
reference file and the live schema disagree, the schema wins — and the reference is a bug worth
reporting.
