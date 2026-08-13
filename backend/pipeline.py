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
import json
import logging
import sqlite3
import pickle
import datetime
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
DB_PATH = KB_DIR / "knowledge.db"
RETRIEVAL_INDEX_PATH = KB_DIR / "retrieval_index.pkl"
ARTEFACTS_DIR = KB_DIR / "artefacts"
UPLOADED_TESTS_DIR = KB_DIR / "uploaded_tests"

for d in (KB_DIR, DOCS_DIR, PYTEST_DIR, ARTEFACTS_DIR, UPLOADED_TESTS_DIR):
    d.mkdir(parents=True, exist_ok=True)

SUPPORTED_UPLOAD_EXTENSIONS = (".txt", ".md", ".log", ".pdf", ".py")
COVERAGE_CANDIDATE_K = 15
MAX_TEST_CONTENT_CHARS = 8000

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
    needs_review INTEGER DEFAULT 0
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


def ingest_rfc(rfc_number: str, rfc_title: str, raw_text: str, source_label: str, protocol_override: str = ""):
    """Full re-ingestion: parse -> extract -> persist -> rebuild retrieval index.
    This is the ONLY function that reads raw RFC text. Everything else in this
    module reads from the database.

    protocol_override lets the caller force a specific protocol_profiles key
    instead of auto-detecting from rfc_number/rfc_title -- for an RFC the
    built-in table doesn't recognize (see protocol_profiles.resolve_profile).
    An unrecognized override falls back to auto-detection rather than
    silently no-op'ing."""
    profile = protocol_profiles.get_profile(protocol_override) if protocol_override else None
    if profile is None or (protocol_override and profile.key != protocol_override):
        profile = protocol_profiles.resolve_profile(rfc_number, rfc_title)

    lines = _clean_lines(raw_text)
    sections = _split_sections(lines)

    requirements = []
    counter_by_section = {}
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

    # Clear old generated test files (fresh RFC = fresh test package)
    for f in list(DOCS_DIR.glob("*.md")) + list(PYTEST_DIR.glob("*.py")):
        f.unlink()

    _build_retrieval_index()
    return {"requirement_count": len(requirements)}


def get_active_profile():
    """The protocol_profiles.ProtocolProfile for whatever RFC is currently
    ingested. Falls back to the generic profile if nothing's ingested yet
    or the stored key isn't recognized -- never raises."""
    conn = get_conn()
    row = conn.execute("SELECT protocol_key FROM rfc_meta WHERE id=1").fetchone()
    conn.close()
    return protocol_profiles.get_profile(row["protocol_key"] if row else "")


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

{assertion_hint}

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
    # Expected: {assertion_hint}
    assert {result_var} is not None, "Could not read {result_var} via PyEZ RPC"
{assertion_block}
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
        if obs["source"] == "primary":
            lines.append(f'    {obs["var_name"]} = _info.findtext("{obs["xpath"]}")')
            continue
        source_key = obs["source"]
        holder_var = holder_var_by_source.get(source_key)
        if holder_var is None:
            holder_var = f"_{source_key}"
            holder_var_by_source[source_key] = holder_var
            lines.append(f'    {holder_var} = r1.{profile.secondary_observations[source_key]}')
        lines.append(f'    {obs["var_name"]} = {holder_var}.findtext("{obs["xpath"]}")')
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

        # Retrieval context: semantically related requirements from the SAME
        # persisted knowledge base (hybrid retriever, semantic side).
        related = [r for r in semantic_search(req_dict["statement"], k=4) if r["requirement_id"] != rid][:3]

        ai_intent, mode = ai_generation.generate_ai_test_intent(rfc_label, req_dict, related, profile, artefact_context)
        ai_backend = ai_generation.get_active_backend_key() if ai_intent else ""

        if ai_intent:
            test_type = ai_intent["test_type"]
            risk = ai_intent["risk"]
            protocol_reasoning = ai_intent["protocol_reasoning"]
            steps = ai_intent["steps"]
            assertion_hint = ai_intent["assertion_hint"]
            pyez_observation = ai_intent["pyez_observation"]
            requires_emulator = bool(ai_intent["requires_peer_emulator"])
            emulator_tool = ai_intent.get("emulator_tool", "none")
            topology_note = ai_intent.get("topology_note", "")
            notes = ai_intent.get("notes", "")
            confidence = ai_intent["confidence"]
            assertion_code = ai_intent.get("assertion_code", "")
            assertion_is_safe = ai_intent.get("assertion_code_is_safe", False) and confidence == "high"
            observations = ai_intent.get("observations", [])
            needs_review = 0 if confidence == "high" else 1
        else:
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
            assertion_code = ""
            assertion_is_safe = False
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
            assertion_hint=assertion_hint, pyez_observation=pyez_observation,
            observations_doc_block=observations_doc_block,
            emulator_note=emulator_note, keyword=keyword, statement=req_dict["statement"],
            reuse_note=reuse_note, notes_block=notes_block, protocol_reasoning=protocol_reasoning,
        )

        emulator_warning = (f"\nREQUIRES PEER EMULATOR: {emulator_tool} -- Junos will not originate this "
                             f"condition on its own; this stub alone cannot exercise this requirement.\n"
                             if requires_emulator else "")
        if assertion_is_safe and assertion_code:
            assertion_block = f'    assert {assertion_code}, "AI-suggested assertion ({confidence} confidence) -- verify against lab output"'
        elif assertion_code:
            assertion_block = (f'    # AI-suggested assertion ({confidence} confidence, needs review before use):\n'
                                f'    # assert {assertion_code}\n'
                                f'    # TODO: replace with the precise assertion for this requirement')
        else:
            assertion_block = (f'    # TODO: replace with the precise assertion for this requirement\n'
                                f'    # assert {profile.result_var} == "<expected value for this protocol/requirement>"')

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
            assertion_hint=assertion_hint, stimulus_block=stimulus_block, assertion_block=assertion_block,
        )

        return {
            "rid": rid, "test_id": test_id, "category": category, "test_type": test_type, "risk": risk,
            "priority": priority, "section_id": req_dict["section_id"], "section_title": req_dict["section_title"],
            "statement": req_dict["statement"], "keyword": keyword, "timers": timers,
            "doc_content": doc_content, "pytest_content": pytest_content, "mode": mode, "ai_backend": ai_backend,
            "protocol_reasoning": protocol_reasoning, "requires_emulator": requires_emulator,
            "emulator_tool": emulator_tool, "needs_review": needs_review,
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
        }


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
                    emulator_tool, needs_review)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (rec["test_id"], rec["rid"], rec["category"], rec["test_type"], rec["risk"], rec["priority"],
                 rec["section_id"], rec["section_title"], rec["statement"], rec["keyword"], profile.topology_key,
                 json.dumps(rec["timers"]), rec["doc_content"], rec["pytest_content"], derived_from, batch_id,
                 datetime.datetime.now().isoformat(timespec="seconds"), rec["mode"], rec["ai_backend"],
                 rec["protocol_reasoning"], int(rec["requires_emulator"]), rec["emulator_tool"],
                 rec["needs_review"]),
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
    return {"batch_id": batch_id, "created": created, "skipped_already_covered": skipped}


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
                         "section_title, statement, keyword, derived_from, batch_id, created_at, "
                         "generation_mode, ai_backend, requires_peer_emulator, emulator_tool, needs_review "
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


def generate_all_gaps(batch_label: str = "bulk-fill-all-gaps", limit: int = None):
    """Bulk-fill: generate tests for every remaining automatable gap (the
    same set the Gap Analysis tab shows), instead of the 3-per-category demo
    seed or 5-at-a-time per-category buttons. This is what actually moves
    the out-of-the-box coverage number -- the seed package only exists so
    the dashboard isn't empty on first launch; this is the "make coverage
    real" action. `limit` caps how many gaps to fill in one call (useful to
    avoid a single very long-running request); omit for all of them."""
    cov = get_coverage()
    gap_ids = [g["requirement_id"] for g in cov["gaps_after_existing_tests"]]
    if limit is not None:
        gap_ids = gap_ids[:limit]
    if not gap_ids:
        return {"batch_id": None, "created": [], "skipped_already_covered": [], "gap_count_before": 0}
    result = generate_tests(gap_ids, batch_label=batch_label)
    result["gap_count_before"] = len(gap_ids)
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
