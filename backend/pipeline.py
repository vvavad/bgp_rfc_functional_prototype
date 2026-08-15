"""
pipeline.py — the actual engine behind the dashboard.

Everything the Flask app exposes as an API call routes through here. The
knowledge base (kb/knowledge.db) is the single source of truth: RFC text is
parsed into requirements exactly once per ingest; every later call
(list requirements, generate tests, compute coverage) reads FROM the database,
never from the raw RFC text again. That "read once, reuse forever" property
is the thing this prototype needs to prove, so it's structural here, not a
one-off demo script.
"""
import os
import re
import sys
import json
import logging
import sqlite3
import pickle
import datetime
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from string import Template
from collections import defaultdict, Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

import ai_generation
import protocol_profiles

logger = logging.getLogger(__name__)

# How many requirements to run through AI generation concurrently. Each call
# is I/O-bound (a subprocess or a network request), so a modest thread pool
# gets real wall-clock savings on bulk generation without hammering the
# machine -- override via env var if the local box can take more/less.
GENERATION_CONCURRENCY = max(1, int(os.environ.get("AI_GENERATION_CONCURRENCY", "4")))

BASE = Path(__file__).resolve().parent
KB_DIR = BASE / "kb"
DOCS_DIR = BASE / "generated_tests" / "docs"
PYTEST_DIR = BASE / "generated_tests" / "pytest"
# Deduplicated view: the full, unfiltered record of everything ever
# generated stays in DOCS_DIR/PYTEST_DIR above; this is the curated subset
# with duplicate tests (see _dedup_key) collapsed to one representative
# each -- refreshed wholesale by refresh_deduplicated_tests() after every
# generation batch, not incrementally maintained.
DEDUP_DOCS_DIR = BASE / "generated_tests" / "deduplicated" / "docs"
DEDUP_PYTEST_DIR = BASE / "generated_tests" / "deduplicated" / "pytest"
LOGS_DIR = BASE / "logs"
DB_PATH = KB_DIR / "knowledge.db"
RETRIEVAL_INDEX_PATH = KB_DIR / "retrieval_index.pkl"
ARTEFACTS_DIR = KB_DIR / "artefacts"
UPLOADED_TESTS_DIR = KB_DIR / "uploaded_tests"
# Committed source files a demo/operator can pick from via the knowledge-
# library API (see ingest_rfc_incremental below) -- unlike ARTEFACTS_DIR/
# UPLOADED_TESTS_DIR this is source-controlled content, not runtime output,
# same category as kb/rfc4271_raw.txt.
RFC_LIBRARY_DIR = KB_DIR / "rfc_library"

for d in (KB_DIR, DOCS_DIR, PYTEST_DIR, DEDUP_DOCS_DIR, DEDUP_PYTEST_DIR, ARTEFACTS_DIR, UPLOADED_TESTS_DIR,
          RFC_LIBRARY_DIR, LOGS_DIR):
    d.mkdir(parents=True, exist_ok=True)

SUPPORTED_UPLOAD_EXTENSIONS = (".txt", ".md", ".log", ".pdf", ".py")
COVERAGE_CANDIDATE_K = 15
MAX_TEST_CONTENT_CHARS = 8000

# ------------------------------------------------------------------ #
# Process log — a real file on disk recording generation activity (RFC
# ingests, per-test AI-vs-heuristic outcome, dedup results), not just the
# in-DB ingestion_log table the dashboard reads. Attached to this module's
# logger and ai_generation's (by name, not the root logger) so Flask's own
# request/werkzeug logging isn't pulled in here.
# ------------------------------------------------------------------ #

PROCESS_LOG_PATH = LOGS_DIR / "generation.log"


def _setup_process_log_file():
    for logger_name in ("pipeline", "ai_generation"):
        target = logging.getLogger(logger_name)
        if any(getattr(h, "_is_process_log_handler", False) for h in target.handlers):
            continue  # already configured -- avoid duplicate handlers on re-import/reload
        handler = logging.FileHandler(PROCESS_LOG_PATH, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s"))
        handler._is_process_log_handler = True
        target.addHandler(handler)
        target.setLevel(logging.INFO)


_setup_process_log_file()

# ------------------------------------------------------------------ #
# Schema
# ------------------------------------------------------------------ #

SCHEMA = """
CREATE TABLE IF NOT EXISTS rfc_meta (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    rfc_number TEXT,
    rfc_title TEXT,
    source TEXT,
    ingested_at TEXT,
    protocol_key TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS requirements (
    requirement_id TEXT PRIMARY KEY,
    rfc TEXT,
    section_id TEXT,
    section_title TEXT,
    keyword TEXT,
    statement TEXT,
    category TEXT,
    testability TEXT
);

CREATE TABLE IF NOT EXISTS requirement_status (
    requirement_id TEXT PRIMARY KEY,
    has_generated_test INTEGER DEFAULT 0,
    test_id TEXT DEFAULT '',
    FOREIGN KEY (requirement_id) REFERENCES requirements(requirement_id)
);

CREATE TABLE IF NOT EXISTS test_intents (
    test_id TEXT PRIMARY KEY,
    requirement_id TEXT,
    category TEXT,
    test_type TEXT,
    risk TEXT,
    priority TEXT,
    section_id TEXT,
    section_title TEXT,
    statement TEXT,
    keyword TEXT,
    topology TEXT,
    timers TEXT,
    doc_content TEXT,
    pytest_content TEXT,
    derived_from TEXT DEFAULT '',
    batch_id INTEGER,
    created_at TEXT,
    generation_mode TEXT DEFAULT 'heuristic',
    ai_backend TEXT DEFAULT '',
    protocol_reasoning TEXT DEFAULT '',
    requires_peer_emulator INTEGER DEFAULT 0,
    emulator_tool TEXT DEFAULT '',
    needs_review INTEGER DEFAULT 0,
    context_requirement_ids TEXT DEFAULT '[]',
    context_stale INTEGER DEFAULT 0,
    context_stale_reason TEXT DEFAULT '',
    updated_at TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS knowledge_sources (
    filename TEXT PRIMARY KEY,
    rfc_number TEXT,
    rfc_title TEXT,
    protocol_key TEXT,
    ingested_at TEXT,
    requirements_added INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS ingestion_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event TEXT,
    source TEXT,
    timestamp TEXT
);

CREATE TABLE IF NOT EXISTS artefacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    artefact_type TEXT,
    filename TEXT,
    content_text TEXT,
    char_count INTEGER,
    uploaded_at TEXT,
    notes TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS uploaded_tests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filename TEXT,
    content_text TEXT,
    char_count INTEGER,
    uploaded_at TEXT,
    notes TEXT DEFAULT '',
    analyzed INTEGER DEFAULT 0,
    analysis_mode TEXT DEFAULT '',
    analyzed_at TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS uploaded_test_requirement_map (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uploaded_test_id INTEGER,
    requirement_id TEXT,
    confidence TEXT,
    rationale TEXT,
    FOREIGN KEY (uploaded_test_id) REFERENCES uploaded_tests(id)
);
"""


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn):
    """CREATE TABLE IF NOT EXISTS doesn't retrofit columns onto a table that
    already exists from before this column was added -- this covers that gap
    for existing kb/knowledge.db files. Cheap no-op once a column exists.

    Commits its own changes before returning -- get_conn() is called by
    plenty of read-only code paths that never call conn.commit() themselves
    (they only SELECT), which is fine for pure schema changes (SQLite
    auto-commits DDL) but NOT fine for the protocol_key backfill below,
    which is a DML UPDATE: without an explicit commit here, a caller that
    reads-and-closes without committing would silently roll it back --
    and since the ALTER TABLE already persisted, the `if column not in
    cols` guard would then never retry the backfill again. (Found exactly
    this bug during item 1's regression check -- see todo.md/design.md.)"""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(test_intents)").fetchall()}
    if "ai_backend" not in cols:
        conn.execute("ALTER TABLE test_intents ADD COLUMN ai_backend TEXT DEFAULT ''")
    if "context_requirement_ids" not in cols:
        conn.execute("ALTER TABLE test_intents ADD COLUMN context_requirement_ids TEXT DEFAULT '[]'")
    if "context_stale" not in cols:
        conn.execute("ALTER TABLE test_intents ADD COLUMN context_stale INTEGER DEFAULT 0")
    if "context_stale_reason" not in cols:
        conn.execute("ALTER TABLE test_intents ADD COLUMN context_stale_reason TEXT DEFAULT ''")
    if "updated_at" not in cols:
        conn.execute("ALTER TABLE test_intents ADD COLUMN updated_at TEXT DEFAULT ''")

    rfc_cols = {r["name"] for r in conn.execute("PRAGMA table_info(rfc_meta)").fetchall()}
    if "protocol_key" not in rfc_cols:
        conn.execute("ALTER TABLE rfc_meta ADD COLUMN protocol_key TEXT DEFAULT ''")
        # Backfill the existing row (if any) by re-resolving from its stored
        # rfc_number/title -- without this, an already-ingested RFC (e.g.
        # today's live BGP demo data) would silently fall back to the
        # generic profile instead of keeping its correct one after upgrade.
        row = conn.execute("SELECT rfc_number, rfc_title FROM rfc_meta WHERE id=1").fetchone()
        if row:
            profile = protocol_profiles.resolve_profile(row["rfc_number"], row["rfc_title"])
            conn.execute("UPDATE rfc_meta SET protocol_key=? WHERE id=1", (profile.key,))

    conn.commit()


def _log(conn, event, source):
    conn.execute(
        "INSERT INTO ingestion_log (event, source, timestamp) VALUES (?,?,?)",
        (event, source, datetime.datetime.now().isoformat(timespec="seconds")),
    )


# ------------------------------------------------------------------ #
# File uploads — RFC files and supporting artefacts (product specs, etc.)
# ------------------------------------------------------------------ #

def extract_text_from_file(filename: str, raw_bytes: bytes):
    """Best-effort text extraction from an uploaded file. Returns
    (text, note) -- note is empty on success, or an explanation of why
    extraction failed/produced nothing (surfaced to the UI, never silent)."""
    ext = Path(filename).suffix.lower()
    if ext in (".txt", ".md", ".log", ".py"):
        try:
            return raw_bytes.decode("utf-8", errors="ignore"), ""
        except Exception as e:
            return "", f"could not decode file as text: {e}"
    if ext == ".pdf":
        try:
            import io
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(raw_bytes))
            text = "\n".join((page.extract_text() or "") for page in reader.pages)
            if not text.strip():
                return "", "PDF parsed but no extractable text (scanned/image-only PDF?)"
            return text, ""
        except ImportError:
            return "", "pypdf not installed on the server -- cannot extract PDF text (pip install pypdf)"
        except Exception as e:
            return "", f"PDF extraction failed: {e}"
    return "", f"unsupported file type '{ext or '(none)'}' -- upload .txt, .md, or .pdf"


def save_artefact(artefact_type: str, filename: str, raw_bytes: bytes):
    """Stores an uploaded product spec / other reference artefact. Extracted
    text becomes part of the grounding context handed to the AI on every
    later test-generation call (see get_artefact_context) -- it never feeds
    the RFC requirement extractor, only the AI's protocol reasoning step."""
    text, note = extract_text_from_file(filename, raw_bytes)
    conn = get_conn()
    uploaded_at = datetime.datetime.now().isoformat(timespec="seconds")
    cur = conn.execute(
        "INSERT INTO artefacts (artefact_type, filename, content_text, char_count, uploaded_at, notes) "
        "VALUES (?,?,?,?,?,?)",
        (artefact_type, filename, text, len(text), uploaded_at, note),
    )
    artefact_id = cur.lastrowid
    try:
        (ARTEFACTS_DIR / f"{artefact_id}_{filename}").write_bytes(raw_bytes)
    except OSError:
        pass
    status = "extracted" if text else f"NOT extracted ({note})"
    _log(conn, f"ARTEFACT_UPLOADED ({artefact_type}: {filename}, {status})", filename)
    conn.commit()
    conn.close()
    return {"id": artefact_id, "artefact_type": artefact_type, "filename": filename,
            "char_count": len(text), "uploaded_at": uploaded_at, "notes": note}


def get_artefacts():
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, artefact_type, filename, char_count, uploaded_at, notes FROM artefacts ORDER BY id DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def delete_artefact(artefact_id: int):
    conn = get_conn()
    row = conn.execute("SELECT filename FROM artefacts WHERE id=?", (artefact_id,)).fetchone()
    conn.execute("DELETE FROM artefacts WHERE id=?", (artefact_id,))
    if row:
        _log(conn, f"ARTEFACT_DELETED ({row['filename']})", row["filename"])
    conn.commit()
    conn.close()
    for p in ARTEFACTS_DIR.glob(f"{artefact_id}_*"):
        p.unlink(missing_ok=True)
    return {"deleted": bool(row)}


def get_artefact_context(max_chars: int = 6000) -> str:
    """Builds one grounding blob from uploaded artefacts for the AI generation
    prompt -- product specs first, other reference material after, each
    truncated to a fair per-doc share so a single large upload can't crowd
    out the rest. Returns '' if nothing usable has been uploaded."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT artefact_type, filename, content_text FROM artefacts WHERE content_text != '' "
        "ORDER BY CASE artefact_type WHEN 'product_spec' THEN 0 ELSE 1 END, id"
    ).fetchall()
    conn.close()
    if not rows:
        return ""
    per_doc_budget = max(500, max_chars // len(rows))
    parts = []
    for r in rows:
        label = "PRODUCT SPEC" if r["artefact_type"] == "product_spec" else "REFERENCE MATERIAL"
        parts.append(f"[{label}: {r['filename']}]\n{r['content_text'][:per_doc_budget]}")
    return "\n\n".join(parts)[:max_chars]


# ------------------------------------------------------------------ #
# Existing test suite uploads -- AI-reviewed against the RFC, uploaded
# artefacts, and this tool's own generated tests, to find REAL gaps
# (i.e. requirements no test -- generated or pre-existing -- covers).
# ------------------------------------------------------------------ #

def save_uploaded_test(filename: str, raw_bytes: bytes):
    """Stores an uploaded existing test file (pytest source, a documented
    test case, an exported test-case list, etc). Not analyzed yet -- call
    analyze_uploaded_test_coverage() to map it against RFC requirements."""
    text, note = extract_text_from_file(filename, raw_bytes)
    conn = get_conn()
    uploaded_at = datetime.datetime.now().isoformat(timespec="seconds")
    cur = conn.execute(
        "INSERT INTO uploaded_tests (filename, content_text, char_count, uploaded_at, notes) "
        "VALUES (?,?,?,?,?)",
        (filename, text, len(text), uploaded_at, note),
    )
    uploaded_test_id = cur.lastrowid
    try:
        (UPLOADED_TESTS_DIR / f"{uploaded_test_id}_{filename}").write_bytes(raw_bytes)
    except OSError:
        pass
    status = "ready to analyze" if text else f"NOT extracted ({note})"
    _log(conn, f"EXISTING_TEST_UPLOADED ({filename}, {status})", filename)
    conn.commit()
    conn.close()
    return {"id": uploaded_test_id, "filename": filename, "char_count": len(text),
            "uploaded_at": uploaded_at, "notes": note, "analyzed": False}


def get_uploaded_tests():
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, filename, char_count, uploaded_at, notes, analyzed, analysis_mode, analyzed_at "
        "FROM uploaded_tests ORDER BY id DESC"
    ).fetchall()
    counts = {r["uploaded_test_id"]: r["n"] for r in conn.execute(
        "SELECT uploaded_test_id, COUNT(*) AS n FROM uploaded_test_requirement_map GROUP BY uploaded_test_id"
    ).fetchall()}
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d["analyzed"] = bool(d["analyzed"])
        d["matched_requirement_count"] = counts.get(d["id"], 0)
        out.append(d)
    return out


def delete_uploaded_test(uploaded_test_id: int):
    conn = get_conn()
    row = conn.execute("SELECT filename FROM uploaded_tests WHERE id=?", (uploaded_test_id,)).fetchone()
    conn.execute("DELETE FROM uploaded_test_requirement_map WHERE uploaded_test_id=?", (uploaded_test_id,))
    conn.execute("DELETE FROM uploaded_tests WHERE id=?", (uploaded_test_id,))
    if row:
        _log(conn, f"EXISTING_TEST_DELETED ({row['filename']})", row["filename"])
    conn.commit()
    conn.close()
    for p in UPLOADED_TESTS_DIR.glob(f"{uploaded_test_id}_*"):
        p.unlink(missing_ok=True)
    return {"deleted": bool(row)}


def analyze_uploaded_test_coverage(uploaded_test_id: int):
    """The core of this feature: asks the AI whether an uploaded existing
    test actually verifies each of a semantically-narrowed set of candidate
    RFC requirements (retrieved via the same persisted TF-IDF index used
    everywhere else -- never a re-derivation from raw RFC text), grounded
    with any uploaded product-spec context. Idempotent -- re-running clears
    and replaces this file's previous mapping rather than duplicating it."""
    conn = get_conn()
    row = conn.execute("SELECT * FROM uploaded_tests WHERE id=?", (uploaded_test_id,)).fetchone()
    if not row:
        conn.close()
        return {"error": "not found"}
    test_row = dict(row)

    if not test_row["content_text"]:
        conn.execute(
            "UPDATE uploaded_tests SET analyzed=1, analysis_mode=?, analyzed_at=? WHERE id=?",
            ("skipped:no-extractable-text", datetime.datetime.now().isoformat(timespec="seconds"), uploaded_test_id),
        )
        conn.commit()
        conn.close()
        return {"uploaded_test_id": uploaded_test_id, "matched_requirement_ids": [], "mode": "skipped:no-extractable-text"}

    rfc_row = conn.execute("SELECT rfc_number, rfc_title FROM rfc_meta WHERE id=1").fetchone()
    rfc_label = f"RFC {rfc_row['rfc_number']} ({rfc_row['rfc_title']})" if rfc_row else "RFC"
    conn.close()

    candidates = semantic_search(test_row["content_text"][:4000], k=COVERAGE_CANDIDATE_K)
    artefact_context = get_artefact_context()
    test_content = test_row["content_text"][:MAX_TEST_CONTENT_CHARS]

    result, mode = ai_generation.analyze_existing_test_coverage(
        rfc_label, test_row["filename"], test_content, candidates, get_active_profile(), artefact_context
    )

    conn = get_conn()
    conn.execute("DELETE FROM uploaded_test_requirement_map WHERE uploaded_test_id=?", (uploaded_test_id,))
    matched_ids = []
    for match in result:
        conn.execute(
            "INSERT INTO uploaded_test_requirement_map (uploaded_test_id, requirement_id, confidence, rationale) "
            "VALUES (?,?,?,?)",
            (uploaded_test_id, match["requirement_id"], match["confidence"], match["rationale"]),
        )
        matched_ids.append(match["requirement_id"])

    conn.execute(
        "UPDATE uploaded_tests SET analyzed=1, analysis_mode=?, analyzed_at=? WHERE id=?",
        (mode, datetime.datetime.now().isoformat(timespec="seconds"), uploaded_test_id),
    )
    _log(conn, f"EXISTING_TEST_ANALYZED ({test_row['filename']}, {len(matched_ids)} requirement(s) matched, mode={mode})",
         test_row["filename"])
    conn.commit()
    conn.close()
    return {"uploaded_test_id": uploaded_test_id, "matched_requirement_ids": matched_ids, "mode": mode}


def get_existing_test_coverage_map():
    """requirement_id -> list of {uploaded_test_id, filename, confidence, rationale},
    used by get_coverage() to tell real gaps from ones already exercised by an
    uploaded existing test."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT m.requirement_id, m.confidence, m.rationale, t.id AS uploaded_test_id, t.filename "
        "FROM uploaded_test_requirement_map m JOIN uploaded_tests t ON t.id = m.uploaded_test_id"
    ).fetchall()
    conn.close()
    out = defaultdict(list)
    for r in rows:
        out[r["requirement_id"]].append({
            "uploaded_test_id": r["uploaded_test_id"], "filename": r["filename"],
            "confidence": r["confidence"], "rationale": r["rationale"],
        })
    return out


# ------------------------------------------------------------------ #
# Stage A — Ingestion & normalization + requirement extraction
# ------------------------------------------------------------------ #

SECTION_RE = re.compile(r'^(\d+(?:\.\d+)*)\.\s+([A-Z][A-Za-z0-9 ()/",.\'\-]+)\s*$')
PAGE_HEADER_RE = re.compile(r'^(RFC \d+|[A-Za-z]+, et al\.)')
FORM_FEED = '\x0c'

KEYWORDS = ["MUST NOT", "SHOULD NOT", "SHALL NOT", "MUST", "REQUIRED", "SHALL",
            "SHOULD", "RECOMMENDED", "MAY", "OPTIONAL"]
KEYWORD_RE = re.compile(r'\b(' + '|'.join(k.replace(' ', r'\s+') for k in KEYWORDS) + r')\b')

NOT_OBSERVABLE_HINTS = [
    r'implementation[- ]dependent', r'local matter', r'internal to', r'MAY choose to store',
    r'is a matter of local', r'not required to advertise this attribute',
    r'SHALL NOT use.{0,40}as its inputs', r'degree of preference', r'is outside the scope',
    r'implementation[- ]specific', r'a private matter',
]


def _clean_lines(raw_text):
    cleaned = []
    for ln in raw_text.split('\n'):
        ln = ln.replace(FORM_FEED, '')
        if PAGE_HEADER_RE.match(ln.strip()):
            continue
        cleaned.append(ln)
    return cleaned


def _split_sections(lines):
    sections, current = [], None
    for ln in lines:
        stripped = ln.strip()
        m = SECTION_RE.match(stripped)
        if m and not ln.startswith(' '):
            if current:
                sections.append(current)
            current = {"section_id": m.group(1), "title": m.group(2).strip(), "body_lines": []}
        elif current:
            current["body_lines"].append(ln)
    if current:
        sections.append(current)
    for s in sections:
        s["body"] = "\n".join(s.pop("body_lines")).strip()
    return sections


def _classify_category(section_title, statement_text, profile):
    """Profile-specific rules are tried first (e.g. BGP's path_attribute
    patterns), then the rules common to every protocol (timer,
    message_format, error_handling, capability_negotiation) -- see
    protocol_profiles.py. Falls back to general_conformance."""
    hay = f"{section_title} {statement_text}"
    for category, patterns in list(profile.category_rules) + protocol_profiles.COMMON_CATEGORY_RULES:
        for p in patterns:
            if re.search(p, hay, re.IGNORECASE):
                return category
    return "general_conformance"


def _classify_testability(statement_text):
    for p in NOT_OBSERVABLE_HINTS:
        if re.search(p, statement_text, re.IGNORECASE):
            return "not_independently_observable"
    return "automatable"


def _split_into_statements(body_text):
    text = re.sub(r'\s+', ' ', body_text).strip()
    raw_sentences = re.split(r'(?<=[a-z0-9\)])\.\s+(?=[A-Z])', text)
    statements = []
    for s in raw_sentences:
        s = s.strip()
        if s and KEYWORD_RE.search(s):
            statements.append(s if s.endswith('.') else s + '.')
    return statements


def _extract_requirements(raw_text: str, rfc_number: str, profile, counter_by_section: dict = None) -> list:
    """Stage-A extraction only: raw text -> a list of requirement dicts, no
    persistence. Shared by ingest_rfc (fresh full extraction, counters start
    at 0) and ingest_rfc_incremental (counters seeded from the existing
    per-section counts already in the DB, so requirement_id numbering keeps
    counting up instead of restarting at 1) -- neither reimplements the
    section/statement parsing, they only differ in what happens to the
    result afterward (replace vs. merge)."""
    counter_by_section = dict(counter_by_section) if counter_by_section else {}
    lines = _clean_lines(raw_text)
    sections = _split_sections(lines)

    requirements = []
    for sec in sections:
        if not sec["body"]:
            continue
        for stmt in _split_into_statements(sec["body"]):
            sid = sec["section_id"]
            counter_by_section[sid] = counter_by_section.get(sid, 0) + 1
            n = counter_by_section[sid]
            kw_match = KEYWORD_RE.search(stmt)
            keyword = kw_match.group(1).upper() if kw_match else "UNKNOWN"
            req_id = f"RFC{rfc_number}-S{sid}-REQ-{n:02d}"
            requirements.append({
                "requirement_id": req_id, "rfc": f"RFC{rfc_number}", "section_id": sid,
                "section_title": sec["title"], "keyword": keyword, "statement": stmt,
                "category": _classify_category(sec["title"], stmt, profile),
                "testability": _classify_testability(stmt),
            })
    return requirements


def ingest_rfc(rfc_number: str, rfc_title: str, raw_text: str, source_label: str, protocol_override: str = ""):
    """Full re-ingestion: parse -> extract -> persist -> rebuild retrieval index.
    This is the ONLY *replace* path that reads raw RFC text -- it wipes and
    replaces the entire knowledge base every call (requirements, statuses,
    generated tests, rfc_meta). For adding more knowledge onto an existing
    knowledge base without losing already-generated tests, see
    ingest_rfc_incremental below -- a separate, additive function, not a mode
    flag on this one, so this destructive path's behavior can't be changed
    by accident.

    protocol_override lets the caller force a specific protocol_profiles key
    instead of auto-detecting from rfc_number/rfc_title -- for an RFC the
    built-in table doesn't recognize (see protocol_profiles.resolve_profile).
    An unrecognized override falls back to auto-detection rather than
    silently no-op'ing."""
    profile = protocol_profiles.get_profile(protocol_override) if protocol_override else None
    if profile is None or (protocol_override and profile.key != protocol_override):
        profile = protocol_profiles.resolve_profile(rfc_number, rfc_title)

    requirements = _extract_requirements(raw_text, rfc_number, profile)

    conn = get_conn()
    conn.execute("DELETE FROM requirements")
    conn.execute("DELETE FROM requirement_status")
    conn.execute("DELETE FROM test_intents")
    conn.execute("DELETE FROM rfc_meta")
    for r in requirements:
        conn.execute(
            "INSERT INTO requirements VALUES (?,?,?,?,?,?,?,?)",
            (r["requirement_id"], r["rfc"], r["section_id"], r["section_title"],
             r["keyword"], r["statement"], r["category"], r["testability"]),
        )
        conn.execute("INSERT INTO requirement_status (requirement_id) VALUES (?)", (r["requirement_id"],))
    conn.execute(
        "INSERT INTO rfc_meta (id, rfc_number, rfc_title, source, ingested_at, protocol_key) VALUES (1,?,?,?,?,?)",
        (rfc_number, rfc_title, source_label, datetime.datetime.now().isoformat(timespec="seconds"), profile.key),
    )
    _log(conn, f"RFC_INGESTED (RFC {rfc_number}, {len(requirements)} requirements extracted, "
               f"protocol={profile.key})", source_label)
    conn.commit()
    conn.close()

    # Clear old generated test files (fresh RFC = fresh test package) --
    # both the full record and the deduplicated view.
    for f in (list(DOCS_DIR.glob("*.md")) + list(PYTEST_DIR.glob("*.py"))
              + list(DEDUP_DOCS_DIR.glob("*.md")) + list(DEDUP_PYTEST_DIR.glob("*.py"))):
        f.unlink()

    _build_retrieval_index()
    logger.info(f"Ingested RFC {rfc_number} ({rfc_title!r}): {len(requirements)} requirements extracted, "
                f"protocol={profile.key}, source={source_label}")
    return {"requirement_count": len(requirements)}


def get_active_profile():
    """The protocol_profiles.ProtocolProfile for whatever RFC is currently
    ingested. Falls back to the generic profile if nothing's ingested yet
    or the stored key isn't recognized -- never raises."""
    conn = get_conn()
    row = conn.execute("SELECT protocol_key FROM rfc_meta WHERE id=1").fetchone()
    conn.close()
    return protocol_profiles.get_profile(row["protocol_key"] if row else "")


# ------------------------------------------------------------------ #
# Knowledge library -- additive ingestion from a curated folder of source
# files (backend/kb/rfc_library/), as opposed to ingest_rfc's destructive
# paste/upload "replace everything" path above. See design.md for the full
# rationale; in short: this is what lets a demo ingest an RFC in stages
# (part 1, generate, part 2, generate again) without losing part 1's tests.
# ------------------------------------------------------------------ #

_LIBRARY_HEADER_RE = re.compile(r'^([A-Z_]+):\s*(.*)$')


def _parse_library_file(raw_text: str) -> dict:
    """Splits a knowledge-library file into its small metadata header
    (RFC_NUMBER / RFC_TITLE / PROTOCOL, one per line, ended by a lone '---'
    line) and the actual RFC body text that follows -- lets a library file
    self-describe what it is without needing a separate ingest form. Any
    header field not present just comes back as ''; PROTOCOL is optional
    (empty means auto-detect, same as ingest_rfc's protocol_override='')."""
    lines = raw_text.split('\n')
    meta = {"RFC_NUMBER": "", "RFC_TITLE": "", "PROTOCOL": ""}
    body_start = 0
    for i, ln in enumerate(lines):
        if ln.strip() == "---":
            body_start = i + 1
            break
        m = _LIBRARY_HEADER_RE.match(ln.strip())
        if m and m.group(1) in meta:
            meta[m.group(1)] = m.group(2).strip()
    else:
        body_start = 0  # no '---' divider found -- treat the whole file as body, no header
    return {
        "rfc_number": meta["RFC_NUMBER"], "rfc_title": meta["RFC_TITLE"], "protocol": meta["PROTOCOL"],
        "body": "\n".join(lines[body_start:]),
    }


def _flag_context_stale_tests(new_requirement_ids: set) -> list:
    """After new requirements are merged in, checks every existing generated
    test to see whether its retrieval context (see _retrieval_context_for)
    would now include one of the just-added requirements -- if so, the test
    was generated without seeing knowledge that's now part of its local
    context, so it's flagged context_stale for the next generation batch to
    pick up and regenerate (see regenerate_stale_tests). This is a real
    signal, not a guess: the TF-IDF retrieval index was already rebuilt by
    the caller before this runs, so semantic_search here reflects the
    enlarged corpus."""
    if not new_requirement_ids:
        return []
    conn = get_conn()
    rows = conn.execute(
        "SELECT test_id, requirement_id FROM test_intents WHERE context_stale=0"
    ).fetchall()
    flagged = []
    for row in rows:
        req_row = conn.execute("SELECT * FROM requirements WHERE requirement_id=?", (row["requirement_id"],)).fetchone()
        if not req_row:
            continue
        _related, related_ids = _retrieval_context_for(dict(req_row))
        newly_appeared = related_ids & new_requirement_ids
        if newly_appeared:
            reason = f"new related requirement(s) in context: {', '.join(sorted(newly_appeared))}"
            conn.execute(
                "UPDATE test_intents SET context_stale=1, context_stale_reason=? WHERE test_id=?",
                (reason, row["test_id"]),
            )
            flagged.append(row["test_id"])
    conn.commit()
    conn.close()
    return flagged


def ingest_rfc_incremental(filename: str, raw_text: str, source_label: str):
    """Additive counterpart to ingest_rfc: merges new requirements from a
    knowledge-library file INTO the current knowledge base instead of
    replacing it -- existing requirements, generated tests, and files are
    never deleted. Only requirement_ids not already present get inserted
    (section-scoped IDs mean a non-overlapping section split never
    collides); existing tests whose retrieval context now includes one of
    the newly-added requirements are flagged context_stale for the next
    generation batch to regenerate (see _flag_context_stale_tests,
    regenerate_stale_tests).

    Raises ValueError if a different RFC is already loaded -- merging
    requirements from two different RFCs into one flat knowledge base isn't
    semantically sound here; use ingest_rfc (replace) or reset first."""
    parsed = _parse_library_file(raw_text)
    rfc_number = parsed["rfc_number"]
    rfc_title = parsed["rfc_title"] or f"RFC {rfc_number}"
    if not rfc_number:
        raise ValueError(f"{filename}: missing RFC_NUMBER header -- cannot determine requirement IDs")

    conn = get_conn()
    already_ingested = conn.execute("SELECT 1 FROM knowledge_sources WHERE filename=?", (filename,)).fetchone()
    if already_ingested:
        # Idempotent: re-POSTing the same filename (double-click, retry)
        # must NOT re-extract -- the per-section requirement_id counters
        # below are seeded from the CURRENT count in that section, so
        # blindly re-running extraction against a file already merged in
        # would mint a second, higher-numbered batch of "new" requirement
        # IDs for content that's already there, silently duplicating it.
        conn.close()
        logger.info(f"Incremental ingest of {filename}: already ingested, no-op")
        return {"requirement_count_added": 0, "new_requirement_ids": [], "flagged_stale_test_ids": [],
                "already_ingested": True}

    existing_meta = conn.execute("SELECT rfc_number, rfc_title, protocol_key FROM rfc_meta WHERE id=1").fetchone()
    if existing_meta and existing_meta["rfc_number"] != rfc_number:
        conn.close()
        raise ValueError(
            f"Knowledge base currently holds RFC {existing_meta['rfc_number']}; {filename} is RFC {rfc_number}. "
            f"Use /api/ingest to replace it, or reset the knowledge base first."
        )

    if existing_meta:
        profile = protocol_profiles.get_profile(existing_meta["protocol_key"])
    else:
        protocol_override = parsed["protocol"]
        profile = protocol_profiles.get_profile(protocol_override) if protocol_override else None
        if profile is None or (protocol_override and profile.key != protocol_override):
            profile = protocol_profiles.resolve_profile(rfc_number, rfc_title)

    counter_by_section = {r["section_id"]: r["n"] for r in
                           conn.execute("SELECT section_id, COUNT(*) AS n FROM requirements GROUP BY section_id").fetchall()}
    candidates = _extract_requirements(parsed["body"], rfc_number, profile, counter_by_section)

    existing_ids = {r["requirement_id"] for r in conn.execute("SELECT requirement_id FROM requirements").fetchall()}
    new_requirements = [r for r in candidates if r["requirement_id"] not in existing_ids]

    for r in new_requirements:
        conn.execute(
            "INSERT INTO requirements VALUES (?,?,?,?,?,?,?,?)",
            (r["requirement_id"], r["rfc"], r["section_id"], r["section_title"],
             r["keyword"], r["statement"], r["category"], r["testability"]),
        )
        conn.execute("INSERT INTO requirement_status (requirement_id) VALUES (?)", (r["requirement_id"],))

    if not existing_meta:
        conn.execute(
            "INSERT INTO rfc_meta (id, rfc_number, rfc_title, source, ingested_at, protocol_key) VALUES (1,?,?,?,?,?)",
            (rfc_number, rfc_title, source_label, datetime.datetime.now().isoformat(timespec="seconds"), profile.key),
        )

    conn.commit()
    conn.close()

    _build_retrieval_index()

    new_ids = {r["requirement_id"] for r in new_requirements}
    flagged_stale = _flag_context_stale_tests(new_ids)

    conn = get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO knowledge_sources (filename, rfc_number, rfc_title, protocol_key, ingested_at, "
        "requirements_added) VALUES (?,?,?,?,?,?)",
        (filename, rfc_number, rfc_title, profile.key,
         datetime.datetime.now().isoformat(timespec="seconds"), len(new_requirements)),
    )
    _log(conn, f"RFC_INGESTED_INCREMENTAL ({filename}, {len(new_requirements)} requirement(s) added, "
                f"{len(flagged_stale)} existing test(s) flagged context_stale)", source_label)
    conn.commit()
    conn.close()

    logger.info(f"Incremental ingest of {filename}: {len(new_requirements)} new requirement(s), "
                f"{len(flagged_stale)} existing test(s) flagged context_stale")
    return {
        "requirement_count_added": len(new_requirements),
        "new_requirement_ids": sorted(new_ids),
        "flagged_stale_test_ids": flagged_stale,
    }


def get_knowledge_library():
    """Lists every file in kb/rfc_library/ alongside its ingested status --
    powers GET /api/knowledge-library. Scans the folder fresh each call (it's
    a small, source-controlled set of files) and left-joins against
    knowledge_sources in Python rather than SQL since the file list, not the
    DB, is the source of truth for what's selectable."""
    conn = get_conn()
    sources = {r["filename"]: dict(r) for r in conn.execute("SELECT * FROM knowledge_sources").fetchall()}
    conn.close()
    out = []
    for path in sorted(RFC_LIBRARY_DIR.glob("*.txt")):
        src = sources.get(path.name)
        out.append({
            "filename": path.name,
            "ingested": src is not None,
            "ingested_at": src["ingested_at"] if src else None,
            "rfc_number": src["rfc_number"] if src else None,
            "rfc_title": src["rfc_title"] if src else None,
            "requirements_added": src["requirements_added"] if src else None,
        })
    return out


def ingest_library_file(filename: str):
    """Resolves `filename` safely within RFC_LIBRARY_DIR (rejects path
    traversal / anything not actually in that folder) and ingests it via
    ingest_rfc_incremental -- the implementation behind
    POST /api/knowledge-library/<filename>/ingest."""
    candidate = (RFC_LIBRARY_DIR / filename).resolve()
    if RFC_LIBRARY_DIR.resolve() not in candidate.parents or not candidate.is_file():
        raise ValueError(f"{filename!r} is not a file in the knowledge library")
    raw_text = candidate.read_text(encoding="utf-8", errors="ignore")
    return ingest_rfc_incremental(filename, raw_text, f"kb/rfc_library/{filename}")


def _build_retrieval_index():
    """Semantic retrieval side of the hybrid retriever. Reads from the DB
    (already-persisted requirements), never from raw RFC text."""
    from sklearn.feature_extraction.text import TfidfVectorizer
    conn = get_conn()
    rows = conn.execute("SELECT requirement_id, section_title, category, statement FROM requirements").fetchall()
    conn.close()
    if not rows:
        return
    ids = [r["requirement_id"] for r in rows]
    corpus = [f"{r['section_title']} {r['category']} {r['statement']}" for r in rows]
    vectorizer = TfidfVectorizer(stop_words="english", ngram_range=(1, 2), max_features=5000)
    matrix = vectorizer.fit_transform(corpus)
    with open(RETRIEVAL_INDEX_PATH, "wb") as f:
        pickle.dump({"ids": ids, "vectorizer": vectorizer, "matrix": matrix}, f)


def semantic_search(query: str, k: int = 10):
    """Hybrid retrieval — semantic side. Reads the persisted index only."""
    if not RETRIEVAL_INDEX_PATH.exists():
        return []
    from sklearn.metrics.pairwise import cosine_similarity
    with open(RETRIEVAL_INDEX_PATH, "rb") as f:
        idx = pickle.load(f)
    qv = idx["vectorizer"].transform([query])
    sims = cosine_similarity(qv, idx["matrix"])[0]
    top = sims.argsort()[::-1][:k]
    conn = get_conn()
    results = []
    for i in top:
        rid = idx["ids"][i]
        row = conn.execute("SELECT * FROM requirements WHERE requirement_id=?", (rid,)).fetchone()
        if row:
            results.append({**dict(row), "similarity": round(float(sims[i]), 3)})
    conn.close()
    return results


# ------------------------------------------------------------------ #
# Stage B — Test generation (Test-Intent -> compiled doc + pytest)
# ------------------------------------------------------------------ #

DOC_TEMPLATE = """# {test_id}

| Field | Value |
|---|---|
| RFC Reference | {rid} — {rfc}, §{section_id} ({section_title}) |
| Test Type | {test_type} |
| Category | {category} |
| Risk | {risk} |
| Priority | {priority} |
| Generation Mode | {generation_mode}{review_flag} |

## Normative Statement

> **{keyword}:** {statement}

## Protocol Reasoning

{protocol_reasoning}

## Preconditions

- **Topology:** {topology} ({topology_description}){topology_note}
- **Timers:** {timers_line}
- **Devices:** Juniper vJunos-router / vMX pair reachable via NETCONF (PyEZ)
{emulator_note}

## Test Steps

{steps}

## Expected Result

{checks_doc_block}

**Observation point:** `{pyez_observation}`
{observations_doc_block}
## Safety

- Lab-only execution: **Yes**
- Requires `commit confirmed` on configuration changes: **Yes**

## Traceability

- Knowledge base: `kb/knowledge.db` (retrieved, not re-derived from raw RFC text)
{reuse_note}
{notes_block}
"""

PYTEST_TEMPLATE = '''"""
Auto-generated pytest/PyEZ stub -- traced to {rid}
{rfc}, Section {section_id} ({section_title})
Generation mode: {generation_mode}

Requirement ({keyword}): {statement}

Protocol reasoning: {protocol_reasoning}

STATUS: lab-ready stub. Uses real PyEZ call patterns but device host/credentials
are placeholders -- wire to your vJunos-router/vMX lab inventory before running.
{emulator_warning}"""
import pytest
from jnpr.junos import Device
from jnpr.junos.utils.config import Config


REQUIREMENT_ID = "{rid}"
TEST_TYPE = "{test_type}"
RISK = "{risk}"
GENERATION_MODE = "{generation_mode}"


@pytest.fixture(scope="module")
def r1():
    dev = Device(host="R1_LAB_HOST", user="LAB_USER", password="LAB_PASSWORD", port=22)
    dev.open()
    yield dev
    dev.close()


@pytest.fixture(scope="module")
def r2():
    dev = Device(host="R2_LAB_HOST", user="LAB_USER", password="LAB_PASSWORD", port=22)
    dev.open()
    yield dev
    dev.close()


def test_{test_id}(r1, r2):
    """
    Traces to: {rid}
    {statement}

    Reasoning: {protocol_reasoning}
    """
    with Config(r1, mode="exclusive") as cu:
        cu.load(
            \'\'\'
{config_stanza}
            \'\'\',
            format="text",
        )
        cu.commit(confirm=2)

    # --- stimulus ---
{stimulus_block}

    # --- observation ---
    # PyEZ observation point (AI-suggested): {pyez_observation}
    _info = r1.{observation_call}
    {result_var} = _info.findtext("{observation_field}")
{extra_observations_block}
    # --- assertion ---
    # Expected checks:
{expected_checks_comment}
    assert {result_var} is not None, "Could not read {result_var} via PyEZ RPC"
{assertion_blocks}
'''

STEPS_BY_TYPE = {
    "positive": ["Bring up the eBGP session between R1 and R2 with default/valid parameters.",
                 "Verify the specific protocol behavior described by the requirement is exhibited."],
    "negative": ["Configure or inject the specific invalid/non-conformant condition the requirement guards against.",
                 "Verify the implementation rejects/handles it exactly as specified, not silently."],
    "boundary": ["Configure the parameter at, just below, and just above the specified boundary value.",
                 "Verify accept/reject behavior matches the boundary exactly."],
    "policy": ["Apply the relevant policy/configuration knob referenced by the requirement.",
               "Verify behavior changes correctly when the policy is toggled on vs. off."],
    "recovery": ["Establish a healthy session, then induce the fault/collision/teardown condition.",
                 "Verify the FSM recovers to the correct end state per the requirement."],
}
ASSERTION_BY_TYPE = {
    "positive": "bgp_peer_state == Established (or requirement-specific field observed)",
    "negative": "NOTIFICATION sent with correct Error Code/Subcode, OR session correctly refuses the condition",
    "boundary": "acceptance/rejection flips exactly at the documented boundary value",
    "policy": "observed behavior differs correctly between policy-enabled and policy-disabled runs",
    "recovery": "FSM returns to Established (or correct terminal state) after the induced fault",
}


def infer_test_type(category: str, keyword: str) -> str:
    """Heuristic default test_type when the caller doesn't specify one."""
    if keyword in ("MUST NOT", "SHALL NOT", "SHOULD NOT"):
        return "negative"
    if category == "timer":
        return "boundary"
    if category in ("connection_management",) and "collision" in keyword.lower():
        return "recovery"
    return "positive"


def _render_extra_observations(observations: list, profile) -> str:
    """Renders AI-declared extra data fetches into real PyEZ code -- the AI
    only ever supplies a variable name, a source key (validated against
    profile.secondary_observations), and an XPath string; the actual RPC
    call text always comes from the trusted profile, never from AI output.
    Secondary sources are deduped so a shared source is only fetched once
    even if multiple declared variables read from it. Returns '' if there's
    nothing to add (the common case: no extra observations declared)."""
    if not observations:
        return ""
    lines = ["", "    # additional AI-requested observation(s)"]
    holder_var_by_source = {}
    for obs in observations:
        # repr() (not manual "..." quoting) so an AI-supplied XPath
        # containing a literal quote character (valid XPath syntax, e.g.
        # an attribute-value predicate) still renders as a syntactically
        # valid Python string literal instead of breaking the generated file.
        xpath_literal = repr(obs["xpath"])
        if obs["source"] == "primary":
            lines.append(f'    {obs["var_name"]} = _info.findtext({xpath_literal})')
            continue
        source_key = obs["source"]
        holder_var = holder_var_by_source.get(source_key)
        if holder_var is None:
            holder_var = f"_{source_key}"
            holder_var_by_source[source_key] = holder_var
            lines.append(f'    {holder_var} = r1.{profile.secondary_observations[source_key]}')
        lines.append(f'    {obs["var_name"]} = {holder_var}.findtext({xpath_literal})')
    return "\n".join(lines) + "\n"


def _render_observations_doc(observations: list, profile) -> str:
    """Doc-side counterpart to _render_extra_observations -- lists the same
    declared fetches for human traceability. '' if none were declared."""
    if not observations:
        return ""
    lines = ["", "**Additional observations captured:**"]
    for obs in observations:
        source_desc = ("the primary observation above" if obs["source"] == "primary"
                        else f'`{obs["source"]}` ({profile.secondary_observations[obs["source"]]})')
        lines.append(f'- `{obs["var_name"]}` = `{obs["xpath"]}` via {source_desc}')
    return "\n".join(lines) + "\n"


def _render_checks_doc(checks: list) -> str:
    """Markdown-numbered list of each check's description, for the doc's
    'Expected Result' section -- replaces the old single assertion_hint
    line now that a test can carry more than one independently-graded
    check (see ai_generation's 'checks' schema)."""
    return "\n".join(f"{i+1}. {c['description']}" for i, c in enumerate(checks))


def _render_checks_comment(checks: list) -> str:
    """Same list, rendered as commented lines inside the generated pytest
    stub, just above the executable assert block."""
    return "\n".join(f"    #   {i+1}. {c['description'].replace(chr(10), ' ')}" for i, c in enumerate(checks))


def _build_assertion_blocks(checks: list, confidence: str, result_var: str) -> str:
    """One block per check: a promoted executable assert if that check's
    assertion_code passed ai_generation._safe_assertion_expr, a commented
    suggestion if it has code that failed safety, or a commented TODO if
    the model left assertion_code empty. Each check is judged entirely on
    its own -- one check failing safety no longer drags its siblings down
    to a single generic base check, which is the direct fix for tests that
    used to render just one assert regardless of how many facts the
    requirement actually implied."""
    blocks = []
    for check in checks:
        desc = check["description"].replace('"', "'").replace("\n", " ")
        code = check.get("assertion_code", "")
        if check.get("assertion_code_is_safe") and code:
            blocks.append(f'    assert {code}, "{desc} ({confidence} confidence) -- verify against lab output"')
        elif code:
            blocks.append(f'    # AI-suggested assertion for "{desc}" ({confidence} confidence, needs review before use):\n'
                           f'    # assert {code}\n'
                           f'    # TODO: replace with the precise assertion for this check')
        else:
            blocks.append(f'    # TODO: {desc}\n'
                           f'    # assert {result_var} == "<expected value for this check>"')
    return "\n".join(blocks)


def _same_section_siblings(req_dict: dict, exclude_ids: set, limit: int = 2) -> list:
    """Plain-SQL sibling lookup (not semantic search) -- other requirements
    extracted from the exact same RFC section as req_dict, so the model
    sees the full local cluster of related MUST/SHOULD clauses in that
    section, not just cross-similarity hits from semantic_search."""
    conn = get_conn()
    placeholders = ",".join("?" * len(exclude_ids))
    rows = conn.execute(
        f"SELECT * FROM requirements WHERE section_id=? AND requirement_id NOT IN ({placeholders}) "
        f"ORDER BY requirement_id LIMIT ?",
        (req_dict["section_id"], *exclude_ids, limit)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _retrieval_context_for(req_dict: dict) -> tuple:
    """The retrieval context for one requirement: semantically related
    requirements from the SAME persisted knowledge base (hybrid retriever,
    semantic side), plus same-section siblings via plain SQL (structural
    side) -- gives the model the full local cluster of related MUST/SHOULD
    clauses in that section, not just cross-similarity hits, so it has more
    raw material to draw distinct checks from. Shared by _generate_one (what
    the AI actually sees) and _flag_context_stale_tests (what "did this
    test's context change" checks against) so both agree on the definition
    of "this test's context." Returns (related_list, related_ids_set)."""
    rid = req_dict["requirement_id"]
    semantic_related = [r for r in semantic_search(req_dict["statement"], k=6) if r["requirement_id"] != rid][:4]
    exclude_ids = {rid} | {r["requirement_id"] for r in semantic_related}
    sibling_related = _same_section_siblings(req_dict, exclude_ids, limit=2)
    related = (semantic_related + sibling_related)[:6]
    return related, {r["requirement_id"] for r in related}


def _generate_one(req_dict: dict, rfc_label: str, artefact_context: str, derived_from: str, profile) -> dict:
    """The AI-call + template-render work for a single requirement, with no
    DB writes and no file writes -- safe to run concurrently across a thread
    pool (see generate_tests). Returns a fully-populated record ready for
    the caller to persist. Never raises: any unexpected failure here falls
    back to the same heuristic path AI failures already use, so one bad
    record can't sink a whole bulk-generation batch.

    `profile` (a protocol_profiles.ProtocolProfile) supplies every
    protocol-specific default that used to be hardcoded to BGP: topology
    description, timer fields, the Junos config stanza, and the PyEZ
    observation call actually executed by the rendered pytest stub -- the
    AI's own pyez_observation suggestion stays advisory documentation only,
    never the literal executed call, same safety boundary as assertion_code."""
    rid = req_dict["requirement_id"]
    try:
        category = req_dict["category"]
        keyword = req_dict["keyword"]

        related, related_ids = _retrieval_context_for(req_dict)

        ai_intent, mode = ai_generation.generate_ai_test_intent(rfc_label, req_dict, related, profile, artefact_context)
        ai_backend = ai_generation.get_active_backend_key() if ai_intent else ""

        if ai_intent:
            logger.info(f"{rid}: generated via AI backend '{ai_backend}' (mode={mode})")
            test_type = ai_intent["test_type"]
            risk = ai_intent["risk"]
            protocol_reasoning = ai_intent["protocol_reasoning"]
            steps = ai_intent["steps"]
            pyez_observation = ai_intent["pyez_observation"]
            requires_emulator = bool(ai_intent["requires_peer_emulator"])
            emulator_tool = ai_intent.get("emulator_tool", "none")
            topology_note = ai_intent.get("topology_note", "")
            notes = ai_intent.get("notes", "")
            confidence = ai_intent["confidence"]
            # Each check's promotion to an executable assert depends only on
            # its own safety check (AST allowlist + known-variable-names,
            # see ai_generation._safe_assertion_expr) -- confidence no
            # longer gates this; it still gates needs_review below, since a
            # safe-to-run assertion can still be worth a second look if the
            # model wasn't sure about it.
            checks = ai_intent.get("checks", [])
            observations = ai_intent.get("observations", [])
            needs_review = 0 if confidence == "high" else 1
        else:
            logger.info(f"{rid}: AI unavailable/failed ({mode}) -- used heuristic fallback")
            # Heuristic fallback -- same defaults as before AI was wired in.
            test_type = infer_test_type(category, keyword)
            risk = "high" if test_type in ("negative", "recovery") else ("medium" if test_type == "boundary" else "low")
            protocol_reasoning = "(heuristic fallback -- AI reasoning unavailable for this test; see notes)"
            steps = STEPS_BY_TYPE[test_type]
            assertion_hint = ASSERTION_BY_TYPE[test_type]
            pyez_observation = profile.observation_hint()
            requires_emulator = test_type in ("negative", "boundary")
            emulator_tool = profile.default_emulator_tool if requires_emulator else "none"
            topology_note = ""
            notes = mode  # e.g. "heuristic-fallback:no-api-key"
            confidence = "n/a"
            # Heuristic fallback keeps exactly one canned, unverified check
            # -- still the weakest path by design, the whole point of
            # needs_review.
            checks = [{"description": assertion_hint, "assertion_code": "", "assertion_code_is_safe": False}]
            observations = []
            needs_review = 1

        test_id = rid.lower().replace("-", "_").replace(".", "_")
        priority = "high" if risk == "high" else "medium"
        timers = profile.timers_for(category)

        reuse_note = f"- **Derived from:** {derived_from} (topology/timer fixture reused)" if derived_from else ""
        emulator_note = (f"- **Requires peer emulator: {emulator_tool}** — Junos will not originate this "
                          f"condition on its own." if requires_emulator else "")
        review_flag = " — ⚠ **NEEDS ENGINEER REVIEW**" if needs_review else ""
        notes_block = f"\n## Notes\n\n{notes}\n" if notes else ""
        topology_note_fmt = f" — {topology_note}" if topology_note else ""
        observations_doc_block = _render_observations_doc(observations, profile)

        doc_content = DOC_TEMPLATE.format(
            test_id=test_id, rid=rid, rfc=rfc_label, section_id=req_dict["section_id"],
            section_title=req_dict["section_title"], test_type=test_type, category=category,
            risk=risk, priority=priority, generation_mode=mode, review_flag=review_flag,
            topology=profile.topology_key, topology_description=profile.topology_description,
            topology_note=topology_note_fmt, timers_line=profile.timers_line(timers),
            steps="\n".join(f"{i+1}. {s}" for i, s in enumerate(steps)),
            checks_doc_block=_render_checks_doc(checks), pyez_observation=pyez_observation,
            observations_doc_block=observations_doc_block,
            emulator_note=emulator_note, keyword=keyword, statement=req_dict["statement"],
            reuse_note=reuse_note, notes_block=notes_block, protocol_reasoning=protocol_reasoning,
        )

        emulator_warning = (f"\nREQUIRES PEER EMULATOR: {emulator_tool} -- Junos will not originate this "
                             f"condition on its own; this stub alone cannot exercise this requirement.\n"
                             if requires_emulator else "")

        stimulus_block = (
            f"    # NOTE: requires a peer emulator ({emulator_tool}) to construct this stimulus --\n"
            f"    # PyEZ/Junos alone cannot originate a non-conformant message.\n"
            f"    # TODO: send the crafted stimulus via {emulator_tool} here."
        ) if requires_emulator else "    # (positive-path stimulus -- standard peering brings this up naturally)"

        config_stanza = Template(profile.config_template).substitute(**timers)
        extra_observations_block = _render_extra_observations(observations, profile)

        pytest_content = PYTEST_TEMPLATE.format(
            rid=rid, rfc=rfc_label, section_id=req_dict["section_id"], section_title=req_dict["section_title"],
            keyword=keyword, statement=req_dict["statement"].replace('"""', "'"), test_type=test_type,
            risk=risk, test_id=test_id, generation_mode=mode,
            protocol_reasoning=protocol_reasoning.replace('"""', "'"),
            emulator_warning=emulator_warning, pyez_observation=pyez_observation,
            config_stanza=config_stanza, observation_call=profile.observation_call,
            observation_field=profile.observation_field, result_var=profile.result_var,
            extra_observations_block=extra_observations_block,
            expected_checks_comment=_render_checks_comment(checks), stimulus_block=stimulus_block,
            assertion_blocks=_build_assertion_blocks(checks, confidence, profile.result_var),
        )

        return {
            "rid": rid, "test_id": test_id, "category": category, "test_type": test_type, "risk": risk,
            "priority": priority, "section_id": req_dict["section_id"], "section_title": req_dict["section_title"],
            "statement": req_dict["statement"], "keyword": keyword, "timers": timers,
            "doc_content": doc_content, "pytest_content": pytest_content, "mode": mode, "ai_backend": ai_backend,
            "protocol_reasoning": protocol_reasoning, "requires_emulator": requires_emulator,
            "emulator_tool": emulator_tool, "needs_review": needs_review,
            "context_requirement_ids": sorted(related_ids),
        }
    except Exception as e:
        logger.warning(f"_generate_one crashed unexpectedly for {rid}, falling back to a minimal stub: {e}")
        test_id = rid.lower().replace("-", "_").replace(".", "_")
        fallback_notes = f"heuristic-fallback:unexpected-error:{e}"
        doc_content = (f"# {test_id}\n\nGeneration failed unexpectedly for {rid}: {e}\n\n"
                        f"Statement: {req_dict.get('statement', '')}\n")
        pytest_content = (f'"""Generation failed unexpectedly for {rid}: {e}"""\n'
                           f"import pytest\n\n\ndef test_{test_id}():\n"
                           f"    pytest.skip(\"generation failed -- see doc for details\")\n")
        return {
            "rid": rid, "test_id": test_id, "category": req_dict.get("category", "general_conformance"),
            "test_type": "positive", "risk": "medium", "priority": "medium",
            "section_id": req_dict.get("section_id", ""), "section_title": req_dict.get("section_title", ""),
            "statement": req_dict.get("statement", ""), "keyword": req_dict.get("keyword", ""),
            "timers": profile.timers_for(req_dict.get("category", "general_conformance")),
            "doc_content": doc_content, "pytest_content": pytest_content, "mode": fallback_notes, "ai_backend": "",
            "protocol_reasoning": "", "requires_emulator": False, "emulator_tool": "none", "needs_review": 1,
            "context_requirement_ids": [],
        }


# Written into generated_tests/deduplicated/pytest/conftest.py by
# refresh_deduplicated_tests() on every refresh (that directory's *.py
# files are cleared and rewritten each time, so this can't be a plain
# static file living there -- it has to be re-written alongside the
# generated tests). Shims jnpr.junos / jnpr.junos.utils.config with
# pyez.mock_device's MockJunosDevice/MockConfig *before* pytest imports the
# generated test files, since conftest.py is always loaded first for
# whatever directory it lives in. PyEZ isn't a real project dependency, so
# there's nothing genuine for this to shadow/conflict with.
CONFTEST_CONTENT = '''"""
Auto-written by pipeline.refresh_deduplicated_tests() on every refresh --
do not hand-edit, it will be overwritten. Shims jnpr.junos /
jnpr.junos.utils.config so the generated pytest stubs run against
pyez.mock_device's MockJunosDevice/MockConfig instead of needing a real
Junos lab.
"""
import sys
import types
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parents[3]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from pyez.mock_device import MockJunosDevice, MockConfig

_jnpr = types.ModuleType("jnpr")
_junos = types.ModuleType("jnpr.junos")
_junos.Device = MockJunosDevice
_utils = types.ModuleType("jnpr.junos.utils")
_config = types.ModuleType("jnpr.junos.utils.config")
_config.Config = MockConfig
_utils.config = _config
_junos.utils = _utils
_jnpr.junos = _junos

sys.modules["jnpr"] = _jnpr
sys.modules["jnpr.junos"] = _junos
sys.modules["jnpr.junos.utils"] = _utils
sys.modules["jnpr.junos.utils.config"] = _config
'''


def _dedup_key(row) -> tuple:
    """Two tests count as duplicates if they'd exercise the exact same
    check the exact same way: same test_type and identical protocol
    reasoning. This catches the real, observed duplication case --
    heuristic-fallback tests share ONE fixed reasoning string per
    test_type (STEPS_BY_TYPE/ASSERTION_BY_TYPE are lookup tables, not
    per-requirement reasoning, so any two heuristic tests of the same
    test_type render identical Steps/Assertion text) -- without
    over-merging genuine AI reasoning, which is unique per test in
    practice (verified across every real generation run so far)."""
    return (row["test_type"], (row["protocol_reasoning"] or "").strip())


def refresh_deduplicated_tests() -> dict:
    """Recomputes the deduplicated view from the FULL current catalog (not
    just the latest batch -- a new test can duplicate one from an older
    batch) and rewrites generated_tests/deduplicated/ from scratch each
    time. generated_tests/docs and generated_tests/pytest are never
    touched by this -- they stay the complete, unfiltered record of
    everything ever generated; this is purely an additional curated view.
    Within each duplicate group, keeps the earliest-created test."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT test_id, test_type, protocol_reasoning, doc_content, pytest_content "
        "FROM test_intents ORDER BY created_at"
    ).fetchall()
    conn.close()

    groups = defaultdict(list)
    for r in rows:
        groups[_dedup_key(r)].append(r)

    for f in list(DEDUP_DOCS_DIR.glob("*.md")) + list(DEDUP_PYTEST_DIR.glob("*.py")):
        f.unlink()

    kept = 0
    for members in groups.values():
        representative = members[0]  # earliest created_at -- rows are ordered
        (DEDUP_DOCS_DIR / f"{representative['test_id']}.md").write_text(
            representative["doc_content"], encoding="utf-8")
        (DEDUP_PYTEST_DIR / f"test_{representative['test_id']}.py").write_text(
            representative["pytest_content"], encoding="utf-8")
        kept += 1
    duplicates_ignored = len(rows) - kept

    # The *.py glob above just deleted conftest.py along with everything
    # else -- put it back so the deduplicated tests can actually be run
    # (see run_deduplicated_tests) against the mocked PyEZ layer.
    (DEDUP_PYTEST_DIR / "conftest.py").write_text(CONFTEST_CONTENT, encoding="utf-8")

    logger.info(f"Deduplication refresh: {len(rows)} total test(s), {kept} unique kept, "
                f"{duplicates_ignored} duplicate(s) ignored")
    return {"total": len(rows), "unique": kept, "duplicates_ignored": duplicates_ignored}


def run_deduplicated_tests(timeout_seconds: int = 300) -> dict:
    """Actually executes every test in generated_tests/deduplicated/pytest
    via a real `pytest` subprocess (the same interpreter/venv this Flask
    process runs under), against the mocked PyEZ layer (pyez.mock_device) --
    no real Junos lab required, and no protocol behavior is simulated
    beyond plausible default field values (see mock_device's module
    docstring for what that mock is and isn't good for). Uses pytest's
    built-in --junitxml report (no extra plugin dependency) for a
    structured, reliable per-test result instead of scraping stdout."""
    test_files = list(DEDUP_PYTEST_DIR.glob("test_*.py"))
    if not test_files:
        return {"total": 0, "passed": 0, "failed": 0, "errored": 0, "tests": [],
                "note": "No deduplicated tests to run yet -- generate some first."}

    report_path = LOGS_DIR / "test_run_report.xml"
    cmd = [sys.executable, "-m", "pytest", str(DEDUP_PYTEST_DIR),
           "-q", "--tb=short", f"--junitxml={report_path}"]
    logger.info(f"Running {len(test_files)} deduplicated test(s) against mocked PyEZ: {' '.join(cmd)}")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_seconds, cwd=str(BASE))
    except subprocess.TimeoutExpired as e:
        logger.warning(f"Test run timed out after {timeout_seconds}s")
        return {"total": len(test_files), "passed": 0, "failed": 0, "errored": len(test_files), "tests": [],
                "note": f"Test run timed out after {timeout_seconds}s", "returncode": None}

    tests = []
    passed = failed = errored = skipped = 0
    if report_path.exists():
        root = ET.parse(report_path).getroot()
        for tc in root.iter("testcase"):
            failure = tc.find("failure")
            error = tc.find("error")
            skip = tc.find("skipped")
            if failure is not None:
                outcome, message, failed = "failed", (failure.get("message") or "")[:500], failed + 1
            elif error is not None:
                outcome, message, errored = "error", (error.get("message") or "")[:500], errored + 1
            elif skip is not None:
                outcome, message, skipped = "skipped", (skip.get("message") or "")[:500], skipped + 1
            else:
                outcome, message, passed = "passed", "", passed + 1
            tests.append({
                "test_id": tc.get("classname", "") + "::" + tc.get("name", ""),
                "outcome": outcome,
                "duration": round(float(tc.get("time", 0) or 0), 3),
                "message": message,
            })

    summary = {
        "total": len(tests), "passed": passed, "failed": failed, "errored": errored, "skipped": skipped,
        "tests": tests, "returncode": proc.returncode, "stdout_tail": proc.stdout[-3000:],
    }
    logger.info(f"Test run complete: {len(tests)} test(s) -- {passed} passed, {failed} failed, "
                f"{errored} errored, {skipped} skipped")
    return summary


def generate_tests(requirement_ids: list, batch_label: str = "manual", derived_from: str = ""):
    """The single generation entry point. For each requirement, retrieves a
    small context pack (the requirement itself + semantically related ones
    from the SAME persisted knowledge base -- never raw RFC text), asks the
    AI reasoning step for a Test Intent, validates it, and renders doc +
    pytest through the deterministic templates. Falls back to a heuristic
    Test Intent if no AI backend is available or the AI call/response fails
    validation -- generation always succeeds, just with lower confidence.

    The AI-call/render step for each requirement (_generate_one) touches no
    shared state, so it runs concurrently across GENERATION_CONCURRENCY
    threads -- the only thing that matters for a bulk batch of 100+
    requirements is wall-clock time, and each call is I/O-bound (subprocess
    or network), not CPU-bound. All DB writes and file writes happen
    afterward, sequentially, on the single connection below."""
    conn = get_conn()
    batch_row = conn.execute("SELECT COALESCE(MAX(batch_id),0)+1 AS b FROM test_intents").fetchone()
    batch_id = batch_row["b"]

    rfc_row = conn.execute("SELECT rfc_number, rfc_title FROM rfc_meta WHERE id=1").fetchone()
    rfc_label = f"RFC {rfc_row['rfc_number']} ({rfc_row['rfc_title']})" if rfc_row else "RFC"
    artefact_context = get_artefact_context()
    profile = get_active_profile()

    skipped = []
    to_process = []
    for rid in requirement_ids:
        row = conn.execute("SELECT * FROM requirements WHERE requirement_id=?", (rid,)).fetchone()
        if not row:
            skipped.append(rid)
            continue
        status = conn.execute("SELECT * FROM requirement_status WHERE requirement_id=?", (rid,)).fetchone()
        if status and status["has_generated_test"]:
            skipped.append(rid)
            continue
        to_process.append(dict(row))

    logger.info(f"Batch #{batch_id} ('{batch_label}'): {len(to_process)} requirement(s) to generate, "
                f"{len(skipped)} skipped (already covered or unknown)")

    created = []
    if to_process:
        with ThreadPoolExecutor(max_workers=min(GENERATION_CONCURRENCY, len(to_process))) as pool:
            futures = [pool.submit(_generate_one, req_dict, rfc_label, artefact_context, derived_from, profile)
                       for req_dict in to_process]
            records = {r["rid"]: r for r in (f.result() for f in as_completed(futures))}

        # Write in the caller's original order, not completion order -- keeps
        # batch listings/logs deterministic regardless of thread timing.
        for req_dict in to_process:
            rec = records.get(req_dict["requirement_id"])
            if rec is None:
                skipped.append(req_dict["requirement_id"])
                continue

            (DOCS_DIR / f"{rec['test_id']}.md").write_text(rec["doc_content"], encoding="utf-8")
            (PYTEST_DIR / f"test_{rec['test_id']}.py").write_text(rec["pytest_content"], encoding="utf-8")

            conn.execute(
                """INSERT OR REPLACE INTO test_intents
                   (test_id, requirement_id, category, test_type, risk, priority, section_id, section_title,
                    statement, keyword, topology, timers, doc_content, pytest_content, derived_from, batch_id,
                    created_at, generation_mode, ai_backend, protocol_reasoning, requires_peer_emulator,
                    emulator_tool, needs_review, context_requirement_ids)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (rec["test_id"], rec["rid"], rec["category"], rec["test_type"], rec["risk"], rec["priority"],
                 rec["section_id"], rec["section_title"], rec["statement"], rec["keyword"], profile.topology_key,
                 json.dumps(rec["timers"]), rec["doc_content"], rec["pytest_content"], derived_from, batch_id,
                 datetime.datetime.now().isoformat(timespec="seconds"), rec["mode"], rec["ai_backend"],
                 rec["protocol_reasoning"], int(rec["requires_emulator"]), rec["emulator_tool"],
                 rec["needs_review"], json.dumps(rec["context_requirement_ids"])),
            )
            conn.execute(
                "UPDATE requirement_status SET has_generated_test=1, test_id=? WHERE requirement_id=?",
                (rec["test_id"], rec["rid"]),
            )
            created.append(rec["test_id"])

    active_backend = ai_generation.get_active_backend_key()
    ai_mode_note = f"AI reasoning via {active_backend}" if active_backend else "heuristic templates (no AI backend available)"
    _log(
        conn,
        f"TESTS_GENERATED (batch #{batch_id}, {len(created)} new tests, label='{batch_label}', mode={ai_mode_note})",
        "kb/knowledge.db ONLY -- raw RFC text not reopened",
    )
    conn.commit()
    conn.close()
    logger.info(f"Batch #{batch_id} complete: {len(created)} created, {len(skipped)} skipped, mode={ai_mode_note}")

    dedup_summary = refresh_deduplicated_tests()
    return {"batch_id": batch_id, "created": created, "skipped_already_covered": skipped, "dedup": dedup_summary}


# ------------------------------------------------------------------ #
# Stage C — Coverage / gap analysis (always computed live from the DB)
# ------------------------------------------------------------------ #

RECOMMENDATIONS = {
    "fsm_state": "Requires a peer emulator (ExaBGP/Scapy) capable of driving specific FSM events that a conformant Junos peer won't generate on its own.",
    "timer": "Requires precise timer manipulation and packet-capture to verify timing boundaries -- PyEZ RPC polling alone is too coarse.",
    "message_format": "Needs a packet-crafting tool (Scapy) to send intentionally malformed BGP messages.",
    "path_attribute": "Needs UPDATE messages with specific attribute combinations crafted via Scapy/ExaBGP rather than through Junos config.",
    "error_handling": "Needs fault injection via a peer emulator, plus NOTIFICATION Error Code/Subcode capture on the wire.",
    "capability_negotiation": "Needs a peer that can offer a divergent BGP version/capability set -- requires a scriptable peer emulator.",
    "decision_process": "Requires multiple candidate routes with controlled attribute variations across a 3+ router topology.",
    "update_handling": "Needs UPDATE messages with attributes deliberately out of order -- requires Scapy/ExaBGP construction.",
    "connection_management": "Requires deliberately triggering TCP collision -- needs scripted dual-connect, not just standard peering.",
    "general_conformance": "Broad conformance statements; typically covered indirectly by other category tests.",
    # OSPF-specific categories (see protocol_profiles.OSPF_PROFILE).
    "neighbor_adjacency": "Requires a peer emulator capable of driving down/malformed Hello packets that a conformant Junos peer won't generate on its own.",
    "lsa_flooding": "Needs a peer emulator (Scapy) to inject LSAs with specific sequence numbers/ages/checksums outside normal flooding.",
    "spf_calculation": "Requires a multi-router topology with controlled link costs to verify SPF tie-breaking and route installation.",
    "area_management": "Requires a multi-area topology (ABR/virtual link) to exercise area-boundary behavior.",
    "authentication": "Needs a peer emulator capable of sending mismatched/absent authentication to verify rejection.",
}


def get_coverage():
    conn = get_conn()
    total_reqs = conn.execute("SELECT * FROM requirements").fetchall()
    covered_ids = {r["requirement_id"] for r in
                   conn.execute("SELECT requirement_id FROM requirement_status WHERE has_generated_test=1")}
    conn.close()

    total = len(total_reqs)
    automatable = [r for r in total_reqs if r["testability"] == "automatable"]
    covered = [r for r in total_reqs if r["requirement_id"] in covered_ids]
    gaps_rows = [r for r in automatable if r["requirement_id"] not in covered_ids]

    overall_pct = round(100 * len(covered) / total, 1) if total else 0
    automatable_pct = round(100 * len(covered) / len(automatable), 1) if automatable else 0

    by_category = defaultdict(lambda: {"total": 0, "covered": 0})
    for r in total_reqs:
        by_category[r["category"]]["total"] += 1
        if r["requirement_id"] in covered_ids:
            by_category[r["category"]]["covered"] += 1
    category_breakdown = [
        {"category": cat, "total": c["total"], "covered": c["covered"],
         "coverage_pct": round(100 * c["covered"] / c["total"], 1) if c["total"] else 0}
        for cat, c in sorted(by_category.items(), key=lambda kv: -kv[1]["total"])
    ]

    gaps = [{
        "requirement_id": r["requirement_id"], "section_id": r["section_id"], "section_title": r["section_title"],
        "category": r["category"], "keyword": r["keyword"], "statement": r["statement"],
        "recommendation": RECOMMENDATIONS.get(r["category"], "Review manually to determine automation feasibility."),
    } for r in gaps_rows]

    # Cross-check remaining gaps against AI-reviewed existing test uploads --
    # a gap the generator hasn't closed yet may already be exercised by a
    # test the team already has. Never silently drop these from view: split
    # into "still a real gap" vs. "flagged as covered, needs human sign-off",
    # each existing_test_coverage entry carries filename + confidence + the
    # AI's rationale so a reviewer can confirm or reject it.
    existing_map = get_existing_test_coverage_map()
    true_gaps = [g for g in gaps if g["requirement_id"] not in existing_map]
    existing_test_coverage = [
        {**g, "matched_by": existing_map[g["requirement_id"]]}
        for g in gaps if g["requirement_id"] in existing_map
    ]

    return {
        "total_requirements": total,
        "automatable_requirements": len(automatable),
        "not_independently_observable": total - len(automatable),
        "tests_generated": len(covered_ids),
        "requirements_covered": len(covered),
        "overall_coverage_pct": overall_pct,
        "automatable_coverage_pct": automatable_pct,
        "category_breakdown": category_breakdown,
        "gap_count": len(gaps),
        "gaps": gaps,
        "gaps_after_existing_tests": true_gaps,
        "gap_count_after_existing_tests": len(true_gaps),
        "existing_test_coverage": existing_test_coverage,
        "existing_test_covered_gap_count": len(existing_test_coverage),
    }


def get_matrix():
    conn = get_conn()
    reqs = [dict(r) for r in conn.execute("SELECT * FROM requirements").fetchall()]
    covered_ids = {r["requirement_id"] for r in
                   conn.execute("SELECT requirement_id FROM requirement_status WHERE has_generated_test=1")}
    conn.close()
    return {"requirements": reqs, "covered_ids": list(covered_ids)}


def get_test_catalog():
    conn = get_conn()
    rows = conn.execute("SELECT test_id, requirement_id, category, test_type, risk, priority, section_id, "
                         "section_title, statement, keyword, derived_from, batch_id, created_at, updated_at, "
                         "generation_mode, ai_backend, requires_peer_emulator, emulator_tool, needs_review, "
                         "context_stale "
                         "FROM test_intents ORDER BY created_at").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_test_detail(test_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM test_intents WHERE test_id=?", (test_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_ingestion_log():
    conn = get_conn()
    rows = conn.execute("SELECT event, source, timestamp FROM ingestion_log ORDER BY id").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_rfc_meta():
    conn = get_conn()
    row = conn.execute("SELECT * FROM rfc_meta WHERE id=1").fetchone()
    conn.close()
    if not row:
        return None
    meta = dict(row)
    meta["protocol_display_name"] = protocol_profiles.get_profile(meta.get("protocol_key", "")).display_name
    return meta


def regenerate_stale_tests(batch_label: str = "context-refresh") -> dict:
    """Regenerates every test flagged context_stale (see
    _flag_context_stale_tests) through the exact same _generate_one used for
    fresh generation -- no separate rendering logic -- overwriting the same
    test_id's doc/pytest files and test_intents row in place (UPDATE, not a
    new row) so the catalog doesn't grow duplicate entries for one
    requirement. This is the "few tests modified because of new knowledge"
    half of the incremental-ingestion story: unlike newly-created tests,
    these keep their original test_id/requirement_id, just with content
    regenerated against the now-larger retrieval context.

    Same concurrency shape as generate_tests(): the AI-call/render step
    (_generate_one) touches no shared state, so it runs across
    GENERATION_CONCURRENCY threads; all DB writes and file writes happen
    afterward, sequentially, on the single connection below. Originally this
    ran one requirement at a time -- fine for the handful of tests a typical
    incremental ingest flags, but a real batch (11+ stale tests found while
    verifying the knowledge-library demo) took noticeably longer than the
    equivalently-sized "create" pass for no good reason, since neither pass
    is CPU-bound."""
    conn = get_conn()
    stale_rows = [dict(r) for r in conn.execute(
        "SELECT test_id, requirement_id, derived_from FROM test_intents WHERE context_stale=1"
    ).fetchall()]
    if not stale_rows:
        conn.close()
        return {"modified": []}

    rfc_row = conn.execute("SELECT rfc_number, rfc_title FROM rfc_meta WHERE id=1").fetchone()
    rfc_label = f"RFC {rfc_row['rfc_number']} ({rfc_row['rfc_title']})" if rfc_row else "RFC"
    artefact_context = get_artefact_context()
    profile = get_active_profile()

    to_process = []
    for row in stale_rows:
        req_row = conn.execute("SELECT * FROM requirements WHERE requirement_id=?", (row["requirement_id"],)).fetchone()
        if not req_row:
            continue
        to_process.append((dict(req_row), row["derived_from"] or ""))

    modified = []
    if to_process:
        with ThreadPoolExecutor(max_workers=min(GENERATION_CONCURRENCY, len(to_process))) as pool:
            futures = [pool.submit(_generate_one, req_dict, rfc_label, artefact_context, derived_from, profile)
                       for req_dict, derived_from in to_process]
            records = {r["rid"]: r for r in (f.result() for f in as_completed(futures))}

        # Write in the caller's original (stale-list) order, not completion
        # order -- keeps the log/return list deterministic regardless of
        # thread timing, same reasoning as generate_tests().
        for req_dict, _derived_from in to_process:
            rec = records.get(req_dict["requirement_id"])
            if rec is None:
                continue

            (DOCS_DIR / f"{rec['test_id']}.md").write_text(rec["doc_content"], encoding="utf-8")
            (PYTEST_DIR / f"test_{rec['test_id']}.py").write_text(rec["pytest_content"], encoding="utf-8")

            conn.execute(
                """UPDATE test_intents SET
                       category=?, test_type=?, risk=?, priority=?, doc_content=?, pytest_content=?,
                       generation_mode=?, ai_backend=?, protocol_reasoning=?, requires_peer_emulator=?,
                       emulator_tool=?, needs_review=?, context_requirement_ids=?, context_stale=0,
                       context_stale_reason='', updated_at=?
                   WHERE test_id=?""",
                (rec["category"], rec["test_type"], rec["risk"], rec["priority"], rec["doc_content"],
                 rec["pytest_content"], rec["mode"], rec["ai_backend"], rec["protocol_reasoning"],
                 int(rec["requires_emulator"]), rec["emulator_tool"], rec["needs_review"],
                 json.dumps(rec["context_requirement_ids"]),
                 datetime.datetime.now().isoformat(timespec="seconds"), rec["test_id"]),
            )
            modified.append(rec["test_id"])

    _log(conn, f"TESTS_MODIFIED (batch label='{batch_label}', {len(modified)} existing test(s) regenerated "
                f"due to newly ingested related knowledge)", "kb/knowledge.db ONLY -- raw RFC text not reopened")
    conn.commit()
    conn.close()
    logger.info(f"regenerate_stale_tests: {len(modified)} test(s) regenerated (batch label='{batch_label}')")

    refresh_deduplicated_tests()
    return {"modified": modified}


def generate_all_gaps(batch_label: str = "bulk-fill-all-gaps", limit: int = None):
    """Bulk-fill: generate tests for every remaining automatable gap (the
    same set the Gap Analysis tab shows), instead of the 3-per-category demo
    seed or 5-at-a-time per-category buttons. This is what actually moves
    the out-of-the-box coverage number -- the seed package only exists so
    the dashboard isn't empty on first launch; this is the "make coverage
    real" action. `limit` caps how many gaps to fill in one call (useful to
    avoid a single very long-running request); omit for all of them.

    Also regenerates any test flagged context_stale by a knowledge-library
    ingest since it was last generated (see regenerate_stale_tests) -- this
    is the single "generate tests" action a demo re-runs after ingesting
    more knowledge, and it reports both newly-created and modified tests."""
    cov = get_coverage()
    gap_ids = [g["requirement_id"] for g in cov["gaps_after_existing_tests"]]
    if limit is not None:
        gap_ids = gap_ids[:limit]
    if not gap_ids:
        result = {"batch_id": None, "created": [], "skipped_already_covered": [], "gap_count_before": 0}
    else:
        result = generate_tests(gap_ids, batch_label=batch_label)
        result["gap_count_before"] = len(gap_ids)

    stale_result = regenerate_stale_tests(batch_label=batch_label)
    result["modified"] = stale_result["modified"]
    return result


def get_ai_status():
    available = ai_generation.ai_available()
    return {
        "ai_available": available,
        "model": ai_generation.AI_MODEL if available else None,
        "backend": ai_generation.get_active_backend_key(),
        "backend_mode_requested": ai_generation.AI_BACKEND_MODE,
        "mode": "ai" if available else "heuristic",
    }


def get_batches():
    conn = get_conn()
    rows = conn.execute(
        "SELECT batch_id, COUNT(*) as n, MIN(created_at) as created_at, derived_from "
        "FROM test_intents GROUP BY batch_id ORDER BY batch_id"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]
