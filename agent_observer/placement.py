"""Keep unproven legacy T3 checkout attribution out of task totals.

Claude tool records carry a working directory and branch, without a forge
or repository identity. Codex records Git identity at session start and
a working directory at command execution, without binding the two. Neither
source proves the checkout of a specific call. Legacy checkout tables were
populated from live Git and cannot supply that missing native evidence.
"""

from __future__ import annotations


def read_placements(con, session_key: str) -> dict | None:
    """An empty map leaves legacy Codex/Claude responses unplaced.

    None delegates other assignment types and harnesses to the caller.
    """
    rows = con.execute(
        "SELECT s.harness, a.evidence FROM sessions s"
        " JOIN session_assignments a USING(session_key) WHERE s.session_key=?",
        (session_key,)).fetchall()
    if (len(rows) >= 2 and rows[0]["harness"] in ("claude", "codex")
            and all(r["evidence"] == "t3" or r["evidence"].startswith("t3:")
                    for r in rows)):
        return {}
    return None


# Compatibility for callers using the original report helper.
place_session = read_placements


def record_shared_checkouts(con) -> int:
    """Return the legacy T3 sync counter."""
    return con.execute(
        "SELECT COUNT(*) FROM (SELECT 1 FROM session_assignments"
        " GROUP BY session_key HAVING COUNT(*) > 1)").fetchone()[0]
