"""Read chat messages on the collector that owns them; no arbitrary file paths."""
import json
import re
import sqlite3
from pathlib import Path

ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{6,128}$")


def text_content(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(x.get("text", "")) for x in content if isinstance(x, dict)
                         and x.get("type") in ("text", "input_text", "output_text"))
    return ""


def clean_text(text):
    text = re.sub(r"<(system-reminder|app-context|environment_context|skills_instructions|recommended_plugins|multi_agent_mode|user_instructions)>[\s\S]*?</\1>", "", text)
    return text.strip()[:20000]


def read_transcript(source, conversation_id, t3_root, claude_root, codex_root):
    if not ID_PATTERN.fullmatch(conversation_id):
        raise ValueError("A valid chat id is required")
    messages = []
    title = ""

    def append(role, text, at):
        if role not in ("user", "assistant"):
            return
        text = clean_text(text)
        if text and not text.startswith("Base directory for this skill:"):
            messages.append({"role": role, "text": text, "at": at})

    if source == "T3 Code":
        database = t3_root / "state.sqlite"
        if not database.is_file():
            return None
        with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as conn:
            thread = conn.execute("select title from projection_threads where thread_id = ?", (conversation_id,)).fetchone()
            if not thread:
                return None
            title = thread[0] or ""
            for role, text, at in conn.execute("select role, text, created_at from projection_thread_messages where thread_id = ? order by created_at, rowid", (conversation_id,)):
                append(role, text or "", at)
    else:
        if source == "Claude":
            file = next(claude_root.glob(f"*/{conversation_id}.jsonl"), None)
        elif source in ("Codex", "ChatGPT"):
            file = next(codex_root.glob(f"**/*-{conversation_id}.jsonl"), None)
        else:
            raise ValueError("Unknown chat source")
        if not file:
            return None
        with file.open() as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(row, dict):
                    continue
                if source == "Claude":
                    if row.get("type") == "ai-title":
                        title = row.get("title") or title
                    if row.get("isSidechain") or row.get("isMeta"):
                        continue
                    message = row.get("message")
                    if isinstance(message, dict):
                        append(row.get("type"), text_content(message.get("content")), row.get("timestamp"))
                else:
                    payload = row.get("payload")
                    if row.get("type") == "response_item" and isinstance(payload, dict) and payload.get("type") == "message":
                        append(payload.get("role"), text_content(payload.get("content")), row.get("timestamp"))
    return {"source": source, "conversationId": conversation_id, "title": title, "messages": messages}


def transcript_page(transcript, offset=0, limit=100):
    total = len(transcript["messages"])
    return {**transcript, "messages": transcript["messages"][offset:offset + limit],
            "totalMessages": total, "nextOffset": offset + limit if offset + limit < total else None}
