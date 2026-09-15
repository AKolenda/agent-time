import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from contextlib import closing
from agent_transcripts import read_transcript, transcript_page


class TranscriptTests(unittest.TestCase):
    def test_t3_chat_and_pagination_keep_all_messages(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with closing(sqlite3.connect(root / "state.sqlite")) as db, db:
                db.executescript("create table projection_threads(thread_id text, title text); create table projection_thread_messages(thread_id text, role text, text text, created_at text);")
                db.execute("insert into projection_threads values (?, ?)", ("chat-123", "Improve the search"))
                db.executemany("insert into projection_thread_messages values (?, ?, ?, ?)", [("chat-123", "user", f"message {i}", f"{i:04}") for i in range(105)])
            transcript = read_transcript("T3 Code", "chat-123", root, root, root)
            first = transcript_page(transcript)
            last = transcript_page(transcript, first["nextOffset"])
            self.assertEqual(first["title"], "Improve the search")
            self.assertEqual(first["totalMessages"], 105)
            self.assertEqual(len(first["messages"]) + len(last["messages"]), 105)
            self.assertEqual(last["messages"][-1]["text"], "message 104")
            self.assertIsNone(last["nextOffset"])
            self.assertIsNone(read_transcript("T3 Code", "missing-chat", root, root, root))

    def test_claude_excludes_tool_results_and_keeps_real_text(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            project = root / "project"
            project.mkdir()
            rows = [
                {"type": "user", "message": {"content": [{"type": "tool_result", "content": "private output"}]}},
                {"type": "user", "message": {"content": "Please fix search"}},
                {"type": "assistant", "message": {"content": [{"type": "text", "text": "Search is working"}]}},
            ]
            (project / "chat-123.jsonl").write_text("\n".join(json.dumps(x) for x in rows) + "\n{partial")
            transcript = read_transcript("Claude", "chat-123", root, root, root)
            self.assertEqual([m["text"] for m in transcript["messages"]], ["Please fix search", "Search is working"])

    def test_rejects_arbitrary_paths(self):
        with self.assertRaises(ValueError):
            read_transcript("Claude", "../../secret", Path("/tmp"), Path("/tmp"), Path("/tmp"))
