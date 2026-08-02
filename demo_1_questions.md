# Anticipated Audience Questions — Demo Q&A

Likely questions from `demo_1.md`'s audience, grouped by theme, with short
(2-4 line) answers grounded in what the POC actually does today — not
aspirational claims. Where something is a real limitation, the answer says
so directly; that's consistent with how the tool itself surfaces
uncertainty (`needs_review`, `heuristic-fallback:<reason>` labels).

---

## AI reasoning & trust

**Q: How do you know the AI-generated tests are actually correct?**
You don't, automatically — that's why every test carries a confidence
level. High-confidence assertions get promoted to an executable `assert`
line after passing a safety check; medium/low confidence keeps the AI's
suggestion as a commented TODO and flags the test `needs_review` in the
catalog. Nothing below "high" is silently trusted.

**Q: What stops the model from writing broken or dangerous Python into
these test files?**
It never writes Python at all. The model returns a structured JSON "Test
Intent" (test type, reasoning, steps, a candidate assertion expression);
a fixed template renderer is the only thing that produces the actual
doc/pytest text. If the JSON doesn't validate against the schema,
generation falls back to a heuristic template instead of shipping
anything unvalidated.

**Q: Could the assertion code itself do something unsafe if it got
promoted to executable?**
The promotion path runs the AI's suggested expression through an AST-based
allowlist first — no imports, no arbitrary function calls, only a small
set of safe attribute methods (`.strip()`, `.lower()`, `len()`, etc.).
Anything that doesn't pass stays a commented suggestion, never executes.

**Q: What happens if the AI call fails, times out, or returns garbage?**
Generation still succeeds — it falls back to the original heuristic
template path (the same one used before AI was wired in), and the test is
watermarked `heuristic-fallback:<reason>` in the catalog and the doc file
itself. The app never blocks on AI availability.

**Q: Is this reproducible? Does the same requirement always generate the
same test?**
No — it's a live model call each time, so wording, exact steps, and
confidence can vary run to run for the same requirement (already-covered
requirements are skipped, not regenerated, so this only matters if you
explicitly regenerate). The RFC statement and the requirement's
classification are the only fixed parts.

---

## Key-less AI / cost

**Q: Do we need to buy or provision an Anthropic API key to use this?**
Not if you're running it inside a Claude Code session — it detects and
uses that session's own login automatically (`AI_BACKEND=auto`), no key
involved. `ANTHROPIC_API_KEY` is only needed for running the app standalone
outside Claude Code.

**Q: Is our RFC or product data sent anywhere insecure?**
Requirement text, related context, and any uploaded product-spec excerpts
go to Claude (via the Claude Code CLI or the Anthropic API, whichever
backend is active) — same as any other use of Claude in this environment.
Nothing is sent to a third service beyond that.

**Q: What does this actually cost to run at scale?**
It's usage against your existing Claude Code session/subscription, not a
separate per-token bill you have to track — that was the whole point of
the key-less backend. A real 40-test bulk-generation batch took about 5
minutes wall-clock with 4 requirements running concurrently.

**Q: Could we swap in GPT or Gemini instead of Claude?**
Technically the backend is pluggable, but it isn't done and isn't
currently planned — it would mean re-validating the strict
"JSON-object-only, exact schema" instruction-following the whole
confidence-routing design depends on against a different model family, for
no problem that's actually been raised (see `todo.md`'s AI-provider
addendum).

---

## Protocol coverage

**Q: Does "protocol-agnostic" really work, or is this just BGP with the
labels changed?**
It's a real second profile, not relabeling — ingesting an OSPF snippet
produces OSPF-specific categories (`lsa_flooding`, `neighbor_adjacency`,
`spf_calculation`...), an OSPF topology/timers section, a real
`protocols { ospf { ... } }` Junos config stanza, and
`get_ospf_neighbor_information()` as the observation call. The AI's own
reasoning referenced router-LSAs and the LSDB specifically, not BGP
vocabulary.

**Q: What happens if we ingest an RFC for a protocol you haven't built a
profile for (IS-IS, MPLS, EVPN)?**
It falls back to a generic profile: the protocol-neutral categories
(timer, message_format, error_handling, capability_negotiation) still
apply, but the topology/config/observation fields become explicit `TODO`
placeholders rather than silently assuming BGP or guessing at a protocol
it doesn't know.

**Q: How do you add support for a new protocol?**
Add one `ProtocolProfile` entry (category keyword rules, topology
description, timer fields, a Junos config template, the PyEZ observation
call) — no changes needed to the ingestion, generation, or AI-prompt code
itself, since all of that already reads from whichever profile is active.

**Q: How does the app know which protocol profile to use for a given
RFC?**
It resolves automatically from the RFC number (a known table, e.g. 4271 →
BGP, 2328 → OSPF) or a keyword scan of the title if the number isn't
recognized. There's also a manual override dropdown on the ingest form for
edge cases.

---

## Test execution & lab readiness

**Q: Can these generated tests actually run against a real router right
now?**
Not out of the box — the pytest stubs use real PyEZ call patterns, but the
device host/credentials are placeholders you wire to your own
vJunos-router/vMX lab inventory. The protocol reasoning and structure are
real; the last-mile lab connection is deliberately left as a stub.

**Q: How do negative/malformed-message tests actually get executed, since
Junos won't send bad packets itself?**
Those are flagged `requires_peer_emulator` with a named tool (ExaBGP or
Scapy, depending on protocol), and the AI is explicitly instructed to be
honest about when a conformant router would never originate the condition
itself. This tool identifies which tests need an emulator and names it —
it doesn't generate the emulator scripts themselves.

**Q: What fraction of the generated tests are actually ready to trust
versus needing review?**
That's exactly what the Overview tab's Generation Quality panel shows —
total tests, high-confidence count, needs-review count, and how many
require a peer emulator — so bulk-generating more tests doesn't quietly
mean generating less trustworthy ones.

---

## Knowledge base & architecture

**Q: Why parse the RFC into a database instead of just re-reading the text
each time?**
Two reasons: it's the thing this prototype is built to prove (parse once,
reuse forever — ingestion happens exactly once, everything else reads the
DB), and practically, re-parsing on every request would be slower and
would re-run the section/sentence-splitting logic redundantly for no
benefit once it's already been extracted.

**Q: Is this using RAG (retrieval-augmented generation)?**
Yes, in a narrow, verifiable form: a TF-IDF index over the extracted
requirement corpus retrieves the 3 most semantically related requirements
as context for each generation call — the same index that powers the
dashboard's search box, not a separate hidden mechanism.

**Q: What's actually in the SQLite database?**
Requirements (with section, keyword, category, testability), which
requirements have a generated test, the full generated test catalog (doc +
pytest content, confidence, backend used), an ingestion activity log,
uploaded artefacts/existing tests, and RFC metadata including which
protocol profile is active.

**Q: Does re-ingesting a new RFC merge with the old one, or replace it?**
Replaces it entirely — re-ingestion clears the current requirements, all
generated tests, and the generated test files on disk, then rebuilds from
the new text. That's a deliberate destructive operation, gated behind a
confirmation dialog in the UI.

---

## Extraction accuracy & gaps

**Q: Does the extraction catch every MUST/SHOULD/MAY statement in the
RFC?**
Mostly, with one known, documented gap: RFC-style lettered/numbered
sub-lists under one lead-in sentence (e.g. "a) ... b) ... 1) ... 2) ...")
currently get merged into a single oversized requirement instead of split
into the several independently-testable statements they actually contain.
Nothing is silently dropped — it's under-segmentation, not loss.

**Q: Why hasn't that been fixed yet?**
Fixing the sentence-splitter and re-ingesting would renumber every
requirement ID (they're assigned by per-section sequence), which would
orphan every already-generated test, since re-ingestion is destructive.
It's deliberately deferred as its own pass rather than done as a
side-effect of something else.

**Q: How is a requirement classified as "not testable" versus
"automatable"?**
A small set of phrase patterns (e.g. "implementation-dependent," "a matter
of local policy," "outside the scope") flag a statement as not
independently observable. Everything else defaults to automatable —
conservative in the sense that it doesn't try to guess intent beyond those
explicit phrasings.

---

## Comparison to existing test suites / practical adoption

**Q: We already have a test suite — does this duplicate work or create
conflicting tests?**
Neither, by design — the Gap Analysis tab lets you upload your existing
tests, and the AI reviews each one against candidate RFC requirements
(via the same retrieval index), reporting which ones it actually verifies
with a confidence + rationale. Matched requirements are removed from the
"real gaps" list, so the tool tells you what's still missing, not what to
duplicate.

**Q: Is this meant to replace manual test-writing entirely?**
No — it's positioned as accelerating the first draft and the traceability
work (which requirement maps to which test, and why), with confidence
scoring and `needs_review` flags built in specifically so a human stays in
the loop before anything is trusted, especially anything below high
confidence.

**Q: What's the actual measured impact — coverage, time saved?**
As of this demo: 203 requirements extracted from RFC 4271, 196 of them
automatable, with coverage raised from an initial ~14% (28 seed tests) to
39.3% (77 tests) through on-demand bulk generation — real generated
output, not a projection.

**Q: Who's responsible for verifying a test before it goes into an
official suite?**
The catalog's confidence badge and `needs_review` flag are the signal for
that decision — high-confidence tests still deserve a look before trusting
them in a real suite, and anything medium/low is explicitly marked as
needing an engineer's review before use.
