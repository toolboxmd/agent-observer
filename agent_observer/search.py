"""Word search over one machine's ledger, read-only (#70).

A capture judge on another machine calls `event search` over SSH to check
whether a lesson recurred. It searches what the ledger already keeps:
event names and targets (commands, paths), assistant excerpts, and user
message excerpts, all redacted at import. Native logs are not read, so no
transcript text beyond those excerpts leaves the machine.

Every word must appear in one record, case-insensitively and literally. Results are newest first, with the total count, so
a caller can tell an empty result from a truncated one.
"""

from __future__ import annotations

import datetime
import sqlite3

ANCHOR_CHARS = 160

# One searchable text per record. json_extract decodes the stored JSON, so
# non-ASCII excerpts match as written.
_RECORDS = """
SELECT 'event' AS type, CAST(e.id AS TEXT) AS id, e.session_key AS session,
       e.ts AS ts, e.family AS family,
       COALESCE(e.name, '') || ' ' || COALESCE(e.target, '') || ' ' ||
       COALESCE(json_extract(CASE WHEN json_valid(e.detail_json)
                                  THEN e.detail_json END, '$.excerpt'), '')
         AS text
  FROM events e
UNION ALL
SELECT 'user_message', u.native_id, u.session_key, u.ts, 'user_message',
       u.text_excerpt
  FROM user_messages u
"""


def _iso(ts) -> str | None:
    if not isinstance(ts, (int, float)):
        return None
    return datetime.datetime.fromtimestamp(
        ts, datetime.timezone.utc).isoformat(timespec="seconds").replace(
            "+00:00", "Z")


def _like(word: str) -> str:
    escaped = word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _anchor(text: str, words: list[str]) -> str:
    text = " ".join(text.split())
    lowered = text.casefold()
    hits = [lowered.find(w.casefold()) for w in words]
    start = min((h for h in hits if h >= 0), default=0)
    left = max(0, start - ANCHOR_CHARS // 4)
    piece = text[left:left + ANCHOR_CHARS]
    return ("..." if left else "") + piece + \
        ("..." if left + ANCHOR_CHARS < len(text) else "")


def search(con: sqlite3.Connection, words: list[str], *, since=None,
           until=None, harness=None, project=None, limit: int = 50,
           machine: str | None = None) -> dict:
    words = [w for w in words if w.strip()]
    if not words:
        raise ValueError("give at least one search word")
    if limit < 1:
        raise ValueError("--limit must be at least 1")
    # SQLite's LIKE and lower() fold ASCII only; Python's casefold covers
    # every script, and instr() takes words literally.
    con.create_function("ao_fold", 1,
                        lambda t: t.casefold() if isinstance(t, str) else t,
                        deterministic=True)
    where, args = [], []
    for w in words:
        where.append("instr(ao_fold(r.text), ?) > 0")
        args.append(w.casefold())
    if since is not None:
        where.append("r.ts >= ?")
        args.append(since)
    if until is not None:
        where.append("r.ts <= ?")
        args.append(until)
    if harness:
        where.append("r.session LIKE ? ESCAPE '\\'")
        args.append(_like(harness + ":")[1:])
    if project:
        where.append("s.project_dir LIKE ? ESCAPE '\\'")
        args.append(_like(project))
    body = (f" FROM ({_RECORDS}) r LEFT JOIN sessions s"
            f" ON s.session_key = r.session WHERE " + " AND ".join(where))
    total = con.execute("SELECT COUNT(*)" + body, args).fetchone()[0]
    rows = con.execute(
        "SELECT r.type, r.id, r.session, r.ts, r.family, r.text,"
        " s.project_dir" + body +
        " ORDER BY r.ts IS NULL, r.ts DESC, r.type, r.id LIMIT ?",
        args + [limit]).fetchall()
    matches = []
    for row in rows:
        item = {"type": row[0], "id": int(row[1]) if row[0] == "event"
                else row[1], "session": row[2], "at": _iso(row[3]),
                "family": row[4], "project": row[6],
                "anchor": _anchor(row[5] or "", words)}
        matches.append(item)
    synced = con.execute("SELECT MAX(imported_at) FROM sources").fetchone()[0]
    return {"machine": machine, "words": words,
            "filters": {"since": _iso(since), "until": _iso(until),
                        "harness": harness, "project": project},
            "ledger_synced_at": _iso(synced), "total": total,
            "truncated": total > len(matches), "matches": matches}


def render(result: dict) -> str:
    lines = [f"{result['total']} matches on {result['machine']}"
             f" (ledger synced {result['ledger_synced_at'] or 'never'})"
             + (f", showing {len(result['matches'])}" if result["truncated"]
                else "")]
    for m in result["matches"]:
        lines.append(f"{m['type']} {m['id']}  {m['at'] or '-'}  {m['session']}"
                     f"  {m['anchor']}")
    return "\n".join(lines)
