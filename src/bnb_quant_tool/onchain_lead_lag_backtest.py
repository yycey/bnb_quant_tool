"""链上 lead-lag 离线回测：扫描最优领先窗口并写入配置表。

不进入实时 intelligence_loop。建议每日凌晨跑：
  python -m bnb_quant_tool.onchain_lead_lag_backtest
  或 scripts/run_onchain_lead_lag_backtest.py
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

DEFAULT_HORIZONS = (1, 2, 4, 8)


def _workspace_data_dir(config: Optional[dict] = None) -> Path:
    try:
        from bnb_quant_tool.data_localization import get_localization_manager

        root = Path(get_localization_manager().workspace)
    except Exception:
        root = Path(__file__).resolve().parents[2]
    data = root / "data"
    data.mkdir(parents=True, exist_ok=True)
    return data


def params_db_path(config: Optional[dict] = None) -> Path:
    cfg = (config or {}).get("onchain_lead_lag") or {}
    raw = cfg.get("params_db") or "data/factor_params.db"
    p = Path(str(raw))
    if p.is_absolute():
        p.parent.mkdir(parents=True, exist_ok=True)
        return p
    # 相对路径统一落到 workspace/data/
    out = _workspace_data_dir(config) / p.name
    out.parent.mkdir(parents=True, exist_ok=True)
    return out


def params_json_path(config: Optional[dict] = None) -> Path:
    return _workspace_data_dir(config) / "onchain_lead_lag_params.json"


def ensure_params_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS onchain_lead_lag_config (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            effective_date TEXT NOT NULL,
            symbol TEXT NOT NULL,
            lookback_days INTEGER,
            best_horizon_hours INTEGER,
            correlation REAL,
            hit_rate REAL,
            sign INTEGER,
            n_events INTEGER,
            payload TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_oll_eff "
        "ON onchain_lead_lag_config(effective_date DESC, symbol)"
    )
    conn.commit()


def _pearson(xs: Sequence[float], ys: Sequence[float]) -> float:
    n = len(xs)
    if n < 5:
        return 0.0
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    denx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    deny = math.sqrt(sum((y - my) ** 2 for y in ys))
    if denx <= 1e-12 or deny <= 1e-12:
        return 0.0
    return num / (denx * deny)


def build_proxy_event_series(df) -> List[Tuple[Any, float]]:
    """用 1h K 线构造链上事件代理：放量下跌=+流入偏空(-1)，缩量上涨=流出偏多(+1)。

    无 Glassnode 时仍可做窗口扫描；有真实净流序列时应优先用真实数据。
    """
    import pandas as pd

    if df is None or len(df) < 48:
        return []
    closes = df["close"].astype(float)
    vols = df["volume"].astype(float)
    ret = closes.pct_change()
    vol_ma = vols.rolling(24, min_periods=8).mean()
    vol_ratio = vols / vol_ma.replace(0, pd.NA)

    events: List[Tuple[Any, float]] = []
    for i in range(len(df)):
        vr = vol_ratio.iloc[i]
        r = ret.iloc[i]
        if pd.isna(vr) or pd.isna(r):
            continue
        # 事件强度：放量异动
        if float(vr) < 1.35 and abs(float(r)) < 0.008:
            continue
        # 约定：正信号 = 看多领先（净流出代理）
        if float(r) > 0.005 and float(vr) < 0.95:
            sig = min(2.0, float(vr) and (1.0 + abs(float(r)) * 20))
        elif float(r) < -0.005 and float(vr) > 1.35:
            sig = -min(2.0, float(vr) * abs(float(r)) * 15)
        elif float(vr) >= 1.8:
            # 单纯放量：用收益方向反作资金流代理
            sig = -math.copysign(min(1.5, float(vr) - 1), float(r) or 0.001)
        else:
            continue
        ts = df.index[i] if hasattr(df.index, "to_pydatetime") else i
        events.append((ts, float(sig)))
    return events


def build_netflow_events_from_daily(
    daily_rows: List[Dict[str, Any]],
    *,
    threshold_pct: float = 0.5,
) -> List[Tuple[datetime, float]]:
    """日频净流 → 事件：相对滚动均值的偏离。"""
    if len(daily_rows) < 10:
        return []
    vals = []
    for row in daily_rows:
        try:
            vals.append((row["time"], float(row["netflow"])))
        except Exception:
            continue
    if len(vals) < 10:
        return []
    flows = [v for _, v in vals]
    events = []
    window = 7
    for i in range(window, len(vals)):
        hist = flows[i - window : i]
        mean = sum(hist) / len(hist)
        std = math.sqrt(sum((x - mean) ** 2 for x in hist) / max(len(hist) - 1, 1)) or 1.0
        z = (flows[i] - mean) / std
        if abs(z) < threshold_pct:
            continue
        # 净流出(负) → 看多(+); 净流入 → 看空(-)
        sig = -z
        t = vals[i][0]
        if isinstance(t, str):
            try:
                t = datetime.fromisoformat(t.replace("Z", "+00:00"))
            except Exception:
                continue
        events.append((t, float(sig)))
    return events


def scan_horizons(
    price_df,
    events: List[Tuple[Any, float]],
    horizons: Sequence[int] = DEFAULT_HORIZONS,
) -> Dict[str, Any]:
    """对每个领先窗口计算相关与方向命中率。"""
    import pandas as pd

    if price_df is None or len(price_df) < 50 or not events:
        return {"ok": False, "error": "insufficient_data", "horizons": {}}

    closes = price_df["close"].astype(float)
    # 统一索引为位置
    if not isinstance(price_df.index, pd.DatetimeIndex):
        try:
            price_df = price_df.copy()
            if "open_time" in price_df.columns:
                price_df.index = pd.to_datetime(price_df["open_time"], unit="ms", utc=True)
            else:
                price_df.index = pd.RangeIndex(len(price_df))
        except Exception:
            pass

    results: Dict[str, Any] = {}
    best_key = None
    best_score = -1.0

    for h in horizons:
        xs: List[float] = []
        ys: List[float] = []
        hits = 0
        total = 0
        for ts, sig in events:
            try:
                if isinstance(ts, (int, float)) and not isinstance(ts, bool):
                    # positional index from proxy builder when index wasn't datetime
                    i = int(ts) if ts == int(ts) and 0 <= int(ts) < len(closes) else None
                    if i is None:
                        # try locate
                        continue
                else:
                    # datetime locate
                    idx = price_df.index.get_indexer([pd.Timestamp(ts)], method="nearest")
                    i = int(idx[0]) if len(idx) else -1
                    if i < 0 or i >= len(closes):
                        continue
                j = i + int(h)
                if j >= len(closes):
                    continue
                fwd = float(closes.iloc[j] / closes.iloc[i] - 1.0) * 100.0
                xs.append(float(sig))
                ys.append(fwd)
                total += 1
                if (sig > 0 and fwd > 0) or (sig < 0 and fwd < 0):
                    hits += 1
            except Exception:
                continue

        corr = _pearson(xs, ys)
        hit_rate = (hits / total) if total else 0.0
        # 综合分：|corr| 为主，命中率为辅
        score = abs(corr) * 0.7 + abs(hit_rate - 0.5) * 0.6
        sign = 1 if corr >= 0 else -1
        entry = {
            "horizon_hours": h,
            "correlation": round(corr, 4),
            "hit_rate": round(hit_rate, 4),
            "n_events": total,
            "sign": sign,
            "score": round(score, 4),
        }
        results[str(h)] = entry
        if total >= 8 and score > best_score:
            best_score = score
            best_key = str(h)

    if not best_key:
        # fallback: pick max |corr| even if sparse
        for k, v in results.items():
            s = abs(float(v.get("correlation") or 0))
            if s > best_score:
                best_score = s
                best_key = k

    best = results.get(best_key or "4") or {
        "horizon_hours": 4,
        "correlation": 0.0,
        "hit_rate": 0.5,
        "n_events": 0,
        "sign": 1,
    }
    return {
        "ok": True,
        "horizons": results,
        "best": best,
        "best_horizon_hours": int(best.get("horizon_hours") or 4),
    }


def fetch_coinmetrics_netflow_daily(asset: str = "bnb", days: int = 30) -> List[Dict[str, Any]]:
    """尽量拉取 CoinMetrics 日频净流（失败则空）。"""
    import requests

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days + 5)
    params = {
        "assets": asset.lower(),
        "metrics": "FlowNetExInclUSD",
        "frequency": "1d",
        "page_size": max(days + 5, 40),
        "sort": "time",
        "start_time": start.strftime("%Y-%m-%d"),
        "end_time": end.strftime("%Y-%m-%d"),
    }
    try:
        r = requests.get(
            "https://community-api.coinmetrics.io/v4/timeseries/asset-metrics",
            params=params,
            headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
            timeout=30,
        )
        r.raise_for_status()
        rows = []
        for row in (r.json().get("data") or []):
            nf = row.get("FlowNetExInclUSD")
            if nf is None:
                continue
            rows.append({"time": row.get("time"), "netflow": float(nf)})
        return rows
    except Exception as e:
        logger.info("CoinMetrics netflow 不可用，将用价格代理事件: %s", e)
        return []


def run_backtest(
    config: Optional[dict] = None,
    *,
    symbol: str = "BNBUSDT",
    lookback_days: int = 30,
    horizons: Sequence[int] = DEFAULT_HORIZONS,
) -> Dict[str, Any]:
    """执行扫描并落库；effective_date = 次日 UTC 日期。"""
    from bnb_quant_tool.data_fetcher import BinanceDataFetcher

    cfg = (config or {}).get("onchain_lead_lag") or {}
    lookback_days = int(cfg.get("backtest_lookback_days", lookback_days) or lookback_days)
    horizons = tuple(cfg.get("backtest_horizons") or list(horizons))

    fetcher = BinanceDataFetcher()
    # 30d * 24h + buffer
    limit = min(1000, lookback_days * 24 + 48)
    df = fetcher.get_klines(symbol=symbol, interval="1h", limit=limit)
    if df is None or len(df) < 100:
        return {"ok": False, "error": "kline_fetch_failed"}

    asset = symbol.replace("USDT", "").replace("USD", "")
    daily = fetch_coinmetrics_netflow_daily(asset=asset, days=lookback_days)
    source = "coinmetrics"
    events = build_netflow_events_from_daily(daily) if daily else []
    if len(events) < 8:
        source = "price_volume_proxy"
        # 用位置索引事件
        import pandas as pd

        closes = df["close"].astype(float)
        vols = df["volume"].astype(float)
        ret = closes.pct_change()
        vol_ma = vols.rolling(24, min_periods=8).mean()
        vol_ratio = vols / vol_ma.replace(0, pd.NA)
        events = []
        for i in range(len(df)):
            vr, r = vol_ratio.iloc[i], ret.iloc[i]
            if pd.isna(vr) or pd.isna(r):
                continue
            if float(vr) < 1.35 and abs(float(r)) < 0.008:
                continue
            if float(r) > 0.005 and float(vr) < 0.95:
                sig = 1.0 + abs(float(r)) * 10
            elif float(r) < -0.005 and float(vr) > 1.35:
                sig = -(float(vr) * abs(float(r)) * 10)
            elif float(vr) >= 1.8:
                sig = -math.copysign(min(1.5, float(vr) - 1), float(r) or 0.001)
            else:
                continue
            events.append((i, float(sig)))

    scanned = scan_horizons(df, events, horizons=horizons)
    if not scanned.get("ok"):
        return scanned

    best = scanned["best"]
    now = datetime.now(timezone.utc)
    effective = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    payload = {
        "updated_at": now.isoformat(timespec="seconds"),
        "effective_date": effective,
        "symbol": symbol.upper(),
        "lookback_days": lookback_days,
        "source": source,
        "n_raw_events": len(events),
        "best_horizon_hours": int(best.get("horizon_hours") or 4),
        "correlation": float(best.get("correlation") or 0),
        "hit_rate": float(best.get("hit_rate") or 0),
        "sign": int(best.get("sign") or 1),
        "n_events": int(best.get("n_events") or 0),
        "horizons": scanned.get("horizons") or {},
        "note": "次日生效；sign=-1 表示领先关系与启发式反向（震荡市常见）",
    }

    # JSON 镜像
    jpath = params_json_path(config)
    jpath.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    # SQLite 配置表
    db = params_db_path(config)
    conn = sqlite3.connect(str(db), timeout=30)
    try:
        ensure_params_table(conn)
        conn.execute(
            """
            INSERT INTO onchain_lead_lag_config
                (created_at, effective_date, symbol, lookback_days,
                 best_horizon_hours, correlation, hit_rate, sign, n_events, payload)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now.isoformat(timespec="seconds"),
                effective,
                symbol.upper(),
                lookback_days,
                int(payload["best_horizon_hours"]),
                float(payload["correlation"]),
                float(payload["hit_rate"]),
                int(payload["sign"]),
                int(payload["n_events"]),
                json.dumps(payload, ensure_ascii=False),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    logger.info(
        "onchain lead-lag backtest done: best=%sh corr=%.3f hit=%.1f%% sign=%s effective=%s source=%s",
        payload["best_horizon_hours"],
        payload["correlation"],
        payload["hit_rate"] * 100,
        payload["sign"],
        effective,
        source,
    )
    return {"ok": True, **payload}


def load_active_params(
    config: Optional[dict] = None,
    *,
    symbol: str = "BNBUSDT",
    as_of: Optional[str] = None,
) -> Dict[str, Any]:
    """加载已生效的最优窗口（effective_date <= today）。"""
    today = as_of or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    # prefer DB
    db = params_db_path(config)
    if db.exists():
        try:
            conn = sqlite3.connect(str(db), timeout=15)
            conn.row_factory = sqlite3.Row
            try:
                ensure_params_table(conn)
                row = conn.execute(
                    """
                    SELECT * FROM onchain_lead_lag_config
                    WHERE symbol=? AND effective_date<=?
                    ORDER BY effective_date DESC, id DESC LIMIT 1
                    """,
                    (symbol.upper(), today),
                ).fetchone()
                if row:
                    data = dict(row)
                    try:
                        payload = json.loads(data.get("payload") or "{}")
                    except Exception:
                        payload = {}
                    return {
                        "enabled": True,
                        "from": "db",
                        "best_horizon_hours": int(data.get("best_horizon_hours") or 4),
                        "correlation": float(data.get("correlation") or 0),
                        "hit_rate": float(data.get("hit_rate") or 0),
                        "sign": int(data.get("sign") or 1),
                        "effective_date": data.get("effective_date"),
                        "payload": payload,
                    }
            finally:
                conn.close()
        except Exception as e:
            logger.debug("load lead-lag db: %s", e)

    jpath = params_json_path(config)
    if jpath.exists():
        try:
            payload = json.loads(jpath.read_text(encoding="utf-8"))
            eff = str(payload.get("effective_date") or "")
            if eff and eff > today:
                return {"enabled": False, "reason": "not_yet_effective", "payload": payload}
            return {
                "enabled": True,
                "from": "json",
                "best_horizon_hours": int(payload.get("best_horizon_hours") or 4),
                "correlation": float(payload.get("correlation") or 0),
                "hit_rate": float(payload.get("hit_rate") or 0),
                "sign": int(payload.get("sign") or 1),
                "effective_date": eff,
                "payload": payload,
            }
        except Exception as e:
            logger.debug("load lead-lag json: %s", e)

    return {"enabled": False, "reason": "no_params"}


def main(argv: Optional[List[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Onchain lead-lag offline backtest")
    parser.add_argument("--symbol", default="BNBUSDT")
    parser.add_argument("--lookback-days", type=int, default=30)
    parser.add_argument("--config", default="", help="可选 config.yaml 路径")
    args = parser.parse_args(argv)

    config = {}
    if args.config:
        try:
            import yaml

            config = yaml.safe_load(Path(args.config).read_text(encoding="utf-8")) or {}
        except Exception as e:
            logger.warning("load config failed: %s", e)
    else:
        # try default
        for cand in (
            Path("config.yaml"),
            Path(__file__).resolve().parents[2] / "config.yaml",
        ):
            if cand.exists():
                try:
                    import yaml

                    config = yaml.safe_load(cand.read_text(encoding="utf-8")) or {}
                    break
                except Exception:
                    pass

    result = run_backtest(
        config,
        symbol=args.symbol,
        lookback_days=args.lookback_days,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
