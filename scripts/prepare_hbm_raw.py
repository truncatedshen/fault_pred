"""把原始 HBM ECC 事件日志转成平台能直接建模的"一行一个事件"表。

输入（原始，OpenSource HBM 日志）：``Datacenter, Server, Name, Stack, SID, PcId, BankGroup,
BankArray, Col, Row, Time, EccType``——一行是一个 ECC 事件；地址类字段是十六进制字符串；
没有实体键、没有标签、`EccType` 是字符串。

平台的表达式求值是**纯数值**的（无比较、无字符串、无进制解析），所以下面这几件事必须在这里做，
这也正是 skill 阶段 1 说的"平台外转换，并说明转换了什么"：

| 产出列 | 怎么来 | 为什么 |
| --- | --- | --- |
| `entity` | `Datacenter + "\\|" + Server` | 用户指定的实体：数据中心的哪台服务器；作为窗口的 `group_column` |
| `asset` | `Datacenter` | 让验证器能做"整座数据中心留出"（比留一台服务器更狠的泛化问题） |
| `time` | `Time` 原样（Unix 秒） | 平台对**数值型**时间列按秒解释，不需要转 datetime |
| `label` | `EccType == "UER"` → 1，否则 0 | 用户指定：UER 是故障，CE/UEO 是正常。窗口标签由平台按预测视野聚合 |
| `is_ce` / `is_ueo` / `is_uer` | `EccType` 的一热指示 | 让"窗口内的错误类型构成"成为可聚合的数值特征 |
| `stack` / `sid` / `pcid` / `bank_group` / `bank_array` / `col` / `row` | 十六进制字符串 → 整数 | 地址/几何是事件日志里唯一可与"未来是否出 UER"关联的物理量 |

刻意保留 `ecc_type` 与 `server_name` 原列：它们是数据事实，删掉就没法回头核对。

用法::

    python scripts/prepare_hbm_raw.py --source <原始 csv> --target <输出 csv>
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

HEX_COLUMNS = ("Stack", "SID", "PcId", "BankGroup", "BankArray", "Col", "Row")


def prepare(source: Path) -> pd.DataFrame:
    """读取原始日志并给出建模用的行表（不做任何过滤或抽样）。"""
    raw = pd.read_csv(source)
    required = {"Datacenter", "Server", "Time", "EccType", *HEX_COLUMNS}
    missing = required - set(raw.columns)
    if missing:
        raise ValueError(f"Source is missing required columns: {sorted(missing)}")
    ecc = raw["EccType"].astype(str).str.strip().str.upper()
    unknown = sorted(set(ecc.unique()) - {"CE", "UEO", "UER"})
    if unknown:
        # 静默把未知类型当成"正常"是这里最容易犯的错，直接拒绝并点名。
        raise ValueError(f"Unknown EccType values: {unknown}; expected CE/UEO/UER")
    frame = pd.DataFrame(
        {
            "entity": raw["Datacenter"].astype(str) + "|" + raw["Server"].astype(str),
            "asset": raw["Datacenter"].astype(str),
            "time": pd.to_numeric(raw["Time"], errors="raise").astype("int64"),
            "ecc_type": ecc,
            "label": (ecc == "UER").astype("int8"),
            "is_ce": (ecc == "CE").astype("int8"),
            "is_ueo": (ecc == "UEO").astype("int8"),
            "is_uer": (ecc == "UER").astype("int8"),
            "server_name": raw["Name"].astype(str),
        }
    )
    for column in HEX_COLUMNS:
        target = column if column in {"Stack", "SID", "PcId"} else _snake(column)
        # 十六进制地址：统一转成整数，前端/表达式里都能当数值用。
        frame[target] = raw[column].map(lambda value: int(str(value), 16))
    # 按实体 + 时间稳定排序：窗口切分依赖"组内按时间有序"，这里先排好，后面就不靠运气。
    frame = frame.sort_values(["entity", "time"], kind="stable").reset_index(drop=True)
    frame = frame.rename(columns={"Stack": "stack", "SID": "sid", "PcId": "pcid"})
    return frame


def _snake(name: str) -> str:
    """BankGroup → bank_group，Col → col。"""
    return name[0].lower() + "".join(f"_{ch.lower()}" if ch.isupper() else ch for ch in name[1:])


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare the raw HBM ECC log for the platform")
    parser.add_argument("--source", default="test_hbm_raw/data/dataset(opensource).csv")
    parser.add_argument("--target", default="test_hbm_raw/data/prepared_hbm_rows.csv")
    arguments = parser.parse_args()
    source = Path(arguments.source)
    target = Path(arguments.target)
    frame = prepare(source)
    target.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(target, index=False)
    span_days = (frame["time"].max() - frame["time"].min()) / 86400
    print(f"written: {target}")
    print(
        f"rows: {len(frame)}  entities: {frame['entity'].nunique()}  "
        f"assets(datacenters): {frame['asset'].nunique()}"
    )
    print(
        f"time span: {span_days:.1f} days "
        f"({pd.to_datetime(frame['time'].min(), unit='s')} → {pd.to_datetime(frame['time'].max(), unit='s')})"
    )
    print("ecc_type:", frame["ecc_type"].value_counts().to_dict())
    print("label rate:", f"{frame['label'].mean():.4%}")
    print("columns:", list(frame.columns))


if __name__ == "__main__":
    main()
