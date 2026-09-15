"""Window features with aligned labels and source coverage for validation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable, Iterator

import numpy as np
import pandas as pd
from scipy import stats
from sklearn.metrics import r2_score
from sklearn.model_selection import KFold

from fault_core.data import numeric_columns
from fault_core.preprocessing import mark_fitted

STATISTICS = (
    "mean",
    "std",
    "variance",
    "min",
    "max",
    "median",
    "rms",
    "skewness",
    "kurtosis",
    "quantile",
    "range",
    "iqr",
    "mad",
    "peak",
    "crest_factor",
)

SPECTRAL = (
    "dominant_frequency",
    "dominant_amplitude",
    "spectral_centroid",
    "spectral_spread",
    "spectral_entropy",
    "spectral_rms",
    "high_frequency_ratio",
    "harmonic_ratio",
    "band_energy_ratio",
)

CATEGORICAL_METHODS = ("onehot", "ordinal", "frequency", "target", "category_statistics")
UNKNOWN_CATEGORY_POLICIES = ("ignore", "error")
MISSING_CATEGORY = "<missing>"

#: Shorter windows cannot resolve a usable spectrum, so they are rejected outright.
MIN_SPECTRAL_SAMPLES = 8


def windows(
    data: pd.DataFrame, group_column: str | None, window_size: int, step: int, time_column: str | None = None
) -> Iterator[tuple[str, pd.DataFrame, str, list[Any]]]:
    if not data.index.is_unique:
        raise ValueError("Source row index must be unique")
    groups = data.groupby(group_column, sort=False, dropna=False) if group_column else [("all", data)]
    for group_number, (group, frame) in enumerate(groups):
        for start, chunk, source_rows in windows_in_frame(frame, window_size, step, time_column):
            yield f"g{group_number}_w{start}", chunk, str(group), source_rows


def group_assets(data: pd.DataFrame, group_column: str, asset_column: str) -> dict[str, str]:
    """Map each window group to its asset (one well can contribute several instances)."""
    if asset_column not in data.columns:
        raise ValueError(f"Missing asset column: {asset_column}")
    pairs = data[[group_column, asset_column]].drop_duplicates()
    conflicting = pairs.groupby(group_column)[asset_column].nunique()
    if (conflicting > 1).any():
        raise ValueError(f"Groups map to several assets via {asset_column}: {conflicting[conflicting > 1].index[:5].tolist()}")
    return {str(row[group_column]): str(row[asset_column]) for _, row in pairs.iterrows()}


def windows_in_frame(
    frame: pd.DataFrame, window_size: int, step: int, time_column: str | None = None
) -> Iterator[tuple[int, pd.DataFrame, list[Any]]]:
    """Complete windows of one group; shared by the batch and streaming paths."""
    # Sorting copies the whole group; skip it when the rows are already ordered.
    if time_column and not frame[time_column].is_monotonic_increasing:
        frame = frame.sort_values(time_column, kind="stable")
    size = window_size or len(frame)
    if not size:
        return
    stride = step or size
    for start in range(0, len(frame) - size + 1, stride):
        chunk = frame.iloc[start : start + size]
        yield start, chunk, chunk.index.tolist()


def _stat(x: np.ndarray, name: str, quantile: float) -> float:
    rms = float(np.sqrt(np.mean(x**2)))
    funcs = {
        "mean": lambda: np.mean(x),
        "std": lambda: np.std(x),
        "variance": lambda: np.var(x),
        "min": lambda: np.min(x),
        "max": lambda: np.max(x),
        "median": lambda: np.median(x),
        "rms": lambda: rms,
        "skewness": lambda: stats.skew(x) if np.ptp(x) else 0.0,
        "kurtosis": lambda: stats.kurtosis(x) if np.ptp(x) else 0.0,
        "quantile": lambda: np.quantile(x, quantile),
        "range": lambda: np.ptp(x),
        "iqr": lambda: np.quantile(x, 0.75) - np.quantile(x, 0.25),
        "mad": lambda: np.median(np.abs(x - np.median(x))),
        "peak": lambda: np.max(np.abs(x)),
        "crest_factor": lambda: np.max(np.abs(x)) / rms if rms else 0.0,
    }
    return float(funcs[name]())


def _window_label(chunk: pd.DataFrame, label_column: str, label_policy: str) -> Any:
    y = chunk[label_column]
    if y.isna().any():
        raise ValueError("Labels contain missing values")
    if label_policy == "strict" and y.nunique() != 1:
        raise ValueError("Mixed labels in a window; reduce window or select an explicit label_policy")
    return y.mode().iloc[0] if label_policy == "mode" else y.iloc[-1]


def _feature_row(
    chunk: pd.DataFrame,
    cols: list[str],
    kind: str,
    chosen: list[str],
    quantile: float,
    degree: int,
    fitting_method: str,
    time_column: str | None,
) -> dict[str, float]:
    """One row of features for one window; shared by the batch and streaming paths."""
    row: dict[str, float] = {}
    for col in cols:
        values = chunk[col].to_numpy(dtype=float)
        if kind == "statistical":
            row.update({f"{col}__{name}": _stat(values, name, quantile) for name in chosen})
            continue
        if time_column:
            raw_t = chunk[time_column]
            if pd.api.types.is_numeric_dtype(raw_t):
                t = raw_t.to_numpy(dtype=float)
            else:
                t = pd.to_datetime(raw_t).astype("int64").to_numpy(dtype=float) / 1e9
            t = t - t[0]
        else:
            t = np.arange(len(values), dtype=float)
        deg = degree if fitting_method == "polynomial" else 1
        if len(values) <= deg or len(np.unique(t)) <= deg:
            raise ValueError("Fitting window needs more distinct time points than polynomial degree")
        if fitting_method == "exponential" and (values <= 0).any():
            raise ValueError("Exponential fitting requires positive values")
        coefficients = np.polyfit(t, np.log(values) if fitting_method == "exponential" else values, deg)
        fitted = np.polyval(coefficients, t)
        if fitting_method == "exponential":
            fitted = np.exp(fitted)
        residual = values - fitted
        for i, coefficient in enumerate(coefficients):
            row[f"{col}__coef_{deg - i}"] = float(coefficient)
        row.update(
            {
                f"{col}__r2": float(r2_score(values, fitted)),
                f"{col}__trend_strength": float(max(0, 1 - np.var(residual) / np.var(values)))
                if np.var(values)
                else 0.0,
                f"{col}__residual_mean": float(np.mean(residual)),
                f"{col}__residual_std": float(np.std(residual)),
            }
        )
    return row


def _assemble(
    rows: list[dict[str, float]],
    keys: list[str],
    labels: list[Any],
    group_ids: list[str],
    coverage: list[list[Any]],
    attrs: dict[str, Any],
    group_column: str | None,
    label_column: str | None,
    window_size: int,
    step: int,
    coverage_as_ranges: bool = False,
) -> dict[str, Any]:
    if not rows:
        raise ValueError("No complete feature windows; reduce window_size")
    result = pd.DataFrame(rows, index=pd.Index(keys, name="window_id"))
    result.attrs = {
        **attrs,
        "groups": group_ids,
        "grouped": bool(group_column),
        "window_size": window_size,
        "step": step,
        "overlapping": bool(window_size and step and step < window_size),
    }
    # Streamed extraction records [start, stop) ranges; a full list of label objects
    # per window would cost ~36 bytes per source row and dominate memory.
    if coverage_as_ranges:
        result.attrs["source_rows_ranges"] = coverage
    else:
        result.attrs["source_rows"] = coverage
    outputs: dict[str, Any] = {"features": result}
    if label_column:
        outputs["labels"] = pd.Series(labels, index=result.index, name=label_column)
        outputs["labels"].attrs = dict(result.attrs)
    return outputs


def extract_features(
    data: pd.DataFrame,
    columns: list[str],
    kind: str = "statistical",
    features: list[str] | None = None,
    group_column: str | None = None,
    label_column: str | None = None,
    time_column: str | None = None,
    window_size: int = 0,
    step: int = 0,
    label_policy: str = "strict",
    quantile: float = 0.75,
    degree: int = 2,
    fitting_method: str = "linear",
    asset_column: str | None = None,
) -> dict[str, Any]:
    cols = numeric_columns(data, columns)
    if label_column in cols or group_column in cols:
        raise ValueError("Label/group columns cannot be feature inputs")
    if not np.isfinite(data[cols].to_numpy(dtype=float, copy=False)).all():
        raise ValueError("Feature extraction requires finite numeric values")
    chosen = features or ["mean", "std", "rms"]
    asset_of = group_assets(data, group_column, asset_column) if asset_column and group_column else None
    rows, labels, keys, group_ids, coverage = [], [], [], [], []
    for key, chunk, group, source_rows in windows(data, group_column, window_size, step, time_column):
        row = _feature_row(chunk, cols, kind, chosen, quantile, degree, fitting_method, time_column)
        if label_column:
            labels.append(_window_label(chunk, label_column, label_policy))
        rows.append(row)
        keys.append(key)
        group_ids.append(group)
        coverage.append(source_rows)
    outputs = _assemble(
        rows,
        keys,
        labels,
        group_ids,
        coverage,
        dict(data.attrs),
        group_column,
        label_column,
        window_size,
        step,
    )
    if asset_of:
        outputs["features"].attrs["assets"] = [asset_of[group] for group in group_ids]
        if "labels" in outputs:
            outputs["labels"].attrs["assets"] = list(outputs["features"].attrs["assets"])
    return outputs


def _spectral_names(chosen: list[str], edges: list[float]) -> list[str]:
    """Every column a spectral row would contain, so flat windows can emit aligned NaNs."""
    names: list[str] = []
    for name in chosen:
        if name == "band_energy_ratio":
            names.extend(f"band_energy_ratio_{index}" for index in range(len(edges) + 1))
        else:
            names.append(name)
    return names


def _flat_warnings(flat_counts: dict[str, int], window_count: int) -> list[str]:
    return [
        f"{col}: {count} of {window_count} windows are flat (constant); spectral features are NaN there."
        for col, count in flat_counts.items()
        if count
    ]


def _stream_runs(chunk: pd.DataFrame, group_column: str) -> Iterator[tuple[Any, pd.DataFrame]]:
    """Maximal runs of one group value inside a chunk (vectorised boundary detection)."""
    values = chunk[group_column]
    shifted = values.shift()
    changed = values.ne(shifted) & ~(values.isna() & shifted.isna())
    if len(changed):
        changed.iloc[0] = True
    starts = np.flatnonzero(changed.to_numpy())
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(values)
        yield values.iloc[start], chunk.iloc[start:end]


def _stream_windows(
    chunks: Iterable[pd.DataFrame],
    columns: list[str],
    build_row: Callable[[pd.DataFrame, list[str]], dict[str, float]],
    group_column: str | None,
    label_column: str | None,
    time_column: str | None,
    window_size: int,
    step: int,
    label_policy: str,
    attrs: dict[str, Any],
    min_window_rows: int = 1,
    on_progress: Callable[[int], None] | None = None,
) -> dict[str, Any]:
    """Window features over a chunk iterator without ever holding the whole input.

    Memory is bounded by one chunk plus the largest single group (grouped mode), or by
    the rolling window buffer (group-less mode). Rows must already be ordered: by group
    (contiguous) and, when ``time_column`` is given, by time inside each group.
    """
    rows: list[dict[str, float]] = []
    labels: list[Any] = []
    keys: list[str] = []
    group_ids: list[str] = []
    coverage: list[list[Any]] = []
    processed = 0
    cols: list[str] = []

    def add_window(group_number: int, start: int, window: pd.DataFrame, group_label: str) -> None:
        if len(window) < min_window_rows:
            raise ValueError(f"Window needs at least {min_window_rows} samples")
        if time_column and not window[time_column].is_monotonic_increasing:
            raise ValueError(
                "Streamed feature extraction requires rows ordered by time within each group; "
                "sort the file first or disable streaming on the data input"
            )
        row = build_row(window, cols)
        if row is None:  # the builder asked to skip this window
            return
        rows.append(row)
        keys.append(f"g{group_number}_w{start}")
        group_ids.append(group_label)
        coverage.append([int(window.index[0]), int(window.index[-1]) + 1])
        if label_column:
            labels.append(_window_label(window, label_column, label_policy))

    def check_chunk(chunk: pd.DataFrame) -> list[str]:
        found = numeric_columns(chunk, columns)
        if label_column in found or group_column in found:
            raise ValueError("Label/group columns cannot be feature inputs")
        if not np.isfinite(chunk[found].to_numpy(dtype=float, copy=False)).all():
            raise ValueError("Feature extraction requires finite numeric values")
        return found

    if group_column is None:
        carry: pd.DataFrame | None = None
        offset = 0
        for chunk in chunks:
            cols = check_chunk(chunk)
            processed += len(chunk)
            frame = chunk if carry is None else pd.concat([carry, chunk])
            last_start = None
            for start, window, source_rows in windows_in_frame(frame, window_size, step, None):
                add_window(0, offset + start, window, "all")
                last_start = start
            stride = step or window_size or len(frame)
            if last_start is None:
                carry = frame
            else:
                consumed = last_start + stride
                carry = frame.iloc[consumed:] if consumed < len(frame) else None
                offset += consumed
            if on_progress is not None:
                on_progress(processed)
    else:
        pending: list[pd.DataFrame] = []
        pending_group: Any = None
        group_number = -1
        seen: set[Any] = set()
        for chunk in chunks:
            cols = check_chunk(chunk)
            processed += len(chunk)
            for group_value, run in _stream_runs(chunk, group_column):
                key = group_value
                if pending_group is None or key != pending_group:
                    if pending:
                        frame = pending[0] if len(pending) == 1 else pd.concat(pending)
                        for start, window, _ in windows_in_frame(frame, window_size, step, time_column):
                            add_window(group_number, start, window, str(pending_group))
                    pending, pending_group = [run], key
                    if key in seen:
                        raise ValueError(
                            "Streamed input requires each group's rows to be contiguous; "
                            "sort the file by the group column first"
                        )
                    seen.add(key)
                    group_number += 1
                else:
                    pending.append(run)
            if on_progress is not None:
                on_progress(processed)
        if pending:
            frame = pending[0] if len(pending) == 1 else pd.concat(pending)
            for start, window, _ in windows_in_frame(frame, window_size, step, time_column):
                add_window(group_number, start, window, str(pending_group))

    attrs = {**attrs, "streamed_rows": processed, "streaming": True}
    return _assemble(
        rows,
        keys,
        labels,
        group_ids,
        coverage,
        attrs,
        group_column,
        label_column,
        window_size,
        step,
        coverage_as_ranges=True,
    )


def expand_coverage(attrs: dict[str, Any]) -> list[list[Any]]:
    """Materialise per-window source rows (from a list or from streamed ranges)."""
    ranges = attrs.get("source_rows_ranges")
    if ranges is not None:
        return [list(range(start, stop)) for start, stop in ranges]
    return [list(rows) for rows in attrs.get("source_rows", [])]


def _ranges(attrs: dict[str, Any]) -> list[tuple[int, int]] | None:
    ranges = attrs.get("source_rows_ranges")
    if ranges is None:
        return None
    return [(int(start), int(stop)) for start, stop in ranges]


def _close_ranges(values: list[Any]) -> list[tuple[int, int]]:
    """Compress a concrete label list into [start, stop) intervals."""
    ordered = sorted(int(value) for value in values)
    merged: list[tuple[int, int]] = []
    for value in ordered:
        if merged and value == merged[-1][1]:
            merged[-1] = (merged[-1][0], value + 1)
        else:
            merged.append((value, value + 1))
    return merged


def _merge_intervals(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Merge overlapping or adjacent [start, stop) intervals."""
    merged: list[tuple[int, int]] = []
    for start, stop in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], stop))
        else:
            merged.append((start, stop))
    return merged


def _intervals(attrs: dict[str, Any]) -> list[tuple[int, int]]:
    """One merged interval list per side, without expanding rows where possible."""
    ranges = _ranges(attrs)
    if ranges is not None:
        return _merge_intervals(ranges)
    return _close_ranges([row for window in attrs.get("source_rows", []) for row in window])


def windows_share_rows(left_attrs: dict[str, Any], right_attrs: dict[str, Any]) -> bool:
    """True when any window of one side overlaps any window of the other side."""
    left_intervals, right_intervals = _intervals(left_attrs), _intervals(right_attrs)
    index = 0
    for start, stop in left_intervals:
        while index < len(right_intervals) and right_intervals[index][1] <= start:
            index += 1
        if index < len(right_intervals) and right_intervals[index][0] < stop:
            return True
    return False


def rows_without_overlap(attrs: dict[str, Any], held_out: list[int], candidates: list[int]) -> list[int]:
    """Filter window positions that do not share source rows with the held-out windows."""
    ranges = _ranges(attrs)
    if ranges is None:
        coverage = expand_coverage(attrs)
        blocked = {row for position in held_out for row in coverage[position]}
        return [position for position in candidates if not blocked.intersection(coverage[position])]
    held_out_ranges = _merge_intervals([ranges[position] for position in held_out])
    kept: list[int] = []
    index = 0
    for position in candidates:
        start, stop = ranges[position]
        while index < len(held_out_ranges) and held_out_ranges[index][1] <= start:
            index += 1
        if not (index < len(held_out_ranges) and held_out_ranges[index][0] < stop):
            kept.append(position)
    return kept


def provenance_matches(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """Compare provenance across batch (label lists) and streamed (ranges) features."""
    left_ranges, right_ranges = _ranges(left), _ranges(right)
    if left_ranges is not None and right_ranges is not None:
        return left_ranges == right_ranges
    if left_ranges is None and right_ranges is None:
        return left.get("source_rows") == right.get("source_rows")
    return expand_coverage(left) == expand_coverage(right)


def coverage_subset(attrs: dict[str, Any], positions: list[int]) -> dict[str, Any]:
    """Cheap sub-view of the provenance for a set of window positions."""
    ranges = attrs.get("source_rows_ranges")
    if ranges is not None:
        return {"source_rows_ranges": [ranges[position] for position in positions]}
    source_rows = attrs.get("source_rows", [])
    return {"source_rows": [source_rows[position] for position in positions]}


def extract_features_stream(
    chunks: Iterable[pd.DataFrame],
    columns: list[str],
    kind: str = "statistical",
    features: list[str] | None = None,
    group_column: str | None = None,
    label_column: str | None = None,
    time_column: str | None = None,
    window_size: int = 0,
    step: int = 0,
    label_policy: str = "strict",
    quantile: float = 0.75,
    degree: int = 2,
    fitting_method: str = "linear",
    attrs: dict[str, Any] | None = None,
    on_progress: Callable[[int], None] | None = None,
) -> dict[str, Any]:
    """Streaming counterpart of :func:`extract_features`; identical output schema."""
    chosen = features or ["mean", "std", "rms"]

    def build_row(window: pd.DataFrame, cols: list[str]) -> dict[str, float]:
        return _feature_row(window, cols, kind, chosen, quantile, degree, fitting_method, time_column)

    return _stream_windows(
        chunks,
        columns,
        build_row,
        group_column,
        label_column,
        time_column,
        window_size,
        step,
        label_policy,
        attrs or {},
        on_progress=on_progress,
    )


def spectral_stream(
    chunks: Iterable[pd.DataFrame],
    columns: list[str],
    sampling_rate: float,
    features: list[str] | None = None,
    band_edges: list[float] | None = None,
    harmonic_tolerance: float = 0.02,
    flat_policy: str = "nan",
    flat_threshold: float = 0.0,
    group_column: str | None = None,
    label_column: str | None = None,
    time_column: str | None = None,
    window_size: int = 0,
    step: int = 0,
    label_policy: str = "strict",
    attrs: dict[str, Any] | None = None,
    on_progress: Callable[[int], None] | None = None,
) -> dict[str, Any]:
    """Streaming counterpart of :func:`spectral`; identical output schema."""
    if not isinstance(sampling_rate, (int, float)) or isinstance(sampling_rate, bool):
        raise ValueError("Sampling rate must be numeric")
    if not np.isfinite(sampling_rate) or sampling_rate <= 0:
        raise ValueError("Sampling rate must be a positive, finite number of samples per second")
    if not 0 <= harmonic_tolerance <= 0.5:
        raise ValueError("Harmonic tolerance must be between 0 and 0.5")
    edges = [float(edge) for edge in ([0.25, 0.5] if band_edges is None else band_edges)]
    if any(not np.isfinite(edge) or not 0 < edge < 1 for edge in edges):
        raise ValueError("Band edges must be fractions of the Nyquist frequency between 0 and 1")
    if edges != sorted(set(edges)):
        raise ValueError("Band edges must increase without duplicates")
    if flat_policy not in {"nan", "skip", "error"}:
        raise ValueError("flat_policy must be one of nan, skip, error")
    if not np.isfinite(flat_threshold) or flat_threshold < 0:
        raise ValueError("flat_threshold must be a non-negative, finite number")
    chosen = features or ["dominant_frequency", "spectral_centroid", "spectral_entropy", "band_energy_ratio"]
    unknown = set(chosen) - set(SPECTRAL)
    if unknown:
        raise ValueError(f"Unknown spectral features: {sorted(unknown)}")

    flat_counts: dict[str, int] = {}
    window_count = [0]

    def build_row(window: pd.DataFrame, cols: list[str]) -> dict[str, float] | None:
        window_count[0] += 1
        series = {col: window[col].to_numpy(dtype=float) for col in cols}
        flat_here = _flat_columns(series, flat_threshold)
        if flat_here:
            for col in flat_here:
                flat_counts[col] = flat_counts.get(col, 0) + 1
            if flat_policy == "error":
                raise ValueError(
                    f"Flat (constant) window for {flat_here}; a spectrum needs variation. "
                    "Use flat_policy=nan (keeps row alignment) or skip, or drop quantised channels."
                )
            if flat_policy == "skip":
                return None
        row: dict[str, float] = {}
        for col in cols:
            values = series[col]
            if col in flat_here:
                for name in _spectral_names(chosen, edges):
                    row[f"{col}__{name}"] = float("nan")
                continue
            row.update(
                {
                    f"{col}__{name}": value
                    for name, value in _spectral_row(
                        values, float(sampling_rate), chosen, edges, harmonic_tolerance
                    ).items()
                }
            )
        return row

    outputs = _stream_windows(
        chunks,
        columns,
        build_row,
        group_column,
        label_column,
        time_column,
        window_size,
        step,
        label_policy,
        attrs or {},
        min_window_rows=MIN_SPECTRAL_SAMPLES,
        on_progress=on_progress,
    )
    outputs["warnings"] = _flat_warnings(flat_counts, window_count[0])
    return outputs


def _spectrum(
    values: np.ndarray, sampling_rate: float
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Return frequencies, peak amplitudes, the raw spectrum and the applied taper."""
    taper = np.hanning(values.size)
    coherent_gain = float(taper.sum())
    # Removing the mean keeps the DC bin from dominating amplitude-based features.
    spectrum = np.fft.rfft((values - values.mean()) * taper)
    frequencies = np.fft.rfftfreq(values.size, d=1.0 / sampling_rate)
    amplitude = 2.0 * np.abs(spectrum) / coherent_gain
    amplitude[0] /= 2  # DC is not a doubled two-sided component.
    if values.size % 2 == 0:
        amplitude[-1] /= 2  # Nyquist bin is likewise single-sided.
    return frequencies, amplitude, spectrum, taper


def _signal_power(spectrum: np.ndarray, taper: np.ndarray) -> float:
    """Parseval-consistent mean square of the original, unwindowed signal."""
    interior = spectrum[1:-1] if taper.size % 2 == 0 else spectrum[1:]
    total = float(np.abs(spectrum[0]) ** 2) + 2.0 * float(np.sum(np.abs(interior) ** 2))
    if taper.size % 2 == 0:
        total += float(np.abs(spectrum[-1]) ** 2)
    return total / (taper.size * float(np.sum(taper**2)))


def _spectral_row(
    values: np.ndarray,
    sampling_rate: float,
    chosen: list[str],
    band_edges: list[float],
    harmonic_tolerance: float,
) -> dict[str, float]:
    frequencies, amplitude, spectrum, taper = _spectrum(values, sampling_rate)
    power = np.abs(spectrum) ** 2
    # The mean is removed before the FFT, so the DC bin carries no fault-relevant energy.
    shape = np.where(frequencies > 0, power, 0.0)
    energy = float(shape.sum())
    if energy <= 0:
        raise ValueError("Spectral features need a signal with non-zero variance")
    weights = shape / energy
    peak = int(np.argmax(np.where(frequencies > 0, amplitude, -np.inf)))
    centroid = float(np.sum(frequencies * weights))
    positive = weights[weights > 0]
    nyquist = sampling_rate / 2
    harmonics = np.zeros_like(frequencies, dtype=bool)
    for order in range(2, 6):
        target = order * frequencies[peak]
        if 0 < target < nyquist:
            harmonics |= np.abs(frequencies - target) <= harmonic_tolerance * target
    band_energy = []
    boundaries = [0.0, *band_edges, 1.0]
    for low, high in zip(boundaries[:-1], boundaries[1:], strict=True):
        band = (frequencies > low * nyquist) & (frequencies <= high * nyquist)
        band_energy.append(float(shape[band].sum() / energy))
    row = {
        "dominant_frequency": float(frequencies[peak]),
        "dominant_amplitude": float(amplitude[peak]),
        "spectral_centroid": centroid,
        "spectral_spread": float(np.sqrt(np.sum((frequencies - centroid) ** 2 * weights))),
        "spectral_entropy": float(-np.sum(positive * np.log(positive)) / np.log(positive.size)),
        "spectral_rms": float(np.sqrt(_signal_power(spectrum, taper))),
        "high_frequency_ratio": float(shape[frequencies > nyquist / 2].sum() / energy),
        "harmonic_ratio": float(shape[harmonics].sum() / energy),
        "band_energy_ratio": band_energy,
    }
    flat: dict[str, float] = {}
    for name in chosen:
        if name == "band_energy_ratio":
            flat.update({f"band_energy_ratio_{i}": value for i, value in enumerate(band_energy)})
        else:
            flat[name] = row[name]
    return flat


def _flat_columns(values: dict[str, np.ndarray], flat_threshold: float) -> list[str]:
    """Columns whose window carries no variation: quantised/held process values."""
    return [name for name, window in values.items() if float(np.ptp(window)) <= flat_threshold]


def spectral(
    data: pd.DataFrame,
    columns: list[str],
    sampling_rate: float,
    features: list[str] | None = None,
    band_edges: list[float] | None = None,
    harmonic_tolerance: float = 0.02,
    flat_policy: str = "nan",
    flat_threshold: float = 0.0,
    group_column: str | None = None,
    label_column: str | None = None,
    time_column: str | None = None,
    window_size: int = 0,
    step: int = 0,
    label_policy: str = "strict",
) -> dict[str, Any]:
    """Extract FFT amplitude features per window, with the same window labels as other features."""
    if not isinstance(sampling_rate, (int, float)) or isinstance(sampling_rate, bool):
        raise ValueError("Sampling rate must be numeric")
    if not np.isfinite(sampling_rate) or sampling_rate <= 0:
        raise ValueError("Sampling rate must be a positive, finite number of samples per second")
    if not 0 <= harmonic_tolerance <= 0.5:
        raise ValueError("Harmonic tolerance must be between 0 and 0.5")
    edges = [float(edge) for edge in ([0.25, 0.5] if band_edges is None else band_edges)]
    if any(not np.isfinite(edge) or not 0 < edge < 1 for edge in edges):
        raise ValueError("Band edges must be fractions of the Nyquist frequency between 0 and 1")
    if edges != sorted(set(edges)):
        raise ValueError("Band edges must increase without duplicates")
    if flat_policy not in {"nan", "skip", "error"}:
        raise ValueError("flat_policy must be one of nan, skip, error")
    if not np.isfinite(flat_threshold) or flat_threshold < 0:
        raise ValueError("flat_threshold must be a non-negative, finite number")
    chosen = features or ["dominant_frequency", "spectral_centroid", "spectral_entropy", "band_energy_ratio"]
    unknown = set(chosen) - set(SPECTRAL)
    if unknown:
        raise ValueError(f"Unknown spectral features: {sorted(unknown)}")
    cols = numeric_columns(data, columns)
    if label_column in cols or group_column in cols:
        raise ValueError("Label/group columns cannot be feature inputs")
    if not np.isfinite(data[cols].to_numpy(dtype=float, copy=False)).all():
        raise ValueError("Spectral extraction requires finite numeric values")
    rows, labels, keys, group_ids, coverage = [], [], [], [], []
    flat_counts = {col: 0 for col in cols}
    window_count = 0
    for key, chunk, group, source_rows in windows(data, group_column, window_size, step, time_column):
        if len(chunk) < MIN_SPECTRAL_SAMPLES:
            raise ValueError(f"Spectral extraction needs at least {MIN_SPECTRAL_SAMPLES} samples per window")
        window_count += 1
        series = {col: chunk[col].to_numpy(dtype=float) for col in cols}
        flat_here = _flat_columns(series, flat_threshold)
        if flat_here:
            for col in flat_here:
                flat_counts[col] += 1
            if flat_policy == "error":
                raise ValueError(
                    f"Flat (constant) window for {flat_here}; a spectrum needs variation. "
                    "Use flat_policy=nan (keeps row alignment) or skip, or drop quantised channels."
                )
            if flat_policy == "skip":
                continue
        row: dict[str, float] = {}
        for col in cols:
            values = series[col]
            if col in flat_here:
                # NaN keeps row alignment with the other feature branches, so merge and
                # validation still line up; a skipped row would silently break them.
                for name in _spectral_names(chosen, edges):
                    row[f"{col}__{name}"] = float("nan")
                continue
            row.update(
                {
                    f"{col}__{name}": value
                    for name, value in _spectral_row(
                        values, float(sampling_rate), chosen, edges, harmonic_tolerance
                    ).items()
                }
            )
        if label_column:
            labels.append(_window_label(chunk, label_column, label_policy))
        rows.append(row)
        keys.append(key)
        group_ids.append(group)
        coverage.append(source_rows)
    outputs = _assemble(
        rows,
        keys,
        labels,
        group_ids,
        coverage,
        dict(data.attrs),
        group_column,
        label_column,
        window_size,
        step,
    )
    outputs["warnings"] = _flat_warnings(flat_counts, window_count)
    return outputs


def _categorical_values(data: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    if not columns or len(columns) != len(set(columns)):
        raise ValueError("Select at least one unique categorical column")
    missing = set(columns) - set(data.columns)
    if missing:
        raise ValueError(f"Missing columns: {sorted(missing)}")
    return data[columns].fillna(MISSING_CATEGORY).astype(str)


@dataclass(frozen=True)
class CategoricalEncoder:
    """Fitted categorical mappings with a deterministic inference schema.

    The object contains plain Python state, so it survives the platform's deepcopy,
    checkpoint and pickle-based artifact spill paths. ``transform`` never refits on
    inference data: missing training categories keep their columns, and unseen values
    follow ``handle_unknown``.
    """

    columns: tuple[str, ...]
    method: str
    handle_unknown: str
    categories: dict[str, tuple[str, ...]]
    output_columns: tuple[str, ...]
    frequencies: dict[str, dict[str, float]]
    counts: dict[str, dict[str, float]]
    target_means: dict[str, dict[str, float]]
    target_global_mean: float | None = None

    def signature(self) -> tuple[Any, ...]:
        return self.columns, self.method, self.output_columns

    def describe(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "input_columns": list(self.columns),
            "output_columns": list(self.output_columns),
            "category_counts": {column: len(values) for column, values in self.categories.items()},
            "handle_unknown": self.handle_unknown,
        }

    def _unknown(self, values: pd.DataFrame) -> dict[str, list[str]]:
        unknown: dict[str, list[str]] = {}
        for column in self.columns:
            extra = sorted(set(values[column].unique()) - set(self.categories[column]))
            if extra:
                unknown[column] = extra[:10]
        return unknown

    def transform(self, data: pd.DataFrame) -> pd.DataFrame:
        values = _categorical_values(data, list(self.columns))
        unknown = self._unknown(values)
        if unknown and self.handle_unknown == "error":
            raise ValueError(f"Unknown categories at transform time: {unknown}")

        if self.method == "onehot":
            result = pd.get_dummies(values, dtype=float)
            if not result.columns.is_unique:
                raise ValueError("Categorical column names and values produce duplicate one-hot features")
            result = result.reindex(columns=list(self.output_columns), fill_value=0.0).astype(float)
        else:
            result = pd.DataFrame(index=data.index)
            for column in self.columns:
                series = values[column]
                if self.method == "ordinal":
                    mapping = {value: index for index, value in enumerate(self.categories[column])}
                    result[f"{column}__ordinal"] = series.map(mapping).fillna(-1).astype("int64")
                elif self.method in {"frequency", "category_statistics"}:
                    result[f"{column}__frequency"] = (
                        series.map(self.frequencies[column]).fillna(0.0).astype(float)
                    )
                    if self.method == "category_statistics":
                        result[f"{column}__count"] = series.map(self.counts[column]).fillna(0.0).astype(float)
                elif self.method == "target":
                    result[f"{column}__target"] = (
                        series.map(self.target_means[column]).fillna(self.target_global_mean).astype(float)
                    )
                else:  # defensive guard for manually constructed/persisted objects
                    raise ValueError(f"Unknown categorical method: {self.method}")
            result = result.loc[:, list(self.output_columns)]

        result.attrs = dict(data.attrs)
        encoders = list(result.attrs.get("categorical_encoders", []))
        known = {
            encoder.signature()
            for encoder in encoders
            if isinstance(encoder, CategoricalEncoder)
        }
        if self.signature() not in known:
            encoders.append(self)
        result.attrs["categorical_encoders"] = encoders
        return result


def fit_categorical_encoder(
    data: pd.DataFrame,
    columns: list[str],
    method: str = "onehot",
    target_column: str | None = None,
    handle_unknown: str = "ignore",
) -> CategoricalEncoder:
    if method not in CATEGORICAL_METHODS:
        raise ValueError(f"Unknown categorical method: {method}")
    if handle_unknown not in UNKNOWN_CATEGORY_POLICIES:
        raise ValueError(f"handle_unknown must be one of {UNKNOWN_CATEGORY_POLICIES}")
    if target_column in columns:
        raise ValueError("Target cannot be encoded as an input")
    values = _categorical_values(data, columns)
    categories = {column: tuple(sorted(values[column].unique())) for column in columns}
    frequencies = {
        column: {str(key): float(value) for key, value in values[column].value_counts(normalize=True).items()}
        for column in columns
    }
    counts = {
        column: {str(key): float(value) for key, value in values[column].value_counts().items()}
        for column in columns
    }
    target_means: dict[str, dict[str, float]] = {}
    target_global_mean = None

    if method == "onehot":
        encoded = pd.get_dummies(values, dtype=float)
        if not encoded.columns.is_unique:
            raise ValueError("Categorical column names and values produce duplicate one-hot features")
        output_columns = tuple(str(column) for column in encoded.columns)
    elif method == "ordinal":
        output_columns = tuple(f"{column}__ordinal" for column in columns)
    elif method == "frequency":
        output_columns = tuple(f"{column}__frequency" for column in columns)
    elif method == "category_statistics":
        output_columns = tuple(
            name for column in columns for name in (f"{column}__frequency", f"{column}__count")
        )
    else:
        if not target_column or len(data) < 4:
            raise ValueError("Target encoding requires a numeric target and at least four rows")
        if target_column not in data.columns:
            raise ValueError(f"Missing target column: {target_column}")
        target = pd.to_numeric(data[target_column], errors="raise")
        if not np.isfinite(target.to_numpy(dtype=float, copy=False)).all():
            raise ValueError("Target encoding requires a finite numeric target")
        target_global_mean = float(target.mean())
        for column in columns:
            means = target.groupby(values[column]).mean()
            target_means[column] = {str(key): float(value) for key, value in means.items()}
        output_columns = tuple(f"{column}__target" for column in columns)

    return CategoricalEncoder(
        tuple(columns),
        method,
        handle_unknown,
        categories,
        output_columns,
        frequencies,
        counts,
        target_means,
        target_global_mean,
    )


def fit_categorical(
    data: pd.DataFrame,
    columns: list[str],
    method: str = "onehot",
    target_column: str | None = None,
    random_state: int = 42,
    handle_unknown: str = "ignore",
) -> dict[str, Any]:
    """Fit once, return training features and the reusable fitted encoder."""
    encoder = fit_categorical_encoder(data, columns, method, target_column, handle_unknown)
    if method != "target":
        result = encoder.transform(data)
    else:
        values = _categorical_values(data, columns)
        target = pd.to_numeric(data[target_column], errors="raise")
        result = pd.DataFrame(index=data.index)
        for column in columns:
            encoded = pd.Series(index=data.index, dtype=float)
            # Preserve the previous out-of-fold training behaviour while the encoder
            # retains full training mappings for future transform-only inference.
            for train, test in KFold(min(5, len(data)), shuffle=True, random_state=random_state).split(data):
                means = target.iloc[train].groupby(values[column].iloc[train]).mean()
                encoded.iloc[test] = values[column].iloc[test].map(means).fillna(target.iloc[train].mean())
            result[f"{column}__target"] = encoded
        result.attrs = dict(data.attrs)
        result.attrs["categorical_encoders"] = [encoder]
    return {"features": mark_fitted(result, f"categorical.{method}"), "encoder": encoder}


def categorical(
    data: pd.DataFrame,
    columns: list[str],
    method: str = "onehot",
    target_column: str | None = None,
    random_state: int = 42,
    handle_unknown: str = "ignore",
) -> pd.DataFrame:
    """Backward-compatible fit/transform shortcut returning only training features."""
    return fit_categorical(data, columns, method, target_column, random_state, handle_unknown)["features"]


def merge_features(left: pd.DataFrame, right: pd.DataFrame) -> pd.DataFrame:
    if not left.index.equals(right.index):
        raise ValueError("Feature indices must match exactly")
    if not provenance_matches(left.attrs, right.attrs):
        raise ValueError("Feature provenance differs: source_rows")
    for key in ("groups", "source_path", "source_id"):
        if left.attrs.get(key) != right.attrs.get(key):
            raise ValueError(f"Feature provenance differs: {key}")
    if set(left.columns) & set(right.columns):
        raise ValueError("Feature names overlap; rename before merging")
    result = pd.concat([left, right], axis=1)
    result.attrs = dict(left.attrs)
    result.attrs["evaluation_warnings"] = list(
        dict.fromkeys(left.attrs.get("evaluation_warnings", []) + right.attrs.get("evaluation_warnings", []))
    )
    encoders: list[CategoricalEncoder] = []
    signatures: set[tuple[Any, ...]] = set()
    for encoder in [
        *left.attrs.get("categorical_encoders", []),
        *right.attrs.get("categorical_encoders", []),
    ]:
        if not isinstance(encoder, CategoricalEncoder) or encoder.signature() in signatures:
            continue
        encoders.append(encoder)
        signatures.add(encoder.signature())
    if encoders:
        result.attrs["categorical_encoders"] = encoders
    return result
