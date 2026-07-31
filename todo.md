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

- [ ] **Protocol profile abstraction.** Introduce a `protocol_profiles.py` (or
      similar) with one profile per protocol: category keyword rules, default
      topology description, default PyEZ observation call, config template
      snippet, example AS/router naming. Start with `bgp` (extracted verbatim
      from current `CATEGORY_RULES`/templates) and one second profile (`ospf`
      is the natural pick — RFC 2328 is a good test case and PyEZ has
      `get_ospf_neighbor_information`) to prove the abstraction isn't
      BGP-shaped in disguise.
  - [ ] Move `CATEGORY_RULES` into the profile; keep a protocol-neutral
        fallback rule set (timer/error_handling/message_format/
        capability_negotiation/general_conformance are already generic enough
        to keep as shared defaults).
  - [ ] Store the active protocol on `rfc_meta` (new column, e.g.
        `protocol_key`) at ingest time — inferred from RFC number/title
        against a known table, with a manual override param on `/api/ingest`
        for RFCs the table doesn't recognize.
  - [ ] Parameterize `DOC_TEMPLATE`/`PYTEST_TEMPLATE` topology/config/RPC
        fields from the active profile instead of literal BGP strings.
- [ ] **AI prompt parameterization.** Thread the active protocol profile into
      `ai_generation.SYSTEM_PROMPT` / `_build_user_prompt` — protocol name,
      default topology, and known observation RPCs become template
      variables, not hardcoded prose. Keep the deep-expertise framing
      (BGP/OSPF/IS-IS/MPLS/EVPN/Junos/PyEZ/YANG) since that's genuinely
      protocol-general.
- [ ] **Frontend copy.** Replace the hardcoded "BGP proof of concept" strings
      in `index.html`/`app.js` (`renderHeader`) with the active protocol name
      pulled from `/api/status`.
- [ ] **Unknown-protocol fallback.** When ingesting an RFC that doesn't match
      a known profile, fall back to a generic profile (already-generic
      category rules, protocol-neutral topology language: "two conformant
      peers") rather than defaulting to BGP assumptions silently.
- [ ] **Regression check.** Re-run ingest + generate against the bundled RFC
      4271 after the refactor and diff a sample of generated docs/pytest
      against current output — this must not degrade the BGP demo, since
      that's still the flagship path.

Effort note: this is the largest of the three asks. Recommend scoping the
first pass to "BGP profile extracted cleanly + one second protocol proven
end-to-end," not full coverage of every protocol named in the system prompt.

---

## 2. Increase test coverage against RFC-derived requirements

Current state (see `design.md`): RFC 4271 ingest yields 203 requirements
(196 automatable), bootstrap seed only generates 28 (~14% coverage). The
generation machinery already exists (`generate_tests`,
`/api/generate-by-category`, matrix drill-down); this is about actually
running it further and making sure quality holds up at volume, not new
plumbing.

- [ ] **Bulk-generate to raise default coverage.** Add a way to generate
      "all remaining automatable gaps" in one action — a `/api/generate-all`
      endpoint or a `--all` bootstrap flag — rather than only 3-per-category
      or 5-per-category-on-demand. Batch/paginate the AI calls (existing loop
      in `generate_tests` is sequential; at 196 requirements this is 196
      sequential Claude calls — worth at least a progress-visible batching
      strategy, and consider concurrency with a small worker pool if the
      Anthropic SDK usage supports it safely).
  - [ ] Decide and surface a default target (e.g. seed script generates for
        *all* automatable requirements instead of 3/category) so the
        dashboard's out-of-the-box coverage number is demo-credible, not 14%.
- [ ] **Quality bar to trust the higher volume, not just the count:**
  - [ ] Add a lightweight self-check pass: after bulk generation, summarize
        `needs_review` / `requires_peer_emulator` / confidence distribution
        so "more tests" doesn't quietly mean "more low-confidence tests."
        (The data already exists in `test_intents` — this is a report, not
        new generation logic.)
  - [ ] Spot-check generated pytest stubs still parse (`python -m py_compile`
        or `ast.parse` over `generated_tests/pytest/*.py`) as a cheap
        post-generation smoke test — catches template-rendering regressions
        before they reach the catalog.
- [ ] **Requirement-extraction coverage, not just test coverage.** Worth a
      pass at whether `_split_into_statements`/`SECTION_RE` are missing
      requirements (e.g. multi-sentence normative statements, tables,
      requirements phrased without a clean sentence boundary) — extraction
      recall directly caps how high "coverage against RFC-derived
      requirements" can even go. Suggest a manual audit: sample a few RFC
      4271 sections, count MUST/SHOULD/MAY statements by eye, compare to what
      got extracted.
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

- [ ] **Confirm the mechanism before building.** This needs a short spike,
      not a design decision made blind: check whether the target environment
      exposes (a) a local `claude` CLI on PATH that supports non-interactive,
      scriptable prompting (headless/print mode) with output the app can
      parse, or (b) the Claude Agent SDK importable from Python in that
      environment, or (c) neither, in which case this item may reduce to
      "the app can read Claude Code's *existing* credential storage/config to
      populate `ANTHROPIC_API_KEY` automatically" rather than a different
      call path. (In this authoring session's Bash tool, `claude` was not on
      PATH — that may or may not reflect the actual demo machine, so verify
      there directly.)
- [ ] **Backend abstraction.** Refactor `ai_generation.py`'s single
      `_get_client()`/`Anthropic()` call site into a small backend interface
      with a common `complete(system, user) -> text` shape, so
      `generate_ai_test_intent`/`analyze_existing_test_coverage` don't care
      which backend answered:
  - [ ] `AnthropicAPIBackend` — today's behavior, gated on
        `ANTHROPIC_API_KEY`.
  - [ ] `ClaudeCodeBackend` (or whatever the spike confirms) — uses the local
        Claude Code session, no key required.
  - [ ] Selection order: explicit `AI_BACKEND` env var if set; otherwise
        auto-detect (Claude Code backend available? use it; else fall back
        to API-key check; else heuristic) — keep the existing "always
        succeeds, just labeled" guarantee, don't introduce a new hard
        failure mode.
- [ ] **Badge/labeling update.** The header's "Heuristic mode" /
      "AI reasoning active · <model>" badge (`app.js:renderHeader`) and the
      `generation_mode` values stored per test (`ai-high`/`ai-medium`/...)
      should reflect *which* backend answered, so the catalog stays honest
      about provenance (e.g. `ai-high` via API key vs. via Claude Code — may
      not matter to a demo viewer, but matters for the "review" workflow).
- [ ] **Docs.** Update `README.md`'s "Run it" section once the mechanism is
      confirmed — likely simplifies to "no key needed inside Claude
      Code/VS Code; set `ANTHROPIC_API_KEY` only if running standalone."
- [ ] **Don't regress the standalone path.** Keep the `.env` / API-key mode
      working for anyone running this outside Claude Code (e.g. a colleague
      demoing from a plain terminal with a provisioned key) — this is an
      additive backend option, not a replacement that breaks portability.

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
