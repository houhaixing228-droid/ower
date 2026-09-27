"""对话历史。

按 `session_id` 分开存：契约 §5 要求同一个 session_id 的多次请求算同一段对话，
而**不同 session_id 之间不能串线**。
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Optional

MAX_TURNS = 6
MAX_SESSIONS = 500


class SessionStore:
    """每个 session 各自保留最近几轮对话，够解追问就行。"""

    def __init__(self, max_sessions: int = MAX_SESSIONS, max_turns: int = MAX_TURNS) -> None:
        # 用 OrderedDict 当 LRU：超上限时丢最久没动静的那个会话，防止长期跑内存涨上去。
        self._sessions: "OrderedDict[str, list[dict]]" = OrderedDict()
        self._lock = threading.Lock()
        self.max_sessions = max_sessions
        self.max_turns = max_turns

    def history(self, session_id: Optional[str]) -> list[dict]:
        """返回这个会话的历史。没有 session_id 的请求当作一次性会话，不读历史。"""
        if not session_id:
            return []
        with self._lock:
            turns = self._sessions.get(session_id)
            if turns is None:
                return []
            self._sessions.move_to_end(session_id)
            return list(turns)

    def append(self, session_id: Optional[str], turn: dict) -> None:
        """记一轮。没有 session_id 就不记——它没有下一轮可以追。"""
        if not session_id:
            return
        with self._lock:
            turns = self._sessions.get(session_id)
            if turns is None:
                turns = []
                self._sessions[session_id] = turns
            else:
                self._sessions.move_to_end(session_id)
            turns.append(turn)
            del turns[: max(0, len(turns) - self.max_turns)]
            while len(self._sessions) > self.max_sessions:
                self._sessions.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._sessions.clear()
