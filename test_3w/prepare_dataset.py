"""Download a bounded, real-only slice of the 3W dataset and reshape it for the platform.

The 3W dataset (Petrobras, CC BY 4.0) stores one Parquet file per event instance, with 27
process variables, a ``class`` column and a ``state`` column, indexed at 1 Hz.  This script:

1. lists the real instances (``WELL-*``) per class through the GitHub API;
2. keeps only instances that actually record the CORE sensors (availability varies per
   well, and an all-NaN column cannot be filled without inventing a reading);
3. tops up downloads until every class is represented;
4. writes one table with the group/time/label columns the platform expects.

Stages: inventory | download | topup | build | all
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

import pandas as pd

REPO = "petrobras/3W"
REF = "main"
API = f"https://api.github.com/repos/{REPO}/contents/dataset"
RAW = f"https://raw.githubusercontent.com/{REPO}/{REF}/dataset"

HERE = Path(__file__).resolve().parent
RAW_DIR = HERE / "raw"
OUT_DIR = HERE / "data"
INDEX = RAW_DIR / "index.json"

#: Instances targeted per class.  0 is normal operation, 1..9 are the fault types.
PER_CLASS = {0: 4, 1: 3, 2: 3, 3: 3, 4: 3, 5: 2, 6: 2, 7: 3, 8: 3, 9: 2}

#: Sensors every retained instance must record with a live signal.  Availability and
#: liveness vary per well: over 61 inspected instances ``P-PDG`` was flat-zero in 23 and
#: ``T-PDG`` in 15 (uncommissioned gauges that the historian fills with 0), and
#: ``P-JUS-CKGL`` was absent from 25.  Requiring non-null *and* non-constant leaves these
#: three, which still cover all ten classes over 15 wells.  Columns that fail are dropped,
#: never imputed: a constant filler would act as a well fingerprint the model could use.
CORE = ["P-MON-CKP", "P-TPT", "T-TPT"]

#: Candidates downloaded per class while searching for instances that record CORE.
MAX_TRY = 14

#: Rows kept per instance, at 1 Hz.  Files are hours long; the cap bounds the artifact
#: while preserving the causal order normal -> precursor -> event.
NORMAL_TAIL = 10800  # 3 h of normal operation immediately before the event
TRANSIENT_CAP = 10800  # 3 h of precursor
FAULT_CAP = 14400  # 4 h of the event itself
WHOLE_CAP = 21600  # 6 h when an instance carries a single class


def _get(url: str, *, binary: bool = False, attempts: int = 4):
    """GET with a User-Agent and retries; GitHub resets connections occasionally."""
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "fault-platform-3w-prep"})
            with urllib.request.urlopen(request, timeout=120) as response:
                payload = response.read()
            return payload if binary else json.loads(payload)
        except Exception as exc:  # noqa: BLE001 - retried, reported if all attempts fail
            last = exc
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"failed to fetch {url}: {last}")


def inventory() -> dict[int, list[dict]]:
    """Real instances per class, sorted by well then start timestamp."""
    found: dict[int, list[dict]] = {}
    for label in sorted(PER_CLASS):
        entries = _get(f"{API}/{label}")
        real = [e for e in entries if e["type"] == "file" and e["name"].startswith("WELL-")]
        found[label] = sorted(real, key=lambda e: (e["name"].split("_")[0], e["name"]))
    return found


def load_index(refresh: bool = False) -> dict[str, dict]:
    """Per downloaded file: class, well, row count, columns that hold data, columns that live."""
    if INDEX.exists() and not refresh:
        return json.loads(INDEX.read_text(encoding="utf-8"))
    index: dict[str, dict] = {}
    for path in sorted(RAW_DIR.rglob("*.parquet")):
        frame = pd.read_parquet(path)
        frame = frame[frame["class"].notna()]
        columns = [c for c in frame.columns if c not in {"class", "state"}]
        index[str(path.relative_to(RAW_DIR).as_posix())] = {
            "class": int(path.parent.name),
            "well": path.name.split("_")[0],
            "rows": int(len(frame)),
            "available": [c for c in columns if frame[c].notna().any()],
            "live": [c for c in columns if frame[c].notna().any() and frame[c].std(skipna=True) > 0],
        }
    INDEX.write_text(json.dumps(index, indent=1), encoding="utf-8")
    return index


def _qualifies(entry: dict) -> bool:
    """A candidate must carry a live signal on every core sensor."""
    return all(column in entry.get("live", entry["available"]) for column in CORE)


def qualifying(index: dict[str, dict]) -> dict[int, list[str]]:
    found: dict[int, list[str]] = {}
    for name, entry in sorted(index.items()):
        if _qualifies(entry):
            found.setdefault(entry["class"], []).append(name)
    return found


def _download(label: int, entry: dict) -> None:
    destination = RAW_DIR / str(label) / entry["name"]
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and destination.stat().st_size == entry["size"]:
        return
    destination.write_bytes(_get(f"{RAW}/{label}/{entry['name']}", binary=True))


def download(found: dict[int, list[dict]], labels: list[int] | None = None) -> None:
    for label in (sorted(found) if labels is None else labels):
        for entry in found[label][: PER_CLASS[label]]:
            _download(label, entry)


def _availability(path: Path) -> dict:
    frame = pd.read_parquet(path)
    frame = frame[frame["class"].notna()]
    columns = [c for c in frame.columns if c not in {"class", "state"}]
    return {
        "class": int(path.parent.name),
        "well": path.name.split("_")[0],
        "rows": int(len(frame)),
        "available": [c for c in columns if frame[c].notna().any()],
        "live": [c for c in columns if frame[c].notna().any() and frame[c].std(skipna=True) > 0],
    }


def topup(found: dict[int, list[dict]], index: dict[str, dict]) -> None:
    """Download more candidates for the classes that are short of qualifying instances."""
    have = qualifying(index)
    known_good = {entry["well"] for entry in index.values() if _qualifies(entry)}
    for label in sorted(PER_CLASS):
        need = PER_CLASS[label] - len(have.get(label, []))
        if need <= 0:
            continue
        candidates = [e for e in found[label] if f"{label}/{e['name']}" not in index]
        # Prefer wells already proven to record CORE, then go alphabetically.
        candidates.sort(key=lambda e: (e["name"].split("_")[0] not in known_good, e["name"]))
        tried = 0
        for entry in candidates:
            if need <= 0 or tried >= MAX_TRY:
                break
            tried += 1
            _download(label, entry)
            info = _availability(RAW_DIR / str(label) / entry["name"])
            index[f"{label}/{entry['name']}"] = info
            if _qualifies(info):
                need -= 1
                known_good.add(info["well"])
                print(f"  class {label}: +{entry['name']} qualifies")
        if need > 0:
            print(f"  class {label}: still short by {need} after {tried} candidates")
    INDEX.write_text(json.dumps(index, indent=1), encoding="utf-8")


def select(index: dict[str, dict]) -> list[tuple[int, str, dict]]:
    """Up to PER_CLASS qualifying instances per class, one per well where possible."""
    chosen: list[tuple[int, str, dict]] = []
    for label in sorted(PER_CLASS):
        pool = [
            (name, entry)
            for name, entry in sorted(index.items())
            if entry["class"] == label and _qualifies(entry)
        ]
        by_well: dict[str, list[tuple[str, dict]]] = {}
        for name, entry in pool:
            by_well.setdefault(entry["well"], []).append((name, entry))
        queue = [by_well[well] for well in sorted(by_well)]
        picked: list[tuple[str, dict]] = []
        while len(picked) < PER_CLASS[label] and any(queue):
            for bucket in queue:
                if bucket and len(picked) < PER_CLASS[label]:
                    picked.append(bucket.pop(0))
        chosen.extend((label, name, entry) for name, entry in picked)
    return chosen


def _phase(cls: int) -> int:
    """0 = normal, 1 = precursor (transient label), 2 = the event itself."""
    if cls == 0:
        return 0
    return 1 if cls >= 100 else 2


def _trim(frame: pd.DataFrame) -> pd.DataFrame:
    """Keep the normal tail, the precursor and the head of the event."""
    event = (frame["_phase"].to_numpy() > 0)
    if not event.any():
        return frame.iloc[:WHOLE_CAP]
    first_event = int(event.argmax())
    before = frame.iloc[max(0, first_event - NORMAL_TAIL) : first_event]
    after = frame.iloc[first_event:]
    return pd.concat(
        [
            before,
            after[after["_phase"] == 1].iloc[:TRANSIENT_CAP],
            after[after["_phase"] == 2].iloc[:FAULT_CAP],
        ]
    )


def build(index: dict[str, dict], chosen: list[tuple[int, str, dict]]) -> None:
    frames = []
    for label, name, entry in chosen:
        frame = pd.read_parquet(RAW_DIR / name)
        frame = frame[frame["class"].notna()].copy()  # the first recorded hour is unlabeled
        frame["class"] = frame["class"].astype("int16")
        frame["_phase"] = frame["class"].map(_phase).astype("int8")
        frame = _trim(frame).copy()
        # Sensors drop out for short intervals; carry the last reading forward inside the
        # instance so the feature matrix holds no NaN.  Gaps here are seconds, not hours.
        sensors = [c for c in frame.columns if c not in {"class", "state", "_phase"}]
        frame[sensors] = frame[sensors].ffill().bfill()
        stamp = Path(name).stem.rsplit("_", 1)[-1]
        frame.insert(0, "instance", f"{label}_{entry['well']}_{stamp}")
        frame.insert(1, "time_s", range(len(frame)))
        frame.insert(2, "label", (frame["class"] % 100).astype("int16"))
        frame.insert(3, "fault", (frame["_phase"] > 0).astype("int8"))
        frame.insert(4, "phase", frame["_phase"].astype("int8"))
        frames.append(frame.drop(columns=["_phase"]))

    combined = pd.concat(frames, ignore_index=True)
    # Any extra column must also be complete and non-constant in every retained instance,
    # otherwise it would either break the learners or leak the well's identity.
    reserved = {"instance", "time_s", "label", "fault", "phase", "class", "state", *CORE}
    extras = [
        c
        for c in combined.columns
        if c not in reserved
        and all(f[c].notna().all() and f[c].nunique() > 1 for f in frames)
    ]
    combined = combined[["instance", "time_s", "label", "fault", "phase", "state", *CORE, *extras]]

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    target = OUT_DIR / "3w_events.parquet"
    combined.to_parquet(target, index=False, compression="brotli")

    print(f"rows={len(combined):,}  columns={len(combined.columns)}  file={target.stat().st_size / 1e6:.1f} MB")
    print(f"instances={combined['instance'].nunique()}  core={CORE}  extras={extras}")
    print("instances per class:", combined.groupby(combined["instance"].str.split("_").str[0])["instance"].nunique().to_dict())
    print("label counts:", combined["label"].value_counts().sort_index().to_dict())
    print("fault counts:", combined["fault"].value_counts().sort_index().to_dict())
    print("phase counts:", combined["phase"].value_counts().sort_index().to_dict())
    print("NaN in sensors:", int(combined[[*CORE, *extras]].isna().sum().sum()))


def main() -> None:
    stage = sys.argv[1] if len(sys.argv) > 1 else "all"
    if stage == "build":
        index = load_index()
        chosen = select(index)
        print(f"selected {len(chosen)} instances")
        build(index, chosen)
        return

    found = inventory()
    if stage == "inventory":
        for label, entries in found.items():
            wells = sorted({e["name"].split("_")[0] for e in entries})
            print(f"class {label}: real={len(entries):4d} wells={len(wells):3d} {wells[:8]}")
        return

    index = load_index()
    if stage in {"all", "download"}:
        download(found)
        index = load_index(refresh=True)
    if stage in {"all", "topup"}:
        print("topping up classes that lack qualifying instances")
        topup(found, index)
        index = load_index(refresh=True)

    chosen = select(index)
    wells = {entry["well"] for _, _, entry in chosen}
    print(f"selected {len(chosen)} instances over {len(wells)} wells")
    for label, name, entry in chosen:
        print(f"  class {label}: {name}")
    if stage in {"all", "topup", "download"}:
        build(index, chosen)


if __name__ == "__main__":
    main()
