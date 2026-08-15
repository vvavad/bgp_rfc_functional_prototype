# RFC Conformance Test Generation — Functional Prototype

BGP (RFC 4271) proof of concept for Juniper vJunos-router/vMX — the flagship,
most-tested path. Python (Flask) backend, lightweight HTML/CSS/JS frontend,
SQLite-backed knowledge base, AI-powered test reasoning.

The requirement-classification rules, default topology, Junos config
template, and PyEZ observation point are no longer hardcoded to BGP —
they come from a per-protocol profile (`backend/protocol_profiles.py`),
resolved automatically from the ingested RFC's number/title (with a manual
override on the ingest form/API if needed). BGP and OSPF (RFC 2328) both
have dedicated profiles today; an RFC for any other protocol falls back to
a generic profile (protocol-neutral categories only, clearly-marked
placeholder templates) rather than silently assuming BGP.

## Run it

```bash
cd backend
pip install -r requirements.txt
cp .env.example .env    # defaults to AI_BACKEND=auto -- see below
python app.py
```

`backend/.env` is loaded automatically on every startup (via `python-dotenv`) —
no shell `export` needed. **Running inside a Claude Code session, AI-reasoned
generation works with no key at all**: `AI_BACKEND=auto` (the default) detects
the local Claude Code CLI and uses its existing login. Running standalone
(outside Claude Code), set `ANTHROPIC_API_KEY` in `.env` instead. Neither
configured → heuristic mode. See `.env.example` for the full list of
variables and `AI_BACKEND`'s other values (`cli` / `api` / `heuristic`) if you
want to force a specific path instead of auto-detecting.

Open **http://localhost:5000**

First run seeds the knowledge base from the bundled `kb/rfc4271_raw.txt`
(RFC 4271 canonical text) and generates a starter package of ~28 tests spread
across all requirement categories. This takes a few seconds on first launch
only — every run after that loads instantly from `kb/knowledge.db`.

## AI-powered test generation

Test generation is no longer a static lookup table. Each requirement is sent,
with retrieved context, to Claude for actual protocol reasoning before any
doc/pytest text is produced.

- **Backend, not a hardcoded API client (`backend/ai_backends.py`):** two
  interchangeable backends implement the same `complete()` call —
  `ClaudeCodeCLIBackend` shells out to the local Claude Code CLI in
  non-interactive print mode (`claude -p --output-format json`), authenticated
  via that session's own login, no key required; `AnthropicAPIBackend` is the
  original direct API call, gated on `ANTHROPIC_API_KEY`. `AI_BACKEND=auto`
  (default) prefers the CLI, falls back to the API key, falls back to
  heuristic — see `.env.example`. Every generated test's catalog entry
  records which one actually answered (`ai_backend` field), and the header/
  catalog badges reflect it too.
- **Model:** `claude-opus-4-8` by default (set via the `AI_MODEL` env var if
  you want to trade quality for cost/speed, e.g. `claude-sonnet-5`) — applies
  to whichever backend is active.
- **Grounding, not a bare prompt:** the model is given the target requirement
  plus 3 semantically related requirements retrieved from the same persisted
  knowledge base (`/api/search`'s TF-IDF index) — real hybrid retrieval, the
  same one power the dashboard's search box, not a separate mechanism
- **Product specs as extra grounding:** upload product specs or other
  reference material (.txt/.md/.pdf) from the Knowledge Base tab — their
  extracted text is appended to the prompt so the model can reason about
  what the *actual* target product supports, without overriding the RFC
  statement itself (see `pipeline.get_artefact_context`)
- **Domain framing:** the system prompt establishes deep, specific expertise
  in BGP/OSPF/IS-IS/MPLS/EVPN, Junos OS, PyEZ, YANG/NETCONF — and explicitly
  instructs the model to reason about what a *conformant Junos device* can
  and cannot originate, which is exactly the gap the earlier heuristic
  version couldn't reason about at all
- **Test Intent, not free-form code:** the model returns a structured JSON
  object (test type, protocol reasoning, steps, exact PyEZ observation point,
  a candidate assertion) — it never writes Python directly. The same
  deterministic template renderer from before turns that JSON into doc +
  pytest text, so nothing ships that skipped the "compiler" step
- **Safety-gated assertion promotion:** any assertion the model suggests gets
  promoted into an executable `assert` line as long as it passes a static
  safety check — no imports, no arbitrary function calls, and every variable
  name it references is guaranteed to actually exist in the rendered test
  (either the one value fetched by default, or an additional named
  observation the model explicitly declared and pipeline.py wired up —
  see `backend/protocol_profiles.py`'s `secondary_observations`). Promotion
  no longer depends on the model's self-reported confidence — a real,
  runnable check beats none — but confidence still independently flags the
  test `needs_review` in the catalog, so a low-confidence assertion still
  gets a second look even though it's guaranteed not to crash
- **Graceful fallback:** no backend available (neither CLI nor API key), or a
  failed/malformed response from whichever one answered → generation still
  succeeds, just via the original heuristic templates, clearly labeled
  `heuristic-fallback:<reason>` in the catalog and in the doc file itself.
  The app never blocks on AI availability.

With no backend available, the header shows an amber **"Heuristic mode"**
badge and every generated test is watermarked accordingly — no silent
degradation.

## Gap analysis against your existing test suite

The Gap Analysis tab isn't limited to tests generated by this tool. Upload your
team's existing tests (pytest source, documented test cases, exported test-case
lists — `.py`/`.txt`/`.md`/`.pdf`) and the AI reviews each one against:

- the RFC requirements (via the same persisted TF-IDF retrieval used everywhere
  else — a semantic search over the full requirement corpus narrows each
  uploaded test down to a candidate shortlist before the AI judges it, so the
  prompt stays bounded regardless of RFC size)
- any uploaded product specs (see previous section) for grounding on what the
  real target device supports
- what this tool has already generated (trivially true: generated-test coverage
  is already excluded from the gap list)

For each candidate requirement, the model decides whether the test *actually
verifies* it — real assertions/observation points, not just topical overlap —
and returns a confidence + one-line rationale. Same non-hallucinated-JSON
discipline as Test Intent generation: the model can only tag requirement IDs
it was explicitly offered as candidates.

Matched requirements are removed from the "real gaps" list but always surfaced
separately, with the matching filename, confidence, and rationale — nothing is
silently hidden or auto-trusted; the whole point is a reviewable signal, not a
black box. No `ANTHROPIC_API_KEY`? Same graceful fallback as generation: a
crude keyword-overlap heuristic runs instead, always at `low` confidence so
it's visibly a guess.

## Deduplication

Every generated test still lands in `generated_tests/{docs,pytest}` — the
full, unfiltered record of everything ever generated. After every generation
batch, `pipeline.refresh_deduplicated_tests()` also rebuilds
`generated_tests/deduplicated/{docs,pytest}`: a curated view with duplicate
tests collapsed to one representative each. Two tests count as duplicates
when they share the same `test_type` *and* identical `protocol_reasoning` —
this is exactly the real duplication pattern this catches: the heuristic-
fallback path renders Steps/Assertion text from a fixed 5-entry lookup table
(`STEPS_BY_TYPE`/`ASSERTION_BY_TYPE`), so any two heuristic tests of the same
`test_type` are byte-identical apart from the requirement they trace to.
Genuine AI reasoning is unique per test in practice, so this doesn't
over-merge real content — verified with both real generation runs and a
synthetic duplicate pair.

## Running the tests (mocked PyEZ)

The Test Catalog tab's **"Run deduplicated tests"** button (`POST
/api/tests/run`) actually executes `generated_tests/deduplicated/pytest` via
a real `pytest` subprocess — not a simulation. `backend/pyez/mock_device.py`
provides `MockJunosDevice`/`MockConfig`, shimmed in for `jnpr.junos.Device`/
`jnpr.junos.utils.config.Config` by a `conftest.py` written alongside the
deduplicated tests on every refresh (PyEZ isn't a real project dependency, so
there's nothing genuine for this to conflict with). The mock returns
plausible default field values for common patterns (session
Established/Full, a placeholder AS number, a placeholder sequence number...)
rather than simulating real protocol behavior, so it's good for proving the
generate → dedupe → run pipeline works end to end and for exercising simple
state-check assertions meaningfully — but a test that needs a peer emulator
to construct its real stimulus (`requires_peer_emulator` in the catalog) may
pass or fail against this mock without that meaning anything about real
conformance. Results (pass/fail/error counts, per-test outcome and failure
message, parsed from pytest's own `--junitxml` report) show directly in the
dashboard; the same run is recorded in `backend/logs/generation.log`.

## What's real vs. stubbed

**Real, working, and live:**
- RFC text → RFC 2119 requirement extraction (regex-based, section-indexed)
- Persistent SQLite knowledge base (`kb/knowledge.db`) — survives restarts
- TF-IDF semantic retrieval over the requirement corpus (`/api/search`), also
  used internally to build AI generation context
- AI-reasoned Test Intent generation (see above), with schema validation and
  a safety-checked path from suggested assertion → executable code
- Live test generation from the UI: pick a category or a matrix cell, click
  Generate, it calls the model and writes real files right now
- Coverage and gap analysis, always computed live from current DB state
- Existing-test coverage review: upload a real test file, the AI maps it to
  the RFC requirements it actually verifies, and the gap list updates live
- RFC re-ingestion: paste a different RFC's text and rebuild from scratch
- Deduplication (`generated_tests/deduplicated/`) and actual test
  **execution** — the deduplicated catalog runs for real via `pytest`
  against a mocked PyEZ layer, no lab required (see "Running the tests"
  above)

**Deliberately stubbed (needs a real lab to close):**
- Generated pytest files use real PyEZ call patterns, and now genuinely
  *run* against the mock — but host/credentials are still placeholders for
  a *real* lab, and `pyez/mock_device.py` would need swapping back out for
  real PyEZ to execute against actual hardware
- Negative/boundary/malformed-message tests need a peer emulator (ExaBGP or
  Scapy), same as before — the AI correctly *identifies* which tests need
  this and names the tool, but doesn't generate the emulator scripts
  themselves, and the mock can't fake that outcome either (it'll run, but a
  pass/fail against the mock doesn't mean anything about real conformance
  for these specifically)
- Confidence still drives the catalog's `needs_review` badge independently
  of whether an assertion was safe enough to promote to executable — a
  promoted, mock-passing assertion from a low-confidence Test Intent is
  still flagged for a human to check the reasoning behind it; treat
  `review` as a real signal, not decoration

## Architecture

```
backend/
  app.py             Flask routes (read + action APIs); loads .env before anything else
  pipeline.py         extraction, retrieval, generation orchestration, coverage, artefact uploads
  protocol_profiles.py per-protocol defaults: category rules, topology, config template, PyEZ observation
  ai_generation.py     system prompts, schema validation, safety checks (backend-agnostic)
  ai_backends.py       AI backend abstraction: local Claude Code CLI, or Anthropic API key
  pyez/
    mock_device.py    MockJunosDevice + mocked Config -- lets generated tests actually
                      run (see pipeline.run_deduplicated_tests) without a real Junos lab
  .env                 local environment variables (ANTHROPIC_API_KEY, AI_MODEL) — not committed
  .env.example         documented template for .env
  kb/
    knowledge.db      SQLite — requirements, status, test_intents, ingestion_log, artefacts, uploaded_tests
    retrieval_index.pkl   TF-IDF index (rebuilt on ingest, read on search + AI context)
    rfc4271_raw.txt   seed RFC text (only ever read by ingest_rfc())
    artefacts/        uploaded product specs / other reference files (original bytes)
    uploaded_tests/   uploaded existing test files reviewed for RFC coverage (original bytes)
  generated_tests/
    docs/             generated Markdown test cases -- the full, unfiltered record
    pytest/           generated pytest/PyEZ stubs -- the full, unfiltered record
    deduplicated/
      docs/, pytest/  curated subset with duplicate tests collapsed to one
                      representative each (see pipeline.refresh_deduplicated_tests) --
                      docs/pytest above are untouched, this is an additional view.
                      pytest/conftest.py here shims jnpr.junos with pyez/mock_device.py
                      so this specific folder's tests can actually be executed
  logs/
    generation.log    process log: RFC ingests, dedup results, and per-test
                      AI-backend-vs-heuristic-fallback outcome for every generated test
frontend/
  index.html, style.css, app.js    no build step, plain fetch() calls
```

The one property this prototype needs to prove — capture RFC knowledge once,
reuse it across every later request — still holds with AI in the loop: the
model only ever sees requirement text and related requirements pulled from
`kb/knowledge.db`. It's never given the raw RFC file, and it isn't asked to
re-derive anything already-parsed. The Knowledge Base tab's activity log
shows every `TESTS_GENERATED` event tagged with its generation mode.

## API reference

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/status` | RFC metadata + AI availability + coverage summary |
| GET | `/api/coverage` | full coverage + gap report |
| GET | `/api/matrix` | section × category requirement grid |
| GET | `/api/requirements` | all extracted requirements |
| GET | `/api/tests` | generated test catalog (includes generation_mode, needs_review, emulator flags) |
| GET | `/api/tests/<test_id>` | doc + pytest content for one test |
| POST | `/api/tests/run` | actually executes `generated_tests/deduplicated/pytest` via a real `pytest` run against mocked PyEZ (`pyez/mock_device.py`) — returns pass/fail/error counts + per-test detail |
| GET | `/api/search?q=...&k=10` | semantic search over requirements |
| GET | `/api/ingestion-log` | knowledge base activity log |
| GET | `/api/batches` | generation batch history |
| GET | `/api/artefacts` | uploaded product specs / other reference artefacts |
| GET | `/api/existing-tests` | uploaded existing tests + their AI-reviewed RFC coverage status |
| POST | `/api/generate` | `{requirement_ids: [...], label, derived_from}` |
| POST | `/api/generate-by-category` | `{category, count}` — generate N tests for a gap category |
| POST | `/api/generate-all` | `{limit?}` — bulk-fill every remaining automatable gap (optionally capped) in one call, running concurrently (`AI_GENERATION_CONCURRENCY`, default 4) |
| POST | `/api/ingest` | `{rfc_number, rfc_title, raw_text, protocol?}` — replace the knowledge base with pasted text (`protocol` optionally overrides auto-detection, e.g. `bgp`/`ospf`/`generic`) |
| POST | `/api/ingest/upload` | multipart `{rfc_number, rfc_title, rfc_file, protocol?}` (.txt/.md/.pdf) — same as above, from an uploaded file |
| POST | `/api/artefacts/upload` | multipart `{artefact_type, file}` (.txt/.md/.pdf) — upload a product spec or other reference material |
| DELETE | `/api/artefacts/<id>` | remove an uploaded artefact |
| POST | `/api/existing-tests/upload` | multipart `{file}` (.py/.txt/.md/.pdf) — upload an existing test to review against the RFC |
| POST | `/api/existing-tests/<id>/analyze` | AI-review one uploaded existing test against candidate RFC requirements |
| POST | `/api/existing-tests/analyze-all` | (re-)analyze every uploaded existing test |
| DELETE | `/api/existing-tests/<id>` | remove an uploaded existing test and its coverage matches |
