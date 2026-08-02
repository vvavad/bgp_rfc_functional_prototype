# Plan

Companion to `design.md`. Three requested workstreams, plus an evaluation of
what else the "BGP-focused demonstration" feedback implies. Not committed to
sequencing across all three at once — recommended order is 3 → 2 → 1 (key-less
AI unblocks everything else being demoed cheaply; coverage expansion is the
most visible/immediate demo win; protocol-agnosticism is the largest and least
demo-urgent, since the feedback explicitly wants a **BGP-focused** demo).

---

## 1. Make it networking-protocol agnostic

Goal: ingesting an OSPF/IS-IS/MPLS/etc. RFC should produce sensible
categories, generation prompts, and templates — not BGP vocabulary bent onto
a different protocol. See `design.md`'s "Where BGP is hard-coded" section for
the 6 concrete coupling points this touches.

- [x] **Protocol profile abstraction.** `protocol_profiles.py`: one
      `ProtocolProfile` per protocol (category rules, topology description,
      timer fields + per-category override, `string.Template`-based Junos
      config stanza, PyEZ observation call/field/result-var, default
      emulator tool, AI expertise note). `bgp` extracted verbatim from the
      original `CATEGORY_RULES`/templates (regression-verified — see below).
      `ospf` (RFC 2328) is the second profile, proven end-to-end. `generic`
      is the fallback for anything unrecognized (common categories only,
      clearly-marked placeholder config/observation, no BGP assumptions).
  - [x] `CATEGORY_RULES` moved into profiles; `protocol_profiles.COMMON_CATEGORY_RULES`
        (timer/message_format/error_handling/capability_negotiation) shared
        by all profiles, tried after each profile's own rules.
  - [x] `rfc_meta.protocol_key` column, resolved at ingest time
        (`protocol_profiles.resolve_profile`, RFC-number match then
        title-keyword scan then generic) with a manual `protocol` override
        param on `/api/ingest` and `/api/ingest/upload` (and a select in the
        Knowledge Base tab's ingest form).
  - [x] `DOC_TEMPLATE`/`PYTEST_TEMPLATE` topology/timers/config-stanza/
        observation fields all sourced from the active profile
        (`pipeline._generate_one`) instead of literal BGP strings. The
        AI's own `pyez_observation` suggestion stays advisory documentation
        only — the literal RPC call the rendered pytest stub actually
        executes always comes from the trusted profile, same safety
        boundary as `assertion_code`.
- [x] **AI prompt parameterization.** `ai_generation.SYSTEM_PROMPT_TEMPLATE` /
      `COVERAGE_SYSTEM_PROMPT_TEMPLATE` (both `string.Template`, not
      `.format()` — the JSON schema examples are dense with literal `{ }`
      that would collide with `.format()`'s field syntax) take the active
      profile's topology description, emulator-tool examples/enum, and
      expertise note. Verified the OSPF-profile prompt actually changes
      model behavior, not just labels: the AI's `protocol_reasoning` for a
      real OSPF requirement referenced Type-1 router-LSAs, Router-IDs, and
      the LSDB specifically — not BGP vocabulary reused.
- [x] **Frontend copy.** `renderHeader()` now reads `meta.protocol_display_name`
      from `/api/status` instead of a hardcoded "BGP proof of concept" string.
- [x] **Unknown-protocol fallback.** `GENERIC_PROFILE` — common categories
      only, topology text says "two conformant peers," config/observation
      are explicit `TODO` placeholders naming `rfc_meta.protocol_key` rather
      than guessing BGP.
- [x] **Regression check.** Generated a fresh BGP test after the full
      refactor landed (`RFC4271-S6.1-REQ-03`, a timer-category requirement
      to specifically exercise the hold-time override path) — topology line,
      timers (hold=6 override correctly applied), Junos config stanza, and
      the `get_bgp_neighbor_information()`/`.//peer-state` observation call
      all matched the pre-refactor hardcoded output exactly.
  - **Second-protocol proof, done safely:** `ingest_rfc()` is destructive
    (wipes `requirements`/`test_intents`, clears `generated_tests/`), so
    re-ingesting a real OSPF RFC into the live `kb/knowledge.db` would have
    destroyed today's real BGP demo data. Instead, ran a throwaway script
    that monkeypatches `pipeline.DB_PATH`/`RETRIEVAL_INDEX_PATH`/`DOCS_DIR`/
    `PYTEST_DIR` to a temp directory, ingested synthetic OSPF-2328-style
    text, and generated real tests through the live AI backend — categories
    (lsa_flooding/neighbor_adjacency/spf_calculation/area_management/
    authentication), topology, timers, config stanza, and observation call
    were all correctly OSPF-flavored, and the live BGP DB was never touched.
    Temp dir cleaned up after.
  - **Bug found and fixed during this check:** `_migrate()`'s protocol_key
    backfill was a DML `UPDATE`, but `get_conn()` never commits and several
    read-only call sites (including `get_active_profile()` itself, and my
    own first verification script) only `SELECT` + close — so the backfill
    silently rolled back, and since the `ALTER TABLE` had already persisted,
    the `if column not in cols` guard meant it would never retry. Fixed by
    having `_migrate()` commit its own changes before returning, and
    manually repaired the already-corrupted live `rfc_meta.protocol_key`
    value. Worth remembering for any future migration that both alters
    schema and backfills data: don't rely on the caller to commit.

Landed for both `bgp` and `ospf`; a third protocol is a matter of adding one
more `ProtocolProfile`, not touching pipeline.py/ai_generation.py again.

---

## 2. Increase test coverage against RFC-derived requirements

Starting state (see `design.md`): RFC 4271 ingest yields 203 requirements
(196 automatable), bootstrap seed only generated 28 (~14% coverage). Current
state after this pass: **77 tests generated, automatable coverage 39.3%**
(overall 37.9%), 119 gaps remaining — real generation, not padding (see the
quality summary below).

- [x] **Bulk-generate to raise default coverage.** `pipeline.generate_all_gaps()`
      + `POST /api/generate-all` (optional `{limit}`), plus a "Generate all
      remaining gaps" button in the Gap Analysis tab. `generate_tests`'s
      inner loop now fans the AI-call/render step for each requirement out
      across a `ThreadPoolExecutor` (`AI_GENERATION_CONCURRENCY`, default 4)
      — DB writes and file writes still happen sequentially afterward on a
      single connection, only the I/O-bound AI calls run concurrently. Ran
      a real 40-test batch end-to-end (~5.3 minutes wall time, all 40
      succeeded, no failures) to prove the concurrency path — see the race
      condition found and fixed below.
  - **Concurrency bug found and fixed:** `ai_generation._select_backend()`'s
    memoization wasn't thread-safe — two threads racing the very first call
    could both see `_backend_checked` flip `True` before `_backend` was
    actually assigned, so one of them read the still-`None` default and
    incorrectly fell back to heuristic (`generation_mode:
    heuristic-fallback:no-ai-backend`) even though the backend was genuinely
    available. Fixed with proper double-checked locking (`threading.Lock`).
    Verified by re-running a 4-item concurrent batch and confirming all 4
    now correctly show `ai_backend: claude-code-cli`.
  - Decided the out-of-the-box seed question: left the bootstrap seed at
    its existing demo-scale default (fast first-launch startup) rather than
    generating for all ~196 automatable requirements on every fresh
    install — bulk-fill is now a deliberate, on-demand action
    (`/api/generate-all` or the button) to run before a demo, not something
    that slows down every casual first run.
- [x] **Quality bar to trust the higher volume, not just the count:**
  - [x] Overview tab now has a "Generation quality" panel (`app.js:renderGenQuality`,
        computed client-side from the already-loaded catalog): total tests,
        high-confidence count/%, needs-review count/%, peer-emulator-required
        count, heuristic-fallback count — so a big bulk-generation run is
        visibly checked for confidence distribution, not just a bigger
        headline number.
  - [x] `backend/verify_generated_tests.py` — `ast.parse`s every generated
        pytest stub as a post-generation smoke test. Run after the 40-test
        bulk batch and again after the protocol-agnostic refactor's
        regression test: 77/77 valid both times.
- [x] **Requirement-extraction coverage audit — done, real finding.**
      Cross-checked two ways: (1) a naive whole-document keyword-sentence
      recount (bypassing section boundaries entirely) matched the DB's
      extracted count exactly (203=203) — section splitting isn't dropping
      keyword-bearing text at section edges. (2) Manual read of §5.1.2
      (AS_PATH) surfaced a real gap: `_split_into_statements` only splits on
      `". " + capital letter`, so RFC-style lettered/numbered sub-lists under
      one lead-in sentence — e.g. the AS_PATH modification rules ("a) ...
      the advertising speaker SHALL NOT modify..." / "b) ... 1) ... it
      SHOULD prepend..." / "2) ..." / "3) ...") — get merged into a single
      oversized "requirement" instead of split into the ~4-5 independently
      testable statements they actually contain. This is under-segmentation,
      not text loss: nothing is silently missing, but distinct MUST/SHOULD
      clauses are bundled together, undercounting true requirement count and
      producing harder-to-test multi-clause statements.
  - [ ] **Follow-up fix (not done — deliberately deferred):** teach
        `_split_into_statements` to also break before lettered/numbered list
        markers (`a)`, `b)`, `1)`, `2)`...) following a colon-introduced lead
        sentence. Deferred because `ingest_rfc()` is destructive — it wipes
        `requirements`/`test_intents` and clears `generated_tests/` on every
        re-ingest, and requirement IDs are assigned by per-section sequence
        order, so improving the splitter and re-ingesting would renumber
        requirements and orphan every already-generated test (including the
        real ones generated today). Do this as its own deliberate pass, with
        a plan for either accepting the reset or migrating existing
        `test_intents` rows to their nearest new requirement ID first.
- [ ] **Actual test-count metric for "coverage" as demoed.** Decide what
      number the demo leads with: `overall_coverage_pct` (against *all*
      requirements, including non-automatable) or
      `automatable_coverage_pct`. Recommend automatable — non-automatable
      requirements can never reach 100% and would cap the headline number
      artificially low.

---

## 3. Replace API key with sandbox/VS Code-native AI access

Goal: this app currently requires a standalone `ANTHROPIC_API_KEY` in
`backend/.env` to get AI-reasoned generation; heuristic mode is the fallback.
Since the target running environment is Claude Code / a VS Code extension
that's already authenticated, the ask is to use *that* authentication instead
of asking a demo operator to provision and paste in a separate key.

- [x] **Confirm the mechanism before building.** Spiked directly: the
      bundled Claude Code CLI binary (found via `CLAUDE_CODE_EXECPATH` or
      the VS Code extension's `resources/native-binary/claude`) supports
      `-p --output-format json` for clean single-line JSON on stdout (trust-
      dialog/permission warnings go to stderr only), `--system-prompt` fully
      replaces the default system prompt, `--json-schema` is available for
      validated structured output, and auth is OAuth-based
      (`~/.claude/.credentials.json`, subscription-tied) — no
      `ANTHROPIC_API_KEY` involved. Confirmed with live smoke-test calls
      before writing any code.
- [x] **Backend abstraction.** Built `backend/ai_backends.py`: `AIBackend`
      interface (`available()`, `complete(system, user) -> text`), so
      `generate_ai_test_intent`/`analyze_existing_test_coverage` don't care
      which backend answered.
  - [x] `AnthropicAPIBackend` — today's behavior, gated on
        `ANTHROPIC_API_KEY`.
  - [x] `ClaudeCodeCLIBackend` — shells out to the local CLI; binary
        discovery order: `CLAUDE_CLI_PATH` override → `CLAUDE_CODE_EXECPATH`
        → known VS Code/Cursor extension install globs → `claude` on PATH.
  - [x] Selection order: `AI_BACKEND` env var (`auto`/`cli`/`api`/
        `heuristic`); `auto` (default) tries CLI first, then API key, then
        heuristic — same "always succeeds, just labeled" guarantee as
        before, no new hard failure mode.
- [x] **Badge/labeling update.** `test_intents.ai_backend` column (migrated
      into existing DBs via `pipeline._migrate`) records which backend
      answered each generated test; `/api/status`'s `ai.backend` field and
      the header/catalog badges (`app.js`) surface it.
- [x] **Docs.** `README.md`'s "Run it" and "AI-powered test generation"
      sections updated; `.env.example`/`.env` document `AI_BACKEND` and
      `CLAUDE_CLI_PATH`.
- [x] **Don't regress the standalone path.** `AnthropicAPIBackend` is
      untouched behavior-wise, just moved behind the same interface —
      `AI_BACKEND=api` forces it explicitly if needed.

---

## Evaluation: what else is needed for the "BGP-focused demonstration" feedback

The feedback asks for a demo showcasing **Knowledge ingestion, Test catalogue
generation, AI-generated test cases, Dashboard visualization** — all four of
these already exist and work today (see `design.md`'s "what's real" section):
ingestion is `/api/ingest[/upload]` + the Knowledge Base tab, test catalogue
generation is the Test Catalog tab + `/api/generate*`, AI-generated test
cases are the whole `ai_generation.py` path, dashboard visualization is the
Overview tab's charts + Coverage Matrix. So this isn't "build these four
things" — it's "make them demo-ready and legible to an audience watching a
walkthrough." Concrete gaps against that bar:

- [ ] **A guided demo script/checklist.** Nothing currently sequences "here's
      what to click, in what order, to tell the ingestion → catalogue →
      AI-generation → dashboard story" for someone presenting this live.
      Worth a short `DEMO.md`: e.g. (1) show Knowledge Base tab / ingestion
      log proving RFC 4271 was parsed once, (2) generate a batch live from a
      gap category to show real-time AI reasoning happening, (3) open a
      generated test's doc+pytest modal to show the Test Intent → template
      compile step, (4) walk the Overview charts and Coverage Matrix to show
      the aggregate picture, (5) optionally show the Gap Analysis /
      existing-test upload flow if time allows.
- [ ] **Coverage number credibility at demo time** — directly the same gap as
      workstream 2 above (14% out-of-the-box coverage undersells the tool on
      first look). Bulk-generating before a demo (or via the bootstrap
      change in item 2) matters more here than anywhere else.
- [ ] **AI-generation visibly "live" during the demo**, not pre-baked —
      depends on workstream 3 landing (or at minimum, a real
      `ANTHROPIC_API_KEY` provisioned for the demo machine) so the audience
      sees the "AI reasoning active" badge and a real Claude call happening
      on click, not the heuristic-fallback path. Verify this explicitly
      before presenting — an unset/expired key silently downgrades to
      heuristic mode with only a small badge change, easy to miss while
      presenting.
- [ ] **Dashboard visual polish pass.** The charts/matrix are functional but
      worth a once-over with fresh eyes right before a demo: category names
      are raw enum strings (`fsm_state` → "fsm state"), the matrix's color
      legend assumes viewers know the 4-bucket scheme, and there's no
      empty-state / loading-state handling if `/api/status` is slow — minor,
      but the kind of thing that reads as unpolished on a screen-share.
- [ ] **Sample "before" state for gap analysis** — if the demo wants to show
      the existing-test-upload / gap-closing story, it needs 1-2 realistic
      "team's existing pytest tests" sample files staged ahead of time
      (`kb/uploaded_tests/` is currently empty) rather than improvising a
      file to upload live.
- [ ] **Explicitly out of scope for this demo:** workstream 1 (protocol
      agnosticism) — the feedback asks for a *BGP-focused* demo, so don't let
      the protocol-agnostic refactor block or delay demo prep; sequence it
      after, as noted at the top of this file.
