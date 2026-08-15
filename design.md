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
- `_safe_assertion_expr(code, known_names)` is a narrow AST allowlist (no
  imports, no calls beyond a handful of safe attribute methods, **and every
  `ast.Name` reference must be in `known_names`**) that decides whether a
  suggested assertion gets promoted to an executable `assert` line or stays a
  commented TODO. `known_names` is `{profile.result_var}` plus any variable
  the model declared via the `observations` schema field (see below) —
  promotion is gated on safety only, **not confidence**; a safe assertion
  from a low-confidence Test Intent still gets promoted, it just also keeps
  `needs_review=1` so a human double-checks the reasoning behind it
  independently of whether it's guaranteed to run without crashing.
- **`observations` schema field (Test Intent):** the model can declare up to
  3 named data fetches — `{var_name, source, xpath}` — instead of only ever
  getting the one value auto-fetched via the profile's `observation_call`/
  `observation_field` (`profile.result_var`). `source` is either `"primary"`
  (a different XPath into the same already-fetched response) or a key from
  `profile.secondary_observations` (a small trusted per-protocol menu of
  *additional* RPC calls, e.g. BGP's `route_table` →
  `get_route_information(table='inet.0')`, OSPF's `lsdb` →
  `get_ospf_database_information()`). The model only ever picks a menu key
  and supplies data (a variable name, an XPath string) — `pipeline.py`
  renders the actual fetch code from the trusted profile, never from AI
  text. This exists because the single generic `result_var` is usually too
  shallow to test a specific requirement (e.g. AS_PATH content, an LSA
  field) — before this, the model would invent a plausible-sounding
  variable name for data it had no real way to fetch, and that assertion,
  if promoted, raised `NameError` the moment anyone ran it. Verified fix by
  regenerating all 33 then-existing tests: 36 real `assert` statements
  across 33 files, zero referencing an undefined name.
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
test's Markdown doc in the detail modal. Overview tab also has the
Generation Quality panel (confidence/needs-review/emulator-required
distribution, computed client-side from the already-loaded catalog); Test
Catalog tab has the "Run deduplicated tests" button + results table
(`renderRunTestsResult`) that triggers `POST /api/tests/run` and shows
pass/fail/error counts and per-test outcome/message.

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

A second, more serious bug from the same refactor was caught later, only by
literally running `demo_1.md` end to end (see the demo-validation note in
`todo.md`): the edit that added `get_active_profile()` inserted it in the
middle of `ingest_rfc()`'s body, right after `conn.close()` — splitting off
`ingest_rfc()`'s tail (deleting stale generated test files from the
previous RFC, rebuilding the TF-IDF retrieval index, and the
`return {"requirement_count": ...}` the bootstrap code depends on) as dead,
unreachable code sitting after `get_active_profile()`'s own `return`. The
function still parsed and ran without error — it just silently returned
`None`, never rebuilt the retrieval index, and never cleared old generated
files on re-ingest. This crashed the app's first-run bootstrap outright
(`app.py` unpacks `res['requirement_count']`) and would have silently
broken semantic-search grounding and left stale files behind on every
other re-ingest. Caught and fixed by moving the tail back to the end of
`ingest_rfc()`, where it belongs. Static checks (`ast.parse`, syntax
validation) never catch this class of bug — only actually running the
code path exposes it, which is exactly why `demo_1.md` got run for real
rather than just read for plausibility.

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

## Deduplication (`pipeline.refresh_deduplicated_tests`)

Real duplicates were found in the catalog: 29 of the first 77 generated
tests were `heuristic-fallback` (generated before any AI backend existed —
timestamped at the very first bootstrap), and the heuristic path renders
Steps/Assertion text from a fixed 5-entry lookup table
(`STEPS_BY_TYPE`/`ASSERTION_BY_TYPE`, keyed only by `test_type`) rather than
per-requirement reasoning — so any two heuristic tests of the same
`test_type` are byte-identical apart from the requirement ID/statement.

- **`generated_tests/docs` and `generated_tests/pytest` are untouched** —
  they stay the complete, unfiltered record of everything ever generated.
- **`generated_tests/deduplicated/{docs,pytest}`** is a new, additional
  view: `refresh_deduplicated_tests()` (called automatically at the end of
  every `generate_tests()` batch) recomputes it from the *entire* current
  `test_intents` table — not just the latest batch, since a new test can
  duplicate one from an older batch — clears and rewrites both directories
  from scratch each time.
- **`_dedup_key(row) = (test_type, protocol_reasoning.strip())`.** Two tests
  are duplicates if both match. For heuristic tests, `protocol_reasoning`
  is the same fixed string for every heuristic test regardless of
  `test_type`, so `test_type` is what actually separates the 5 real
  duplicate groups; for genuine AI reasoning, `protocol_reasoning` is
  unique per test in practice (verified across every real generation run
  this session), so this key doesn't over-merge real content. Within a
  duplicate group, the earliest-created test is kept as the
  representative.
- Verified with a synthetic duplicate pair injected directly into
  `test_intents` (same `test_type`/`protocol_reasoning`, different
  `requirement_id`): correctly collapsed to 1, `duplicates_ignored: 1`;
  cleaned up after. Real generation runs since then have shown 0 false
  merges among genuine AI content.

## Process log (`backend/logs/generation.log`)

A real file on disk, separate from the in-DB `ingestion_log` table the
dashboard reads. `pipeline._setup_process_log_file()` attaches a
`FileHandler` to the `pipeline` and `ai_generation` loggers **by name**
(not the root logger), so Flask/werkzeug's own request logging isn't
pulled in. Records, at INFO level: every RFC ingest, every generated
test's outcome — explicitly **which backend answered or that it fell back
to heuristic** (`"{rid}: generated via AI backend '{backend}' (mode=...)"`
vs. `"{rid}: AI unavailable/failed ({mode}) -- used heuristic fallback"`) —
batch start/end summaries, and dedup refresh results. Gitignored like
other runtime output (`backend/logs/`).

## Running the tests (`pyez/mock_device.py`, `pipeline.run_deduplicated_tests`)

Closes the loop from "generate a test" to "prove it actually runs,"
without a real vJunos-router/vMX lab:

- **`backend/pyez/mock_device.py`** — `MockJunosDevice` (constructor
  signature matches `jnpr.junos.Device`; `.open()`/`.close()` are no-ops,
  `.rpc` dynamically returns a `MockRpcResponse` for any attribute
  access, mirroring PyEZ's per-RPC-tag method pattern) and `MockConfig`
  (context manager; `.load()`/`.commit()` record but don't apply
  anything). `MockRpcResponse.findtext(xpath)` pattern-matches the XPath
  string (`peer-state`→`"Established"`, `neighbor-state`→`"Full"`,
  `as-path`→`"65001"`, `sequence-number`→`"0x80000001"`, etc.), falling
  back to a generic placeholder — a demo/smoke-test double, explicitly
  **not** a protocol simulator; it can't fabricate a real negative-path
  outcome for a test that actually needs a peer emulator to construct its
  stimulus.
- **Injection mechanism**: `refresh_deduplicated_tests()` writes a
  `conftest.py` into `generated_tests/deduplicated/pytest/` on every
  refresh (its `*.py`-glob cleanup would otherwise delete it, so it can't
  be a plain static file living there — `pipeline.CONFTEST_CONTENT` is
  rewritten alongside the tests each time). That `conftest.py` shims
  `sys.modules["jnpr.junos"]`/`sys.modules["jnpr.junos.utils.config"]`
  with the mock classes *before* pytest imports the generated test files
  (conftest.py always loads first for its directory) — PyEZ isn't a real
  project dependency, so there's nothing genuine to conflict with.
- **`pipeline.run_deduplicated_tests()`** shells out to a real `pytest`
  subprocess (`sys.executable -m pytest ... --junitxml=...`) against
  `generated_tests/deduplicated/pytest` — a genuine test run, not a
  simulation of one. Parses pytest's own built-in JUnit XML report (no
  extra plugin dependency) into a structured pass/fail/error summary with
  per-test duration and failure message. New dependency: `pytest>=8.0.0`
  (`backend/requirements.txt`) — the app itself never imported it before.
- **API**: `POST /api/tests/run`. **UI**: "Run deduplicated tests" button +
  results table in the Test Catalog tab (`app.js:renderRunTestsResult`).
- **Verified live, twice** (direct call and real HTTP): 39 tests, 36
  passed, 0 errored — 0 errored is the important number, since a broken
  mock injection would show up as 39 import errors, not partial failures.
  The 3 failures were inspected and are legitimate, not bugs: one expects
  a route to be genuinely absent (the generic mock always returns a
  placeholder instead), one expects a rejected/non-Established outcome
  from a test that actually needs a peer emulator to construct its real
  stimulus, one expects an exact literal IP the mock was never told about.
  This is stated plainly in the UI description and README, not hidden.

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
AI-reviewed coverage mapping, RFC re-ingestion, deduplication, and now
**actual test execution** — `generated_tests/deduplicated/pytest` runs for
real via `pytest` against a mocked PyEZ layer, no lab required.

Stubbed: generated pytest files use placeholder lab host/credentials for
*real* execution — wiring to an actual vJunos-router/vMX lab still means
swapping the mock back out for real PyEZ/device credentials; negative/
boundary/malformed-message tests still need a peer emulator (ExaBGP/Scapy)
that this tool identifies the need for but doesn't generate, and the mock
runner can't fake that outcome either (see the "Running the tests" section
above — those specific tests may pass or fail against the mock without
that meaning anything about real conformance). Confidence still drives
`needs_review` independently of whether an assertion was safe enough to
promote to executable — a promoted, passing-against-the-mock assertion
from a low-confidence Test Intent is still flagged for a human to check
the reasoning behind it.
