"""
胜率学习优化 — 已合并至 learning_analytics，本模块保留兼容导入。

新代码请使用:
  from bnb_quant_tool.learning_analytics import (
      build_win_rate_context,
      apply_vote_adjustments,
      ...
  )
"""

from bnb_quant_tool.learning_analytics import (  # noqa: F401
    DEFAULT_CFG,
    WIN_RATE_DEFAULT_CFG,
    apply_direction_blocks,
    apply_vote_adjustments,
    build_win_rate_context,
    format_win_rate_cockpit_lines,
    format_win_rate_for_prompt,
    gate_adjustments_from_context,
    resolve_win_rate_config,
)

__all__ = [
    "DEFAULT_CFG",
    "WIN_RATE_DEFAULT_CFG",
    "apply_direction_blocks",
    "apply_vote_adjustments",
    "build_win_rate_context",
    "format_win_rate_cockpit_lines",
    "format_win_rate_for_prompt",
    "gate_adjustments_from_context",
    "resolve_win_rate_config",
]
