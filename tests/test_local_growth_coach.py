"""本地盈利成长教练测试。"""

from bnb_quant_tool.local_growth_coach import LocalGrowthCoach, growth_brief_for_prompt


def test_seed_when_few_trades():
    coach = LocalGrowthCoach({})
    plan = coach.diagnose(paper_stats={"total_trades": 3, "expectancy_r": 0.2})
    assert plan.stage == "SEED"
    assert "样本" in plan.next_lesson or "攒" in plan.next_lesson


def test_stop_bleed_on_negative_er():
    coach = LocalGrowthCoach({})
    plan = coach.diagnose(
        paper_stats={
            "total_trades": 20,
            "expectancy_r": -0.15,
            "win_rate": 0.45,
            "profit_factor": 0.8,
            "avg_win_r": 0.5,
            "avg_loss_r": -2.0,
            "gave_back_count": 2,
        }
    )
    assert plan.stage == "STOP_BLEED"
    assert plan.focus in ("exit_discipline", "open_quality")


def test_compound_when_healthy():
    coach = LocalGrowthCoach({})
    plan = coach.diagnose(
        paper_stats={
            "total_trades": 20,
            "expectancy_r": 0.18,
            "win_rate": 0.52,
            "profit_factor": 1.5,
            "avg_win_r": 1.0,
            "avg_loss_r": -0.7,
            "gave_back_count": 2,
        }
    )
    assert plan.stage == "COMPOUND"


def test_gave_back_forces_edge_exit_lesson():
    coach = LocalGrowthCoach({})
    plan = coach.diagnose(
        paper_stats={
            "total_trades": 20,
            "expectancy_r": 0.05,
            "win_rate": 0.5,
            "profit_factor": 1.1,
            "avg_win_r": 0.8,
            "avg_loss_r": -0.7,
            "gave_back_count": 8,
        },
        last_trade={"side": "LONG", "realized_pnl_usdt": -12, "mfe_r": 0.9},
    )
    assert plan.stage == "EDGE"
    assert "锁" in plan.next_lesson or "回吐" in plan.next_lesson or "0.6R" in plan.next_lesson


def test_growth_brief_empty_without_file(tmp_path, monkeypatch):
    monkeypatch.setenv("BNB_QUANT_WORKSPACE", str(tmp_path))
    # no plan file → empty brief is ok
    brief = growth_brief_for_prompt({"local_growth": {"path": str(tmp_path / "missing.json")}})
    assert brief == "" or "本地成长" in brief or brief == ""
