"""双模主分析：provider 列表与报告块。"""

from bnb_quant_tool.llm_provider import (
    format_ai_analyses_report_block,
    is_dual_mode,
    list_analyzer_providers,
)


def test_list_analyzer_providers_dual():
    cfg = {
        "llm": {
            "mode": "dual",
            "analyzer_provider": "deepseek",
            "dual_providers": ["deepseek", "qianwen"],
        },
        "deepseek": {"api_key": "sk-ds-test", "model": "deepseek-chat"},
        "qianwen": {
            "api_key": "sk-qw-test",
            "model": "qwen3.7-plus",
            "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        },
    }
    assert is_dual_mode(cfg)
    assert list_analyzer_providers(cfg) == ["deepseek", "qianwen"]


def test_format_ai_analyses_report_block_both():
    block = format_ai_analyses_report_block(
        by_provider={
            "deepseek": {
                "signal": "卖出",
                "confidence": 0.62,
                "trend": "看跌",
                "analysis": "DS 空头",
                "self_reflection": "规避追多",
                "_provider_label": "DeepSeek",
                "_model": "deepseek-chat",
            },
            "qianwen": {
                "signal": "持有",
                "confidence": 0.55,
                "trend": "震荡",
                "analysis": "千问观望",
                "self_reflection": "",
                "_provider_label": "千问",
                "_model": "qwen3.7-plus",
            },
        },
        note="双模主分析分歧",
    )
    assert "[3.1] AI 主分析 (DeepSeek)" in block
    assert "[3.2] AI 主分析 (千问)" in block
    assert "卖出" in block and "持有" in block
    assert "多模备注" in block
    assert "双模主分析分歧" in block
