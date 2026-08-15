"""
BNB 量化工具 - BNB 特异性因子层 (Binance-specific Factors)
============================================================
平台币 BNB 的专属驱动：

1. Launchpool 挖矿 APY — 新一期高收益 → BNB 质押买盘
2. 币安公告/监管 NLP — 比通用新闻更致命的事件识别
3. BTC/ETH Beta 剥离 — 相对大盘的超额收益 (Alpha) 信号
4. 币安成交量/注意力份额 — 交易所热度代理
5. BNB Chain 健康 — TVL / 稳定币沉淀 / 安全哨兵

输出统一结构供 TradeAdvisor / AI / 研究员 Agent 消费。
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests

from .binance_announcement_nlp import BinanceAnnouncementNLP
from .binance_volume_share_factor import BinanceVolumeShareFactor
from .bnb_chain_health import BNBChainHealthFactor
from .bnb_event_calendar import BNBEventCalendar
from .bnb_risk_sentry import BNBRiskSentry
from .bnb_symbol import is_bnb_trading_pair
from .launchpool_mining_factor import LaunchpoolMiningFactor
from .news_credibility import NewsCredibilityFilter

logger = logging.getLogger(__name__)


class BNBSpecificFactors:
    """BNB 专属因子聚合器。"""

    BAPI_MIRRORS = (
        "https://www.binance.com",
        "https://www.binance.info",
    )

    LAUNCHPOOL_ENDPOINTS = (
        "/bapi/earn/v1/friendly/launchpool/project/list",
        "/bapi/earn/v1/public/launchpool/project/list",
    )

    ANNOUNCEMENT_ENDPOINT = (
        "/bapi/composite/v1/public/cms/article/list/query"
    )

    def __init__(
        self,
        fetcher=None,
        config: Optional[Dict] = None,
    ):
        cfg = config or {}
        self.fetcher = fetcher
        self.enabled = bool(cfg.get("enabled", True))
        self.apply_only_to_bnb_pairs = bool(cfg.get("apply_only_to_bnb_pairs", True))
        self.cache_seconds = int(cfg.get("cache_seconds", 600))
        self.alpha_lookback_hours = int(cfg.get("alpha_lookback_hours", 168))
        self.alpha_interval = str(cfg.get("alpha_interval", "1h"))
        self.high_apy_threshold = float(cfg.get("high_apy_threshold", 8.0))
        self.extreme_apy_threshold = float(cfg.get("extreme_apy_threshold", 15.0))
        self.positive_alpha_threshold = float(cfg.get("positive_alpha_threshold", 0.003))
        self.launchpool_weight = float(cfg.get("launchpool_weight", 0.35))
        self.alpha_weight = float(cfg.get("alpha_weight", 0.35))
        self.nlp_weight = float(cfg.get("nlp_weight", 0.30))
        self.volume_share_weight = float(cfg.get("volume_share_weight", 0.12))
        self.chain_health_weight = float(cfg.get("chain_health_weight", 0.12))
        self.gate_relaxation_max = float(cfg.get("gate_relaxation_max", 0.08))
        self.timeout = int(cfg.get("timeout", 12))

        nlp_cfg = cfg.get("announcement_nlp") or {}
        self.nlp = BinanceAnnouncementNLP(
            regulatory_weight=float(nlp_cfg.get("regulatory_weight", 1.2)),
            launchpool_weight=float(nlp_cfg.get("launchpool_weight", 0.9)),
            min_confidence=float(nlp_cfg.get("min_confidence", 0.35)),
        )

        event_cfg = cfg.get("event_calendar") or {}
        self.event_calendar = BNBEventCalendar(
            config=event_cfg, fetcher=fetcher,
        ) if event_cfg.get("enabled", True) else None

        mining_cfg = cfg.get("mining_factor") or {}
        self.mining_factor = LaunchpoolMiningFactor(
            pre_unlock_hours=float(mining_cfg.get("pre_unlock_hours", 12)),
            pre_unlock_soft_hours=float(mining_cfg.get("pre_unlock_soft_hours", 24)),
        ) if mining_cfg.get("enabled", True) else None

        cred_cfg = cfg.get("news_credibility") or {}
        self.news_credibility = NewsCredibilityFilter(config=cred_cfg) if cred_cfg.get("enabled", True) else None

        sentry_cfg = cfg.get("risk_sentry") or {}
        self.risk_sentry = BNBRiskSentry(fetcher=fetcher, config=sentry_cfg) if sentry_cfg.get("enabled", True) else None

        vol_cfg = cfg.get("volume_share") or {}
        self.volume_share = BinanceVolumeShareFactor(
            fetcher=fetcher, config=vol_cfg,
        ) if vol_cfg.get("enabled", True) else None

        chain_cfg = cfg.get("chain_health") or {}
        self.chain_health = BNBChainHealthFactor(
            config=chain_cfg,
        ) if chain_cfg.get("enabled", True) else None

        self._cache: Dict[str, Tuple[float, Dict]] = {}
        self._headers = {
            "User-Agent": "Mozilla/5.0 (compatible; BNBQuantTool/2.11)",
            "Accept": "application/json",
        }

    def fetch_all(
        self,
        symbol: str = "BNBUSDT",
        news_items: Optional[List[Dict]] = None,
    ) -> Dict[str, Any]:
        """拉取并聚合全部 BNB 专属因子。"""
        if not self.enabled:
            return self._disabled_result()

        if self.apply_only_to_bnb_pairs and not is_bnb_trading_pair(symbol):
            return self._skipped_non_bnb_result(symbol)

        cache_key = f"bnb_factors:{symbol}"
        cached = self._get_cache(cache_key)
        if cached is not None:
            return cached

        launchpool = self._fetch_launchpool(news_items=news_items)
        alpha = self._compute_alpha_vs_btc_eth(symbol=symbol)
        announcements = self._fetch_binance_announcements()
        all_news = list(news_items or []) + announcements
        news_cred = {}
        if self.news_credibility:
            news_cred = self.news_credibility.analyze(all_news)

        nlp_result = self.nlp.analyze_items(all_news)

        event_cycle = {}
        if self.event_calendar:
            lp_projects = (launchpool.get("projects") or [])
            event_cycle = self.event_calendar.analyze(
                news_items=all_news,
                launchpool_projects=lp_projects if lp_projects else None,
            )

        mining_event = {}
        if self.mining_factor:
            mining_event = self.mining_factor.compute(
                event_cycle=event_cycle,
                launchpool=launchpool,
                nlp_result=nlp_result,
            )

        risk_sentry = {}
        if self.risk_sentry:
            risk_sentry = self.risk_sentry.fetch_all(symbol=symbol, news_items=all_news)

        volume_share = {}
        if self.volume_share:
            volume_share = self.volume_share.fetch(symbol=symbol)

        chain_health = {}
        if self.chain_health:
            chain_health = self.chain_health.fetch(
                news_items=all_news, nlp_result=nlp_result,
            )

        # 链安全硬事件并入 risk_sentry，复用 TradeAdvisor 禁多门控
        if chain_health.get("block_long"):
            risk_sentry = dict(risk_sentry or {"enabled": True})
            risk_sentry["block_long"] = True
            risk_sentry["chain_health_block"] = True
            sec = chain_health.get("security") or {}
            risk_sentry["interpretation"] = (
                sec.get("interpretation")
                or chain_health.get("interpretation")
                or "BNB Chain 安全哨兵禁多"
            )
            risk_sentry["position_scale"] = min(
                float(risk_sentry.get("position_scale") or 1.0), 0.40,
            )

        bnb_score = self._aggregate_score(
            launchpool, alpha, nlp_result, event_cycle, risk_sentry, mining_event,
            volume_share, chain_health,
        )
        gate_relaxation = self._compute_gate_relaxation(
            launchpool, alpha, nlp_result, event_cycle, risk_sentry, mining_event,
            volume_share, chain_health,
        )
        position_boost = self._compute_position_boost(
            launchpool, alpha, nlp_result, event_cycle, risk_sentry, mining_event,
            volume_share, chain_health,
        )

        result = {
            "bnb_score": round(bnb_score, 3),
            "gate_relaxation": round(gate_relaxation, 3),
            "position_boost": round(position_boost, 3),
            "launchpool": launchpool,
            "alpha": alpha,
            "announcement_nlp": nlp_result,
            "event_cycle": event_cycle,
            "mining_event": mining_event,
            "news_credibility": news_cred,
            "risk_sentry": risk_sentry,
            "volume_share": volume_share,
            "chain_health": chain_health,
            "interpretation": "",
            "trade_bias": self._trade_bias(
                bnb_score, launchpool, alpha, nlp_result, event_cycle, mining_event,
                chain_health,
            ),
            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        result["interpretation"] = self._interpret(result)
        self._set_cache(cache_key, result)
        return result

    def _fetch_launchpool(self, news_items: Optional[List[Dict]] = None) -> Dict:
        projects: List[Dict] = []
        data_source = "none"

        raw = self._bapi_get(self.LAUNCHPOOL_ENDPOINTS)
        if raw:
            projects = self._parse_launchpool_response(raw)
            if projects:
                data_source = "binance_bapi"

        if not projects and news_items:
            projects = self._launchpool_from_news(news_items)
            if projects:
                data_source = "news_fallback"

        active = [p for p in projects if p.get("status") in ("active", "ongoing", "mining", None)]
        if not active:
            active = projects

        max_apy = max((float(p.get("apy") or 0) for p in active), default=0.0)
        total_stake_demand = sum(float(p.get("stake_demand_score") or 0) for p in active)

        signal_strength = 0.0
        if max_apy >= self.extreme_apy_threshold:
            signal_strength = 0.9
        elif max_apy >= self.high_apy_threshold:
            signal_strength = 0.6
        elif max_apy >= self.high_apy_threshold * 0.5:
            signal_strength = 0.3

        score = clamp(signal_strength + min(0.2, total_stake_demand * 0.05))

        return {
            "active_projects": len(active),
            "max_apy_pct": round(max_apy, 2),
            "projects": active[:5],
            "signal_strength": round(signal_strength, 3),
            "launchpool_score": round(score, 3),
            "high_apy_event": max_apy >= self.high_apy_threshold,
            "extreme_apy_event": max_apy >= self.extreme_apy_threshold,
            "data_source": data_source,
            "interpretation": self._launchpool_interpret(active, max_apy, data_source),
        }

    def _parse_launchpool_response(self, raw: Dict) -> List[Dict]:
        projects: List[Dict] = []
        data = raw.get("data") or raw
        items = (
            data.get("list") or data.get("projects") or data.get("rows")
            or (data if isinstance(data, list) else [])
        )
        if not isinstance(items, list):
            return projects

        for item in items:
            if not isinstance(item, dict):
                continue
            name = (
                item.get("projectName") or item.get("project_name")
                or item.get("asset") or item.get("rebateCoin") or "?"
            )
            apy_raw = (
                item.get("apr") or item.get("apy") or item.get("latestApr")
                or item.get("totalApr") or item.get("avgApr") or 0
            )
            apy = self._parse_apy(apy_raw)
            stake_asset = (
                item.get("stakeAsset") or item.get("stake_asset")
                or item.get("asset") or "BNB"
            )
            status = (
                item.get("status") or item.get("projectStatus")
                or ("active" if item.get("miningStatus") == "MINING" else "unknown")
            )
            projects.append({
                "name": str(name),
                "apy": apy,
                "stake_asset": str(stake_asset).upper(),
                "status": str(status).lower(),
                "stake_demand_score": 1.0 if "BNB" in str(stake_asset).upper() else 0.3,
            })
        return projects

    @staticmethod
    def _parse_apy(value: Any) -> float:
        if value is None:
            return 0.0
        if isinstance(value, (int, float)):
            v = float(value)
            return v * 100 if v < 1 else v
        s = str(value).strip().replace("%", "")
        try:
            v = float(s)
            return v * 100 if v < 1 else v
        except ValueError:
            return 0.0

    def _launchpool_from_news(self, news_items: List[Dict]) -> List[Dict]:
        projects = []
        apy_pattern = re.compile(r"(\d+(?:\.\d+)?)\s*%?\s*(?:apy|apr|年化|收益率)", re.I)
        for item in news_items:
            text = f"{item.get('title', '')} {item.get('summary', '')}"
            if not re.search(r"launch\s*pool|launchpool|新币挖矿|质押.*bnb", text, re.I):
                continue
            apy_match = apy_pattern.search(text)
            apy = float(apy_match.group(1)) if apy_match else self.high_apy_threshold
            projects.append({
                "name": item.get("title", "Launchpool")[:60],
                "apy": apy,
                "stake_asset": "BNB",
                "status": "active",
                "stake_demand_score": 1.0,
            })
        return projects

    @staticmethod
    def _launchpool_interpret(projects: List[Dict], max_apy: float, source: str) -> str:
        if not projects:
            return "当前无活跃 Launchpool 信号"
        names = ", ".join(p.get("name", "?")[:20] for p in projects[:3])
        return (
            f"Launchpool: {len(projects)} 个项目活跃，最高 APY {max_apy:.1f}% "
            f"({names}) [来源: {source}]"
        )

    def _compute_alpha_vs_btc_eth(self, symbol: str = "BNBUSDT") -> Dict:
        if self.fetcher is None:
            return self._alpha_empty("no_fetcher")

        lookback = max(48, self.alpha_lookback_hours)
        interval = self.alpha_interval

        try:
            bnb_df = self.fetcher.get_klines(symbol=symbol, interval=interval, limit=lookback)
            btc_df = self.fetcher.get_klines(symbol="BTCUSDT", interval=interval, limit=lookback)
            eth_df = self.fetcher.get_klines(symbol="ETHUSDT", interval=interval, limit=lookback)
        except Exception as e:
            logger.warning(f"Alpha 计算 K 线拉取失败: {e}")
            return self._alpha_empty(str(e))

        if bnb_df is None or btc_df is None or eth_df is None:
            return self._alpha_empty("empty_data")
        if len(bnb_df) < 30 or len(btc_df) < 30 or len(eth_df) < 30:
            return self._alpha_empty("insufficient_bars")

        bnb_ret = bnb_df["close"].pct_change().dropna()
        btc_ret = btc_df["close"].pct_change().dropna()
        eth_ret = eth_df["close"].pct_change().dropna()

        n = min(len(bnb_ret), len(btc_ret), len(eth_ret))
        if n < 24:
            return self._alpha_empty("insufficient_aligned")

        bnb_r = bnb_ret.iloc[-n:].values
        btc_r = btc_ret.iloc[-n:].values
        eth_r = eth_ret.iloc[-n:].values

        beta_btc, beta_eth = self._ols_betas(bnb_r, btc_r, eth_r)

        recent_n = min(24, n)
        recent_bnb = bnb_r[-recent_n:]
        recent_btc = btc_r[-recent_n:]
        recent_eth = eth_r[-recent_n:]
        expected = beta_btc * recent_btc + beta_eth * recent_eth
        residuals = recent_bnb - expected
        alpha_recent = float(residuals.mean())
        alpha_cum_24h = float(residuals.sum())

        btc_trend_24h = float(recent_btc.sum())
        bnb_trend_24h = float(recent_bnb.sum())

        defensive_strength = 0.0
        if btc_trend_24h < -0.005 and alpha_recent > self.positive_alpha_threshold:
            defensive_strength = min(1.0, alpha_recent / 0.01)
        elif btc_trend_24h < 0 and alpha_recent > 0:
            defensive_strength = min(0.6, alpha_recent / 0.008)

        alpha_score = clamp(alpha_recent / 0.008)
        if defensive_strength > 0.5:
            alpha_score = max(alpha_score, 0.4 + defensive_strength * 0.4)

        return {
            "alpha_recent": round(alpha_recent, 5),
            "alpha_cum_24h": round(alpha_cum_24h, 5),
            "beta_btc": round(beta_btc, 3),
            "beta_eth": round(beta_eth, 3),
            "btc_trend_24h": round(btc_trend_24h, 5),
            "bnb_trend_24h": round(bnb_trend_24h, 5),
            "defensive_strength": round(defensive_strength, 3),
            "positive_alpha": alpha_recent > self.positive_alpha_threshold,
            "market_down_bnb_resilient": btc_trend_24h < -0.005 and alpha_recent > 0,
            "alpha_score": round(alpha_score, 3),
            "lookback_bars": n,
            "interval": interval,
            "interpretation": self._alpha_interpret(
                alpha_recent, beta_btc, beta_eth, btc_trend_24h, defensive_strength
            ),
        }

    @staticmethod
    def _ols_betas(y: Any, x1: Any, x2: Any) -> Tuple[float, float]:
        """双因子收益 β。

        BTC/ETH 收益高度共线时，联合过原点 OLS 会发散；改为：
        1) 先估 β_BTC；2) 残差上再估 β_ETH。
        """
        import numpy as np

        y_a = np.asarray(y, dtype=float).ravel()
        x1_a = np.asarray(x1, dtype=float).ravel()
        x2_a = np.asarray(x2, dtype=float).ravel()
        if len(y_a) < 2:
            return 1.0, 0.0

        s11 = float((x1_a * x1_a).sum())
        beta_btc = float((x1_a * y_a).sum() / s11) if s11 > 1e-12 else 1.0
        resid = y_a - beta_btc * x1_a
        s22 = float((x2_a * x2_a).sum())
        beta_eth = float((x2_a * resid).sum() / s22) if s22 > 1e-12 else 0.0
        return beta_btc, beta_eth

    @staticmethod
    def _alpha_interpret(
        alpha: float, beta_btc: float, beta_eth: float,
        btc_trend: float, defensive: float,
    ) -> str:
        parts = [
            f"Alpha={alpha:+.3%}/bar",
            f"β_BTC={beta_btc:.2f}",
            f"β_ETH={beta_eth:.2f}",
        ]
        if defensive > 0.5:
            parts.append("大盘走弱但 BNB 抗跌(独立买盘)")
        elif alpha > 0.003:
            parts.append("相对 BTC/ETH 超额收益为正")
        elif alpha < -0.003:
            parts.append("跑输大盘")
        if btc_trend < 0:
            parts.append(f"BTC 24h {btc_trend:+.2%}")
        return " | ".join(parts)

    @staticmethod
    def _alpha_empty(reason: str) -> Dict:
        return {
            "alpha_recent": 0.0,
            "alpha_cum_24h": 0.0,
            "beta_btc": 1.0,
            "beta_eth": 0.0,
            "btc_trend_24h": 0.0,
            "bnb_trend_24h": 0.0,
            "defensive_strength": 0.0,
            "positive_alpha": False,
            "market_down_bnb_resilient": False,
            "alpha_score": 0.0,
            "lookback_bars": 0,
            "interval": "",
            "interpretation": f"Alpha 不可用 ({reason})",
        }

    def _fetch_binance_announcements(self, page_size: int = 10) -> List[Dict]:
        items: List[Dict] = []
        for mirror in self.BAPI_MIRRORS:
            url = f"{mirror}{self.ANNOUNCEMENT_ENDPOINT}"
            try:
                resp = requests.get(
                    url,
                    params={"type": 1, "pageNo": 1, "pageSize": page_size},
                    headers=self._headers,
                    timeout=self.timeout,
                )
                if resp.status_code != 200:
                    continue
                body = resp.json()
                catalogs = (body.get("data") or {}).get("catalogs") or []
                for cat in catalogs:
                    for art in (cat.get("articles") or []):
                        title = art.get("title") or ""
                        code = art.get("code") or ""
                        items.append({
                            "title": title,
                            "summary": title,
                            "url": f"{mirror}/en/support/announcement/{code}" if code else "",
                            "source": "BinanceAnnouncement",
                            "published_ts": int(time.time()),
                        })
                if items:
                    break
            except Exception as e:
                logger.debug(f"币安公告拉取失败 {mirror}: {e}")
        return items

    def _bapi_get(self, endpoints: Tuple[str, ...]) -> Optional[Dict]:
        for mirror in self.BAPI_MIRRORS:
            for ep in endpoints:
                url = f"{mirror}{ep}"
                try:
                    resp = requests.get(url, headers=self._headers, timeout=self.timeout)
                    if resp.status_code != 200:
                        continue
                    body = resp.json()
                    if body.get("code") in ("000000", 0, "0", None) or body.get("success"):
                        return body
                    if body.get("data"):
                        return body
                except Exception as e:
                    logger.debug(f"BAPI {url} 失败: {e}")
        return None

    def _aggregate_score(
        self, launchpool: Dict, alpha: Dict, nlp: Dict,
        event_cycle: Optional[Dict] = None, risk_sentry: Optional[Dict] = None,
        mining_event: Optional[Dict] = None,
        volume_share: Optional[Dict] = None,
        chain_health: Optional[Dict] = None,
    ) -> float:
        lp = float(launchpool.get("launchpool_score") or 0)
        al = float(alpha.get("alpha_score") or 0)
        nl = float(nlp.get("score") or 0)
        vs = float((volume_share or {}).get("volume_share_score") or 0)
        ch = float((chain_health or {}).get("chain_health_score") or 0)
        w_sum = (
            self.launchpool_weight
            + self.alpha_weight
            + self.nlp_weight
            + self.volume_share_weight
            + self.chain_health_weight
        )
        raw = (
            lp * self.launchpool_weight
            + al * self.alpha_weight
            + nl * self.nlp_weight
            + vs * self.volume_share_weight
            + ch * self.chain_health_weight
        ) / max(w_sum, 1e-9)
        ec = event_cycle or {}
        phase = ec.get("phase", "normal")
        if phase == "anticipation":
            raw = min(1.0, raw + 0.25)
        elif phase == "unlock_dump":
            raw = max(-1.0, raw - 0.45)
        elif phase == "staking_lock":
            raw *= 0.85
        rs = risk_sentry or {}
        if rs.get("block_long"):
            raw = min(raw, -0.3)
        elif rs.get("bnb_btc_weakness", {}).get("weak"):
            raw *= 0.7
        me = mining_event or {}
        mf = float(me.get("mining_event_factor") or 0)
        if mf != 0:
            raw = max(-1.0, min(1.0, raw * 0.7 + mf * 0.3))
        if (chain_health or {}).get("block_long"):
            raw = min(raw, -0.45)
        return clamp(raw)

    def _compute_gate_relaxation(
        self, launchpool: Dict, alpha: Dict, nlp: Dict,
        event_cycle: Optional[Dict] = None, risk_sentry: Optional[Dict] = None,
        mining_event: Optional[Dict] = None,
        volume_share: Optional[Dict] = None,
        chain_health: Optional[Dict] = None,
    ) -> float:
        relaxation = 0.0
        if launchpool.get("high_apy_event"):
            relaxation += 0.04
        if launchpool.get("extreme_apy_event"):
            relaxation += 0.03
        if alpha.get("market_down_bnb_resilient"):
            relaxation += 0.04
        elif alpha.get("positive_alpha"):
            relaxation += 0.02
        if (volume_share or {}).get("rising"):
            relaxation += 0.02
        if (chain_health or {}).get("healthy"):
            relaxation += 0.02
        ec = event_cycle or {}
        relaxation += float(ec.get("gate_relaxation") or 0)
        tightening = float(ec.get("gate_tightening") or 0)
        relaxation = max(0.0, relaxation - tightening)
        if nlp.get("impact_level") in ("critical", "high") and (nlp.get("score") or 0) < -0.2:
            relaxation = 0.0
        if ec.get("block_long"):
            relaxation = 0.0
        rs = risk_sentry or {}
        if rs.get("block_long"):
            relaxation = 0.0
        me = mining_event or {}
        if me.get("block_long"):
            relaxation = 0.0
        if (chain_health or {}).get("block_long"):
            relaxation = 0.0
        cap = self.gate_relaxation_max + max(0.0, float(ec.get("gate_relaxation") or 0))
        return max(0.0, min(cap, relaxation))

    def _compute_position_boost(
        self, launchpool: Dict, alpha: Dict, nlp: Dict,
        event_cycle: Optional[Dict] = None, risk_sentry: Optional[Dict] = None,
        mining_event: Optional[Dict] = None,
        volume_share: Optional[Dict] = None,
        chain_health: Optional[Dict] = None,
    ) -> float:
        boost = 1.0
        if launchpool.get("high_apy_event"):
            boost += 0.08
        if alpha.get("market_down_bnb_resilient"):
            boost += 0.12
        elif alpha.get("positive_alpha"):
            boost += 0.05
        if (nlp.get("score") or 0) > 0.3 and nlp.get("dominant_category") in (
            "launchpool", "bnb_burn", "settlement_resolved",
        ):
            boost += 0.06
        if nlp.get("impact_level") == "critical" and (nlp.get("score") or 0) < 0:
            boost -= 0.25
        elif (nlp.get("score") or 0) < -0.4:
            boost -= 0.15
        if (volume_share or {}).get("rising"):
            boost += 0.05
        elif (volume_share or {}).get("fading"):
            boost -= 0.06
        if (chain_health or {}).get("healthy"):
            boost += 0.05
        elif (chain_health or {}).get("stressed"):
            boost -= 0.08
        ec = event_cycle or {}
        if ec.get("position_boost"):
            boost *= float(ec["position_boost"])
        rs = risk_sentry or {}
        if rs.get("position_scale"):
            boost *= float(rs["position_scale"])
        me = mining_event or {}
        if me.get("mining_event_factor", 0) < -0.3:
            boost *= 0.65
        if (chain_health or {}).get("block_long"):
            boost *= 0.40
        return max(0.35, min(1.35, boost))

    @staticmethod
    def _trade_bias(
        bnb_score: float, launchpool: Dict, alpha: Dict, nlp: Dict,
        event_cycle: Optional[Dict] = None,
        mining_event: Optional[Dict] = None,
        chain_health: Optional[Dict] = None,
    ) -> str:
        if (chain_health or {}).get("block_long"):
            return "WAIT"
        me = mining_event or {}
        if me.get("suggest_hedge_short"):
            return "SHORT"
        if me.get("block_long"):
            return "WAIT"
        if me.get("action_hint") in ("LONG", "SHORT", "WAIT"):
            hint = me["action_hint"]
            if hint != "WAIT" or bnb_score == 0:
                if abs(float(me.get("mining_event_factor") or 0)) >= 0.25:
                    return hint
        ec = event_cycle or {}
        if ec.get("block_long"):
            return "SHORT" if ec.get("suggest_short") else "WAIT"
        if ec.get("phase") == "anticipation" and bnb_score > 0.1:
            return "LONG"
        if nlp.get("impact_level") == "critical" and (nlp.get("score") or 0) < -0.3:
            return "SHORT"
        if bnb_score > 0.35:
            return "LONG"
        if bnb_score < -0.35:
            return "SHORT"
        if launchpool.get("extreme_apy_event") and alpha.get("alpha_score", 0) >= 0:
            return "LONG"
        return "WAIT"

    def _interpret(self, result: Dict) -> str:
        parts = []
        lp = result.get("launchpool") or {}
        al = result.get("alpha") or {}
        nlp = result.get("announcement_nlp") or {}
        if lp.get("active_projects"):
            parts.append(lp.get("interpretation", ""))
        if al.get("lookback_bars"):
            parts.append(al.get("interpretation", ""))
        if nlp.get("matched_count"):
            parts.append(nlp.get("interpretation", ""))
        ec = result.get("event_cycle") or {}
        if ec.get("phase") and ec.get("phase") != "normal":
            parts.append(ec.get("interpretation", ""))
        me = result.get("mining_event") or {}
        if me.get("interpretation"):
            parts.append(me.get("interpretation", ""))
        nc = result.get("news_credibility") or {}
        if nc.get("regime_impact") not in (None, "NORMAL"):
            parts.append(nc.get("interpretation", ""))
        rs = result.get("risk_sentry") or {}
        if rs.get("interpretation") and rs.get("block_long") or rs.get("bnb_btc_weakness", {}).get("weak"):
            parts.append(rs.get("interpretation", ""))
        vs = result.get("volume_share") or {}
        if vs.get("interpretation") and (vs.get("rising") or vs.get("fading") or vs.get("volume_share_score")):
            parts.append(vs.get("interpretation", ""))
        ch = result.get("chain_health") or {}
        if ch.get("interpretation") and (
            ch.get("block_long") or ch.get("healthy") or ch.get("stressed") or ch.get("chain_health_score")
        ):
            parts.append(ch.get("interpretation", ""))
        if result.get("gate_relaxation", 0) > 0:
            parts.append(f"门控放宽 -{result['gate_relaxation']:.0%} confidence")
        score = result.get("bnb_score", 0)
        parts.append(f"BNB专属综合分 {score:+.2f} → {result.get('trade_bias', 'WAIT')}")
        return "；".join(p for p in parts if p)

    def _get_cache(self, key: str) -> Optional[Dict]:
        if key in self._cache:
            ts, data = self._cache[key]
            if time.time() - ts < self.cache_seconds:
                return data
        return None

    def _set_cache(self, key: str, data: Dict) -> None:
        self._cache[key] = (time.time(), data)

    @staticmethod
    def _disabled_result() -> Dict:
        return {
            "bnb_score": 0.0,
            "gate_relaxation": 0.0,
            "position_boost": 1.0,
            "launchpool": {},
            "alpha": {},
            "announcement_nlp": {},
            "volume_share": {},
            "chain_health": {},
            "interpretation": "BNB 专属因子已禁用",
            "trade_bias": "WAIT",
            "enabled": False,
        }

    @staticmethod
    def _skipped_non_bnb_result(symbol: str) -> Dict:
        return {
            "bnb_score": 0.0,
            "gate_relaxation": 0.0,
            "position_boost": 1.0,
            "launchpool": {},
            "alpha": {},
            "announcement_nlp": {},
            "event_cycle": {},
            "mining_event": {},
            "news_credibility": {},
            "risk_sentry": {"enabled": False},
            "volume_share": {"enabled": False},
            "chain_health": {"enabled": False, "block_long": False},
            "interpretation": f"非 BNB 交易对({symbol})，BNB 专属因子已跳过",
            "trade_bias": "WAIT",
            "enabled": False,
            "skipped": True,
            "skip_reason": "non_bnb_pair",
            "symbol": symbol,
        }

    @classmethod
    def format_for_prompt(cls, factors: Dict) -> str:
        if not factors or factors.get("enabled") is False:
            return ""
        if not factors.get("bnb_score") and not (factors.get("launchpool") or {}).get("active_projects"):
            if not (factors.get("alpha") or {}).get("lookback_bars"):
                return ""
        lines = [
            "\n【BNB 专属因子 — 平台币特异性】",
            f"- {factors.get('interpretation', '')}",
        ]
        lp = factors.get("launchpool") or {}
        if lp.get("max_apy_pct"):
            lines.append(
                f"- Launchpool: 最高 APY {lp['max_apy_pct']:.1f}%, "
                f"{'高质押需求' if lp.get('high_apy_event') else '正常'}"
            )
        al = factors.get("alpha") or {}
        if al.get("lookback_bars"):
            lines.append(f"- Beta剥离: {al.get('interpretation', '')}")
        nlp = factors.get("announcement_nlp") or {}
        if nlp.get("matched_count"):
            lines.append(BinanceAnnouncementNLP.format_for_prompt(nlp).strip())
        ec = factors.get("event_cycle") or {}
        if ec.get("enabled") is not False:
            lines.append(BNBEventCalendar.format_for_prompt(ec).strip())
        me = factors.get("mining_event") or {}
        lines.append(LaunchpoolMiningFactor.format_for_prompt(me).strip())
        nc = factors.get("news_credibility") or {}
        lines.append(NewsCredibilityFilter.format_for_prompt(nc).strip())
        rs = factors.get("risk_sentry") or {}
        if rs.get("enabled") is not False:
            lines.append(BNBRiskSentry.format_for_prompt(rs).strip())
        lines.append(BinanceVolumeShareFactor.format_for_prompt(factors.get("volume_share")).strip())
        lines.append(BNBChainHealthFactor.format_for_prompt(factors.get("chain_health")).strip())
        lines.append(
            f"- 门控放宽: {factors.get('gate_relaxation', 0):.0%} | "
            f"仓位加成: {factors.get('position_boost', 1.0):.2f}x\n"
        )
        return "\n".join(lines)


def clamp(v: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))
