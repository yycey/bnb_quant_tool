#!/usr/bin/env python3
"""Web 行情桥接 — PHP 直连 Binance 失败时由 Python 拉取现价/24h 涨跌等。"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
WEB_ROOT = SCRIPT_DIR.parent
PROJECT_DIR = WEB_ROOT.parent if (WEB_ROOT.parent / "config.yaml").exists() else WEB_ROOT

sys.path.insert(0, str(PROJECT_DIR / "src"))

# 仅输出 JSON 到 stdout，避免 PHP 解析失败
logging.basicConfig(level=logging.WARNING)

import yaml

from bnb_quant_tool.config_access import build_data_fetcher
from bnb_quant_tool.data_localization import init_workspace
from bnb_quant_tool.market_sentiment import MarketSentiment

init_workspace(str(PROJECT_DIR))


def main() -> int:
    cfg_path = PROJECT_DIR / "config.yaml"
    cfg: dict = {}
    if cfg_path.exists():
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}

    symbol = sys.argv[1] if len(sys.argv) > 1 else cfg.get("trading", {}).get("symbol", "BNBUSDT")
    result = {
        "price": 0.0,
        "change_24h": 0.0,
        "funding_rate": 0.0,
        "fear_greed": 0,
        "source": "python",
    }

    fetcher = build_data_fetcher(cfg)
    try:
        ticker = fetcher.get_ticker(symbol)
        result["price"] = float(ticker.get("lastPrice") or 0)
        result["change_24h"] = float(ticker.get("priceChangePercent") or 0)
        result["source"] = getattr(fetcher, "last_data_source", "python")
    except Exception as exc:
        try:
            price = float(fetcher.get_last_price(symbol) or 0)
            if price > 0:
                result["price"] = price
        except Exception:
            pass
        if result["price"] <= 0:
            print(json.dumps({"ok": False, "error": str(exc), **result}, ensure_ascii=False))
            return 1

    try:
        ms = MarketSentiment(timeout=8)
        fng = ms.fetch_fear_greed()
        if fng.get("value") is not None:
            result["fear_greed"] = int(fng["value"])
        fr = ms.fetch_funding_rate(symbol)
        if fr.get("rate_pct") is not None:
            result["funding_rate"] = float(fr["rate_pct"])
    except Exception:
        pass

    print(json.dumps({"ok": True, **result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
