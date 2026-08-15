# Demo Script — RFC Conformance Test Generation POC

A walkthrough script for demoing this prototype live. Covers, in order:
**RFC ingestion → Test Intent generation → actual pytest generation →
running those tests for real against mocked PyEZ → incremental knowledge
growth (ingest more RFC text later and watch existing tests get flagged and
regenerated, not just new ones created) → proof that later runs never
re-touch the raw RFC text → protocol-agnostic support (BGP + OSPF)**.
Includes cleanup before the first run and after the demo, so the
environment is left exactly as it was found.

Total runtime: ~35-40 minutes at a comfortable pace (the incremental
knowledge-growth section, steps 3/5/7/8, is the newest and most important
addition — don't cut it for time before cutting the OSPF section instead).

A root `Makefile` wraps every terminal action below as a target
(`make status`, `make library`, `make ingest FILE=...`, `make generate-all`,
`make run-tests`, `make demo-incremental`) — this script shows the
UI-clicking version for a live audience, with the equivalent `make` command
called out alongside each step for a more technical audience or for your own
sanity-checking. Run `make` with no target to see the full list.

---

## 0. What you're proving, in one paragraph

RFC text gets parsed into individual normative requirements. Every later
action — listing requirements, computing coverage, generating a test — reads
from that database, never the raw RFC text again. Knowledge doesn't have to
arrive all at once, either: RFC source files live in a small library
separate from the app code (`backend/kb/rfc_library/`), selectable one at a
time via an API, and **ingesting one adds to the knowledge base instead of
replacing it** — existing requirements and already-generated tests survive.
If newly-added knowledge changes what an *existing* test's reasoning should
have seen, that test is automatically flagged and regenerated the next time
you generate tests — the catalog reports both **new** tests and **modified**
ones, not just a bigger total. For each requirement, Claude reasons about
the actual protocol mechanics and returns a structured **Test Intent** (never
free-form code), which a deterministic template renderer compiles into two
artifacts: a Markdown doc (the "test intent file") and a pytest/PyEZ stub
(the "actual test"). None of this needs a provisioned `ANTHROPIC_API_KEY` —
it runs through the local Claude Code session's own login. Duplicate tests
(mostly a heuristic-fallback artifact) get collapsed into a separate curated
folder, and **that curated folder actually runs** — a real `pytest`
execution against a mocked PyEZ layer, no lab required, proving this isn't
just text generation. And the whole pipeline is protocol-agnostic under the
hood: BGP is the flagship, but OSPF works today too, through the same code
path.

---

## 1. Before you start — pre-demo cleanup

The OSPF section of this demo (step 10) **replaces the knowledge base**,
which wipes whatever BGP requirements/tests exist at that point. Back
everything up first so you can restore the real BGP demo state afterward —
don't skip this.

```bash
# Run every command in this script from the repo root (not inside backend/)
# -- the Makefile's targets cd into backend/ themselves.

# 1a. Back up the current runtime state (safe even if you've never run the
#     app before -- cp on a missing source is a no-op error you can ignore).
#     knowledge_sources (the Knowledge Library's ingested/not-ingested
#     tracking) lives inside knowledge.db too, so this one backup covers it.
mkdir -p /tmp/poc_demo_backup
cp backend/kb/knowledge.db /tmp/poc_demo_backup/ 2>/dev/null
cp backend/kb/retrieval_index.pkl /tmp/poc_demo_backup/ 2>/dev/null
rm -rf /tmp/poc_demo_backup/generated_tests
cp -r backend/generated_tests /tmp/poc_demo_backup/

# 1b. Confirm dependencies are installed.
make install

# 1c. Confirm an AI backend will actually be used, not the heuristic
#     fallback -- if this prints ai_available: True with backend
#     claude-code-cli (or anthropic-api if you're running standalone with
#     a key), you're good. If it prints False, the demo still works but
#     every test will be watermarked "heuristic-fallback" instead of
#     showing real AI reasoning -- fix this before presenting.
cd backend && python3 -c "
import pipeline
print(pipeline.get_ai_status())
" && cd ..

# 1d. Clear the knowledge base, generated tests, and process log so the
#     Knowledge Library demo starts from a genuinely empty state.
make reset-demo
```

---

## 2. Start the app — empty, on purpose (~1 min)

Unlike a normal launch, start this demo with `run-empty` instead of `run` —
it skips the usual "seed the whole bundled RFC on first boot" bootstrap
(`SKIP_SEED_BOOTSTRAP=1`) so the dashboard opens with **nothing loaded yet**.
That's the point: the next section ingests knowledge live, piece by piece,
which the normal one-shot bootstrap would otherwise pre-empt.

```bash
make run-empty
```

The console prints nothing about ingestion this time — just the normal
Flask startup banner. Open **http://localhost:5000**.

**Say while pointing at the empty dashboard:** "Nothing's loaded. Zero
requirements, zero tests, no RFC in the header. Normally this app seeds
itself with the full BGP spec the instant it starts — I've turned that off
for a minute so you can watch knowledge actually arrive, in stages, instead
of already being there."

---

## 3. Ingest RFC 4271 — Part 1, via the Knowledge Library (~3 min)

This is the new centerpiece: RFC source text lives in files kept separate
from the application code, tracked, and selected for ingestion through an
API — not pasted into a form each time.

1. Go to the **Knowledge Base** tab. The new **"Knowledge library"** panel
   lists two files, both **not ingested**:
   - `rfc4271_part1_sections1-6.3.txt` — RFC 4271, sections 1 through 6.3
     (107 requirements)
   - `rfc4271_part2_sections6.4-10.txt` — the rest, sections 6.4 through 10
     (96 requirements)
2. **Say while pointing at the panel:** "These are plain text files sitting
   in `backend/kb/rfc_library/` — not baked into the app, not pasted through
   a form. The API can list what's in that folder and tell you what's
   already been ingested versus what hasn't, and ingest a specific one by
   name. Right now, neither half of RFC 4271 has been ingested."
3. Click **"Ingest"** next to `rfc4271_part1_sections1-6.3.txt`.
   *(Terminal alternative: `make ingest FILE=rfc4271_part1_sections1-6.3.txt`)*
4. Watch the stat row populate: **107 requirements**, still **0 tests
   generated**, **0% coverage**. The row for this file now reads
   **ingested**, with a timestamp.
5. **Say while this is fresh on screen:** "That's the first roughly half of
   the real BGP spec — sections 1 through 6.3 — parsed into 107 individual
   testable requirements, just now, from a file the app didn't have loaded a
   few seconds ago. Notice this is additive: if there'd been anything here
   already, ingesting this file would add to it, not replace it. That
   distinction matters — it's what makes the rest of this demo possible."

---

## 4. Tour the dashboard (~3 min)

- **Header:** RFC title/number now reads *"A Border Gateway Protocol 4
  (BGP-4) (RFC 4271) — Conformance Coverage,"* and the AI badge — point out
  it says *"AI reasoning active · &lt;model&gt; · via local Claude Code · no
  API key."* This is the key-less AI path — no `ANTHROPIC_API_KEY` was
  provisioned for this demo; if you're running standalone with a key
  instead, it'll say "via Anthropic API key" there instead.
- **Overview tab:** coverage-by-category chart, test-type distribution, and
  the **Generation Quality** panel — confidence distribution, so a big batch
  of tests never quietly means a big batch of *unreliable* tests. Both are
  still mostly empty right now — that changes in the next step.
- **Knowledge Base tab → activity log:** exactly one
  **`RFC_INGESTED_INCREMENTAL`** event (not the older plain
  `RFC_INGESTED` you'd see from the paste/upload form) — source reads
  `kb/rfc_library/rfc4271_part1_sections1-6.3.txt`. Keep this tab open —
  you'll come back to it in steps 7 and 9.
- **Test Catalog tab:** empty for now, but note the **"Run deduplicated
  tests"** button near the bottom — you'll use it in step 6.

---

## 5. Generate tests live — Test Intent → actual test (~6-8 min)

1. Go to **Gap Analysis** (or **Coverage Matrix**, click any non-empty
   cell).
2. Pick a category with gaps (e.g. `path_attribute` or `error_handling`)
   and click **"Generate 5 tests for this category."**
3. While it's running (a few seconds to ~30s per test, depending on the
   backend), narrate: "Right now, for each requirement, we're pulling the
   requirement text plus the nearest related requirements from the same
   database — real retrieval, the same index that powers the search box —
   and sending that to Claude. Claude reasons about the actual protocol
   mechanics: is this something a real Junos router would ever do on its
   own, or does it need a peer emulator to construct? What's the precise
   PyEZ observation point? It returns a structured Test Intent — never
   Python code directly."
4. When it finishes, open the **Test Catalog** tab and click one of the
   newly-created rows (or note the "AI · high/medium/low" badge — hover it
   to see which backend answered).
5. In the modal:
   - **"Documented test case" tab — this is the Test Intent, rendered.**
     Point out: the normative statement it traces to, the protocol
     reasoning paragraph (Claude's own words, not a template), the
     topology/timers preconditions, the test steps, and whether it flags
     `needs_review` or a required peer emulator.
   - **"pytest / PyEZ stub" tab — this is the actual generated test.**
     Point out: real PyEZ `Device`/`Config` fixtures, a real Junos config
     stanza rendered for the current protocol, the actual RPC call that
     gets executed (`rpc.get_bgp_neighbor_information()` for BGP), and the
     AI's suggested assertion — promoted to a real, executable `assert`
     line whenever it passes a static safety check, otherwise left as a
     commented suggestion with `needs_review` set.
6. Now click **"Generate all remaining gaps"** in the Gap Analysis tab to
   fully cover this first half of the RFC before moving on — this sets up
   the payoff in step 8. **This can take several minutes for a full batch**
   (roughly a few seconds per test, several running concurrently) — use the
   wait to walk through the **Coverage Matrix** tab (section × category
   grid) and try the **Overview tab's semantic search box** (e.g. search
   "hold timer expired" and show it's the same retrieval index feeding AI
   context, exposed directly).
   *(Terminal alternative: `make generate-all` — or cap it with a smaller
   batch first via `curl -X POST localhost:5000/api/generate-all -d
   '{"limit": 20}' -H "Content-Type: application/json"` if you're short on
   time; a smaller batch here just means a smaller "modified" count later.)*
7. Once it finishes: **~102 tests generated, 100% automatable coverage** for
   this first half of the RFC (out of 107 requirements, ~5 are flagged
   `not_independently_observable` — the coverage math already accounts for
   that, it's not a gap).

**Key line to land here:** "The model never wrote this Python file. It
returned JSON — test type, reasoning, steps, a list of checks. This
template is the only thing that turns that into doc + pytest text. If the
JSON doesn't validate, generation falls back to a heuristic template
instead of shipping something untrusted."

---

## 6. Run the tests for real — mocked PyEZ (~3 min)

Generating a test is only half the story. This step proves the pytest
stubs aren't just plausible-looking text — they actually execute.

1. Go to the **Test Catalog** tab and click **"Run deduplicated tests."**
   *(Terminal alternative: `make run-tests`)*
2. While it runs (a few seconds — this is a real `pytest` subprocess, not
   a simulation), narrate: "This is running every test in the
   deduplicated catalog against a mocked Junos device — `jnpr.junos`
   isn't even a real dependency of this project, so `pyez/mock_device.py`
   stands in for it. A `conftest.py`, rewritten every time the
   deduplicated folder refreshes, swaps in `MockJunosDevice` and a mocked
   `Config` before pytest ever imports the test files. No lab, no SSH, no
   NETCONF connection — genuinely running the Python, not faking a
   result."
3. When it finishes, point out the **Total / Passed / Failed / Errored**
   stat row and the per-test results table below it.
4. **Say while looking at the results:** "Zero errored is the number that
   matters most here — if the mock wiring were broken, every single test
   would error out with an import failure. What you're seeing instead is
   real pass/fail based on real assertions running against mock data."
5. If anything failed, open it in the results table and read the message.
   **Be upfront about why**, don't dodge it: the mock returns *plausible*
   default values (session Established/Full, a placeholder AS number...)
   for common field patterns — it does not simulate real protocol
   behavior. A test that checks a route is genuinely *absent*, or that a
   session was rejected because of a malformed message a peer emulator
   would have had to construct, can fail against the mock without that
   meaning anything about real conformance. That's exactly what
   `requires_peer_emulator` in the catalog already flags — those are the
   ones that need a real lab (or ExaBGP/Scapy) for a real signal, not this
   mock.

---

## 7. Ingest RFC 4271 — Part 2: new knowledge lands on existing tests (~2 min)

This is where the story stops being "ingest once, use forever" and becomes
"knowledge can keep growing without losing what you already built."

1. Go back to the **Knowledge Base** tab → **Knowledge library** panel.
   Click **"Ingest"** next to `rfc4271_part2_sections6.4-10.txt`.
   *(Terminal alternative: `make ingest FILE=rfc4271_part2_sections6.4-10.txt`)*
2. Watch the toast/status message closely — it doesn't just say how many
   new requirements were added:
   > *Ingested rfc4271_part2_sections6.4-10.txt: 96 new requirement(s), N
   > existing test(s) flagged for a refresh.*
3. **Say while pointing at that message:** "96 new requirements just landed
   — the rest of the FSM, UPDATE message handling, timers. But look at the
   second number: some tests we already generated, from the *first* half of
   the RFC, just got flagged. Not because they're wrong — because the
   retrieval index that fed their reasoning just changed shape. New,
   semantically related material showed up for a requirement we already
   built a test for, and this tool knows that, instead of quietly leaving
   that test's reasoning stale."
4. Requirements total now reads **203** — the complete RFC — with coverage
   temporarily dipping, since 96 new requirements just arrived with no tests
   yet.

---

## 8. Generate again — new tests *and* modified tests (~4-6 min)

This is the payoff for the whole incremental story, and the direct answer
to "show me knowledge actually being added, not just re-labeled."

1. Go to **Gap Analysis** → **"Generate all remaining gaps"** again.
   *(Terminal alternative: `make generate-all`)*
2. While it runs, narrate: "This single action is doing two things now.
   It's filling every gap opened by the 96 requirements we just added — same
   as before. But it's also re-running generation for every test flagged in
   the last step, with the fuller context those requirements now provide."
3. When it finishes, read the status line out loud — it now reports **both**
   numbers:
   > *Done — 94 test(s) created, N modified.*
4. Open the **Test Catalog** tab. Point out:
   - New rows for the second half of the RFC, same as any other batch.
   - A **`modified`** pill on rows that existed *before* this batch — hover
     it to see the regeneration timestamp. Filter by **batch** and note a
     modified test still carries its **original** batch number: it wasn't
     duplicated into a new entry, the same test got refreshed in place.
5. Click into one modified test and re-read its protocol reasoning —
   nothing about the test's identity (test_id, requirement_id) changed, only
   its content, because it now had more of the RFC to reason against.
6. Optional, quick close: click **"Run deduplicated tests"** one more time —
   the full merged catalog (originals, new, and modified alike) still runs
   for real against the mock.

**Key line to land here:** "That's items your peers and director specifically
asked for: prove new knowledge gets added, prove it's visible, and prove
existing work gets updated when it should be — not just a bigger number at
the end. Nothing about this required re-ingesting the whole RFC from
scratch, and nothing about it touched a test that didn't actually need a
second look."

---

## 9. Prove later runs never re-touch the RFC text (~4 min)

1. Go back to the **Knowledge Base tab → activity log**. Scroll through it:
   exactly **two** `RFC_INGESTED_INCREMENTAL` events (one per library file),
   no matter how many `TESTS_GENERATED`/`TESTS_MODIFIED` events have
   happened since. Each ingestion event is logged with its own source —
   `kb/rfc_library/rfc4271_part1_sections1-6.3.txt`,
   `kb/rfc_library/rfc4271_part2_sections6.4-10.txt` — and every generation
   event's source reads `kb/knowledge.db ONLY -- raw RFC text not reopened`.
2. **Restart the server** to show it doesn't re-parse on every boot:
   ```
   Ctrl+C
   make run
   ```
   Use plain `make run` this time (not `run-empty`) — the bootstrap check
   (`if count == 0`) sees requirements already in the DB and skips straight
   to `app.run()`, so it behaves identically either way once there's data.
   The console prints nothing about ingestion. Reload the browser: same 203
   requirements, same 196-ish test catalog, instantly — no re-parse delay.
3. **Optional, for a technical audience — the convincing version:**
   temporarily move the two source files this demo actually used out of the
   way, and prove the app doesn't need them anymore:
   ```bash
   mv backend/kb/rfc_library/rfc4271_part1_sections1-6.3.txt /tmp/
   mv backend/kb/rfc_library/rfc4271_part2_sections6.4-10.txt /tmp/
   ```
   Restart the app again, reload the dashboard, generate one more test from
   the Gap Analysis tab — all of it still works, and the Knowledge Library
   panel now shows an empty list (nothing to scan in that folder), while the
   knowledge already ingested from those files is completely unaffected.
   Then put the files back immediately:
   ```bash
   mv /tmp/rfc4271_part1_sections1-6.3.txt backend/kb/rfc_library/
   mv /tmp/rfc4271_part2_sections6.4-10.txt backend/kb/rfc_library/
   ```

---

## 10. Protocol-agnostic support — ingest OSPF instead of BGP (~5 min)

Everything so far — category classification, the Junos config template,
the PyEZ observation point, even the AI's system prompt — has been
protocol-aware, not hardcoded to BGP. Prove it by swapping in OSPF.

> **Heads up:** this step uses the *other* ingestion path on purpose — the
> paste-text form, not the Knowledge Library. That form **replaces** the
> entire knowledge base and clears every generated test, unlike the additive
> library ingest you just watched twice. Both exist deliberately: replace
> for swapping to a wholly different RFC/protocol, additive for growing
> knowledge of the same one. This is exactly what you backed up in step 1 —
> do this last.

1. Go to **Knowledge Base tab → "Ingest a different RFC."**
2. Fill in:
   - **RFC number:** `2328`
   - **RFC title:** `OSPF Version 2`
   - **Protocol:** leave on *"Auto-detect protocol"* (it'll correctly
     resolve to OSPF from the number/title) — or explicitly pick *"Force:
     OSPF"* to show the override control exists.
3. Paste this into the "paste the full RFC text" textarea (a short,
   realistic OSPF-2328-style excerpt — enough to extract several real
   requirements across different categories without waiting on the full
   RFC):

   ```
   1. Introduction

      This document describes OSPF version 2.

   2. The Link-State Database

      Each router MUST originate a router-LSA describing its links.  The
      router SHOULD flood this LSA to all adjacent neighbors within the
      area whenever the link state changes.

   3. Interface Hello Protocol

      A router MUST send Hello packets out each OSPF-enabled interface at
      HelloInterval seconds.  If a router does not receive a Hello packet
      from a neighbor within RouterDeadInterval seconds, it MUST declare
      the neighbor down.  On a broadcast network, the router SHOULD
      participate in Designated Router election.

   4. Shortest Path Calculation

      Each router MUST run the SPF (Dijkstra) algorithm over its Link
      State Database to compute the shortest-path tree.  The cost of a
      route SHOULD reflect the configured interface cost.

   5. Area Configuration

      A router MUST support configuration of the backbone area
      (0.0.0.0).  A router SHOULD support stub areas to reduce LSA
      flooding into the area.

   6. Authentication

      A router MUST support simple password authentication on a
      per-interface basis.
   ```

4. Click **"Re-ingest pasted text & rebuild knowledge base,"** confirm the
   replace-everything dialog.
5. Watch the header update: *"OSPF (Open Shortest Path First) —
   Conformance Coverage."* Point out the Coverage Matrix now shows OSPF
   categories — `neighbor_adjacency`, `lsa_flooding`, `spf_calculation`,
   `area_management`, `authentication` — not BGP's `path_attribute`/
   `fsm_state`. Also note the **Knowledge Library panel is untouched** —
   both RFC 4271 files still show as ingested-before, since that tracking
   lives independently of whichever RFC is currently loaded.
6. Generate a test for one of the new requirements (Gap Analysis → generate
   for a category). Open it and show, side by side with what you saw in
   step 5:
   - **Topology** now reads *"R1 ↔ R2, both in Area 0.0.0.0"* — no AS
     numbers.
   - **Timers** now read *Hello Interval / Dead Interval / Retransmit
     Interval* — not Hold/KeepAlive/ConnectRetry.
   - **The Junos config stanza** in the pytest tab is
     `protocols { ospf { area 0.0.0.0 { interface ... hello-interval ... } } }`
     — a real OSPF config, not BGP's `group EBGP-PEER`.
   - **The observation call** is `r1.rpc.get_ospf_neighbor_information()` —
     not the BGP RPC.
   - If you read the protocol-reasoning paragraph, it'll reference OSPF
     concepts specifically (router-LSAs, Router-ID, the LSDB, SPF) — proof
     the AI reasoning is protocol-aware too, not just the templates.

**Key line to land here:** "None of this required touching a line of the
generation code. Ingest, classification, templates, and the AI prompt all
read from a `ProtocolProfile` that's resolved automatically from the RFC
number and title. BGP and OSPF both have one today; anything else falls
back to a generic profile instead of silently assuming BGP."

---

## 11. After the demo — cleanup

Restore the real BGP state you backed up in step 1, so the environment is
ready for next time.

```bash
# Stop the app first.
Ctrl+C

# Restore the pre-demo knowledge base and generated tests, overwriting the
# OSPF state from step 10. Restoring knowledge.db also resets the Knowledge
# Library panel back to "not ingested" for both files -- nothing extra to
# clean up there, since that tracking lives inside this same file.
cp /tmp/poc_demo_backup/knowledge.db backend/kb/knowledge.db
cp /tmp/poc_demo_backup/retrieval_index.pkl backend/kb/retrieval_index.pkl
rm -rf backend/generated_tests/docs backend/generated_tests/pytest
cp -r /tmp/poc_demo_backup/generated_tests/docs backend/generated_tests/docs
cp -r /tmp/poc_demo_backup/generated_tests/pytest backend/generated_tests/pytest

# Confirm the restore worked, then rebuild the deduplicated view (and its
# conftest.py) from the restored DB -- don't try to cp -r it back from the
# backup, the DB is the source of truth and this regenerates it correctly.
cd backend && python3 -c "
import pipeline
print(pipeline.get_rfc_meta())
print(pipeline.get_coverage()['tests_generated'], 'tests restored')
print(pipeline.refresh_deduplicated_tests())
" && cd ..
```

Once you've confirmed the numbers match what you backed up (your numbers
will reflect whatever state you were in before this demo — check against
what step 1a actually backed up), you can remove the backup and any scratch
files:

```bash
rm -rf /tmp/poc_demo_backup
rm -f /tmp/rfc4271_part1_sections1-6.3.txt /tmp/rfc4271_part2_sections6.4-10.txt   # only if step 9's optional bonus left them behind
```

If you uploaded any sample files during an optional Gap Analysis / existing-
test-upload detour, remove them too so the next demo starts clean:

```bash
cd backend && python3 -c "
import pipeline
for t in pipeline.get_uploaded_tests():
    pipeline.delete_uploaded_test(t['id'])
for a in pipeline.get_artefacts():
    pipeline.delete_artefact(a['id'])
" && cd ..
```

Restart the app one last time (`make run`) and confirm the dashboard looks
exactly like it did before the demo started.

---

## Optional extensions (if time allows)

Not core to the things this script proves in steps 2-10, but available if
the audience wants more:

- **Run the whole incremental story from the terminal instead of clicking.**
  `make demo-incremental` runs steps 3/5 (part 1)/7/8 end to end as `curl`
  calls, printing each response — good for an engineering audience that
  wants to see the raw API contract (`created`/`modified`/
  `flagged_stale_test_ids`) instead of watching the dashboard.
- **Gap Analysis → upload an existing test.** Upload a sample pytest file,
  click Analyze, and show the AI deciding whether it *actually* verifies
  specific RFC requirements (not just topical overlap) — matched
  requirements move out of the "real gaps" list with a confidence + one-line
  rationale, never silently.
- **Run the OSPF tests too, right after step 10.** Click "Run deduplicated
  tests" again while OSPF is loaded — same mock, same button, same
  mechanism, and it'll pick OSPF-flavored default values for the mocked
  fields (`neighbor-state` → `"Full"`, etc.) with zero protocol-specific
  code in the runner itself. Good proof that the "run" feature is exactly
  as protocol-agnostic as generation is.
- **Peek at `backend/logs/generation.log`.** A real file on disk with
  every RFC ingest (both the additive and replace kind), every generated
  test's AI-backend-vs-heuristic-fallback outcome, every regeneration from
  the "modified tests" pass, and every test-run summary — useful if someone
  asks "how would I audit this after the fact, not just watch the dashboard
  live."
