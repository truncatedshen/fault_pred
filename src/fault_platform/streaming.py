"""Lazy dataset descriptor: rows stay on disk until a component asks for a chunk.

The runtime treats a ``StreamedDataset`` as a ``Dataset``; components that can work
incrementally consume it through :meth:`chunks`, everything else must be fed a
materialised frame (``data.materialize``). Chunks carry a global row index so the
provenance recorded by streaming feature extraction matches a full read exactly.

中文说明：这是"懒加载数据集"——文件留在磁盘上，只有真正需要时逐块读取。
运行时会把它当作普通 ``Dataset`` 传递，能流式处理的组件（窗口特征、数据概览、
``data.materialize``）调用 :meth:`chunks`；其余组件会收到明确报错，
提示先插入 ``data.materialize``。

关键细节：每个分块的索引都是**全局行号**（``RangeIndex(offset, offset+len)``），
因此流式提取出的窗口覆盖区间与整表读取的结果完全一致——
这正是训练/测试"是否共享原始行"的检查能在两种模式间通用的原因。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

import pandas as pd


@dataclass
class StreamedDataset:
    path: Path
    format: str = "csv"
    chunk_rows: int = 200_000
    columns: list[str] | None = None
    encoding: str = "utf-8-sig"
    separator: str = ","
    filters: list[list[Any]] | None = None
    total_rows: int | None = None
    attrs: dict[str, Any] = field(default_factory=dict)

    @property
    def shape_hint(self) -> tuple[int | None, int | None]:
        """尽力给出的 (行数, 列数)；未知的行数为 None（CSV 不预先扫描）。"""
        return self.total_rows, len(self.columns) if self.columns else None

    def chunks(self, chunk_rows: int | None = None) -> Iterator[pd.DataFrame]:
        """按 ``chunk_rows`` 逐块产出 DataFrame（可覆盖默认块大小）。"""
        size = int(chunk_rows or self.chunk_rows)
        if size <= 0:
            raise ValueError("chunk_rows must be positive")
        if self.format == "parquet":
            yield from self._parquet_chunks(size)
        else:
            yield from self._csv_chunks(size)

    def _csv_chunks(self, size: int) -> Iterator[pd.DataFrame]:
        """CSV 分块读取：``usecols`` 让列裁剪在解析阶段就生效，省内存也省时间。"""
        reader = pd.read_csv(
            self.path,
            encoding=self.encoding,
            sep=self.separator,
            usecols=self.columns,
            chunksize=size,
        )
        offset = 0
        for chunk in reader:
            # 全局行号：块内行号会从 0 重新开始，那样窗口覆盖就无法与整表读取对齐了。
            chunk.index = pd.RangeIndex(offset, offset + len(chunk))  # global row identity
            offset += len(chunk)
            yield chunk

    def _parquet_chunks(self, size: int) -> Iterator[pd.DataFrame]:
        """Parquet 行组级分块读取；``columns`` 直接下推到读取层。"""
        import pyarrow.parquet as pq

        table = pq.ParquetFile(self.path)
        offset = 0
        for batch in table.iter_batches(batch_size=size, columns=self.columns):
            chunk = batch.to_pandas()
            chunk.index = pd.RangeIndex(offset, offset + len(chunk))
            offset += len(chunk)
            yield chunk

    def describe(self) -> dict[str, Any]:
        """给 summarize()/MCP 用的描述：只报告路径、格式、块大小与行数，绝不读数据。"""
        return {
            "kind": "streamed",
            "path": str(self.path),
            "format": self.format,
            "chunk_rows": self.chunk_rows,
            "columns": list(self.columns) if self.columns else None,
            "rows": self.total_rows,
            "filters": self.filters,
        }

    def materialize(self) -> pd.DataFrame:
        """把所有分块读成一张表；**此时内存重新随输入规模增长**。

        只应在全局操作（排序、去重、画图）确实需要整表时调用，
        调用点会带警告，报告里要如实说明这次运行是物化的。
        """
        parts = list(self.chunks())
        if not parts:
            raise ValueError("Data source produced no rows")
        frame = parts[0] if len(parts) == 1 else pd.concat(parts)
        frame.attrs.update(self.attrs)
        return frame
