import importlib.util
import sys
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("fable-time.py")
SPEC = importlib.util.spec_from_file_location("agent_time", MODULE_PATH)
agent_time = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = agent_time
SPEC.loader.exec_module(agent_time)


class AgentTimeAttributionTests(unittest.TestCase):
    def test_unix_timestamp_query_values_are_supported(self):
        self.assertEqual(agent_time.parse_ts("1788220800"), 1788220800.0)

    def test_codex_interval_retains_chat_identity_and_title(self):
        record = agent_time.Record(Path("/tmp/codex-session.jsonl"), "codex")
        record.codex({"type": "session_meta", "payload": {"session_id": "chat-123", "cwd": "/work/client"}})
        record.codex({"type": "event_msg", "payload": {"type": "user_message", "message": "Fix the invoice search"}})
        record.codex({"type": "event_msg", "timestamp": "2026-09-01T00:00:00Z", "payload": {"type": "task_started", "turn_id": "turn-1"}})
        record.codex({"type": "event_msg", "timestamp": "2026-09-01T00:05:00Z", "payload": {"type": "task_complete", "turn_id": "turn-1"}})

        interval = record.done[0]
        self.assertEqual(interval.source, "Codex")
        self.assertEqual(interval.conversation_id, "chat-123")
        self.assertEqual(interval.conversation_title, "Fix the invoice search")

    def test_t3_interval_is_identified_as_t3_using_codex(self):
        record = agent_time.Record(Path("/tmp/events.thread-456.log"), "t3")
        record.conversation_id = "thread-456"
        record.conversation_title = "Mobile tracker polish"
        record.t3({"type": "turn.started", "createdAt": "2026-09-01T00:00:00Z", "threadId": "thread-456", "turnId": "turn-1", "provider": "codex", "payload": {}})
        record.t3({"type": "turn.completed", "createdAt": "2026-09-01T00:02:00Z", "threadId": "thread-456", "turnId": "turn-1", "provider": "codex", "payload": {}})

        interval = record.done[0]
        self.assertEqual(interval.source, "T3 Code")
        self.assertEqual(interval.agent, "Codex")
        self.assertEqual(interval.conversation_title, "Mobile tracker polish")


if __name__ == "__main__":
    unittest.main()
