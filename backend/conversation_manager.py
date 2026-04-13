"""
conversation_manager.py

Shared, thread-safe conversation history store used by ALL agents.

Every agent call should:
  1. Call get_history(session_id) to retrieve the current history
  2. Pass the history to the agent's run function
  3. Call append_turn(session_id, user_msg, assistant_msg) after getting a response

History is stored in memory per session. For multi-process deployments, swap
_store for a Redis-backed implementation (see RedisHistoryStore below).

Usage example (in your chat router / view):
    from conversation_manager import history_store

    session_id = "user-123"   # from cookie, JWT, or session

    history = history_store.get(session_id)
    response = run_automation_agent(query, history=history)
    history_store.append(session_id, user_msg=query, assistant_msg=response)
"""
from __future__ import annotations
import threading
from collections import deque

# Maximum number of turns kept per session (older turns are dropped).
# Each turn = 1 user message + 1 assistant message = 2 entries.
# 20 turns → 40 entries, which is plenty for context without blowing the context window.
MAX_TURNS = 20


class InMemoryHistoryStore:
    """
    Thread-safe, per-session conversation history.

    Each history is a list of {"role": "user"|"assistant", "content": str} dicts,
    suitable for direct use as LangChain / OpenAI message history.
    """

    def __init__(self, max_turns: int = MAX_TURNS):
        self._lock = threading.Lock()
        self._store: dict[str, deque[dict]] = {}
        self._max_entries = max_turns * 2  # user + assistant per turn

    def get(self, session_id: str) -> list[dict]:
        """Return a copy of the history for this session (safe to mutate)."""
        with self._lock:
            return list(self._store.get(session_id, []))

    def append(self, session_id: str, user_msg: str, assistant_msg: str) -> None:
        """Append a user+assistant turn to the history, respecting the max size."""
        with self._lock:
            if session_id not in self._store:
                self._store[session_id] = deque(maxlen=self._max_entries)
            dq = self._store[session_id]
            dq.append({"role": "user", "content": user_msg})
            dq.append({"role": "assistant", "content": assistant_msg})

    def clear(self, session_id: str) -> None:
        """Reset the history for a session (e.g. on logout)."""
        with self._lock:
            self._store.pop(session_id, None)

    def all_sessions(self) -> list[str]:
        with self._lock:
            return list(self._store.keys())


# ── Singleton used everywhere ─────────────────────────────────────────────────
history_store = InMemoryHistoryStore()


# ── Optional: Redis-backed store for multi-process deployments ────────────────

class RedisHistoryStore:
    """
    Drop-in replacement for InMemoryHistoryStore backed by Redis.
    Swap `history_store = InMemoryHistoryStore()` above for:
        history_store = RedisHistoryStore(redis_client, max_turns=20)

    Requires: pip install redis
    """

    def __init__(self, redis_client, max_turns: int = MAX_TURNS, ttl_seconds: int = 3600):
        self._r = redis_client
        self._max_entries = max_turns * 2
        self._ttl = ttl_seconds

    def _key(self, session_id: str) -> str:
        return f"chat_history:{session_id}"

    def get(self, session_id: str) -> list[dict]:
        import json
        raw = self._r.lrange(self._key(session_id), 0, -1)
        return [json.loads(item) for item in raw]

    def append(self, session_id: str, user_msg: str, assistant_msg: str) -> None:
        import json
        key = self._key(session_id)
        pipe = self._r.pipeline()
        pipe.rpush(key, json.dumps({"role": "user", "content": user_msg}))
        pipe.rpush(key, json.dumps({"role": "assistant", "content": assistant_msg}))
        # Trim to max size
        pipe.ltrim(key, -self._max_entries, -1)
        pipe.expire(key, self._ttl)
        pipe.execute()

    def clear(self, session_id: str) -> None:
        self._r.delete(self._key(session_id))
