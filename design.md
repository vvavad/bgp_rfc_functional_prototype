# Design Notes — RFC Conformance Test Generation Prototype

Snapshot of how this codebase currently works, written to orient future changes
(protocol-agnostic refactor, coverage expansion, key-less AI backend, demo
readiness). Source of truth is the code itself — treat this as a map, not a
spec; re-derive details from `backend/pipeline.py` / `backend/ai_generation.py`
before relying on anything below that looks stale.

## What it is

A Flask + SQLite + vanilla-JS proof of concept that turns an RFC's normative
text into a traceable requirement→test pipeline for Juniper vJunos-router/vMX,
with Claude doing the "what test proves this requirement" reasoning. Currently
hard-wired to BGP (RFC 4271) end to end — requirement classification,
generation templates, and the AI system prompt all assume BGP.

## Runtime shape

```
frontend/ (no build step, static files served by Flask)
  index.html, style.css, app.js  — fetch()-based SPA, 5 tabs (Overview,
  Coverage Matrix, Test Catalog, Gap Analysis, Knowledge Base)

backend/
  app.py            Flask routes only — thin, delegates everything to pipeline.py
  pipeline.py        the actual engine: RFC parsing, requirement extraction,
                      SQLite persistence, TF-IDF retrieval, test generation
                      orchestration, coverage/gap computation, artefact +
                      existing-test upload handling
  ai_generation.py    Claude client, system prompts, JSON schema validation,
                      the assertion-code safety allowlist
  kb/knowledge.db     SQLite — single source of truth after first ingest
  kb/retrieval_index.pkl   pickled TF-IDF vectorizer + matrix
  kb/rfc4271_raw.txt  bundled seed RFC text (read once, by app.py's bootstrap,
                      never again)
  generated_tests/{docs,pytest}/   rendered Markdown + pytest/PyEZ output
```

Boot sequence (`app.py __main__`): if `requirements` table is empty, ingest
the bundled RFC 4271 text, then generate a stratified seed package (up to 3
tests per category) so the dashboard has something to show on first run.
Every subsequent read/generate call only touches `kb/knowledge.db` — this
"parse once, reuse forever" property is treated as structural, not incidental
(see the comments throughout `pipeline.py`).

## Data model (`pipeline.SCHEMA`)

- `rfc_meta` — single-row table: which RFC is currently loaded.
- `requirements` — one row per extracted normative sentence: `requirement_id`
  (`RFC{n}-S{section}-REQ-{nn}`), section id/title, RFC-2119 `keyword`,
  `statement`, a heuristically assigned `category`, and `testability`
  (`automatable` vs `not_independently_observable`).
- `requirement_status` — whether a requirement has a generated test yet
  (`has_generated_test`, `test_id`).
- `test_intents` — the generated test catalog: everything about a test
  (type/risk/priority, rendered doc + pytest text, `generation_mode` —
  `ai-high`/`ai-medium`/`ai-low`/`heuristic-fallback:<reason>` —
  `needs_review`, `requires_peer_emulator`, `emulator_tool`).
- `ingestion_log` — append-only activity feed (drives the Knowledge Base tab
  timeline).
- `artefacts` — uploaded product specs / reference docs, extracted text
  cached, used only as extra AI grounding (never touches requirement
  extraction).
- `uploaded_tests` / `uploaded_test_requirement_map` — a team's existing test
  suite, uploaded and AI-reviewed against candidate requirements so real gaps
  can be told apart from already-covered ones.

## Pipeline stages

**Stage A — Ingest** (`ingest_rfc`, the *only* function that reads raw RFC
text): clean page headers → split into numbered sections (`SECTION_RE`) →
split section bodies into sentences containing an RFC-2119 keyword
(`_split_into_statements`) → classify `category` via keyword/regex rules
(`CATEGORY_RULES`) → classify `testability` via a small denylist of
"implementation-defined" phrasings (`NOT_OBSERVABLE_HINTS`) → persist →
rebuild the TF-IDF retrieval index (`_build_retrieval_index`).

**Stage B — Generate** (`generate_tests`): for each requested requirement id,
pull the 3 nearest neighbors from the same TF-IDF index as retrieval context,
call `ai_generation.generate_ai_test_intent` for a structured Test Intent
(never free-form code — see below), fall back to a static heuristic
(`STEPS_BY_TYPE`/`ASSERTION_BY_TYPE`) if no key/response fails validation,
then render both a Markdown doc and a pytest/PyEZ stub through fixed string
templates (`DOC_TEMPLATE`, `PYTEST_TEMPLATE`).

**Stage C — Coverage** (`get_coverage`, `get_matrix`): always computed live
from the DB — total vs. covered vs. gaps, broken down by category, cross-
checked against AI-reviewed existing-test uploads so a gap already covered by
a team's real test isn't double-counted.

## AI integration (`ai_generation.py`)

- Single lazy `Anthropic()` client, gated entirely on `ANTHROPIC_API_KEY`
  being present in the environment (loaded from `backend/.env` via
  `python-dotenv`, read at **import time** — hence `app.py` calls
  `load_dotenv()` before importing `pipeline`).
- Model: `claude-opus-4-8` by default, overridable via `AI_MODEL`.
- **Design rule that matters:** the model never writes Python. It returns a
  schema-validated JSON "Test Intent" (test type, risk, protocol reasoning,
  steps, assertion hint, PyEZ observation point, a candidate `assertion_code`
  expression). `pipeline.py`'s string templates are the only thing that
  produce doc/pytest text — this keeps "AI reasoning" and "executed code"
  separated by a validated boundary.
- `_safe_assertion_expr` is a narrow AST allowlist (no imports, no calls
  beyond a handful of safe attribute methods) that decides whether a
  suggested assertion gets promoted to an executable `assert` line or stays a
  commented TODO. Promotion also requires `confidence == "high"`.
- Same pattern reused for existing-test coverage review
  (`analyze_existing_test_coverage`): candidates come from the TF-IDF index,
  the model can only tag requirement IDs it was actually offered, response is
  schema-validated, heuristic keyword-overlap fallback if no key.
- **Every AI path has a heuristic fallback** — the app never hard-fails on
  missing/invalid AI output, it just labels the result lower-confidence and
  visibly flags it (`needs_review`, `heuristic-fallback:<reason>` in the
  catalog and doc text, an amber "Heuristic mode" badge in the header).

## Frontend (`frontend/app.js`)

No framework, no build step — plain `fetch()` against the `/api/*` routes
listed in `README.md`. `boot()` loads `/api/status` then `refreshAll()` pulls
coverage/matrix/catalog/ingestion-log/artefacts/existing-tests in parallel and
re-renders every panel. Chart.js for the two Overview charts (category
stacked bar, test-type donut), `marked` + `DOMPurify` to render a generated
test's Markdown doc in the detail modal.

## Where BGP is hard-coded (relevant to "protocol agnostic" work)

These are the concrete coupling points — anything doing protocol-agnostic
work has to touch all of them, not just the obvious one:

1. **`pipeline.CATEGORY_RULES`** (pipeline.py:399) — regex categories are BGP
   vocabulary: `FSM`, `OpenSent`/`OpenConfirm`, path-attribute names
   (`ORIGIN`, `AS_PATH`, `NEXT_HOP`...), "UPDATE Message", etc. A different
   protocol's RFC (OSPF, IS-IS) would mostly fall into `general_conformance`.
2. **`pipeline.DOC_TEMPLATE` / `PYTEST_TEMPLATE`** (pipeline.py:573-698) —
   hard-codes "two-router-ebgp" topology, `AS 65001 ↔ AS 65002`, and a PyEZ
   config stanza that's literally `protocols { bgp { group EBGP-PEER ... } }`.
   Every generated test gets this regardless of what's actually being tested.
3. **`pipeline.generate_tests`'s default PyEZ observation/stimulus** — the
   heuristic fallback path hardcodes
   `rpc.get_bgp_neighbor_information() -> .//peer-state` and a BGP `bgp_info`
   RPC call in `PYTEST_TEMPLATE`'s body, independent of the AI path.
4. **`ai_generation.SYSTEM_PROMPT`** (ai_generation.py:68) — explicitly
   frames the model as a "BGP/OSPF/IS-IS/... test architect" but then anchors
   every example and default topology on BGP/eBGP AS numbers; there's no
   protocol parameter threaded through the prompt.
5. **`ingest_rfc`'s requirement id format** (`RFC{n}-S{section}-REQ-{nn}`) is
   protocol-neutral already — this one's fine as-is.
6. **Frontend copy** — header/subhead text in `index.html`/`app.js`
   (`renderHeader`) says "BGP proof of concept" regardless of what RFC is
   actually loaded; would mislead once a non-BGP RFC is ingested.

## Where the "increase test coverage" ask actually bites

Not a code defect — the machinery to generate more tests already exists
(`/api/generate-by-category`, matrix drill-down "generate"). The gap is
demo/default state: as of this writing the bundled RFC 4271 ingest extracts
**203 requirements** (196 automatable) but the bootstrap seed only generates
**28 tests** (3 per category, for the demo), i.e. ~14% overall coverage /
~14% automatable coverage out of the box. "Increase coverage" is really
"generate (and validate) a much larger share of the 196 automatable
requirements," which is bounded by AI call volume/cost/time, not by any
missing capability.

## Key management today

`ANTHROPIC_API_KEY` lives in `backend/.env` (gitignored per the README's
instruction, `.env.example` is the committed template), loaded via
`python-dotenv` at process start. No key → heuristic mode, clearly badged.
This is the thing item 3 of the plan (see `todo.md`) wants to change: running
inside a VS Code/Claude Code environment that's already authenticated
shouldn't require provisioning and storing a second, separate Anthropic API
key for this POC to demo AI reasoning.

## What's real vs. stubbed (carried from README, still accurate)

Real: RFC→requirement extraction, persistent KB, TF-IDF retrieval, AI Test
Intent generation with schema validation + safety-checked assertions, live
generation from the UI, coverage/gap computation, existing-test upload +
AI-reviewed coverage mapping, RFC re-ingestion.

Stubbed: generated pytest files use placeholder lab host/credentials (no real
lab wired up); negative/boundary/malformed-message tests still need a peer
emulator (ExaBGP/Scapy) that this tool identifies the need for but doesn't
generate; anything below "high" AI confidence is explicitly marked
`needs_review`, not silently trusted.
