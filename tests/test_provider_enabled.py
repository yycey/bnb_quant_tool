"""Provider enabled 开关：关闭 DS/千问后只跑启用的家。"""
from __future__ import annotations

from bnb_quant_tool.llm_provider import (
    first_enabled_provider,
    is_provider_enabled,
    list_analyzer_providers,
    list_council_providers,
    synthesize_provider_analyses,
)


def test_provider_enabled_filters_analyzers():
    cfg = {
        "deepseek": {"enabled": False, "api_key": "sk-ds"},
        "qianwen": {"enabled": False, "api_key": "sk-qw"},
        "volcengine": {"enabled": True, "api_key": "sk-volc"},
        "llm": {
            "mode": "multi",
            "analyzer_providers": ["deepseek", "qianwen", "volcengine"],
            "council_providers": ["deepseek"],
            "council_fallback_provider": "deepseek",
            "synthesis_min_agree": 2,
        },
    }
    assert is_provider_enabled(cfg, "deepseek") is False
    assert is_provider_enabled(cfg, "volcengine") is True
    assert list_analyzer_providers(cfg) == ["volcengine"]
    # council 列表里只有已关的 DS → 回退到启用的豆包
    assert list_council_providers(cfg) == ["volcengine"]
    assert first_enabled_provider(cfg) == "volcengine"


def test_single_provider_synthesis_can_open():
    cfg = {
        "volcengine": {"enabled": True, "api_key": "sk-volc"},
        "deepseek": {"enabled": False, "api_key": "sk-ds"},
        "qianwen": {"enabled": False, "api_key": "sk-qw"},
        "llm": {
            "mode": "multi",
            "analyzer_providers": ["volcengine"],
            "synthesis_min_agree": 2,  # 配置仍为 2，代码应按启用数降到 1
            "synthesis_min_weight_share": 0.55,
        },
    }
    out = synthesize_provider_analyses(
        {
            "volcengine": {
                "signal": "买入",
                "confidence": 0.75,
                "analysis": "偏多",
                "trade_suggestion": "LONG",
            }
        },
        providers=["volcengine"],
        config=cfg,
    )
    assert out["trade_suggestion"] == "LONG"
