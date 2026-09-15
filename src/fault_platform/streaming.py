"""Lazy dataset descriptor: rows stay on disk until a component asks for a chunk.

The runtime treats a ``StreamedDataset`` as a ``Dataset``; components that can work
incrementally consume it through :meth:`chunks`, everything else must be fed a
materialised frame (``data.materialize``). Chunks carry a global row index so the
provenance recorded by streaming feature extraction matches a full read exactly.
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
        return self.total_rows, len(self.columns) if self.columns else None

    def chunks(self, chunk_rows: int | None = None) -> Iterator[pd.DataFrame]:
        size = int(chunk_rows or self.chunk_rows)
        if size <= 0:
            raise ValueError("chunk_rows must be positive")
        if self.format == "parquet":
            yield from self._parquet_chunks(size)
        else:
            yield from self._csv_chunks(size)

    def _csv_chunks(self, size: int) -> Iterator[pd.DataFrame]:
        reader = pd.read_csv(
            self.path,
            encoding=self.encoding,
            sep=self.separator,
            usecols=self.columns,
            chunksize=size,
        )
        offset = 0
        for chunk in reader:
            chunk.index = pd.RangeIndex(offset, offset + len(chunk))  # global row identity
            offset += len(chunk)
            yield chunk

    def _parquet_chunks(self, size: int) -> Iterator[pd.DataFrame]:
        import pyarrow.parquet as pq

        table = pq.ParquetFile(self.path)
        offset = 0
        for batch in table.iter_batches(batch_size=size, columns=self.columns):
            chunk = batch.to_pandas()
            chunk.index = pd.RangeIndex(offset, offset + len(chunk))
            offset += len(chunk)
            yield chunk

    def describe(self) -> dict[str, Any]:
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
        """Read every chunk into one frame; memory then grows with the input size."""
        parts = list(self.chunks())
        if not parts:
            raise ValueError("Data source produced no rows")
        frame = parts[0] if len(parts) == 1 else pd.concat(parts)
        frame.attrs.update(self.attrs)
        return frame
