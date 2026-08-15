"""LLM provider / 路由逻辑回归：缺 key 不计票、凭据不串台、thinking 配置生效。"""

from bnb_quant_tool.llm_provider import (
    _lookup_api_key,
    _providers_with_keys,
    _thinking_overrides,
    build_llm_analyzer_for,
    get_llm_credentials,
    list_analyzer_providers,
    list_council_providers,
)


def _base_cfg(**overrides):
    cfg = {
        "llm": {
            "mode": "multi",
            "analyzer_provider": "consensus",
            "analyzer_providers": ["deepseek", "qianwen", "volcengine"],
            "council_providers": ["deepseek"],
            "council_fallback_provider": "deepseek",
        },
        "deepseek": {
            "api_key": "sk-ds-test",
            "model": "deepseek-v4-pro",
            "base_url": "https://api.deepseek.com",
            "reasoning_effort": "medium",
            "thinking": {"type": "disabled"},
        },
        "qianwen": {
            "api_key": "sk-qw-test",
            "model": "qwen3.7-plus",
            "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        },
        "volcengine": {
            "api_key": "sk-volc-test",
            "model": "doubao-seed-2-1-pro-260628",
            "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        },
    }
    cfg.update(overrides)
    return cfg


def test_lookup_api_key_no_cross_fallback():
    cfg = _base_cfg()
    cfg["qianwen"] = dict(cfg["qianwen"])
    cfg["qianwen"]["api_key"] = ""
    assert _lookup_api_key(cfg, "qianwen") == ""
    assert _lookup_api_key(cfg, "deepseek") == "sk-ds-test"


def test_providers_with_keys_skips_missing_key():
    """缺 key 的千问不得因 deepseek 回退被算进列表（否则双计票）。"""
    cfg = _base_cfg()
    cfg["qianwen"] = dict(cfg["qianwen"])
    cfg["qianwen"]["api_key"] = ""
    assert _providers_with_keys(cfg, ["deepseek", "qianwen", "volcengine"]) == [
        "deepseek",
        "volcengine",
    ]
    assert list_analyzer_providers(cfg) == ["deepseek", "volcengine"]


def test_get_llm_credentials_fallback_flag():
    cfg = _base_cfg()
    cfg["qianwen"] = dict(cfg["qianwen"])
    cfg["qianwen"]["api_key"] = ""
    # 默认仍可回退 deepseek（单路工具用）
    fb = get_llm_credentials(cfg, provider="qianwen", fallback=True)
    assert fb["provider"] == "deepseek"
    assert fb["api_key"] == "sk-ds-test"
    # 按名构建分析器禁止串台
    no_fb = get_llm_credentials(cfg, provider="qianwen", fallback=False)
    assert no_fb["provider"] == "qianwen"
    assert no_fb["api_key"] == ""


def test_council_defaults_to_deepseek():
    cfg = _base_cfg()
    assert list_council_providers(cfg) == ["deepseek"]


def test_council_empty_when_configured_key_missing():
    cfg = _base_cfg()
    cfg["deepseek"] = dict(cfg["deepseek"])
    cfg["deepseek"]["api_key"] = ""
    assert list_council_providers(cfg) == []


def test_thinking_overrides_from_config():
    cfg = _base_cfg()
    thinking_type, effort = _thinking_overrides(cfg, "deepseek")
    assert thinking_type == "disabled"
    assert effort == "medium"


def test_build_analyzer_for_uses_config_thinking():
    cfg = _base_cfg()
    analyzer, creds = build_llm_analyzer_for(cfg, "deepseek")
    assert creds["provider"] == "deepseek"
    assert analyzer.thinking_type == "disabled"
    assert analyzer.reasoning_effort == "medium"


def test_synthesize_tie_and_min_agree_force_wait():
    from bnb_quant_tool.llm_provider import synthesize_provider_analyses

    cfg = {"llm": {"synthesis_min_agree": 2, "synthesis_min_weight_share": 0.55}}
    # 2 多 vs 2 空 等权 → 必须 WAIT（旧逻辑会因 and 门控漏成 LONG）
    tied = synthesize_provider_analyses(
        {
            "deepseek": {"signal": "买入", "confidence": 0.5, "_provider_label": "DS"},
            "qianwen": {"signal": "买入", "confidence": 0.5, "_provider_label": "QW"},
            "volcengine": {"signal": "卖出", "confidence": 0.5, "_provider_label": "火山"},
            "extra": {"signal": "卖出", "confidence": 0.5, "_provider_label": "X"},
        },
        providers=["deepseek", "qianwen", "volcengine", "extra"],
        config=cfg,
    )
    assert tied["trade_suggestion"] == "WAIT"

    # 单票存活：权重占比 100% 但票数 < min_agree → WAIT
    single = synthesize_provider_analyses(
        {
            "deepseek": {"signal": "买入", "confidence": 0.8, "_provider_label": "DS"},
            "qianwen": {
                "signal": "持有",
                "confidence": 0.2,
                "_error": "timeout",
                "_degraded": True,
                "_provider_label": "QW",
            },
        },
        providers=["deepseek", "qianwen"],
        config=cfg,
    )
    assert single["trade_suggestion"] == "WAIT"

    # 2 同意且权重足够 → LONG
    ok = synthesize_provider_analyses(
        {
            "deepseek": {"signal": "买入", "confidence": 0.7, "_provider_label": "DS"},
            "qianwen": {"signal": "买入", "confidence": 0.6, "_provider_label": "QW"},
            "volcengine": {"signal": "卖出", "confidence": 0.4, "_provider_label": "火山"},
        },
        providers=["deepseek", "qianwen", "volcengine"],
        config=cfg,
    )
    assert ok["trade_suggestion"] == "LONG"


def test_macro_prior_reads_news_polarity():
    from bnb_quant_tool.agents.llm_trader import LLMTrader
    from bnb_quant_tool.agents.personas import PERSONA_BY_ID
    from bnb_quant_tool.agents.base import MarketContext

    trader = LLMTrader(PERSONA_BY_ID["macro"], use_llm=False, api_key="")
    ctx = MarketContext(
        symbol="BNBUSDT",
        timeframe="1h",
        current_price=600.0,
        indicators={},
        trade_advice={},
        news_summary={"polarity": "bullish", "confidence": 0.8},
    )
    score, evidence, _ = trader._prior_macro(ctx)
    assert score > 0
    assert any("新闻偏多" in e for e in evidence)
