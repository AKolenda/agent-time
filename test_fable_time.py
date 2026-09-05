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


    def test_interrupted_claude_turn_does_not_bill_idle_hours(self):
        record = agent_time.Record(Path("/tmp/claude-session.jsonl"), "claude")
        prompt = {"type": "user", "timestamp": "2026-09-04T05:19:10Z", "message": {"role": "user", "content": "optimize the app"}}
        record.claude(prompt)
        record.claude({"type": "assistant", "timestamp": "2026-09-04T05:19:11Z", "message": {"model": "<synthetic>", "content": [{"type": "text", "text": "Request interrupted"}]}})
        # Nearly 13 hours later the session resumes with a meta message and a synthetic reply.
        record.claude({"type": "user", "timestamp": "2026-09-04T18:13:47Z", "isMeta": True, "message": {"role": "user", "content": "resume"}})
        record.claude({"type": "assistant", "timestamp": "2026-09-04T18:13:47Z", "message": {"model": "<synthetic>", "content": [{"type": "text", "text": "ok"}]}})
        record.claude({"type": "user", "timestamp": "2026-09-04T18:13:48Z", "message": {"role": "user", "content": "keep going"}})
        record.claude({"type": "assistant", "timestamp": "2026-09-04T18:16:53Z", "message": {"model": "claude-fable-5-1", "content": [{"type": "text", "text": "done"}]}})
        record.claude({"type": "system", "subtype": "turn_duration", "timestamp": "2026-09-04T18:16:53Z", "durationMs": 185000})

        self.assertTrue(all(interval.end - interval.start < 600 for interval in record.done), record.done)
        self.assertEqual(round(sum(interval.end - interval.start for interval in record.done)), 185)

    def test_long_running_claude_turn_still_counts_when_model_keeps_working(self):
        record = agent_time.Record(Path("/tmp/claude-session.jsonl"), "claude")
        record.claude({"type": "user", "timestamp": "2026-09-04T05:00:00Z", "message": {"role": "user", "content": "run tests"}})
        for minute in range(0, 60, 10):
            record.claude({"type": "assistant", "timestamp": f"2026-09-04T05:{minute:02d}:30Z", "message": {"model": "claude-fable-5-1", "content": [{"type": "text", "text": "working"}]}})
        record.claude({"type": "user", "timestamp": "2026-09-04T06:00:00Z", "message": {"role": "user", "content": "thanks"}})

        self.assertEqual(len(record.done), 1)
        self.assertEqual(round(record.done[0].end - record.done[0].start), 3030)

    def test_slash_command_does_not_open_a_live_turn(self):
        record = agent_time.Record(Path("/tmp/claude-session.jsonl"), "claude")
        record.claude({"type": "user", "timestamp": "2026-09-04T06:00:00Z", "message": {"role": "user", "content": "<command-name>/plugin</command-name> <command-args>install x</command-args>"}})
        record.mtime = agent_time.parse_ts("2026-09-04T23:00:00Z")
        self.assertEqual(record.snapshot(agent_time.parse_ts("2026-09-04T23:00:10Z")), [])

    def test_stale_turn_is_not_live_just_because_file_changed(self):
        record = agent_time.Record(Path("/tmp/claude-session.jsonl"), "claude")
        record.claude({"type": "user", "timestamp": "2026-09-04T06:00:00Z", "message": {"role": "user", "content": "do work"}})
        record.claude({"type": "assistant", "timestamp": "2026-09-04T06:05:00Z", "message": {"model": "claude-opus-5", "content": [{"type": "text", "text": "ok"}]}})
        record.mtime = agent_time.parse_ts("2026-09-04T23:00:00Z")
        intervals = record.snapshot(agent_time.parse_ts("2026-09-04T23:00:10Z"))
        self.assertEqual(len(intervals), 1)
        self.assertFalse(intervals[0].live)
        self.assertEqual(round(intervals[0].end - intervals[0].start), 300)

if __name__ == "__main__":
    unittest.main()


class SummaryTests(unittest.TestCase):
    def test_prompts_are_kept_for_the_summary_excerpt(self):
        record = agent_time.Record(Path("/tmp/x.jsonl"), "claude")
        record.claude({"type": "user", "timestamp": 1000, "sessionId": "s1", "cwd": "/w",
                       "message": {"role": "user", "content": "<system-reminder>hidden</system-reminder>\nMake the invoice page load faster"}})
        record.claude({"type": "user", "timestamp": 1010, "sessionId": "s1",
                       "message": {"role": "user", "content": [{"type": "text", "text": "Also fix the totals"}]}})
        self.assertEqual(record.prompts, ["Make the invoice page load faster", "Also fix the totals"])
        excerpt = agent_time.Summaries.excerpt(record.conversation_title, record.prompts)
        self.assertIn("Client request: Also fix the totals", excerpt)
        self.assertNotIn("hidden", excerpt)

    def test_summary_cache_round_trips_and_cleans_output(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            cache = agent_time.Summaries(Path(folder) / "summaries.json")
            cache.store("chat-1", "Faster invoice page")
            self.assertEqual(agent_time.Summaries(Path(folder) / "summaries.json").get("chat-1"), "Faster invoice page")
        self.assertEqual(agent_time.Summaries.clean('codex\n"Improved mobile search layout."\n'), "Improved mobile search layout")
