# Recipes

Copy-ready call sequences for the jobs that come up most. Argument names are the real MCP
argument names; `...` marks values you must fill in from the data.

Every recipe assumes the recon calls from `SKILL.md` §1 have been made:
`get_server_info()`, `list_datasets()`, and a `retrieve_components(...)` for the pieces you
need.

---

## Recipe A — Tabular data, instance-level holdout, classification baseline

Use when: the table is already feature-like or windowed per instance, rows are not
overlapping, and the question is "which of these known units is faulty?".

```jsonc
// 1. pipeline
create_pipeline({"name": "设备故障基线"})

// 2. nodes in one call
add_components({"pipeline_id": "p_…", "components": [
  {"component_type": "data.input", "node_id": "source",
   "parameters": {"path": "…/data.csv"}},
  {"component_type": "data.quality", "node_id": "quality",
   "parameters": {"columns": ["…measurements…"], "group_column": "equipment",
                  "label_column": "label", "window_size": 64, "step": 0}},
  {"component_type": "feature.statistical", "node_id": "stat",
   "parameters": {"columns": ["…measurements…"], "group_column": "equipment",
                  "label_column": "label", "time_column": "time",
                  "window_size": 64, "step": 0, "features": ["mean","std","rms","kurtosis"]}},
  {"component_type": "feature.imputation", "node_id": "clean", "parameters": {"method": "median"}},
  {"component_type": "validation.random_forest", "node_id": "forest",
   "parameters": {"split_method": "group", "test_size": 0.25, "random_state": 42,
                  "n_estimators": 300, "class_weight": "balanced"}}
]})

// 3. edges in one call (note: quality is a terminal branch, and fan-out is allowed)
connect_many({"pipeline_id": "p_…", "connections": [
  {"source_node": "source", "source_port": "dataset", "target_node": "quality",   "target_port": "dataset"},
  {"source_node": "source", "source_port": "dataset", "target_node": "stat",      "target_port": "dataset"},
  {"source_node": "stat",   "source_port": "features","target_node": "clean",     "target_port": "features"},
  {"source_node": "clean",  "source_port": "features","target_node": "forest",    "target_port": "features"},
  {"source_node": "stat",   "source_port": "labels",  "target_node": "forest",    "target_port": "labels"}
]})

validate_pipeline({"pipeline_id": "p_…"})
execute_pipeline({"pipeline_id": "p_…"})
wait_for_pipeline({"pipeline_id": "p_…", "timeout_seconds": 600})
get_node_result({"pipeline_id": "p_…", "node_id": "forest"})   // metrics + warnings
```

Report: split method `group`, holdout size, `coverage`, the metrics table, and any warning.
`window_size` must be a real decision — `0` would collapse each group to one row.

---

## Recipe B — Onset / degradation data with an honest asset holdout

Use when: labels change over time, the sampling units are instances that belong to assets
(wells, machines, lines), and the question is "does this generalise to unseen equipment?".

```jsonc
add_components({"pipeline_id": "p_…", "components": [
  {"component_type": "data.input", "node_id": "source",
   "parameters": {"path": "…/events.parquet", "format": "parquet"}},
  {"component_type": "data.asset_key", "node_id": "asset",
   "parameters": {"column": "instance", "target": "asset", "mode": "split",
                  "separator": "_", "index": 0}},
  {"component_type": "data.quality", "node_id": "quality",
   "parameters": {"columns": ["…"], "group_column": "instance", "label_column": "label",
                  "time_column": "time", "window_size": 128, "step": 32}},
  {"component_type": "feature.statistical", "node_id": "stat",
   "parameters": {"columns": ["…"], "group_column": "instance", "asset_column": "asset",
                  "label_column": "label", "time_column": "time",
                  "window_size": 128, "step": 32, "label_policy": "mode",
                  "features": ["mean","std","rms","kurtosis","crest_factor"]}},
  {"component_type": "feature.fitting", "node_id": "fit",
   "parameters": {"columns": ["…"], "group_column": "instance", "asset_column": "asset",
                  "label_column": "label", "time_column": "time",
                  "window_size": 128, "step": 32, "label_policy": "mode",
                  "fitting_method": "linear"}},
  {"component_type": "feature.merge", "node_id": "merge"},
  {"component_type": "feature.imputation", "node_id": "clean", "parameters": {"method": "median"}},
  {"component_type": "validation.random_forest", "node_id": "forest",
   "parameters": {"split_method": "asset", "positive_class": "…fault label…",
                  "n_estimators": 300, "class_weight": "balanced"}}
]})

connect_many({"connections": [
  {"source_node": "source", "source_port": "dataset",  "target_node": "asset",   "target_port": "dataset"},
  {"source_node": "asset",  "source_port": "dataset",  "target_node": "quality", "target_port": "dataset"},
  {"source_node": "asset",  "source_port": "dataset",  "target_node": "stat",    "target_port": "dataset"},
  {"source_node": "asset",  "source_port": "dataset",  "target_node": "fit",     "target_port": "dataset"},
  {"source_node": "stat",   "source_port": "features", "target_node": "merge",   "target_port": "left"},
  {"source_node": "fit",    "source_port": "features", "target_node": "merge",   "target_port": "right"},
  {"source_node": "merge",  "source_port": "features", "target_node": "clean",   "target_port": "features"},
  {"source_node": "clean",  "source_port": "features", "target_node": "forest",  "target_port": "features"},
  {"source_node": "stat",   "source_port": "labels",   "target_node": "forest",  "target_port": "labels"}
]})
```

Points that decide whether this recipe is honest:

- `label_policy="mode"` because onset windows straddle the transition; `strict` would abort at
  exactly the interesting moment.
- `asset_column` on **every** window producer — the merge checks that the two branches agree on
  it, and `split_method="asset"` reads it from the features.
- Both window branches share `columns`/`group_column`/`time_column`/`window_size`/`step`, so
  the merge succeeds. Change any of them in one branch and the merge will refuse.
- Read `metrics.coverage`; if `test_assets_unseen` is 0, the split is not doing what you think.

---

## Recipe C — Unsupervised detection (no usable labels)

Use when: there is no reliable label, or the label exists but is the thing you are trying to
discover rather than evaluate.

```jsonc
add_components({"pipeline_id": "p_…", "components": [
  {"component_type": "data.input", "node_id": "source", "parameters": {"path": "…"}},
  {"component_type": "data.quality", "node_id": "quality", "parameters": {"columns": ["…"]}},
  {"component_type": "validation.isolation_forest_detector", "node_id": "iforest",
   "parameters": {"columns": ["vibration", "temperature"], "contamination": 0.05,
                  "n_estimators": 200, "random_state": 42}},
  {"component_type": "validation.persistence_detector", "node_id": "persist",
   "parameters": {"column": "vibration", "threshold": 2.0, "direction": "above",
                  "min_consecutive": 3, "group_column": "instance"}},
  {"component_type": "visual.anomaly", "node_id": "plot",
   "parameters": {"x": "time", "y": "vibration", "anomaly_column": "…", "max_points": 2000}}
]})
```

`contamination` **is** the model: it sets how many points are labelled anomalous, so an
accuracy-like number is meaningless here. Report the anomaly rate you asked for and the
obvious failure modes (threshold windows, spikes, held values) you saw in `data.quality`.
`validation.persistence_detector` needs a threshold the operator can defend — a "stuck value"
rule, not a statistical one.

---

## Recipe D — Large file (above ~1 GB)

```jsonc
// bounded read first: try this before streaming, it is usually enough
{"component_type": "data.input", "parameters": {"path": "events.parquet", "format": "parquet",
  "columns": ["instance","time","asset","label","vibration","temperature"],
  "filters": [["asset", "in", ["WELL-00001","WELL-00002"]]]}}

// or stream everything and let the window components work in chunks
{"component_type": "data.input", "parameters": {"path": "events.parquet", "format": "parquet",
  "streaming": true, "chunk_rows": 200000}}
```

Streaming rules that fail loudly when broken:

- rows must be grouped **contiguously** — sort by the group column before exporting;
- rows must be time-ordered inside each group;
- only `feature.statistical`, `feature.fitting`, `feature.spectral`, `visual.overview` and
  `data.materialize` accept a streamed dataset; anything else (including `data.quality`)
  raises `… cannot consume streamed input; insert data.materialize or turn streaming off on
  data.input`;
- `data.materialize` restores full-memory behaviour and (correctly) warns;
- one group holding millions of rows is still buffered whole — split it into instances.

Run the quality pre-check on a **non-streaming** branch first (or on a bounded sample): it
cannot read a stream.

Then check `get_server_info().artifact_cache` for `evictions`/`spills` and say whether the run
fitted in memory.

---

## Recipe E — Revising a failed run

```jsonc
get_pipeline_status({"pipeline_id": "p_…"})          // which workspace is current
get_pipeline_result({"pipeline_id": "p_…"})          // every node's status in one call
get_node_result({"pipeline_id": "p_…", "node_id": "stat"})   // error + input preview
get_history({"pipeline_id": "p_…", "limit": 20})

configure_components({"pipeline_id": "p_…", "updates": [
  {"node_id": "stat", "parameters": {"label_policy": "mode"}}
]})
retry_node({"pipeline_id": "p_…", "node_id": "stat"})        // re-runs stat and downstream
wait_for_pipeline({"pipeline_id": "p_…"})
```

Patterns worth recognising:

1. **Several nodes failed at once** — look for one upstream cause (a filter that matched
   nothing, a constant column). Fixing the source node fixes the tail.
2. **A single node failed, everything else succeeded** — `retry_node` reuses the upstream
   artifacts, so this is cheap; do not re-run the whole pipeline.
3. **The graph was edited after a run** — results read `PENDING` with "Graph changed; run to
   refresh results". That is not data loss, just invalidation.
4. **Numbers look like they came from nowhere** — read `warnings`: eviction, stale workspace
   or subset notices explain it.

When you give up on an attempt, `delete_pipeline` it so the service does not keep its
workspaces and spilled files.

---

## Recipe F — Comparing three models

```jsonc
// identical features/labels/split for all three; only the estimator differs
{"node_id": "forest", "parameters": {"split_method": "asset", "test_size": 0.25, "random_state": 42}}
{"node_id": "svm",    "parameters": {"split_method": "asset", "test_size": 0.25, "random_state": 42,
                                     "kernel": "rbf", "C": 10, "class_weight": "balanced"}}
{"node_id": "xgb",    "parameters": {"split_method": "asset", "test_size": 0.25, "random_state": 42}}

connect_many({"connections": [
  {"source_node": "forest", "source_port": "metrics", "target_node": "cmp", "target_port": "first"},
  {"source_node": "svm",    "source_port": "metrics", "target_node": "cmp", "target_port": "second"},
  {"source_node": "xgb",    "source_port": "metrics", "target_node": "cmp", "target_port": "third"}
]})
```

`validation.compare` refuses mismatched test indices, which is the point: if the comparison is
rejected, two runs did not share a holdout and any ranking would have been meaningless.
`validation.xgboost` requires the optional dependency; if it is missing, say so instead of
silently dropping the third model.

---

## Recipe G — Exploration before modelling (no model yet)

```jsonc
add_components({"components": [
  {"component_type": "visual.overview", "node_id": "overview", "parameters": {"time_column": "time"}},
  {"component_type": "data.quality", "node_id": "quality",
   "parameters": {"group_column": "instance", "label_column": "label", "time_column": "time",
                  "window_size": 128, "step": 32}},
  {"component_type": "explore.correlation", "node_id": "corr", "parameters": {"method": "pearson"}},
  {"component_type": "explore.distribution", "node_id": "dist", "parameters": {"bins": 40}},
  {"component_type": "explore.periodicity", "node_id": "period",
   "parameters": {"columns": ["vibration"], "max_lag": 200, "sampling_rate": 1000}}
]})
```

All of these are **terminal** branches off `data.input` (fan-out). Their outputs are
`Visualization`/`StatisticsResult`/`CorrelationMatrix`, and none of them can be wired into a
model — that is enforced by the typed ports, not by convention.

Use this recipe when the user asks "what is in this data?" and expects an answer before a
model exists. Report findings (constant channels, flat ratios, mixed labels, correlations),
not a pipeline dump.
