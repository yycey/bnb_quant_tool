"""BNB 交易对识别 — BNB 专属数据仅在此生效。"""

from __future__ import annotations


def normalize_symbol(symbol: str) -> str:
    """统一交易对写法：BNBUSDT / BNB-USDT / BNB/USDT → BNBUSDT。"""
    return (
        (symbol or "")
        .strip()
        .upper()
        .replace("/", "")
        .replace("-", "")
        .replace("_", "")
        .replace(" ", "")
    )


def is_bnb_trading_pair(symbol: str) -> bool:
    """当前交易对是否以 BNB 为标的（如 BNBUSDT、BNBBTC）。

    非 BNB 对（BTCUSDT、ETHUSDT 等）不应应用 Launchpool / 链健康 /
    BNB 风控哨兵 / 事件周期等平台币专属逻辑。
    """
    s = normalize_symbol(symbol)
    if not s:
        return False
    if s.startswith("BNB"):
        # 排除极少数误匹配：BNBx 类合约名若不以 BNB + quote 常见形态，仍视为 BNB 系
        return len(s) >= 6
    return False
