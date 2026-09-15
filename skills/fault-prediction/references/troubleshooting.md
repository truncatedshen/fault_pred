# Troubleshooting catalogue

Grouped by stage, with the exact message where the platform produces one, the reason the
guardrail exists, and the smallest fix. Quotes are verbatim.

---

## 1. Service and workspace

| Message / symptom | Reason | Fix |
| --- | --- | --- |
| `Unknown operation: …` | the tool does not exist in this version | re-read the tool list; names are stable, but check the spelling |
| connection errors to the MCP bridge | the HTTP service is not running | the bridge is a thin proxy: start the service (`python -m fault_platform serve`) first; report this to the operator |
| everything is empty after a restart | pipelines/workspaces/checkpoints are in-memory | reload the XML under `storage_root`; re-upload any data that was outside `data_root` |
| `Reading workspace … which is not the pipeline's latest (…).` | you passed an older `workspace_id` | omit `workspace_id` to read the latest run |
| results unchanged after you changed a parameter | you read a cached result or a stale workspace | check `get_pipeline_status`; re-run the node |
| nodes show `PENDING` although a run succeeded | the graph was edited afterwards ("Graph changed; run to refresh results"), or artifacts were evicted | re-run; incremental reuse makes it cheap |
| `Checkpoint outputs were released; affected nodes recompute.` | a checkpoint referenced artifacts that no longer exist | re-run the affected nodes, or re-save the checkpoint |
| repeated tool names `list_pipelines_1`, `save_pipeline_2` | client-side discovery aliases, not a server feature | use the unsuffixed name |
| artifact cache reports `evictions`/`spills` you did not expect | the byte budget (or the absence of a spill directory) is doing its job | quote the stats; raise `--artifact-cache-mb` / configure `--artifact-spill-dir` with the operator |

## 2. Data access

| Message / symptom | Reason | Fix |
| --- | --- | --- |
| path refused, or empty dataset | `data.input.path` must be **relative to `data_root`**; absolute paths and `..` are rejected | `list_datasets()` → use one of the listed relative paths |
| file missing from `list_datasets` | it lives outside `data_root` | ask the operator to upload it (the UI writes to `data_root/uploads/`) or to restart with `--data-root` |
| `Parquet needs the pyarrow extra` | the server was installed without Parquet support | tell the operator the exact extra to install, or convert to CSV |
| confusing dtypes / parse errors | a binary or whitespace-delimited file renamed to `.csv` | convert properly (see `SKILL.md` §2) |
| memory spike while reading | the whole file was read | bound the read: `columns`, `max_rows`, Parquet `filters`, or `streaming` |

## 3. Graph editing

| Message / symptom | Reason | Fix |
| --- | --- | --- |
| `Incompatible port types: X -> Y` | the typed-port contract | insert the missing step (`feature.select`, a window component, `data.materialize`) |
| `Target input already connected` | one producer per input port | delete the old edge, or fan-in through `feature.merge` |
| `Unknown source output or target input port` | wrong port name | use the port names from `get_component_schema` (`dataset`, `features`, `labels`, `metrics`, `encoder`, …) |
| `Connection creates a cycle` | the graph is a DAG | restructure; there is no back edge |
| `Graph changed in another client; reload before editing` | `replace_pipeline` with a stale `expected_version` | `get_pipeline` again, then replace with the new version |
| `Save path must be an XML file within the pipeline storage directory` | `save_pipeline` confines writes to `storage_root` | pass a plain filename, or a path inside `storage_root` |
| a node cannot be removed | it still has edges | `disconnect_components` first (or remove it and re-wire) |
| `Pipeline is empty` on validate | nothing was added | add nodes before validating |
| `…: required input not connected` | a required port has no producer | connect it — the message names the node and port |

## 4. Quality and windows

| Message / symptom | Reason | Fix |
| --- | --- | --- |
| `data.quality` finds constant/held channels | quantised process points, stuck sensors | drop them from `columns`, or keep them and say why |
| high flat-window ratio with `missing_rate = 0` | held values produce no spectrum | expect NaN spectral features, impute them, or drop the channel |
| `strict` label policy aborts the run | labels change inside a window — normal for onset data | use `label_policy="mode"` (or `last`) |
| window features look one-row-per-group | `window_size=0` means "the whole group" | set a real `window_size` and `step` |
| far fewer windows than expected | incomplete trailing windows are dropped, and `step` controls overlap | recompute expected counts: roughly `rows/step` per group (minus the tail) |
| label length mismatch at the model | row-level labels were wired to windowed features (`data.labels`) | take `labels` from the window component |

## 5. Features

| Message / symptom | Reason | Fix |
| --- | --- | --- |
| `Feature indices must match exactly` | merging branches built from different windows | make the branches use identical parameters |
| `Feature provenance differs: source_rows` | one branch filtered/resampled/changed the window | rebuild both branches identically |
| `Feature provenance differs: groups` / `assets` / `source_path` / `source_id` | same cause, other fields | same fix — the merge is telling you the rows are not the same rows |
| `Feature names overlap; rename before merging` | two branches emit the same column | drop one branch, or select disjoint columns |
| model error naming NaN columns | flat windows / short groups / missing raw values | insert `feature.imputation` before the model |
| spectral features are NaN in a band of rows | the default `flat_policy=nan` kept alignment for flat windows | impute them, or drop those windows with `flat_policy=skip` when nothing merges |
| spectral features NaN at window edges only | Hann taper is zero at the ends, so edge-only variation has no spectrum | expected; treat like any other flat window |
| `sampling_rate` refused / results in nonsense frequencies | it is required and must be the real rate | ask the operator; never guess |
| `feature.categorical_transform` refuses an old encoder | the encoder came from different columns/settings | re-fit with `feature.categorical`, or pass the encoder from the same pipeline |
| model performs suspiciously well | `feature.score_select`/`feature.pca` fitted on all rows | move them to an exploration branch, or report the leakage warning |

## 6. Validation

| Message / symptom | Reason | Fix |
| --- | --- | --- |
| `Group split requires feature extraction with group_column` | no `group_column` upstream | add it to the window component and re-run |
| `Asset split requires asset_column on the upstream window component (it records which asset each window belongs to)` | asset never travelled with the features | `data.asset_key` → `asset_column` on every window component → re-run |
| `Asset split needs at least two assets, found N` | constant or wrongly derived asset column | inspect the derivation (`separator`, `index`, `pattern`) |
| `Overlapping windows require group or temporal split` | repeated groups under `stratified` | switch to `group`, `asset` or `temporal` |
| `positive_class … is not among the labels […]` | wrong label spelling or ordering | use a value from the message |
| `ROC-AUC unavailable: the holdout split does not contain all classes.` | one class is missing from the holdout | recognise it as a data problem (too few instances/assets), not a code problem |
| `ROC-AUC unavailable: this model does not provide probabilities.` | e.g. `probability=false` on the SVM | accept it, or set `probability=true` |
| `validation.compare` refuses the inputs | test indices differ between the runs | make features/labels/split/test_size/random_state identical |
| score collapses when moving from `group` to `asset` | that is the honest number; the instance-level one was optimistic | report both, lead with the asset number |
| `test_assets_unseen = 0` | every test asset also appeared in training | the "holdout" is not what was asked for; fix the split |

## 7. Execution

| Message / symptom | Reason | Fix |
| --- | --- | --- |
| `execute_pipeline` returns `RUNNING` and nothing else | execution is asynchronous | `wait_for_pipeline(pipeline_id, timeout_seconds=…)` |
| `timed_out: true` | the run is still going | wait again with a larger timeout, or check status/history |
| `success: true` but `status: FAILED` | control success ≠ run success | read `errors` / per-node status |
| node `SKIPPED` with `Upstream results unavailable` | an upstream node failed | fix upstream first; downstream will not run |
| `… cannot consume streamed input; insert data.materialize or turn streaming off on data.input` | a global component met a streamed dataset | as the message says; keep the warning in the report |
| run fails immediately after an edit | parameters are invalid for the new graph shape | `validate_pipeline` first, next time |
| `Invalid execution mode` | `mode` must be `all`, `node` or `from` | use `execute_node` / `execute_from_node` wrappers |
| a cancelled run leaves nodes `CANCELLED`/`PENDING` | cancellation is cooperative | re-run when ready |
| everything re-runs after a tiny change | `incremental=false`, or the changed node is upstream of everything | pass `incremental=true` (default) and edit the narrowest node |

## 8. Reading results

| Symptom | Reason | Fix |
| --- | --- | --- |
| the response is huge | `include_indices=true`, or `limit` too large | keep indices collapsed; metrics already contain counts and the confusion matrix |
| `preview` shorter than `shape` | `limit`/50-column caps by design | raise `limit` (max 100) only when the rows are the deliverable |
| `kind: streamed` instead of a table | streaming was on | expected; statistics stream, raw rows stay on disk |
| a plot is missing | `PlotArtifact`s are for the designer, not MCP | tell the user to open the web UI for the graph |
| warnings list is empty on a run you doubt | some checks only exist if you configured them (`data.quality`, asset columns, imputation) | add the check and re-run; absence of a warning is not proof |

---

## Guardrails, and why they exist

These are deliberate refusals rather than bugs. Knowing the intent avoids "fixing" them:

1. **Typed ports** — stop `Dataset` from silently entering a model as if it were features.
2. **One producer per input** — an ambiguous input is a modelling bug; `feature.merge` makes
   the join explicit and checks provenance.
3. **Provenance-checked merges** — prevent row misalignment, the classic silent failure.
4. **Asset split requirements** — the platform refuses to pretend an instance split is an
   asset split.
5. **`strict` label policy** — makes mixed-label windows visible instead of quietly averaging
   a fault onset away.
6. **`flat_policy=nan` by default** — keeps row alignment (so downstream merges still work)
   while making the degenerate channel visible.
7. **Warnings instead of silent success** — leakage, eviction, subset and stale-workspace
   notices are the platform being honest about what it did.
8. **Bounds on previews and results** — MCP payloads stay small enough for an agent to read;
   the artifacts remain addressable by reference for the runtime.
