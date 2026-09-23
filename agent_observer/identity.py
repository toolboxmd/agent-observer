"""Instruction identity: which AgentsMD a session actually ran with.

Evidence, strongest first:
1. The Project Direction hook block (`AGENTSMD_PROJECT_DIRECTION_V1`) that
   AgentsMD injects at session start. It names the AGENTS.md SHA-256, the
   private preferences SHA-256 and the direction status. Preference contents
   travel in the same block and are never stored.
2. Versioned plugin paths the session read, such as
   `.../plugins/cache/toolboxmd/agentsmd/12.0.1/skills/operations/SKILL.md`.

AGENTS.md hashes resolve to releases through the local AgentsMD repository's
tags. Missing evidence stays unknown.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess

BLOCK_RE = re.compile(
    r"<<<AGENTSMD_PROJECT_DIRECTION_V1>>>\s*(\{.*?\})\s*<<<END_AGENTSMD_PROJECT_DIRECTION_V1>>>",
    re.S)
VERSION_PATH_RE = re.compile(r"/agentsmd/(\d+\.\d+\.\d+)/")
SKILL_PATH_RE = re.compile(r"/skills/([A-Za-z0-9_.-]+)/")


def parse_direction_block(text: str) -> dict | None:
    """Return the identity fields of the first direction block in text.

    Only hashes, paths and statuses survive; file contents never do.
    """
    if not text or "AGENTSMD_PROJECT_DIRECTION_V1" not in text:
        return None
    match = BLOCK_RE.search(text)
    if not match:
        return {"status": "unparsed"}
    try:
        block = json.loads(match.group(1))
    except json.JSONDecodeError:
        return {"status": "unparsed"}
    instructions = block.get("instructions") or {}
    preferences = block.get("preferences") or {}
    result = {
        "status": block.get("status"),
        "instructions_sha256": instructions.get("sha256")
        or instructions.get("target_sha256"),
        "instructions_path": instructions.get("resolved_target")
        or instructions.get("target"),
        "preferences_sha256": preferences.get("sha256"),
        "preferences_status": preferences.get("status"),
    }
    direction = {}
    for entry in block.get("files") or []:
        if isinstance(entry, dict) and entry.get("sha256"):
            name = os.path.basename(str(entry.get("path") or entry.get("name") or ""))
            if name:
                direction[name] = entry["sha256"]
    if direction:
        result["direction_sha256"] = direction
    if block.get("repository_root"):
        result["repository_root"] = block["repository_root"]
    head = ((block.get("git") or {}).get("head") or {}).get("sha")
    if head:
        result["git_head"] = head
    return {k: v for k, v in result.items() if v is not None}


def version_from_path(path: str) -> str | None:
    """AgentsMD release named by an installed plugin path, if any."""
    if not path or "agentsmd" not in path:
        return None
    match = VERSION_PATH_RE.search(path)
    return match.group(1) if match else None


def skill_from_path(path: str) -> str | None:
    """Skill directory name for a file read under an installed Skill."""
    if not path or "/skills/" not in path:
        return None
    match = SKILL_PATH_RE.search(path)
    return match.group(1) if match else None


def default_agentsmd_repo() -> str | None:
    """The repository behind the global instruction link, when resolvable."""
    override = os.environ.get("AGENT_OBSERVER_AGENTSMD_REPO")
    if override:
        return override
    for link in ("~/.claude/CLAUDE.md", "~/.codex/AGENTS.md"):
        target = os.path.realpath(os.path.expanduser(link))
        root = os.path.dirname(target)
        if os.path.isdir(os.path.join(root, ".git")) or os.path.isfile(
                os.path.join(root, ".git")):
            return root
    return None


def build_version_map(repo: str) -> list[dict]:
    """AGENTS.md SHA-256 of every release tag in the AgentsMD repository."""
    out = subprocess.run(
        ["git", "-C", repo, "for-each-ref", "--sort=creatordate",
         "--format=%(refname:short)\t%(objectname)\t%(creatordate:unix)",
         "refs/tags"],
        capture_output=True, text=True, check=False)
    rows = []
    for line in out.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 3 or not re.match(r"^v?\d+\.\d+\.\d+$", parts[0]):
            continue
        tag, _obj, when = parts
        blob = subprocess.run(
            ["git", "-C", repo, "show", f"{tag}:AGENTS.md"],
            capture_output=True, check=False)
        if blob.returncode != 0:
            continue
        rows.append({
            "sha256": hashlib.sha256(blob.stdout).hexdigest(),
            "size_bytes": len(blob.stdout),
            "version": tag.lstrip("v"),
            "commit_sha": subprocess.run(
                ["git", "-C", repo, "rev-list", "-n", "1", tag],
                capture_output=True, text=True, check=False).stdout.strip(),
            "released_at": float(when) if when else None,
        })
    return rows


def store_version_map(con: sqlite3.Connection, rows: list[dict]) -> int:
    for row in rows:
        # The earliest release with a given AGENTS.md keeps the hash: a later
        # release that left AGENTS.md unchanged does not relabel old sessions.
        con.execute(
            "INSERT OR IGNORE INTO agentsmd_versions(sha256, version, commit_sha,"
            " released_at, size_bytes) VALUES(?,?,?,?,?)",
            (row["sha256"], row["version"], row["commit_sha"],
             row["released_at"], row.get("size_bytes")))
    con.commit()
    return len(rows)


def resolve_version(con: sqlite3.Connection, sha256: str | None) -> str | None:
    if not sha256:
        return None
    row = con.execute(
        "SELECT version FROM agentsmd_versions WHERE sha256=?",
        (sha256,)).fetchone()
    return row["version"] if row else None


INSTRUCTIONS_RE = re.compile(r"<INSTRUCTIONS>\n(.*?)(?:</INSTRUCTIONS>|\Z)", re.S)


def match_embedded(con: sqlite3.Connection, body: str) -> str | None:
    """AGENTS.md SHA-256 of the release whose text starts the embedded body.

    Hosts such as Codex inject the global AGENTS.md followed by project
    instructions, so the release text is matched as an exact prefix.
    """
    if not body:
        return None
    data = body.encode("utf-8")
    for row in con.execute(
            "SELECT DISTINCT size_bytes FROM agentsmd_versions"
            " WHERE size_bytes IS NOT NULL AND size_bytes <= ?", (len(data),)):
        digest = hashlib.sha256(data[:row["size_bytes"]]).hexdigest()
        hit = con.execute("SELECT sha256 FROM agentsmd_versions WHERE sha256=?",
                          (digest,)).fetchone()
        if hit:
            return hit["sha256"]
    return None


def refresh_session_versions(con: sqlite3.Connection) -> int:
    """Fill agentsmd_version from the instruction hash where the path gave none."""
    cur = con.execute(
        "UPDATE sessions SET agentsmd_version=(SELECT version FROM"
        " agentsmd_versions v WHERE v.sha256=sessions.instructions_sha256)"
        " WHERE agentsmd_version IS NULL AND instructions_sha256 IS NOT NULL"
        " AND EXISTS (SELECT 1 FROM agentsmd_versions v"
        " WHERE v.sha256=sessions.instructions_sha256)")
    con.commit()
    return cur.rowcount


class SessionIdentity:
    """Accumulates identity evidence while an adapter reads one session."""

    def __init__(self):
        self.block = None
        self.path_versions: dict[str, int] = {}
        self.embedded: list[str] = []

    def observe_text(self, text: str) -> None:
        if not text:
            return
        if self.block is None:
            block = parse_direction_block(text)
            if block:
                self.block = block
        if not self.embedded and "<INSTRUCTIONS>" in text:
            match = INSTRUCTIONS_RE.search(text)
            if match:
                self.embedded.append(match.group(1))

    def observe_path(self, path: str) -> None:
        version = version_from_path(path)
        if version:
            self.path_versions[version] = self.path_versions.get(version, 0) + 1

    def fields(self, con: sqlite3.Connection | None = None) -> dict:
        """Identity columns for the session. With a ledger connection the
        embedded instruction text is matched against the release map; only
        the matched hash is kept, never the text."""
        fields = {}
        evidence = {}
        if self.block:
            fields["instructions_sha256"] = self.block.get("instructions_sha256")
            fields["preferences_sha256"] = self.block.get("preferences_sha256")
            fields["direction_status"] = self.block.get("status")
            evidence["direction_block"] = self.block
        if self.embedded and not self.block and con is not None:
            digest = match_embedded(con, self.embedded[0])
            if digest:
                fields["instructions_sha256"] = digest
                evidence["embedded_match"] = digest
        if self.path_versions:
            # The most-read installed version is the one the session used.
            version = max(self.path_versions, key=self.path_versions.get)
            fields["agentsmd_version"] = version
            evidence["plugin_paths"] = self.path_versions
        if evidence:
            fields["identity_json"] = json.dumps(evidence, sort_keys=True)
        return {k: v for k, v in fields.items() if v is not None}
