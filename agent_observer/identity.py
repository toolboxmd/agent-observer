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
SKILL_PATH_RE = re.compile(r"(?:/|^)skills/([A-Za-z0-9_.-]+)/")

# Direction-block identity is sanitized centrally here, fail closed: only
# validated values survive, and identity_json carries only an approved
# allowlist of fields. Marker-shaped native text can never store free
# text through identity.
#
# Statuses: exactly the closed set the canonical AgentsMD Project
# Direction loader emits for direction state (confirmed read-only
# against bin/project-direction). Anything else is omitted, never copied.
DIRECTION_STATUSES = frozenset({
    "ready", "missing", "stale", "potentially_stale", "invalid",
    "uninitialized", "not_in_repository",
})
PREFERENCES_STATUSES = frozenset({
    "ready", "absent", "unreadable", "oversized", "missing",
    "cache-bound-target", "cache-bound-link", "non-symlink",
    "broken-link", "invalid-link-target", "valid-stable-link",
    "divergent-link", "source-unavailable", "source-ambiguous",
    "read_required", "unparsed",
})

# Hashes: SHA-256 fields are exactly 64 lowercase hex characters; the git
# head is the fixed-length lowercase hex Git digest (40 characters).
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_GIT_HEAD_RE = re.compile(r"[0-9a-f]{40}")

# Direction files: only the approved Project Direction names, never
# arbitrary basenames copied from native text.
DIRECTION_FILES = frozenset({"VISION.md", "MISSION.md", "OBJECTIVE.md"})

# Paths: absolute, printable, marker-free. Control characters and
# tag-like markers ('<' plus letter, '/' or '!', or '<<<') fail closed.
_PATH_CHARS = 1024
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def _valid_sha256(value: object) -> bool:
    return isinstance(value, str) and _SHA256_RE.fullmatch(value) is not None


def _valid_git_head(value: object) -> bool:
    return isinstance(value, str) and _GIT_HEAD_RE.fullmatch(value) is not None


def _marker_at(text: str, index: int) -> bool:
    if text.startswith("<<<", index):
        return True
    nxt = index + 1
    if nxt >= len(text):
        return False
    ch = text[nxt]
    return ch == "/" or ch == "!" or ch.isalpha()


def _has_marker(text: str) -> bool:
    start = 0
    while True:
        idx = text.find("<", start)
        if idx == -1:
            return False
        if _marker_at(text, idx):
            return True
        start = idx + 1


def _valid_path(value: object) -> bool:
    """An absolute native path with no control characters or markers."""
    if not isinstance(value, str) or not value:
        return False
    if not value.startswith("/") or len(value) > _PATH_CHARS:
        return False
    if _CONTROL_RE.search(value) is not None:
        return False
    if _has_marker(value):
        return False
    return True


def parse_direction_block(text: str) -> dict | None:
    """Return the sanitized identity fields of the first direction block.

    Only validated hashes, paths and closed-set statuses survive; file
    contents, arbitrary keys, titles and marker-shaped free text never
    do. Unknown statuses, short or non-hex hashes, wrong-type or
    malformed paths, invalid git heads and unapproved direction file
    names are dropped, never copied.
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
    if not isinstance(block, dict):
        return {"status": "unparsed"}
    result: dict = {}
    status = block.get("status")
    if isinstance(status, str) and status in DIRECTION_STATUSES:
        result["status"] = status
    instructions = block.get("instructions")
    if isinstance(instructions, dict):
        for key in ("sha256", "target_sha256"):
            digest = instructions.get(key)
            if _valid_sha256(digest):
                result["instructions_sha256"] = digest
                break
        for key in ("resolved_target", "target"):
            path = instructions.get(key)
            if _valid_path(path):
                result["instructions_path"] = path
                break
    preferences = block.get("preferences")
    if isinstance(preferences, dict):
        digest = preferences.get("sha256")
        if _valid_sha256(digest):
            result["preferences_sha256"] = digest
        pref_status = preferences.get("status")
        if isinstance(pref_status, str) and pref_status in PREFERENCES_STATUSES:
            result["preferences_status"] = pref_status
    files = block.get("files")
    if isinstance(files, list):
        direction = {}
        for entry in files:
            if not isinstance(entry, dict):
                continue
            raw_name = entry.get("path") or entry.get("name")
            name = os.path.basename(raw_name) \
                if isinstance(raw_name, str) and raw_name else ""
            if name in DIRECTION_FILES and _valid_sha256(entry.get("sha256")):
                direction[name] = entry["sha256"]
        if direction:
            result["direction_sha256"] = direction
    repository_root = block.get("repository_root")
    if _valid_path(repository_root):
        result["repository_root"] = repository_root
    git = block.get("git")
    head = git.get("head") if isinstance(git, dict) else None
    sha = head.get("sha") if isinstance(head, dict) else None
    if _valid_git_head(sha):
        result["git_head"] = sha
    return result


def version_from_path(path: str) -> str | None:
    """AgentsMD release named by an installed plugin path, if any."""
    if not path or "agentsmd" not in path:
        return None
    match = VERSION_PATH_RE.search(path)
    return match.group(1) if match else None


def skill_from_path(path: str) -> str | None:
    """Skill directory name for a file read under an installed Skill.

    Absolute and relative spellings both resolve (adapters store both);
    anything else fails closed to None.
    """
    if not path or "skills/" not in path:
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
    """Set agentsmd_version from the AGENTS.md hash wherever it resolves.

    The core instruction hash is the contract a person evaluates, so it
    decides the version; a versioned plugin path is the fallback only."""
    cur = con.execute(
        "UPDATE sessions SET agentsmd_version=(SELECT version FROM"
        " agentsmd_versions v WHERE v.sha256=sessions.instructions_sha256)"
        " WHERE instructions_sha256 IS NOT NULL AND EXISTS (SELECT 1 FROM"
        " agentsmd_versions v WHERE v.sha256=sessions.instructions_sha256)")
    con.commit()
    return cur.rowcount


class SessionIdentity:
    """Accumulates identity evidence while an adapter reads one session."""

    def __init__(self):
        self.block = None
        self.path_versions: dict[str, int] = {}
        self.embedded: list[str] = []
        self.loaded_hashes: list[str] = []

    def observe_text(self, text: str) -> None:
        if not text:
            return
        if self.block is None or self.block.get("status") == "unparsed":
            block = parse_direction_block(text)
            if block and (self.block is None or block.get("status") != "unparsed"):
                self.block = block
        if not self.embedded and "<INSTRUCTIONS>" in text:
            match = INSTRUCTIONS_RE.search(text)
            if match:
                self.embedded.append(match.group(1))

    def observe_loaded_instructions(self, content: str) -> None:
        """Instruction file text as the host loaded it. Hosts may drop the
        final newline, so both spellings are candidates; only a hash that
        names a release is kept."""
        if not content:
            return
        for text in (content, content if content.endswith("\n") else content + "\n"):
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            if digest not in self.loaded_hashes:
                self.loaded_hashes.append(digest)

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
            # identity_json carries only this approved allowlist of
            # already-sanitized fields: validated direction status,
            # hashes, paths and git head. The raw parsed block, arbitrary
            # keys, contents, marker-shaped text, titles and free text
            # are never serialized. Direction status survives only when
            # it belongs to the canonical closed set.
            fields["instructions_sha256"] = self.block.get("instructions_sha256")
            fields["preferences_sha256"] = self.block.get("preferences_sha256")
            status = self.block.get("status")
            if isinstance(status, str) and status in DIRECTION_STATUSES:
                fields["direction_status"] = status
            for key, alias in (
                    ("instructions_sha256", "instructions_sha256"),
                    ("preferences_sha256", "preferences_sha256"),
                    ("preferences_status", "preferences_status"),
                    ("instructions_path", "instructions_path"),
                    ("direction_sha256", "direction_sha256"),
                    ("repository_root", "repository_root"),
                    ("git_head", "git_head")):
                if self.block.get(key) is not None:
                    evidence[alias] = self.block[key]
            if "direction_status" in fields:
                evidence["direction_status"] = fields["direction_status"]
        if self.loaded_hashes and not self.block and con is not None:
            for digest in self.loaded_hashes:
                if con.execute("SELECT 1 FROM agentsmd_versions WHERE sha256=?",
                               (digest,)).fetchone():
                    fields["instructions_sha256"] = digest
                    evidence["loaded_instructions_match"] = digest
                    break
        if self.embedded and not self.block and con is not None \
                and "instructions_sha256" not in fields:
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
