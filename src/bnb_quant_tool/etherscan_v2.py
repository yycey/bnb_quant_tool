"""
Etherscan API V2 客户端
========================
统一端点 https://api.etherscan.io/v2/api + chainid 查询多链数据。
BSC (BNB Smart Chain) 使用 chainid=56。

文档: https://docs.etherscan.io/getting-started
注意: BSC 主网在 Free 套餐不可用，需 Standard 及以上；失败时由调用方回退。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

ETHERSCAN_V2_BASE = "https://api.etherscan.io/v2/api"
BSC_CHAIN_ID = 56


class EtherscanV2Client:
    """Etherscan API V2 — 单 API Key 多链查询。"""

    def __init__(
        self,
        api_key: str,
        chain_id: int = BSC_CHAIN_ID,
        timeout: int = 15,
    ):
        self.api_key = (api_key or "").strip()
        self.chain_id = int(chain_id)
        self.timeout = timeout

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def _request(self, module: str, action: str, **extra: Any) -> Dict[str, Any]:
        if not self.api_key:
            return {"status": "0", "message": "NO_API_KEY", "result": None}
        params = {
            "chainid": self.chain_id,
            "module": module,
            "action": action,
            "apikey": self.api_key,
            **extra,
        }
        try:
            resp = requests.get(
                ETHERSCAN_V2_BASE,
                params=params,
                timeout=self.timeout,
                headers={"Accept": "application/json"},
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.warning("Etherscan V2 %s/%s chain=%s 失败: %s", module, action, self.chain_id, e)
            return {"status": "0", "message": str(e), "result": None}

    @staticmethod
    def _is_ok(body: Dict) -> bool:
        if str(body.get("status")) != "1":
            return False
        result = body.get("result")
        if result is None:
            return False
        if isinstance(result, str) and result.upper() in ("", "NOTOK"):
            return False
        return True

    @staticmethod
    def _upgrade_required(body: Dict) -> bool:
        text = f"{body.get('message', '')} {body.get('result', '')}".lower()
        return (
            "not supported" in text
            or "upgrade" in text
            or "free api" in text
            or "pro endpoint" in text
        )

    def get_gas_oracle_gwei(self) -> Tuple[Optional[float], str]:
        """当前 Gas 建议价 (Gwei)。"""
        body = self._request("gastracker", "gasoracle")
        if not self._is_ok(body):
            if self._upgrade_required(body):
                return None, "etherscan_v2_upgrade_required"
            return None, "etherscan_v2_gas_failed"
        res = body.get("result") or {}
        try:
            gwei = float(
                res.get("ProposeGasPrice")
                or res.get("SafeGasPrice")
                or res.get("FastGasPrice")
                or 0
            )
            if gwei > 0:
                return gwei, "etherscan_v2"
        except (TypeError, ValueError):
            pass
        return None, "etherscan_v2_gas_parse_error"

    def get_daily_tx_series(self, days: int = 7) -> Tuple[Optional[List[Dict]], str]:
        """最近 N 天每日交易笔数 (PRO endpoint)。"""
        end = datetime.now(timezone.utc).date()
        start = end - timedelta(days=max(1, days))
        body = self._request(
            "stats",
            "dailytx",
            startdate=start.isoformat(),
            enddate=end.isoformat(),
            sort="desc",
        )
        if not self._is_ok(body):
            if self._upgrade_required(body):
                return None, "etherscan_v2_upgrade_required"
            return None, "etherscan_v2_dailytx_failed"
        rows = body.get("result")
        if not isinstance(rows, list) or not rows:
            return None, "etherscan_v2_dailytx_empty"
        return rows, "etherscan_v2"

    def get_latest_daily_tx(self) -> Tuple[Optional[int], str]:
        rows, source = self.get_daily_tx_series(days=3)
        if not rows:
            return None, source
        latest = rows[0]
        try:
            count = int(latest.get("transactionCount") or 0)
            return count if count > 0 else None, source
        except (TypeError, ValueError):
            return None, source

    def get_daily_new_address_series(self, days: int = 7) -> Tuple[Optional[List[Dict]], str]:
        """最近 N 天每日新增地址数 (PRO endpoint)。"""
        end = datetime.now(timezone.utc).date()
        start = end - timedelta(days=max(1, days))
        body = self._request(
            "stats",
            "dailynewaddress",
            startdate=start.isoformat(),
            enddate=end.isoformat(),
            sort="desc",
        )
        if not self._is_ok(body):
            if self._upgrade_required(body):
                return None, "etherscan_v2_upgrade_required"
            return None, "etherscan_v2_newaddr_failed"
        rows = body.get("result")
        if not isinstance(rows, list) or not rows:
            return None, "etherscan_v2_newaddr_empty"
        return rows, "etherscan_v2"

    def get_latest_daily_new_addresses(self) -> Tuple[Optional[int], str]:
        rows, source = self.get_daily_new_address_series(days=3)
        if not rows:
            return None, source
        latest = rows[0]
        for key in ("newAddressCount", "addressCount", "newaddressCount"):
            if key in latest:
                try:
                    val = int(latest[key])
                    return val if val > 0 else None, source
                except (TypeError, ValueError):
                    continue
        return None, source

    def fetch_bsc_activity_snapshot(self) -> Dict[str, Any]:
        """拉取 BSC 热度快照：Gas + 日交易 + 日新增地址。"""
        gas_gwei, gas_src = self.get_gas_oracle_gwei()
        daily_tx, tx_src = self.get_latest_daily_tx()
        new_addrs, addr_src = self.get_latest_daily_new_addresses()

        upgrade_required = (
            gas_src == "etherscan_v2_upgrade_required"
            or tx_src == "etherscan_v2_upgrade_required"
            or addr_src == "etherscan_v2_upgrade_required"
        )

        sources = [s for s in (gas_src, tx_src, addr_src) if s and s != "etherscan_v2_upgrade_required"]
        source_label = "+".join(dict.fromkeys(sources)) or "etherscan_v2"

        return {
            "gas_gwei": gas_gwei,
            "daily_tx": daily_tx,
            "daily_new_addresses": new_addrs,
            "chain_id": self.chain_id,
            "api": "etherscan_v2",
            "source": source_label,
            "upgrade_required": upgrade_required,
            "upgrade_hint": (
                "BSC (chainid=56) 需 Etherscan Standard 及以上套餐；"
                "当前 Free Key 将自动回退 DefiLlama/成交量代理"
                if upgrade_required else ""
            ),
        }
