"""Regenerate component references from the single runtime registry."""

from __future__ import annotations

import json
from pathlib import Path

from fault_platform.registry import default_registry


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    definitions = default_registry().list(limit=100)
    (root / "docs/component-registry.json").write_text(
        json.dumps(definitions, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    lines = ["# 组件参考", "", "由 scripts/export_catalog.py 从 Registry 自动生成。", ""]
    for c in definitions:
        lines.extend(
            [
                f"## {c['component_type']} · {c['display_name']}",
                "",
                c["description"],
                "",
                "**输入**："
                + ("，".join(f"{p['name']} : {p['data_type']}" for p in c["input_ports"]) or "无"),
                "",
                "**输出**："
                + "，".join(
                    f"{p['name']} : {p['data_type']}" + ("" if p["required"] else "（可选）")
                    for p in c["output_ports"]
                ),
                "",
                "| 参数 | 类型 | 默认值 | 必填 | 选项 |",
                "| --- | --- | --- | --- | --- |",
            ]
        )
        for p in c["parameter_schema"]:
            constraint = ", ".join(map(str, p["options"]))
            if p["min"] is not None or p["max"] is not None:
                constraint += f" min={p['min']}, max={p['max']}"
            lines.append(
                f"| {p['name']} | {p['type']} | {json.dumps(p['default'], ensure_ascii=False)} | "
                f"{'是' if p['required'] else '否'} | {constraint} |"
            )
        lines.append("")
    (root / "docs/components.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
