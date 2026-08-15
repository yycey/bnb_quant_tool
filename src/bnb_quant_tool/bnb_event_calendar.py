"""
BNB 量化工具 - 币安活动日历与生命周期 (Event Cycle AI)
============================================================
捕捉 Launchpool / Megadrop / HODLer Airdrop 等官方活动的四阶段周期，
并输出可执行的风控与策略权重调整。

四阶段:
  ANTICIPATION   预期发酵期 — 公告刚出，抢筹阶段
  STAKING_LOCK   质押锁仓期 — 挖矿中，流通盘减少
  UNLOCK_DUMP    解锁砸盘期 — 挖矿结束/新币上线，最高级别风控
  VALUE_RECOVERY 价值回归期 — 活动结束，恢复常规定价
  NORMAL         无活跃活动
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

from .bnb_burn_predictor import BNBBurnPredictor

logger = logging.getLogger(__name__)


class EventType(str, Enum):
    LAUNCHPOOL = "launchpool"
    MEGADROP = "megadrop"
    HODLER_AIRDROP = "hodler_airdrop"
    UNKNOWN = "unknown"


class EventPhase(str, Enum):
    ANTICIPATION = "anticipation"
    STAKING_LOCK = "staking_lock"
    UNLOCK_DUMP = "unlock_dump"
    VALUE_RECOVERY = "value_recovery"
    NORMAL = "normal"


PHASE_LABELS = {
    EventPhase.ANTICIPATION: "预期发酵期",
    EventPhase.STAKING_LOCK: "质押锁仓期",
    EventPhase.UNLOCK_DUMP: "解锁砸盘期",
    EventPhase.VALUE_RECOVERY: "价值回归期",
    EventPhase.NORMAL: "常规周期",
}

EVENT_TYPE_PATTERNS: Tuple[Tuple[EventType, Tuple[str, ...]], ...] = (
    (EventType.LAUNCHPOOL, (
        r"launch\s*pool", r"launchpool", r"新币挖矿", r"质押.*bnb.*挖矿",
    )),
    (EventType.MEGADROP, (
        r"megadrop", r"mega\s*drop", r"超级空投",
    )),
    (EventType.HODLER_AIRDROP, (
        r"hodler\s*airdrop", r"hodler.*airdrop", r"持有者空投", r"持仓空投",
    )),
)

DEFAULT_PHASE_POLICIES: Dict[str, Dict] = {
    "anticipation": {
        "gate_relaxation": 0.10,
        "gate_tightening": 0.0,
        "position_boost": 1.15,
        "block_long": False,
        "suggest_short": False,
        "strategy_mode": "aggressive_long",
        "confidence_boost": 0.08,
        "paper_max_position_pct_scale": 1.0,
    },
    "staking_lock": {
        "gate_relaxation": 0.0,
        "gate_tightening": 0.02,
        "position_boost": 0.85,
        "block_long": False,
        "suggest_short": False,
        "strategy_mode": "grid_or_hold",
        "confidence_boost": 0.0,
        "paper_max_position_pct_scale": 0.75,
    },
    "unlock_dump": {
        "gate_relaxation": 0.0,
        "gate_tightening": 0.15,
        "position_boost": 0.45,
        "block_long": True,
        "suggest_short": True,
        "strategy_mode": "defensive_short_bias",
        "confidence_boost": -0.10,
        "paper_max_position_pct_scale": 0.40,
    },
    "value_recovery": {
        "gate_relaxation": 0.0,
        "gate_tightening": 0.0,
        "position_boost": 1.0,
        "block_long": False,
        "suggest_short": False,
        "strategy_mode": "normal",
        "confidence_boost": 0.0,
        "paper_max_position_pct_scale": 1.0,
    },
    "normal": {
        "gate_relaxation": 0.0,
        "gate_tightening": 0.0,
        "position_boost": 1.0,
        "block_long": False,
        "suggest_short": False,
        "strategy_mode": "normal",
        "confidence_boost": 0.0,
        "paper_max_position_pct_scale": 1.0,
    },
}


class BNBEventCalendar:
    """币安官方活动日历 + 四阶段生命周期识别。"""

    BAPI_MIRRORS = (
        "https://www.binance.com",
        "https://www.binance.info",
    )
    ANNOUNCEMENT_ENDPOINT = "/bapi/composite/v1/public/cms/article/list/query"
    LAUNCHPOOL_ENDPOINTS = (
        "/bapi/earn/v1/friendly/launchpool/project/list",
        "/bapi/earn/v1/public/launchpool/project/list",
    )

    def __init__(
        self,
        config: Optional[Dict] = None,
        state_path: Optional[str] = None,
        fetcher=None,
    ):
        cfg = config or {}
        self.enabled = bool(cfg.get("enabled", True))
        self.cache_seconds = int(cfg.get("cache_seconds", 600))
        self.unlock_window_hours = int(cfg.get("unlock_window_hours", 12))
        self.pre_unlock_hours = int(cfg.get("pre_unlock_hours", 12))
        self.post_dump_hours = int(cfg.get("post_dump_hours", 48))
        self.recovery_hours = int(cfg.get("recovery_hours", 48))
        self.anticipation_hours = int(cfg.get("anticipation_hours", 48))
        self.default_staking_days = int(cfg.get("default_staking_days", 7))
        self.timeout = int(cfg.get("timeout", 12))
        self.announcement_page_size = int(cfg.get("announcement_page_size", 20))

        self.phase_policies = self._merge_phase_policies(cfg.get("phases") or {})
        self.manual_events = list(cfg.get("manual_events") or [])

        self._cache: Dict[str, Tuple[float, Dict]] = {}
        self._headers = {
            "User-Agent": "Mozilla/5.0 (compatible; BNBQuantTool/2.11)",
            "Accept": "application/json",
        }
        self._last_applied_phase: Optional[str] = None
        self.fetcher = fetcher

        burn_cfg = cfg.get("burn_predictor") or {}
        self.burn_predictor = BNBBurnPredictor(
            fetcher=fetcher,
            hype_window_days=int(burn_cfg.get("hype_window_days", 15)),
            chase_block_days=int(burn_cfg.get("chase_block_days", 5)),
            cache_seconds=int(burn_cfg.get("cache_seconds", 3600)),
        ) if burn_cfg.get("enabled", True) else None

        if state_path:
            self.state_path = Path(state_path)
        else:
            try:
                from bnb_quant_tool.data_localization import get_localized_db_path
                base = get_localized_db_path("paper_trading").parent
            except ImportError:
                base = Path("data")
            self.state_path = base / "bnb_event_state.json"

    def analyze(
        self,
        news_items: Optional[List[Dict]] = None,
        launchpool_projects: Optional[List[Dict]] = None,
    ) -> Dict[str, Any]:
        if not self.enabled:
            return self._disabled_result()

        cache_key = "event_cycle"
        cached = self._get_cache(cache_key)
        if cached is not None:
            return cached

        now = datetime.now(timezone.utc)
        events = self._collect_events(news_items, launchpool_projects)
        active = self._pick_dominant_event(events, now)
        phase = self._resolve_phase(active, now) if active else EventPhase.NORMAL
        policy = dict(self.phase_policies.get(phase.value, DEFAULT_PHASE_POLICIES["normal"]))

        burn_forecast = {}
        if self.burn_predictor:
            burn_forecast = self.burn_predictor.predict()

        # 销毁窗口叠加风控
        if burn_forecast.get("block_chase_long"):
            policy = dict(policy)
            policy["block_long"] = True
            policy["gate_tightening"] = float(policy.get("gate_tightening", 0)) + float(
                burn_forecast.get("gate_tightening") or 0.10
            )
            policy["confidence_boost"] = float(policy.get("confidence_boost", 0)) + float(
                burn_forecast.get("confidence_penalty") or -0.08
            )

        result = {
            "enabled": True,
            "phase": phase.value,
            "phase_label": PHASE_LABELS.get(phase, phase.value),
            "event_type": (active or {}).get("event_type", EventType.UNKNOWN.value),
            "event_name": (active or {}).get("name", ""),
            "active_event": active,
            "policy": policy,
            "strategy_mode": policy.get("strategy_mode", "normal"),
            "gate_relaxation": float(policy.get("gate_relaxation", 0)),
            "gate_tightening": float(policy.get("gate_tightening", 0)),
            "position_boost": float(policy.get("position_boost", 1.0)),
            "block_long": bool(policy.get("block_long", False)),
            "suggest_short": bool(policy.get("suggest_short", False)),
            "confidence_boost": float(policy.get("confidence_boost", 0)),
            "burn_forecast": burn_forecast,
            "interpretation": "",
            "fetched_at": now.isoformat(timespec="seconds"),
        }
        result["interpretation"] = self._interpret(result)
        if burn_forecast.get("in_hype_window"):
            result["interpretation"] += "；" + burn_forecast.get("interpretation", "")
        self._set_cache(cache_key, result)
        self._persist_state(result)
        return result

    def apply_to_trade_advisor(self, advisor, cycle: Optional[Dict] = None) -> Dict:
        cycle = cycle or self.analyze()
        phase = cycle.get("phase", EventPhase.NORMAL.value)

        advisor._event_cycle = cycle
        advisor._event_gate_relaxation = float(cycle.get("gate_relaxation", 0))
        advisor._event_gate_tightening = float(cycle.get("gate_tightening", 0))
        advisor._event_block_long = bool(cycle.get("block_long", False))
        advisor._event_suggest_short = bool(cycle.get("suggest_short", False))
        advisor._event_position_factor = float(cycle.get("position_boost", 1.0))
        advisor._event_confidence_boost = float(cycle.get("confidence_boost", 0))
        advisor._event_strategy_mode = cycle.get("strategy_mode", "normal")

        if phase != self._last_applied_phase:
            logger.info(
                "BNB 事件周期: %s → %s | %s | mode=%s",
                self._last_applied_phase, phase,
                cycle.get("event_name", "-"),
                cycle.get("strategy_mode"),
            )
            self._last_applied_phase = phase

        return {"phase": phase, "policy": cycle.get("policy"), "applied": True}

    def _collect_events(
        self,
        news_items: Optional[List[Dict]],
        launchpool_projects: Optional[List[Dict]],
    ) -> List[Dict]:
        events: List[Dict] = []
        for raw in self.manual_events:
            ev = self._normalize_manual_event(raw)
            if ev:
                events.append(ev)

        for art in self._fetch_announcements():
            ev = self._parse_announcement_event(art)
            if ev:
                events.append(ev)

        if news_items:
            for item in news_items:
                ev = self._parse_announcement_event(item)
                if ev and not self._is_duplicate_event(events, ev):
                    events.append(ev)

        projects = launchpool_projects if launchpool_projects is not None else self._fetch_launchpool_projects()
        for proj in projects:
            ev = self._parse_launchpool_project_event(proj)
            if ev and not self._is_duplicate_event(events, ev):
                events.append(ev)

        return events

    def _fetch_announcements(self) -> List[Dict]:
        items: List[Dict] = []
        for mirror in self.BAPI_MIRRORS:
            try:
                resp = requests.get(
                    f"{mirror}{self.ANNOUNCEMENT_ENDPOINT}",
                    params={"type": 1, "pageNo": 1, "pageSize": self.announcement_page_size},
                    headers=self._headers,
                    timeout=self.timeout,
                )
                if resp.status_code != 200:
                    continue
                for cat in (resp.json().get("data") or {}).get("catalogs") or []:
                    for art in cat.get("articles") or []:
                        items.append({
                            "title": art.get("title") or "",
                            "summary": art.get("title") or "",
                            "source": "BinanceAnnouncement",
                            "published_ts": self._parse_ms(art.get("releaseDate")) or int(time.time()),
                        })
                if items:
                    break
            except Exception as e:
                logger.debug(f"公告拉取失败: {e}")
        return items

    def _fetch_launchpool_projects(self) -> List[Dict]:
        for mirror in self.BAPI_MIRRORS:
            for ep in self.LAUNCHPOOL_ENDPOINTS:
                try:
                    resp = requests.get(f"{mirror}{ep}", headers=self._headers, timeout=self.timeout)
                    if resp.status_code != 200:
                        continue
                    data = resp.json().get("data") or resp.json()
                    items = data.get("list") or data.get("projects") or []
                    if isinstance(items, list) and items:
                        return items
                except Exception as e:
                    logger.debug(f"Launchpool API: {e}")
        return []

    def _parse_announcement_event(self, item: Dict) -> Optional[Dict]:
        text = f"{item.get('title', '')} {item.get('summary', '')}"
        event_type = self._detect_event_type(text)
        if event_type == EventType.UNKNOWN:
            return None

        announce_at = self._ts_to_dt(item.get("published_ts"))
        start_at, end_at, listing_at = self._extract_dates_from_text(text, announce_at)
        if not start_at and announce_at:
            start_at = announce_at + timedelta(hours=12)
        if not end_at and start_at:
            end_at = start_at + timedelta(days=self.default_staking_days)

        return {
            "name": (item.get("title") or event_type.value)[:120],
            "event_type": event_type.value,
            "source": item.get("source", "announcement"),
            "announce_at": self._dt_iso(announce_at),
            "start_at": self._dt_iso(start_at),
            "end_at": self._dt_iso(end_at),
            "listing_at": self._dt_iso(listing_at),
        }

    def _parse_launchpool_project_event(self, proj: Dict) -> Optional[Dict]:
        name = proj.get("name") or proj.get("projectName") or proj.get("asset") or "Launchpool"
        start_at = self._parse_ms_dt(proj.get("startTime") or proj.get("miningStartTime"))
        end_at = self._parse_ms_dt(proj.get("endTime") or proj.get("miningEndTime"))
        announce_at = self._parse_ms_dt(proj.get("announceTime")) or start_at
        listing_at = self._parse_ms_dt(proj.get("listingTime"))
        if not end_at and start_at:
            end_at = start_at + timedelta(days=self.default_staking_days)

        return {
            "name": str(name)[:120],
            "event_type": EventType.LAUNCHPOOL.value,
            "source": "launchpool_api",
            "announce_at": self._dt_iso(announce_at),
            "start_at": self._dt_iso(start_at),
            "end_at": self._dt_iso(end_at),
            "listing_at": self._dt_iso(listing_at),
        }

    def _normalize_manual_event(self, raw: Dict) -> Optional[Dict]:
        if not raw.get("name"):
            return None
        return {
            "name": raw["name"],
            "event_type": raw.get("type") or raw.get("event_type") or EventType.LAUNCHPOOL.value,
            "source": "manual",
            "announce_at": raw.get("announce_at"),
            "start_at": raw.get("start_at"),
            "end_at": raw.get("end_at"),
            "listing_at": raw.get("listing_at"),
        }

    def _resolve_phase(self, event: Dict, now: datetime) -> EventPhase:
        announce_at = self._parse_iso(event.get("announce_at"))
        start_at = self._parse_iso(event.get("start_at"))
        end_at = self._parse_iso(event.get("end_at"))
        listing_at = self._parse_iso(event.get("listing_at"))

        pre_hours = min(self.unlock_window_hours, self.pre_unlock_hours)
        unlock_start = end_at - timedelta(hours=pre_hours) if end_at else None
        dump_end = end_at + timedelta(hours=self.post_dump_hours) if end_at else None
        if listing_at:
            dump_end = max(dump_end or listing_at, listing_at + timedelta(hours=self.post_dump_hours))

        if listing_at and now >= listing_at - timedelta(hours=6):
            return EventPhase.UNLOCK_DUMP
        if unlock_start and end_at and unlock_start <= now <= (dump_end or end_at):
            return EventPhase.UNLOCK_DUMP
        if start_at and end_at and start_at <= now < (unlock_start or end_at):
            return EventPhase.STAKING_LOCK
        if announce_at and start_at and announce_at <= now < start_at:
            return EventPhase.ANTICIPATION
        if announce_at and not start_at:
            if (now - announce_at).total_seconds() <= self.anticipation_hours * 3600:
                return EventPhase.ANTICIPATION
        if end_at and dump_end and now > dump_end:
            return EventPhase.VALUE_RECOVERY
        if end_at and now > end_at + timedelta(hours=self.recovery_hours):
            return EventPhase.VALUE_RECOVERY
        return EventPhase.NORMAL

    def _pick_dominant_event(self, events: List[Dict], now: datetime) -> Optional[Dict]:
        if not events:
            return None
        priority = {
            EventPhase.UNLOCK_DUMP.value: 100,
            EventPhase.ANTICIPATION.value: 80,
            EventPhase.STAKING_LOCK.value: 60,
            EventPhase.VALUE_RECOVERY.value: 40,
        }
        best: Tuple[int, Optional[Dict]] = (0, None)
        for ev in events:
            ph = self._resolve_phase(ev, now)
            score = priority.get(ph.value, 0)
            if score > best[0]:
                best = (score, ev)
        return best[1] if best[0] > 0 else None

    @staticmethod
    def _detect_event_type(text: str) -> EventType:
        for etype, patterns in EVENT_TYPE_PATTERNS:
            for pat in patterns:
                if re.search(pat, text, re.I):
                    return etype
        return EventType.UNKNOWN

    @staticmethod
    def _extract_dates_from_text(
        text: str, base: Optional[datetime],
    ) -> Tuple[Optional[datetime], Optional[datetime], Optional[datetime]]:
        dates: List[datetime] = []
        for m in re.finditer(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", text):
            try:
                dates.append(datetime(
                    int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=timezone.utc,
                ))
            except ValueError:
                continue
        dates.sort()
        start_at = dates[0] if dates else None
        end_at = dates[-1] if len(dates) > 1 else None
        listing_at = dates[-1] if re.search(r"list(ing)?|上线", text, re.I) and dates else None
        if not start_at and base:
            start_at = base + timedelta(hours=12)
        return start_at, end_at, listing_at

    @staticmethod
    def _is_duplicate_event(events: List[Dict], new_ev: Dict) -> bool:
        key = (new_ev.get("name") or "")[:40].lower()
        return any((e.get("name") or "")[:40].lower() == key for e in events)

    def _merge_phase_policies(self, overrides: Dict) -> Dict[str, Dict]:
        merged = {k: dict(v) for k, v in DEFAULT_PHASE_POLICIES.items()}
        for phase, pol in overrides.items():
            if phase in merged and isinstance(pol, dict):
                merged[phase].update(pol)
        return merged

    def _interpret(self, result: Dict) -> str:
        hints = {
            "anticipation": "抢筹阶段：放宽做多门控",
            "staking_lock": "挖矿中：网格/持仓观望",
            "unlock_dump": "解锁窗口：禁止做多，关注做空",
            "value_recovery": "活动结束：恢复常规分析",
            "normal": "无活动周期信号",
        }
        ph = result.get("phase", "normal")
        name = result.get("event_name") or "无"
        return (
            f"{result.get('phase_label')} [{name}] "
            f"→ {result.get('strategy_mode')}；{hints.get(ph, '')}"
        )

    def _persist_state(self, result: Dict) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(json.dumps({
                "phase": result.get("phase"),
                "phase_label": result.get("phase_label"),
                "event_name": result.get("event_name"),
                "strategy_mode": result.get("strategy_mode"),
                "policy": result.get("policy"),
                "interpretation": result.get("interpretation"),
                "updated_at": result.get("fetched_at"),
            }, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            logger.debug(f"状态持久化失败: {e}")

    @classmethod
    def format_for_prompt(cls, cycle: Dict) -> str:
        if not cycle or not cycle.get("enabled") or cycle.get("phase") == EventPhase.NORMAL.value:
            return ""
        lines = [
            "\n【BNB 事件周期 AI】",
            f"- {cycle.get('interpretation', '')}",
            f"- 策略模式: {cycle.get('strategy_mode')}",
        ]
        if cycle.get("block_long"):
            lines.append("- ⚠ 解锁砸盘期：强制拦截所有做多")
        lines.append("")
        return "\n".join(lines)

    @staticmethod
    def _disabled_result() -> Dict:
        return {
            "enabled": False,
            "phase": EventPhase.NORMAL.value,
            "phase_label": PHASE_LABELS[EventPhase.NORMAL],
            "policy": dict(DEFAULT_PHASE_POLICIES["normal"]),
            "strategy_mode": "normal",
            "gate_relaxation": 0.0,
            "gate_tightening": 0.0,
            "position_boost": 1.0,
            "block_long": False,
            "suggest_short": False,
            "interpretation": "事件周期模块已禁用",
        }

    def _get_cache(self, key: str) -> Optional[Dict]:
        if key in self._cache:
            ts, data = self._cache[key]
            if time.time() - ts < self.cache_seconds:
                return data
        return None

    def _set_cache(self, key: str, data: Dict) -> None:
        self._cache[key] = (time.time(), data)

    @staticmethod
    def _ts_to_dt(ts: Any) -> Optional[datetime]:
        try:
            t = float(ts)
            if t > 1e12:
                t /= 1000.0
            return datetime.fromtimestamp(t, tz=timezone.utc)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _parse_ms(val: Any) -> Optional[int]:
        try:
            ms = int(val)
            return ms if ms > 1e11 else ms * 1000
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _parse_ms_dt(val: Any) -> Optional[datetime]:
        ms = BNBEventCalendar._parse_ms(val)
        if ms is None:
            return None
        return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)

    @staticmethod
    def _parse_iso(val: Any) -> Optional[datetime]:
        if not val:
            return None
        try:
            dt = datetime.fromisoformat(str(val).replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return None

    @staticmethod
    def _dt_iso(dt: Optional[datetime]) -> Optional[str]:
        return dt.astimezone(timezone.utc).isoformat(timespec="seconds") if dt else None
