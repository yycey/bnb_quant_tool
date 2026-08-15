"""统一 LLM 凭据解析 — DeepSeek / 千问 / 火山方舟 等 OpenAI 兼容接口。"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# 文档: https://platform.qianwenai.com/docs/developer-guides/getting-started/first-api-call
QIANWEN_DEFAULT_BASE = "https://dashscope.aliyuncs.com/compatible-mode/v1"
QIANWEN_DEFAULT_MODEL = "qwen3.7-plus"
DEEPSEEK_DEFAULT_BASE = "https://api.deepseek.com"
DEEPSEEK_DEFAULT_MODEL = "deepseek-chat"
# 文档: https://www.volcengine.com/docs/82379/1399008
VOLCENGINE_DEFAULT_BASE = "https://ark.cn-beijing.volces.com/api/v3"
# 可用 Model ID（见控制台模型列表），也可换成推理接入点 ep-xxxx
VOLCENGINE_DEFAULT_MODEL = "doubao-seed-2-1-pro-260628"

_PROVIDER_ALIASES = {
    "qwen": "qianwen",
    "tongyi": "qianwen",
    "dashscope": "qianwen",
    "通义": "qianwen",
    "千问": "qianwen",
    "ark": "volcengine",
    "volc": "volcengine",
    "volces": "volcengine",
    "huoshan": "volcengine",
    "doubao": "volcengine",
    "豆包": "volcengine",
    "火山": "volcengine",
    "火山方舟": "volcengine",
    "consensus": "consensus",
    "ensemble": "consensus",
    "综合": "consensus",
    "三家综合": "consensus",
}

_DEFAULTS = {
    "deepseek": (DEEPSEEK_DEFAULT_BASE, DEEPSEEK_DEFAULT_MODEL),
    "qianwen": (QIANWEN_DEFAULT_BASE, QIANWEN_DEFAULT_MODEL),
    "volcengine": (VOLCENGINE_DEFAULT_BASE, VOLCENGINE_DEFAULT_MODEL),
}

PROVIDER_LABELS = {
    "deepseek": "DeepSeek",
    "qianwen": "千问",
    "volcengine": "火山",
    "consensus": "三家综合",
    "rule_fallback": "规则引擎",
}

PROVIDER_SHORT = {
    "deepseek": "DS",
    "qianwen": "千问",
    "volcengine": "火山",
    "consensus": "综合",
    "rule_fallback": "规则",
}

_ENV_KEY_NAMES = {
    "deepseek": ("DEEPSEEK_API_KEY",),
    "qianwen": ("QIANWEN_API_KEY", "DASHSCOPE_API_KEY"),
    "volcengine": ("ARK_API_KEY", "VOLCENGINE_API_KEY", "HUOSHAN_API_KEY"),
}


def _section(config: dict, name: str) -> dict:
    val = (config or {}).get(name)
    return val if isinstance(val, dict) else {}


def normalize_provider(name: Optional[str]) -> str:
    raw = (name or "deepseek").strip().lower()
    return _PROVIDER_ALIASES.get(raw, raw) or "deepseek"


def is_provider_enabled(config: Optional[dict], name: Optional[str]) -> bool:
    """provider 段 enabled 开关；缺省 True（兼容旧配置）。"""
    pname = normalize_provider(name)
    if pname not in _DEFAULTS:
        return False
    section = _section(config or {}, pname)
    if "enabled" not in section:
        return True
    return bool(section.get("enabled"))


def is_dual_mode(config: Optional[dict] = None) -> bool:
    """是否多 LLM 主分析（DeepSeek / 千问 / 火山等并行）。"""
    llm = _section(config or {}, "llm")
    mode = str(llm.get("mode") or "").strip().lower()
    provider = str(llm.get("provider") or "").strip().lower()
    return mode in ("dual", "both", "multi", "triple") or provider in (
        "dual",
        "both",
        "multi",
        "triple",
        "consensus",
    )


def _lookup_api_key(cfg: dict, name: str) -> str:
    """只读指定 provider 自己的 key（配置段 + 环境变量），不做跨家回退。

    避免缺 key 时经 get_llm_credentials 静默落到 deepseek，导致
    _providers_with_keys 把千问/火山也算进列表 → 双计票。
    """
    name = normalize_provider(name)
    section = _section(cfg, name)
    api_key = str(section.get("api_key") or "").strip()
    if api_key:
        return api_key
    for env_name in _ENV_KEY_NAMES.get(name, ()):
        api_key = str(os.environ.get(env_name) or "").strip()
        if api_key:
            return api_key
    return ""


def _providers_with_keys(cfg: dict, raw_list: List[Any]) -> List[str]:
    out: List[str] = []
    for item in raw_list or []:
        name = normalize_provider(str(item))
        if name in ("dual", "both", "multi", "triple", "consensus", "ensemble") or name in out:
            continue
        if name not in _DEFAULTS:
            continue
        if not is_provider_enabled(cfg, name):
            continue
        if _lookup_api_key(cfg, name):
            out.append(name)
    return out


def first_enabled_provider(config: Optional[dict] = None) -> str:
    """第一个启用且有 key 的分析家；否则 deepseek（兼容旧默认）。"""
    cfg = config or {}
    llm = _section(cfg, "llm")
    raw = (
        llm.get("analyzer_providers")
        or llm.get("dual_providers")
        or ["deepseek", "qianwen", "volcengine"]
    )
    found = _providers_with_keys(cfg, list(raw))
    if found:
        return found[0]
    for name in ("volcengine", "deepseek", "qianwen"):
        if is_provider_enabled(cfg, name) and _lookup_api_key(cfg, name):
            return name
    return "deepseek"


def list_council_providers(config: Optional[dict] = None) -> List[str]:
    """议会 LLM 列表。

    优先 llm.council_providers（控制成本）；未配置时：
    - multi 模式默认只用 analyzer 回退家，避免 6×3 人狂烧 token
    - 若显式 council_use_all_analyzers=true，则跟主分析同队
    指定家全部缺 key（但仍 enabled）时返回空列表，禁止静默串台。
    指定家全部 enabled=false 时，回退到任一启用分析家。
    未配置 council_providers 时按 analyzer / fallback 规则解析。
    """
    cfg = config or {}
    llm = _section(cfg, "llm")
    if llm.get("council_providers"):
        requested = list(llm.get("council_providers") or [])
        found = _providers_with_keys(cfg, requested)
        if found:
            return found
        # 显式配置的议会家：若仍有启用但缺 key → 空列表（禁止静默串台）
        # 若全部被 enabled=false 关掉 → 回退到任一启用分析家
        any_enabled = False
        for item in requested:
            name = normalize_provider(str(item))
            if name not in _DEFAULTS:
                continue
            if is_provider_enabled(cfg, name):
                any_enabled = True
                break
        if any_enabled:
            return []
        fb = first_enabled_provider(cfg)
        return [fb] if _lookup_api_key(cfg, fb) and is_provider_enabled(cfg, fb) else []

    if is_dual_mode(cfg):
        if llm.get("council_use_all_analyzers"):
            return _providers_with_keys(
                cfg,
                list(llm.get("analyzer_providers") or llm.get("dual_providers") or []),
            )
        # 默认：议会单队，主分析仍可多家
        fallback = normalize_provider(
            llm.get("council_fallback_provider")
            or llm.get("analyzer_provider")
            or first_enabled_provider(cfg)
        )
        if fallback in ("consensus", "ensemble", "dual", "multi"):
            fallback = first_enabled_provider(cfg)
        if is_provider_enabled(cfg, fallback) and _lookup_api_key(cfg, fallback):
            return [fallback]
        out = _providers_with_keys(
            cfg, list(llm.get("dual_providers") or ["deepseek", "qianwen", "volcengine"])
        )
        return out[:1]

    name = normalize_provider(llm.get("provider") or first_enabled_provider(cfg))
    if name in ("dual", "both", "multi", "triple", "consensus"):
        name = first_enabled_provider(cfg)
    if is_provider_enabled(cfg, name) and _lookup_api_key(cfg, name):
        return [name]
    return []


def list_analyzer_providers(config: Optional[dict] = None) -> List[str]:
    """主分析应调用的 provider：multi=全部有 key 且 enabled；单模=当前 analyzer。"""
    cfg = config or {}
    llm = _section(cfg, "llm")
    if is_dual_mode(cfg):
        raw = (
            llm.get("analyzer_providers")
            or llm.get("dual_providers")
            or ["deepseek", "qianwen", "volcengine"]
        )
        return _providers_with_keys(cfg, list(raw))

    name = normalize_provider(llm.get("provider") or "deepseek")
    if name in ("dual", "both", "multi", "triple", "consensus"):
        name = normalize_provider(llm.get("analyzer_provider") or "deepseek")
        if name in ("consensus", "ensemble"):
            name = first_enabled_provider(cfg)
    if is_provider_enabled(cfg, name) and _lookup_api_key(cfg, name):
        return [name]
    # 指定家关闭/无 key → 回退到任一启用家
    fb = first_enabled_provider(cfg)
    if fb != name and is_provider_enabled(cfg, fb) and _lookup_api_key(cfg, fb):
        return [fb]
    return []


def get_llm_credentials(
    config: Optional[dict] = None,
    *,
    provider: Optional[str] = None,
    fallback: bool = True,
) -> Dict[str, str]:
    """解析 LLM 凭据。

    优先级:
    1. 显式 provider 参数
    2. dual 模式下 analyzer_provider（单路分析器用；consensus 则回退首选启用家）
    3. config.llm.provider
    4. 第一个 enabled 且有 key 的 provider

    若所选 provider 无 api_key / 已关闭且 fallback=True，自动回退到启用家。
    按名构建分析器时应传 fallback=False，避免静默串台。
    """
    cfg = config or {}
    llm = _section(cfg, "llm")

    if provider:
        name = normalize_provider(provider)
    elif is_dual_mode(cfg):
        name = normalize_provider(llm.get("analyzer_provider") or "deepseek")
    else:
        name = normalize_provider(llm.get("provider") or "deepseek")

    if name in ("dual", "both", "multi", "triple", "consensus", "ensemble"):
        # consensus 不是真实 API；单路调用回退到有 key 的首选
        fb = normalize_provider(
            llm.get("council_fallback_provider") or first_enabled_provider(cfg)
        )
        if fb in ("consensus", "ensemble", "dual", "multi"):
            fb = first_enabled_provider(cfg)
        name = fb

    section = _section(cfg, name)
    api_key = _lookup_api_key(cfg, name) if is_provider_enabled(cfg, name) else ""
    if (not api_key or not is_provider_enabled(cfg, name)) and fallback:
        alt = first_enabled_provider(cfg)
        if alt != name:
            name = alt
            section = _section(cfg, name)
            api_key = _lookup_api_key(cfg, name)

    base_default, model_default = _DEFAULTS.get(name, _DEFAULTS["deepseek"])
    base_url = str(section.get("base_url") or base_default).rstrip("/")
    model = str(section.get("model") or model_default).strip() or model_default
    return {
        "provider": name,
        "api_key": api_key,
        "base_url": base_url,
        "model": model,
        "label": PROVIDER_LABELS.get(name, name),
        "short": PROVIDER_SHORT.get(name, name[:2].upper()),
    }


def _llm_request_timeout(config: Optional[dict] = None) -> float:
    """单次 LLM HTTP / 墙钟超时（秒）。默认 15，防止循环 hung。"""
    llm = _section(config or {}, "llm")
    raw = llm.get("request_timeout_seconds", llm.get("analysis_timeout_seconds", 15))
    try:
        val = float(raw)
    except (TypeError, ValueError):
        val = 15.0
    return max(3.0, min(val, 120.0))


def build_llm_analyzer(config: Optional[dict] = None, **overrides: Any):
    """按全局 / analyzer_provider 构建 OpenAI 兼容分析器。"""
    from .ai_analyzer import DeepSeekAnalyzer

    creds = get_llm_credentials(config)
    timeout = overrides.get("request_timeout")
    if timeout is None:
        timeout = _llm_request_timeout(config)
    thinking_type, reasoning_effort = _thinking_overrides(config, creds["provider"])
    return DeepSeekAnalyzer(
        api_key=str(overrides.get("api_key") or creds["api_key"]),
        model=str(overrides.get("model") or creds["model"]),
        base_url=str(overrides.get("base_url") or creds["base_url"]),
        request_timeout=float(timeout),
        thinking_type=str(overrides.get("thinking_type") or thinking_type),
        reasoning_effort=str(overrides.get("reasoning_effort") or reasoning_effort),
    )


def _thinking_overrides(config: Optional[dict], provider: str) -> Tuple[str, str]:
    """从 config.<provider>.thinking / reasoning_effort 读取主分析默认思考参数。"""
    section = _section(config or {}, provider)
    thinking = section.get("thinking")
    thinking_type = "enabled"
    if isinstance(thinking, dict) and thinking.get("type"):
        thinking_type = str(thinking.get("type")).strip() or "enabled"
    elif isinstance(thinking, str) and thinking.strip():
        thinking_type = thinking.strip()
    reasoning_effort = str(section.get("reasoning_effort") or "high").strip() or "high"
    return thinking_type, reasoning_effort


def build_llm_analyzer_for(config: Optional[dict], provider: str):
    """按指定 provider 构建分析器（不做跨家 key 回退）。"""
    from .ai_analyzer import DeepSeekAnalyzer

    creds = get_llm_credentials(config, provider=provider, fallback=False)
    thinking_type, reasoning_effort = _thinking_overrides(config, creds["provider"])
    analyzer = DeepSeekAnalyzer(
        api_key=str(creds["api_key"]),
        model=str(creds["model"]),
        base_url=str(creds["base_url"]),
        request_timeout=_llm_request_timeout(config),
        thinking_type=thinking_type,
        reasoning_effort=reasoning_effort,
    )
    return analyzer, creds


def _normalize_signal_bucket(signal: Any) -> str:
    """映射各家信号 → long / short / wait。"""
    s = str(signal or "").strip().upper()
    if not s:
        return "wait"
    if any(k in s for k in ("LONG", "BUY", "买入", "看多", "做多", "上涨")):
        return "long"
    if any(k in s for k in ("SHORT", "SELL", "卖出", "看空", "做空", "下跌")):
        return "short"
    return "wait"


def _signal_label(bucket: str) -> str:
    return {"long": "买入", "short": "卖出", "wait": "持有"}.get(bucket, "持有")


def _trend_label(bucket: str) -> str:
    return {"long": "上涨", "short": "下跌", "wait": "震荡"}.get(bucket, "震荡")


def synthesize_provider_analyses(
    by_provider: Dict[str, Any],
    *,
    providers: Optional[List[str]] = None,
    config: Optional[dict] = None,
) -> Dict[str, Any]:
    """三家（或多家）主分析 → 加权多数综合结论。

    规则：
    - 有效票（无 _error）按 confidence 加权投票
    - 方向需至少 2 票（或加权占比≥0.55）才开多/空，否则 WAIT
    - 分歧时压低置信度
    """
    llm = _section(config or {}, "llm")
    min_agree = int(llm.get("synthesis_min_agree", 2) or 2)
    min_share = float(llm.get("synthesis_min_weight_share", 0.55) or 0.55)
    # 单家启用时 min_agree=2 会永远 WAIT → 按启用分析家数降级
    try:
        n_enabled = len(list_analyzer_providers(config))
    except Exception:
        n_enabled = 0
    if n_enabled > 0:
        min_agree = min(min_agree, n_enabled)
    min_agree = max(1, min_agree)

    order = list(providers or by_provider.keys())
    votes: Dict[str, float] = {"long": 0.0, "short": 0.0, "wait": 0.0}
    counts: Dict[str, int] = {"long": 0, "short": 0, "wait": 0}
    valid: List[Tuple[str, Dict[str, Any], str, float]] = []

    for name in order:
        data = by_provider.get(name)
        if not isinstance(data, dict):
            continue
        if data.get("_error") or data.get("_degraded"):
            continue
        try:
            conf = float(data.get("confidence") or 0.4)
        except (TypeError, ValueError):
            conf = 0.4
        # 解析失败/空壳票不进综合
        if conf < 0.15:
            continue
        bucket = _normalize_signal_bucket(data.get("signal") or data.get("trade_suggestion"))
        conf = max(0.15, min(conf, 1.0))
        votes[bucket] += conf
        counts[bucket] += 1
        valid.append((name, data, bucket, conf))

    if not valid:
        # 全部失败：保守观望
        return {
            "signal": "持有",
            "confidence": 0.2,
            "trend": "震荡",
            "analysis": "三家主分析均失败，综合结论强制观望。",
            "self_reflection": "无有效票，禁止开仓。",
            "trade_suggestion": "WAIT",
            "_provider": "consensus",
            "_provider_label": "三家综合",
            "_model": "ensemble",
            "_ensemble": {
                "mode": "fallback_wait",
                "votes": {},
                "counts": {},
                "agreement": 0.0,
            },
        }

    total_w = sum(votes.values()) or 1.0
    # 平票时勿让 dict 插入顺序偏向 long；权重相同则观望
    winner = max(votes.keys(), key=lambda k: (votes[k], counts[k]))
    agree_n = counts[winner]
    share = votes[winner] / total_w
    # 权重并列最高（如 2 多 vs 2 空）→ 强制观望
    tied = [
        k for k in ("long", "short", "wait")
        if abs(votes[k] - votes[winner]) < 1e-9 and counts[k] == counts[winner]
    ]
    if len(tied) > 1 and winner in ("long", "short"):
        winner = "wait"

    # 方向性结论须同时满足票数与权重占比；任一不足 → 观望
    if winner in ("long", "short"):
        if agree_n < min_agree or share < min_share:
            winner = "wait"
        # 存活票不足 min_agree 时，禁止单票冒充「三家综合」开方向
        elif len(valid) < min_agree:
            winner = "wait"

    win_confs = [c for _, _, b, c in valid if b == winner] or [
        c for _, _, _, c in valid
    ]
    base_conf = sum(win_confs) / len(win_confs)
    agreement = share if winner != "wait" else max(share, counts["wait"] / max(len(valid), 1))
    # 全票一致加成；分歧惩罚
    if agree_n == len(valid) and winner in ("long", "short"):
        final_conf = min(0.92, base_conf * 1.08)
    elif winner in ("long", "short"):
        final_conf = max(0.35, base_conf * (0.75 + 0.25 * share))
    else:
        final_conf = max(0.35, min(0.75, 0.45 + (1.0 - share) * 0.2))

    detail_parts = []
    for name, data, bucket, conf in valid:
        lab = data.get("_provider_label") or PROVIDER_LABELS.get(name, name)
        detail_parts.append(f"{lab}={_signal_label(bucket)}@{conf:.0%}")
    vote_line = " / ".join(detail_parts)

    analysis_bits = []
    for name, data, bucket, conf in valid:
        lab = data.get("_provider_label") or PROVIDER_LABELS.get(name, name)
        snippet = str(data.get("analysis") or "")[:280].strip()
        analysis_bits.append(f"【{lab}｜{_signal_label(bucket)} {conf:.0%}】{snippet}")

    reflection_bits = []
    for name, data, bucket, conf in valid:
        lab = data.get("_provider_label") or PROVIDER_LABELS.get(name, name)
        ref = str(data.get("self_reflection") or "").strip()
        if ref:
            reflection_bits.append(f"{lab}: {ref[:160]}")

    signal = _signal_label(winner)
    result = {
        "signal": signal,
        "confidence": round(final_conf, 4),
        "trend": _trend_label(winner),
        "analysis": (
            f"三家综合（加权多数）→ {signal}（共识{agree_n}/{len(valid)}，权重占比{share:.0%}）。\n"
            f"分票: {vote_line}\n\n" + "\n\n".join(analysis_bits)
        ),
        "self_reflection": (
            f"综合裁决: {signal}；同意{agree_n}/{len(valid)}。\n"
            + ("\n".join(reflection_bits) if reflection_bits else "各家反思已并入综合分析。")
        ),
        "trade_suggestion": (
            "LONG" if winner == "long" else ("SHORT" if winner == "short" else "WAIT")
        ),
        "_provider": "consensus",
        "_provider_label": "三家综合",
        "_model": "ensemble:" + "+".join(n for n, _, _, _ in valid),
        "_ensemble": {
            "mode": "weighted_majority",
            "winner": winner,
            "votes": {k: round(v, 4) for k, v in votes.items()},
            "counts": dict(counts),
            "agreement": round(float(agreement), 4),
            "share": round(float(share), 4),
            "valid_providers": [n for n, _, _, _ in valid],
            "detail": vote_line,
        },
    }
    return result


def run_market_analyses(
    config: Optional[dict],
    df,
    indicators: Dict[str, Any],
    *,
    learner=None,
    multi_timeframe: Optional[Dict[str, Any]] = None,
    **analyze_kwargs: Any,
) -> Dict[str, Any]:
    """多模并行跑各家主分析，再综合成最终结论。

    超时保护：每家请求受 llm.request_timeout_seconds（默认 15s）约束；
    全部失败/超时则降级规则引擎（MTF+均线+1.5%ATR止损），仍失败则 WAIT+告警。

    Returns:
        primary: 综合结论（或单模唯一结论）
        by_provider: {provider: analysis_dict} 含各家原文 + 可选 consensus
        primary_provider: str
        providers: list
        note: 分歧/综合说明
        ensemble: 综合细节
    """
    from concurrent.futures import ThreadPoolExecutor, wait

    cfg = config or {}
    llm = _section(cfg, "llm")
    providers = list_analyzer_providers(cfg)
    if not providers:
        raise ValueError("未配置可用的 LLM api_key（deepseek / qianwen / volcengine）")

    timeout_s = _llm_request_timeout(cfg)
    rule_fb_cfg = llm.get("rule_fallback") if isinstance(llm.get("rule_fallback"), dict) else {}
    fallback_enabled = bool(llm.get("rule_fallback_enabled", rule_fb_cfg.get("enabled", True)))

    want_synth = bool(llm.get("synthesis", True)) and len(providers) > 1
    primary_cfg = normalize_provider(llm.get("analyzer_provider") or "consensus")
    if primary_cfg in ("consensus", "ensemble", "综合") or want_synth:
        primary_name = "consensus"
    else:
        primary_name = primary_cfg if primary_cfg in providers else providers[0]

    # analyze_market_data 不接受的额外字段已在本函数消费
    analyze_kwargs = dict(analyze_kwargs)
    if multi_timeframe is None and isinstance(analyze_kwargs.get("multi_timeframe"), dict):
        multi_timeframe = analyze_kwargs.pop("multi_timeframe")
    else:
        analyze_kwargs.pop("multi_timeframe", None)
    analyze_kwargs.pop("learner", None)

    def _one(name: str) -> tuple:
        analyzer, creds = build_llm_analyzer_for(cfg, name)
        result = analyzer.analyze_market_data(df, indicators, **analyze_kwargs)
        if not isinstance(result, dict):
            result = {"signal": "持有", "confidence": 0.3, "analysis": str(result)}
        result = dict(result)
        result["_provider"] = creds["provider"]
        result["_provider_label"] = creds.get("label") or PROVIDER_LABELS.get(name, name)
        result["_model"] = creds.get("model") or ""
        return name, result

    def _timeout_stub(name: str, err: str) -> Dict[str, Any]:
        return {
            "signal": "持有",
            "confidence": 0.2,
            "trend": "震荡",
            "analysis": f"{PROVIDER_LABELS.get(name, name)} 主分析失败: {err}",
            "self_reflection": "",
            "trade_suggestion": "WAIT",
            "_provider": name,
            "_provider_label": PROVIDER_LABELS.get(name, name),
            "_error": err[:200],
            "_timeout": "超时" in err or "timeout" in err.lower(),
            "_degraded": True,
        }

    by_provider: Dict[str, Any] = {}
    errors: Dict[str, str] = {}
    timed_out_any = False

    if len(providers) == 1:
        name = providers[0]
        pool = ThreadPoolExecutor(max_workers=1)
        try:
            fut = pool.submit(_one, name)
            try:
                pname, result = fut.result(timeout=timeout_s)
                by_provider[pname] = result
            except Exception as e:
                err = str(e)
                if "Timeout" in type(e).__name__ or "timeout" in err.lower():
                    err = f"LLM超时({timeout_s:.0f}s)"
                    timed_out_any = True
                errors[name] = err[:200]
                by_provider[name] = _timeout_stub(name, err)
        except Exception as e:
            err = str(e)[:200]
            errors[name] = err
            by_provider[name] = _timeout_stub(name, err)
        finally:
            # 超时后切勿 wait=True，否则主循环被挂死直到底层 HTTP 结束
            pool.shutdown(wait=False, cancel_futures=True)
    else:
        pool = ThreadPoolExecutor(max_workers=len(providers))
        try:
            futs = {pool.submit(_one, p): p for p in providers}
            done, not_done = wait(list(futs.keys()), timeout=timeout_s)
            for fut in not_done:
                name = futs[fut]
                timed_out_any = True
                err = f"LLM超时({timeout_s:.0f}s)"
                errors[name] = err
                by_provider[name] = _timeout_stub(name, err)
            for fut in done:
                name = futs[fut]
                try:
                    pname, result = fut.result()
                    by_provider[pname] = result
                except Exception as e:
                    err = str(e)
                    if "Timeout" in type(e).__name__ or "timeout" in err.lower():
                        err = f"LLM超时({timeout_s:.0f}s)"
                        timed_out_any = True
                    errors[name] = err[:200]
                    by_provider[name] = _timeout_stub(name, err)
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

    def _usable(data: Any) -> bool:
        if not isinstance(data, dict):
            return False
        if data.get("_error") or data.get("_timeout") or data.get("_degraded"):
            return False
        try:
            return float(data.get("confidence") or 0) >= 0.15
        except (TypeError, ValueError):
            return False

    usable_providers = [p for p in providers if _usable(by_provider.get(p))]
    # 全部不可用（超时 / API 错 / 解析失败）→ 规则引擎
    need_rule_fallback = fallback_enabled and not usable_providers

    rule_primary: Optional[Dict[str, Any]] = None
    if need_rule_fallback:
        from bnb_quant_tool.llm_rule_fallback import (
            build_rule_engine_analysis,
            record_llm_timeout_memory,
        )

        reason = "LLM超时" if timed_out_any else "LLM全部失败"
        rule_primary = build_rule_engine_analysis(
            indicators if isinstance(indicators, dict) else {},
            multi_timeframe=multi_timeframe,
            df=df,
            config=cfg,
            reason=reason,
        )
        by_provider["rule_fallback"] = rule_primary
        action = str(rule_primary.get("trade_suggestion") or "WAIT").upper()
        forced = bool(rule_primary.get("_rule_forced_wait"))
        try:
            record_llm_timeout_memory(
                cfg,
                learner=learner,
                reason=reason,
                providers_failed=dict(errors),
                rule_action=action,
                forced_wait=forced,
            )
        except Exception as e:
            logger.warning("record_llm_timeout_memory failed: %s", e)

    ensemble: Optional[Dict[str, Any]] = None
    note_parts: List[str] = []

    if rule_primary is not None and not usable_providers:
        primary = rule_primary
        primary_name = "rule_fallback"
        note_parts.append(
            f"LLM不可用已降级规则引擎→{rule_primary.get('trade_suggestion', 'WAIT')}"
        )
    elif len(providers) > 1 and want_synth and usable_providers:
        consensus = synthesize_provider_analyses(
            by_provider, providers=providers, config=cfg
        )
        by_provider["consensus"] = consensus
        ensemble = consensus.get("_ensemble") or {}
        primary = consensus
        primary_name = "consensus"
        detail = ensemble.get("detail") or ""
        winner = ensemble.get("winner") or "wait"
        note_parts.append(
            f"三家综合→{_signal_label(str(winner))} "
            f"(同意{ensemble.get('counts', {}).get(winner, 0)}/"
            f"{len(ensemble.get('valid_providers') or providers)})；分票: {detail}"
        )
    else:
        if primary_name == "consensus":
            primary_name = (usable_providers[0] if usable_providers else providers[0])
        primary = by_provider.get(primary_name) or next(iter(by_provider.values()))
        if rule_primary is not None and not _usable(primary):
            primary = rule_primary
            primary_name = "rule_fallback"
            note_parts.append("主结论不可用，已用规则引擎替代")
        if len(by_provider) > 1:
            sigs = {
                p: str((by_provider[p] or {}).get("signal") or "?")
                for p in providers
                if p in by_provider
            }
            uniq = {s for s in sigs.values()}
            detail = " / ".join(
                f"{PROVIDER_LABELS.get(p, p)}={sigs[p]}" for p in providers if p in sigs
            )
            if len(uniq) > 1:
                note_parts.append(
                    f"多模分歧: {detail}；跟单取 {PROVIDER_LABELS.get(primary_name, primary_name)}"
                )
            else:
                note_parts.append(
                    f"多模一致 ({next(iter(uniq)) if uniq else '?'})；"
                    f"主结论取 {PROVIDER_LABELS.get(primary_name, primary_name)}"
                )

    for p, err in errors.items():
        note_parts.append(f"{PROVIDER_LABELS.get(p, p)} 失败: {err}")

    return {
        "primary": primary,
        "by_provider": by_provider,
        "primary_provider": primary_name,
        "providers": providers,
        "note": "；".join(note_parts),
        "errors": errors,
        "ensemble": ensemble,
        "rule_fallback": bool(rule_primary is not None and primary_name == "rule_fallback"),
        "llm_timeout": timed_out_any,
    }


def format_ai_analyses_report_block(
    primary: Optional[Dict[str, Any]] = None,
    by_provider: Optional[Dict[str, Any]] = None,
    *,
    note: str = "",
    max_analysis_chars: Optional[int] = None,
) -> str:
    """报告用：逐家列出主分析（DeepSeek / 千问 / 火山）+ 综合结论。默认全文，不截断。"""
    lines: List[str] = []
    items: List[tuple] = []
    if by_provider:
        # 综合结论置顶，其余按固定顺序
        order = ["consensus", "rule_fallback", "deepseek", "qianwen", "volcengine"]
        seen = set()
        for name in order:
            data = by_provider.get(name)
            if isinstance(data, dict):
                items.append((name, data))
                seen.add(name)
        for name, data in by_provider.items():
            if name in seen or not isinstance(data, dict):
                continue
            items.append((name, data))
    elif primary:
        items.append((primary.get("_provider") or "deepseek", primary))

    if not items:
        return ""

    def _clip(text: Any, limit: Optional[int]) -> str:
        s = str(text or "")
        if limit is None or limit <= 0 or len(s) <= limit:
            return s
        return s[:limit] + "..."

    for idx, (name, ai) in enumerate(items, start=1):
        label = (
            ai.get("_provider_label")
            or PROVIDER_LABELS.get(name, name)
            or name
        )
        model = ai.get("_model") or ""
        if name == "consensus" or ai.get("_provider") == "consensus":
            title = "[3] AI 综合结论（三家加权多数）"
        elif len(items) == 1:
            title = f"[3] AI 主分析 ({label})"
        else:
            title = f"[3.{idx}] AI 主分析 ({label})"
        if model:
            title += f" · {model}"
        lines.append(title)
        lines.append("-" * 60)
        if ai.get("_error"):
            lines.append(f"  状态:      失败 — {ai.get('_error')}")
        ens = ai.get("_ensemble") or {}
        if ens:
            lines.append(f"  分票:      {ens.get('detail') or ens}")
            lines.append(
                f"  共识:      {ens.get('winner')} "
                f"权重占比 {float(ens.get('share') or 0):.0%}"
            )
        lines.append(f"  趋势:      {ai.get('trend', 'N/A')}")
        try:
            conf = float(ai.get("confidence") or 0)
        except (TypeError, ValueError):
            conf = 0.0
        lines.append(f"  置信度:    {conf:.2%}")
        lines.append(f"  信号:      {ai.get('signal', 'N/A')}")
        reflection = _clip(ai.get("self_reflection", ""), max_analysis_chars)
        analysis = _clip(ai.get("analysis", ""), max_analysis_chars)
        lines.append(f"  自我反思:  {reflection}")
        lines.append(f"  分析:      {analysis}")
        lines.append("")

    if note:
        lines.append(f"  多模备注:  {note}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def uses_deepseek_thinking(model: str, base_url: str = "") -> bool:
    """是否附加 DeepSeek 专属 thinking / reasoning_effort 字段。"""
    model_l = (model or "").lower()
    base_l = (base_url or "").lower()
    if is_qwen_compatible(model, base_url) or is_volcengine_compatible(model, base_url):
        return False
    if any(x in model_l for x in ("qwen", "tongyi", "doubao")):
        return False
    if "dashscope" in base_l or "qianwen" in base_l or "volces" in base_l or "volcengine" in base_l:
        return False
    if "deepseek" not in model_l and "deepseek" not in base_l:
        return False
    return "reasoner" in model_l or "v4" in model_l or "v5" in model_l


def is_qwen_compatible(model: str, base_url: str = "") -> bool:
    """是否千问 / DashScope 兼容接口。"""
    model_l = (model or "").lower()
    base_l = (base_url or "").lower()
    return any(x in model_l for x in ("qwen", "tongyi")) or "dashscope" in base_l or "qianwen" in base_l


def is_volcengine_compatible(model: str, base_url: str = "") -> bool:
    """是否火山方舟 / 豆包 Ark OpenAI 兼容接口。"""
    model_l = (model or "").lower()
    base_l = (base_url or "").lower()
    if any(x in base_l for x in ("volces.com", "volcengine", "ark.cn-beijing")):
        return True
    if model_l.startswith("ep-"):
        return True
    return any(x in model_l for x in ("doubao", "seedream", "seedance"))


def persona_base_id(trader_id: Optional[str]) -> str:
    """momentum__deepseek → momentum。"""
    tid = str(trader_id or "")
    if "__" in tid:
        return tid.split("__", 1)[0]
    return tid
