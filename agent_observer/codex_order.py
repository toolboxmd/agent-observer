"""Order for one requested set of Codex identities.

A source sequence is the first occurrence of each identity in that file.
Identities emitted from the same native record share a record position and
stay one group: insertion order inside that record is not a cause. A flat
order of the requested set is known when one source covers the set and
every other source's projection of the set agrees. Anything else stays
unknown. This does not build an order over the rest of the session.
"""

from __future__ import annotations


def _groups(con, session_key: str) -> list:
    rows = con.execute(
        "SELECT source_id, row_kind, row_key, record_position"
        " FROM codex_occurrences WHERE session_key=?"
        " ORDER BY source_id, record_position",
        (session_key,)).fetchall()
    sources = []
    current = None
    groups: list = []
    bucket: list = []
    position = None

    def flush_bucket() -> None:
        if bucket:
            groups.append(frozenset(bucket))
            bucket.clear()

    def flush_source() -> None:
        flush_bucket()
        if current is not None and groups:
            sources.append(list(groups))
            groups.clear()

    for row in rows:
        if row["source_id"] != current:
            flush_source()
            current = row["source_id"]
            position = None
        if row["record_position"] != position:
            flush_bucket()
            position = row["record_position"]
        bucket.append((row["row_kind"], row["row_key"]))
    flush_source()
    return sources


def resolve(con, session_key: str, wanted: set):
    """(state, order, sequences) for `wanted`.

    state is absent when the session has no occurrences. order is the
    spanning source's groups, restricted to `wanted`, when that order is
    known. sequences are the same restriction per source that holds any of
    the set. Groups that share a record position are unordered inside.
    """
    sources = _groups(con, session_key)
    if not sources:
        return "absent", None, []
    wanted = set(wanted)
    projections = []
    for groups in sources:
        projected = []
        covered: set = set()
        for group in groups:
            hit = frozenset(ident for ident in group if ident in wanted)
            if not hit:
                continue
            projected.append(hit)
            covered |= hit
        if projected:
            projections.append((covered, projected))
    if not projections:
        return "absent", None, []
    sequences = [projected for _, projected in projections]
    present: set = set()
    for covered, _projected in projections:
        present |= covered
    if present != wanted:
        return "partial", None, sequences
    spanning = [projected for covered, projected in projections
                if covered == wanted]
    if not spanning:
        return "partial", None, sequences
    span = spanning[0]
    for covered, projected in projections:
        filtered = []
        for group in span:
            hit = frozenset(ident for ident in group if ident in covered)
            if hit:
                filtered.append(hit)
        if projected != filtered:
            return "conflict", None, sequences
    return "known", span, sequences
