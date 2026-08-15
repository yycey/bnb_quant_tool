"""知识库持久化与路径稳定性测试。"""

import gc
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from bnb_quant_tool import data_localization
from bnb_quant_tool.capability_memory import CapabilityMemory
from bnb_quant_tool.data_localization import init_workspace, get_localized_db_path

MEM_CFG = {"capability_memory": {"enabled": True, "vector_backend": "tfidf"}}


def _reset_workspace_singleton() -> None:
    data_localization._localization_manager = None


def test_project_root_points_to_data_db():
    root = data_localization._resolve_project_root()
    assert (root / "data" / "ai_learning.db").name == "ai_learning.db"


def test_knowledge_card_persists_after_reopen():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "proj"
        root.mkdir()
        init_workspace(str(root))
        db = str(get_localized_db_path("ai_learning"))

        mem1 = CapabilityMemory(db, config=MEM_CFG)
        cid = mem1.save_knowledge_card(
            {
                "category": "trading_logic",
                "title": "测试卡片",
                "trigger_condition": "RSI>70",
                "action_rule": "WAIT",
                "lesson": "不要追高",
                "confidence": 0.7,
            },
            source="test",
        )
        assert cid is not None
        mem1.checkpoint_wal()
        mem1.reset_connection()
        del mem1
        gc.collect()

        mem2 = CapabilityMemory(db, config=MEM_CFG)
        cards = mem2.list_cards_for_ui()
        assert len(cards) == 1
        assert cards[0]["title"] == "测试卡片"
        assert mem2.count_active_cards() == 1
        mem2.reset_connection()
        del mem2
        gc.collect()
        _reset_workspace_singleton()
