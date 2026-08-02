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

## AI integration (`ai_generation.py` + `ai_backends.py`)

- **Pluggable backend, not a hardcoded client** (`backend/ai_backends.py`):
  `AIBackend` is a two-method interface (`available()`, `complete(system,
  user) -> str`). `ClaudeCodeCLIBackend` shells out to the local Claude Code
  CLI (`claude -p --output-format json --system-prompt ...`), authenticated
  via that session's own OAuth login (`~/.claude/.credentials.json`) — no
  separate key. `AnthropicAPIBackend` is the original direct API call,
  gated on `ANTHROPIC_API_KEY`. Binary discovery order for the CLI backend:
  `CLAUDE_CLI_PATH` override → `CLAUDE_CODE_EXECPATH` (if this process was
  itself launched from inside Claude Code) → known VS Code/Cursor extension
  install globs → `claude` on PATH.
- `ai_generation._select_backend()` picks one per `AI_BACKEND` (env var:
  `auto` default = CLI first, then API key, then heuristic; or `cli`/`api`/
  `heuristic` to force a path). Cached per-process after first check, same
  pattern as the old single-client lazy singleton it replaced.
  `ai_generation.get_active_backend_key()` exposes which one is live, both
  for `/api/status` (`ai.backend`) and per-generated-test provenance
  (`test_intents.ai_backend`, migrated in via `pipeline._migrate` for DBs
  created before this column existed).
- Model: `claude-opus-4-8` by default, overridable via `AI_MODEL` — applies
  to whichever backend answers (CLI accepts the same aliases/full names).
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

## Protocol-agnostic architecture (`protocol_profiles.py`)

The 6 BGP-coupling points originally identified here (category rules, the
two templates, the heuristic-fallback observation/emulator defaults, the AI
system prompt, and header copy) have all been resolved via a
`ProtocolProfile` abstraction:

- **`protocol_profiles.py`** defines one `ProtocolProfile` per protocol:
  `category_rules` (tried before `COMMON_CATEGORY_RULES` — timer/
  message_format/error_handling/capability_negotiation, shared by every
  profile), `topology_key`/`topology_description`, `timer_fields` + a
  per-category override (e.g. BGP shortens `hold` for timer-category tests),
  a `config_template` (Junos config text, rendered via `string.Template`
  — see the note below on why not `str.format()`), `observation_call`/
  `observation_field`/`result_var` (the RPC the rendered pytest stub
  actually executes), `default_emulator_tool`, and `expertise_note` (feeds
  the AI system prompt). `bgp` is extracted verbatim from the original
  hardcoded values; `ospf` (RFC 2328) is the second profile; `generic` is
  the fallback for anything unrecognized.
- **`protocol_profiles.resolve_profile(rfc_number, rfc_title)`** — RFC-number
  match first, then a title-keyword scan, then generic. Called by
  `ingest_rfc()`, which stores the result on `rfc_meta.protocol_key`.
  `pipeline.get_active_profile()` reads it back (falling back to generic if
  unset/unrecognized) for every read/generate path.
- **`pipeline._generate_one`** takes the active profile and uses it for
  every template field that used to be a literal BGP string. **Important
  safety-boundary detail:** the AI's own `pyez_observation` suggestion stays
  advisory documentation only (shown in a comment) — the RPC call the
  rendered pytest stub actually executes always comes from the trusted
  profile, never from AI output. Same pattern as `assertion_code`'s
  AST-safety-checked promotion.
- **`ai_generation.SYSTEM_PROMPT_TEMPLATE` / `COVERAGE_SYSTEM_PROMPT_TEMPLATE`**
  take the profile's topology/expertise/emulator-tool details. Verified
  this isn't just label-deep: prompted with the OSPF profile, the model's
  `protocol_reasoning` for a real OSPF requirement referenced Type-1
  router-LSAs, Router-IDs, and the LSDB specifically.
- **Why `string.Template` (`$name`), not `str.format()`, for config stanzas
  and system prompts:** Junos config text and the JSON schema examples in
  the system prompt are both dense with literal `{`/`}` characters that
  collide with `str.format()`'s field-delimiter syntax (confirmed this
  breaks with a direct repro before picking the fix) — `Template` only
  cares about `$`, so the literal braces need no escaping.
- **Frontend**: `renderHeader()` reads `meta.protocol_display_name` from
  `/api/status` instead of a hardcoded "BGP proof of concept" string; the
  ingest form has an optional protocol-override select.

A migration bug is worth remembering for future schema changes: adding
`rfc_meta.protocol_key` required both an `ALTER TABLE` (DDL) and a backfill
`UPDATE` (DML) in `_migrate()`. `get_conn()` never commits, and several
read-only call sites only `SELECT` + close — so the backfill silently rolled
back the first time, and because the column already existed, the `if column
not in cols` guard meant it would never retry. Fixed by having `_migrate()`
commit its own changes before returning. Any future migration that both
alters schema *and* backfills data needs to do the same — don't rely on the
caller to commit.

## A confirmed extraction-recall gap (under-segmentation, not loss)

Audited during the coverage-expansion work (see `todo.md` item 2): a naive
whole-document keyword-sentence recount (bypassing section boundaries)
matched the DB's extracted count exactly for RFC 4271 (203=203), so section
splitting isn't dropping text at section edges. But `_split_into_statements`
(pipeline.py) only splits on `". " + capital letter`, so RFC-style lettered/
numbered sub-lists under one lead-in sentence get merged into a single
oversized "requirement" instead of the several independently-testable
statements they contain — e.g. §5.1.2's AS_PATH modification rules (a
`SHALL NOT` for internal peers, plus several distinct external-peer
`SHOULD`/conditional rules enumerated as `a)`/`b)`/`1)`/`2)`/`3)`) all
collapse into one `requirements` row. Nothing is silently missing, but true
requirement count is undercounted and some extracted statements are harder
to test precisely than they should be. Not fixed yet — see `todo.md` for
why (fixing the splitter and re-ingesting would renumber every requirement
ID and orphan already-generated tests, since `ingest_rfc()` wipes
`test_intents` on every re-ingest).

## Bulk generation and coverage (resolved — see `todo.md` item 2)

Wasn't a code defect — the machinery to generate more tests already existed
(`/api/generate-by-category`, matrix drill-down "generate"); the gap was
demo/default state (28 tests, ~14% coverage out of the box) and the fact
that `generate_tests`'s per-requirement loop was fully sequential, making a
196-requirement bulk run impractically slow.

- **`pipeline.generate_all_gaps()` / `POST /api/generate-all`** — bulk-fills
  every remaining automatable gap (optionally capped via `{limit}`) in one
  call; a "Generate all remaining gaps" button in the Gap Analysis tab
  drives it from the UI.
- **Concurrency**: `generate_tests` splits into two phases —
  `_generate_one` (the AI-call + template-render step, touches no shared
  state) runs across a `ThreadPoolExecutor` (`AI_GENERATION_CONCURRENCY` env
  var, default 4); all DB writes and file writes happen afterward,
  sequentially, on the single connection. Real 40-item batch: ~5.3 minutes
  wall time via the CLI backend, all 40 succeeded.
- **Concurrency bug found and fixed**: `ai_generation._select_backend()`'s
  memoization wasn't thread-safe — see the "AI integration" section above.
- **Generation-quality panel** (Overview tab, `app.js:renderGenQuality`,
  computed client-side from the catalog): total/high-confidence/needs-review/
  emulator-required/heuristic-fallback counts, so bulk volume is visibly
  checked against confidence, not just a bigger headline number.
- **`backend/verify_generated_tests.py`** — `ast.parse`s every generated
  pytest stub as a cheap post-generation smoke test.
- Current state after this pass: **77 tests generated, 39.3% automatable
  coverage** (up from 28 tests / ~14%), 119 gaps remaining — bulk-filling
  the rest is a deliberate on-demand action before a demo, not part of
  every fresh install's bootstrap (kept the existing fast-startup seed).

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
