"""会话隔离测试。

契约 §5：同一个 session_id 的多次请求算同一段对话，要支持追问；
**不同 session_id 之间不能串线**。

SessionStore 现在只有一条全局 `_turns` 列表，参数里的 session_id 从头到尾没用过，
于是任何两个会话都会互相看见对方的提问和回答。
"""

from __future__ import annotations

from kbqa.sessions import SessionStore


def _turn(text: str) -> dict:
    return {"question": text, "answer": "答：" + text, "answer_type": "data"}


def test_history_is_empty_at_first():
    store = SessionStore()
    assert store.history("s1") == []


def test_followup_sees_previous_turn_in_same_session():
    store = SessionStore()
    store.append("s1", _turn("6 月 S02 营业额多少"))
    history = store.history("s1")
    assert len(history) == 1
    assert history[0]["question"] == "6 月 S02 营业额多少"


def test_different_sessions_do_not_bleed():
    """两个 session 之间串线会让“那 7 月呢”接到别人那段对话上去。"""
    store = SessionStore()
    store.append("s1", _turn("6 月 S02 营业额多少"))
    assert store.history("s2") == []
    store.append("s2", _turn("外卖多久能退款"))
    questions = [turn["question"] for turn in store.history("s2")]
    assert questions == ["外卖多久能退款"]
    assert len(store.history("s1")) == 1


def test_missing_session_id_is_ephemeral():
    """没有 session_id 的请求不能读到别人的历史。"""
    store = SessionStore()
    store.append("s1", _turn("有身份的请求"))
    assert store.history(None) == []


def test_turns_are_capped_per_session():
    store = SessionStore(max_turns=2)
    for index in range(5):
        store.append("s1", _turn("第 %d 句" % index))
    questions = [turn["question"] for turn in store.history("s1")]
    assert questions == ["第 3 句", "第 4 句"]


def test_session_count_is_bounded():
    """会话数上限是防内存泄漏，不是把别人的历史还回来。"""
    store = SessionStore(max_sessions=2)
    store.append("s1", _turn("一"))
    store.append("s2", _turn("二"))
    store.append("s3", _turn("三"))
    assert [turn["question"] for turn in store.history("s3")] == ["三"]
    assert store.history("s1") == []
