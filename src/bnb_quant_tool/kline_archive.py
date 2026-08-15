"""
K 线历史数据本地归档 — 按月分片存储，优先本地读取。

目录结构:
  data/klines/BNBUSDT/1h/
    manifest.json
    chunks/
      2024-12.csv
      2025-01.csv
      ...
    merged.parquet   # 可选，下载完成后自动生成
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd

logger = logging.getLogger(__name__)

KLINE_COLUMNS = [
    "open_time", "open", "high", "low", "close",
    "volume", "quote_volume", "trades",
]

_parquet_engine_warned = False


def default_archive_root(workspace: str | Path = ".") -> Path:
    return Path(workspace).resolve() / "data" / "klines"


class KlineArchive:
    """本地 K 线归档：按月 chunk + manifest + 合并文件。"""

    def __init__(
        self,
        workspace: str | Path = ".",
        symbol: str = "BNBUSDT",
        interval: str = "1h",
    ):
        self.symbol = symbol.upper()
        self.interval = interval
        self.root = default_archive_root(workspace) / self.symbol / interval
        self.chunks_dir = self.root / "chunks"
        self.manifest_path = self.root / "manifest.json"
        self.merged_path = self.root / "merged.parquet"
        self.merged_csv_path = self.root / "merged.csv"
        self.chunks_dir.mkdir(parents=True, exist_ok=True)

    def chunk_path(self, year: int, month: int) -> Path:
        return self.chunks_dir / f"{year:04d}-{month:02d}.csv"

    def list_months(self) -> List[Tuple[int, int]]:
        months: List[Tuple[int, int]] = []
        if not self.chunks_dir.exists():
            return months
        for p in sorted(self.chunks_dir.glob("????-??.csv")):
            try:
                y, m = p.stem.split("-")
                months.append((int(y), int(m)))
            except ValueError:
                continue
        return months

    def has_local_data(self) -> bool:
        return bool(self.list_months()) or self.merged_path.exists() or self.merged_csv_path.exists()

    @staticmethod
    def month_range(year: int, month: int) -> Tuple[datetime, datetime]:
        start = datetime(year, month, 1)
        if month == 12:
            end = datetime(year + 1, 1, 1)
        else:
            end = datetime(year, month + 1, 1)
        return start, end

    @staticmethod
    def iter_months_back(count: int, *, end: Optional[datetime] = None) -> List[Tuple[int, int]]:
        """从 end 所在月往前 count 个月（含 end 月）。"""
        end = end or datetime.now()
        cursor = datetime(end.year, end.month, 1)
        out: List[Tuple[int, int]] = []
        for _ in range(count):
            out.append((cursor.year, cursor.month))
            if cursor.month == 1:
                cursor = datetime(cursor.year - 1, 12, 1)
            else:
                cursor = datetime(cursor.year, cursor.month - 1, 1)
        return list(reversed(out))

    def save_chunk(
        self,
        year: int,
        month: int,
        df: pd.DataFrame,
        *,
        source: str = "unknown",
    ) -> Path:
        path = self.chunk_path(year, month)
        if df.empty:
            logger.warning("跳过空 chunk %s", path.name)
            return path
        out = self._normalize_df(df)
        out.to_csv(path, index=False)
        logger.info("已保存 %s (%d 条, source=%s)", path.name, len(out), source)
        return path

    @staticmethod
    def _normalize_df(df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        for col in KLINE_COLUMNS:
            if col not in out.columns:
                out[col] = 0 if col == "trades" else 0.0
        out = out[KLINE_COLUMNS].sort_values("open_time").drop_duplicates(
            subset=["open_time"], keep="last"
        )
        return out

    def merge_bars(self, df: pd.DataFrame, *, source: str = "unknown") -> Dict:
        """将新 K 线合并进对应月份 chunk，并重建 merged + manifest。"""
        if df.empty:
            return {"updated": False, "new_bars": 0, "updated_months": [], "source": source}

        normalized = self._normalize_df(df)
        if normalized.empty:
            return {"updated": False, "new_bars": 0, "updated_months": [], "source": source}

        updated_months: List[str] = []
        new_bars = 0
        ts = pd.to_datetime(normalized["open_time"])

        for (year, month), group in normalized.groupby([ts.dt.year, ts.dt.month]):
            existing = self.load_chunk(int(year), int(month))
            before = len(existing)
            if existing.empty:
                merged = group.copy()
                content_changed = True
            else:
                merged = pd.concat([existing, group], ignore_index=True)
                merged = self._normalize_df(merged)
                # 同 open_time 用 keep=last 刷新未收盘 K 线 OHLC
                content_changed = len(merged) != before
                if not content_changed and len(merged) > 0 and len(existing) > 0:
                    for col in ("open", "high", "low", "close", "volume"):
                        if float(merged[col].iloc[-1]) != float(existing[col].iloc[-1]):
                            content_changed = True
                            break
                    if pd.Timestamp(merged["open_time"].iloc[-1]) != pd.Timestamp(
                        existing["open_time"].iloc[-1]
                    ):
                        content_changed = True
            added = max(0, len(merged) - before)
            if added > 0 or before == 0 or content_changed:
                self.save_chunk(int(year), int(month), merged, source=source)
                updated_months.append(f"{int(year):04d}-{int(month):02d}")
            new_bars += added

        if updated_months:
            self.rebuild_merged()
            prev_sources: List[str] = []
            if self.manifest_path.exists():
                try:
                    with open(self.manifest_path, "r", encoding="utf-8") as f:
                        prev_sources = json.load(f).get("sources") or []
                except Exception:
                    pass
            sources = list(set(prev_sources + [source]))
            self.write_manifest(sources=sources)
            logger.info(
                "K线归档增量更新: %s %s +%d 条, 月份 %s",
                self.symbol,
                self.interval,
                new_bars,
                ", ".join(updated_months),
            )

        return {
            "updated": bool(updated_months),
            "new_bars": new_bars,
            "updated_months": updated_months,
            "source": source,
            "total_rows": len(self.load_merged()) if updated_months else None,
        }

    def load_chunk(self, year: int, month: int) -> pd.DataFrame:
        path = self.chunk_path(year, month)
        if not path.exists():
            return pd.DataFrame(columns=KLINE_COLUMNS)
        df = pd.read_csv(path, parse_dates=["open_time"])
        return df

    def load_merged(self) -> pd.DataFrame:
        df = pd.DataFrame(columns=KLINE_COLUMNS)
        if self.merged_path.exists():
            try:
                df = pd.read_parquet(self.merged_path)
            except Exception as exc:
                global _parquet_engine_warned
                if not _parquet_engine_warned:
                    _parquet_engine_warned = True
                    logger.warning(
                        "读取 parquet 失败，回退 csv（进程内仅提示一次）。"
                        "请对当前 Python 执行: python scripts/ensure_pyarrow.py | %s",
                        exc,
                    )
                else:
                    logger.debug("读取 parquet 失败，回退 csv: %s", exc)
        if df.empty and self.merged_csv_path.exists():
            df = pd.read_csv(self.merged_csv_path, parse_dates=["open_time"])
        if df.empty:
            frames = [self.load_chunk(y, m) for y, m in self.list_months()]
            frames = [f for f in frames if not f.empty]
            if not frames:
                return pd.DataFrame(columns=KLINE_COLUMNS)
            df = pd.concat(frames, ignore_index=True)
            df = df.sort_values("open_time").drop_duplicates(subset=["open_time"], keep="last")
            return df

        # 自愈：chunk 已更新但 merged 未重建时（旧 bug），以最新 chunk 为准
        months = self.list_months()
        if months and "open_time" in df.columns and not df.empty:
            latest_chunk = self.load_chunk(*months[-1])
            if not latest_chunk.empty:
                chunk_end = pd.Timestamp(latest_chunk["open_time"].iloc[-1])
                merged_end = pd.Timestamp(df["open_time"].iloc[-1])
                if chunk_end > merged_end:
                    logger.warning(
                        "merged 落后于 chunk (%s < %s)，从月度分片重建 %s %s",
                        merged_end,
                        chunk_end,
                        self.symbol,
                        self.interval,
                    )
                    return self.rebuild_merged()
        return df

    def rebuild_merged(self) -> pd.DataFrame:
        """始终从全部月度 chunk 重建 merged（禁止沿用过期的 merged 文件）。"""
        frames = [self.load_chunk(y, m) for y, m in self.list_months()]
        frames = [f for f in frames if not f.empty]
        if not frames:
            return pd.DataFrame(columns=KLINE_COLUMNS)
        df = pd.concat(frames, ignore_index=True)
        df = self._normalize_df(df)
        try:
            df.to_parquet(self.merged_path, index=False)
            try:
                df.to_csv(self.merged_csv_path, index=False)
            except Exception:
                pass
        except Exception:
            df.to_csv(self.merged_csv_path, index=False)
        return df

    def write_manifest(self, *, sources: Optional[List[str]] = None) -> Dict:
        months_meta = []
        total_rows = 0
        earliest = None
        latest = None
        for y, m in self.list_months():
            path = self.chunk_path(y, m)
            df = self.load_chunk(y, m)
            rows = len(df)
            total_rows += rows
            if rows:
                t0 = df["open_time"].iloc[0]
                t1 = df["open_time"].iloc[-1]
                earliest = t0 if earliest is None or t0 < earliest else earliest
                latest = t1 if latest is None or t1 > latest else latest
            months_meta.append({
                "file": f"chunks/{path.name}",
                "year": y,
                "month": m,
                "rows": rows,
            })
        manifest = {
            "symbol": self.symbol,
            "interval": self.interval,
            "updated_at": datetime.now().isoformat(),
            "months": months_meta,
            "total_rows": total_rows,
            "range_start": earliest.isoformat() if earliest is not None else None,
            "range_end": latest.isoformat() if latest is not None else None,
            "merged_parquet": str(self.merged_path.name) if self.merged_path.exists() else None,
            "merged_csv": str(self.merged_csv_path.name) if self.merged_csv_path.exists() else None,
            "sources": sorted(set(sources or [])),
        }
        with open(self.manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2, ensure_ascii=False)
        return manifest

    def load_range(
        self,
        start_str: str = "1 day ago",
        end_str: Optional[str] = None,
    ) -> pd.DataFrame:
        """按与 data_fetcher 相同的时间字符串加载本地 K 线。"""
        if not self.has_local_data():
            return pd.DataFrame(columns=KLINE_COLUMNS)

        if "ago" in start_str:
            num, unit = start_str.split(" ")[0], start_str.split(" ")[1]
            if "day" in unit:
                delta = timedelta(days=int(num))
            elif "hour" in unit:
                delta = timedelta(hours=int(num))
            elif "minute" in unit:
                delta = timedelta(minutes=int(num))
            else:
                raise ValueError(f"不支持的时间单位: {unit}")
            start_ts = pd.Timestamp(datetime.now() - delta)
        else:
            start_ts = pd.Timestamp(start_str)

        end_ts = pd.Timestamp(end_str) if end_str else pd.Timestamp(datetime.now())

        df = self.load_merged()
        if df.empty:
            return df
        mask = (df["open_time"] >= start_ts) & (df["open_time"] <= end_ts)
        return df.loc[mask].copy().reset_index(drop=True)

    def summary(self) -> Dict:
        if self.manifest_path.exists():
            with open(self.manifest_path, "r", encoding="utf-8") as f:
                return json.load(f)
        return self.write_manifest()
