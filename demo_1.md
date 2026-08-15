# Demo Script — RFC Conformance Test Generation POC

A walkthrough script for demoing this prototype live. Covers, in order:
**RFC ingestion → Test Intent generation → actual pytest generation →
running those tests for real against mocked PyEZ → proof that later runs
never re-touch the raw RFC text → protocol-agnostic support (BGP + OSPF)**.
Includes cleanup before the first run and after the demo, so the
environment is left exactly as it was found.

Total runtime: ~25-30 minutes at a comfortable pace.

---

## 0. What you're proving, in one paragraph

RFC text gets parsed into individual normative requirements **exactly once**
(`kb/knowledge.db`). Every later action — listing requirements, computing
coverage, generating a test — reads from that database, never the raw RFC
text again. For each requirement, Claude reasons about the actual protocol
mechanics and returns a structured **Test Intent** (never free-form code),
which a deterministic template renderer compiles into two artifacts: a
Markdown doc (the "test intent file") and a pytest/PyEZ stub (the "actual
test"). None of this needs a provisioned `ANTHROPIC_API_KEY` — it runs
through the local Claude Code session's own login. Duplicate tests (mostly
a heuristic-fallback artifact) get collapsed into a separate curated
folder, and **that curated folder actually runs** — a real `pytest`
execution against a mocked PyEZ layer, no lab required, proving this isn't
just text generation. And the whole pipeline is protocol-agnostic under
the hood: BGP is the flagship, but OSPF works today too, through the same
code path.

---

## 1. Before you start — pre-demo cleanup

The OSPF section of this demo (step 7) **replaces the knowledge base**,
which wipes the current BGP requirements/tests. Back everything up first so
you can restore the real BGP demo state afterward — don't skip this.

```bash
cd backend

# 1a. Back up the current runtime state (safe even if you've never run
#     the app before — cp -r on a missing source dir/file is a no-op error
#     you can ignore).
mkdir -p /tmp/poc_demo_backup
cp kb/knowledge.db /tmp/poc_demo_backup/ 2>/dev/null
cp kb/retrieval_index.pkl /tmp/poc_demo_backup/ 2>/dev/null
rm -rf /tmp/poc_demo_backup/generated_tests
cp -r generated_tests /tmp/poc_demo_backup/

# 1b. Confirm dependencies are installed.
pip install -r requirements.txt

# 1c. Confirm an AI backend will actually be used, not the heuristic
#     fallback -- if this prints ai_available: True with backend
#     claude-code-cli (or anthropic-api if you're running standalone with
#     a key), you're good. If it prints False, the demo still works but
#     every test will be watermarked "heuristic-fallback" instead of
#     showing real AI reasoning -- fix this before presenting.
python3 -c "
import pipeline
print(pipeline.get_ai_status())
"
```

**Decide how "fresh" you want the opening to look:**

- **Option A — start from a clean slate (recommended for a first-time
  audience):** delete `kb/knowledge.db` and `kb/retrieval_index.pkl` so the
  app's bootstrap ingest runs live when you start it in step 2. This is the
  most convincing way to show ingestion actually happening, not just
  already-done.
  ```bash
  rm -f kb/knowledge.db kb/retrieval_index.pkl
  rm -f generated_tests/docs/*.md generated_tests/pytest/*.py
  rm -f generated_tests/deduplicated/docs/*.md generated_tests/deduplicated/pytest/*.py
  ```
  (The deduplicated folder gets fully rebuilt — including a fresh
  `conftest.py` for the mock — the moment step 2's bootstrap generates the
  seed package, so this line is just for a clean look at the folder before
  you start; it isn't load-bearing.)
- **Option B — keep today's already-seeded state** (203 requirements, 77
  tests, 39.3% automatable coverage) and skip straight to generating *more*
  tests live in step 4. Faster, but you don't get to show the first-run
  ingestion log message. If you pick this, skip the `rm -f` above.

This script assumes **Option A**.

---

## 2. Start the app — RFC ingestion happens here

```bash
python app.py
```

Watch the console. On an empty knowledge base you'll see:

```
No requirements in knowledge base yet -- ingesting bundled RFC 4271...
Ingested 203 requirements.
Generated seed demo package of ~28 tests across 10 categories.
```

**Say while this runs:** "That's the entire RFC 4271 text — the actual BGP
spec — being parsed right now: split into sections, every MUST/SHOULD/MAY
statement pulled out as its own requirement, classified by category. This
happens exactly once. Everything from here on reads from the database this
just built, not the RFC text file."

Open **http://localhost:5000**.

---

## 3. Tour the dashboard (~3 min)

- **Header:** RFC title/number, and the AI badge — point out it says
  *"AI reasoning active · &lt;model&gt; · via local Claude Code · no API
  key"*. This is the key-less AI path — no `ANTHROPIC_API_KEY` was
  provisioned for this demo; if you're running standalone with a key
  instead, it'll say "via Anthropic API key" there instead.
- **Overview tab:** coverage-by-category chart, test-type distribution,
  and the **Generation Quality** panel — confidence distribution, so a big
  batch of tests never quietly means a big batch of *unreliable* tests.
- **Knowledge Base tab → activity log:** exactly one `RFC_INGESTED` event.
  Keep this tab open — you'll come back to it in step 6.
- **Test Catalog tab:** note the **"Run deduplicated tests"** button near
  the bottom — you'll use it in step 5, once there's a generated test to
  point it at.

---

## 4. Generate a test live — Test Intent → actual test (~5 min)

1. Go to **Gap Analysis** (or **Coverage Matrix**, click any non-empty
   cell).
2. Pick a category with gaps (e.g. `path_attribute` or `error_handling`)
   and click **"Generate 5 tests for this category."**
3. While it's running (a few seconds to ~30s per test, depending on the
   backend), narrate: "Right now, for each requirement, we're pulling the
   requirement text plus 3 semantically related requirements from the same
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
     line whenever it passes a static safety check (every variable it
     references is guaranteed to actually exist — no more, no less;
     confidence no longer gates this), otherwise left as a commented
     suggestion with `needs_review` set.

**Key line to land here:** "The model never wrote this Python file. It
returned JSON — test type, reasoning, steps, an assertion hint. This
template is the only thing that turns that into doc + pytest text. If the
JSON doesn't validate, generation falls back to a heuristic template
instead of shipping something untrusted."

---

## 5. Run the tests for real — mocked PyEZ (~4 min)

Generating a test is only half the story. This step proves the pytest
stubs aren't just plausible-looking text — they actually execute.

1. Go to the **Test Catalog** tab and click **"Run deduplicated tests."**
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

**Key line to land here:** "This closes the loop: RFC text in, a
runnable, executing test out — and you can point at the two or three
places in this whole pipeline where a human should still look before
trusting a result: `needs_review`, `requires_peer_emulator`, and now,
whether a test passed against the mock for the right reason."

---

## 6. Prove later runs never re-touch the RFC text (~5 min)

1. Go back to the **Knowledge Base tab → activity log**. Scroll through it:
   still exactly **one** `RFC_INGESTED` event, no matter how many
   `TESTS_GENERATED` events have happened since. Each one is logged with
   source `kb/knowledge.db ONLY -- raw RFC text not reopened`.
2. **Restart the server** to show it doesn't re-parse on every boot:
   ```
   Ctrl+C
   python app.py
   ```
   The console this time prints nothing about ingestion — the bootstrap
   check (`if count == 0`) sees requirements already in the DB and skips
   straight to `app.run()`. Reload the browser: same coverage numbers, same
   catalog, instantly — no re-parse delay.
3. **Optional, for a technical audience — the convincing version:**
   temporarily move the source RFC file out of the way and prove the app
   doesn't need it anymore:
   ```bash
   mv kb/rfc4271_raw.txt /tmp/rfc4271_raw.txt.bak
   ```
   Restart the app again, reload the dashboard, generate one more test from
   the Gap Analysis tab — all of it still works. Then put the file back
   immediately (it's still needed for a *future* from-scratch bootstrap on
   an empty DB):
   ```bash
   mv /tmp/rfc4271_raw.txt.bak kb/rfc4271_raw.txt
   ```

---

## 7. Protocol-agnostic support — ingest OSPF instead of BGP (~5 min)

Everything so far — category classification, the Junos config template,
the PyEZ observation point, even the AI's system prompt — has been
protocol-aware, not hardcoded to BGP. Prove it by swapping in OSPF.

> **Heads up:** this step replaces the current knowledge base and clears
> every generated test — that's exactly what you backed up in step 1. Do
> this last.

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
   `fsm_state`.
6. Generate a test for one of the new requirements (Gap Analysis → generate
   for a category). Open it and show, side by side with what you saw in
   step 4:
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

## 8. After the demo — cleanup

Restore the real BGP state you backed up in step 1, so the environment is
ready for next time.

```bash
# Stop the app first.
Ctrl+C

cd backend

# Restore the pre-demo knowledge base and generated tests, overwriting the
# OSPF state from step 7.
cp /tmp/poc_demo_backup/knowledge.db kb/knowledge.db
cp /tmp/poc_demo_backup/retrieval_index.pkl kb/retrieval_index.pkl
rm -rf generated_tests/docs generated_tests/pytest
cp -r /tmp/poc_demo_backup/generated_tests/docs generated_tests/docs
cp -r /tmp/poc_demo_backup/generated_tests/pytest generated_tests/pytest

# Confirm the restore worked, then rebuild the deduplicated view (and its
# conftest.py) from the restored DB -- don't try to cp -r it back from the
# backup, the DB is the source of truth and this regenerates it correctly.
python3 -c "
import pipeline
print(pipeline.get_rfc_meta())
print(pipeline.get_coverage()['tests_generated'], 'tests restored')
print(pipeline.refresh_deduplicated_tests())
"
```

Once you've confirmed the numbers match what you backed up (RFC 4271, BGP,
77 tests / 39.3% automatable coverage as of this writing — your numbers may
differ if you've generated more since), you can remove the backup and any
scratch files:

```bash
rm -rf /tmp/poc_demo_backup
rm -f /tmp/rfc4271_raw.txt.bak   # only if step 6's optional bonus left it behind
```

If you uploaded any sample files during an optional Gap Analysis / existing-
test-upload detour, remove them too so the next demo starts clean:

```bash
# Only needed if you used the Gap Analysis tab's upload feature during the demo.
python3 -c "
import pipeline
for t in pipeline.get_uploaded_tests():
    pipeline.delete_uploaded_test(t['id'])
for a in pipeline.get_artefacts():
    pipeline.delete_artefact(a['id'])
"
```

Restart the app one last time and confirm the dashboard looks exactly like
it did before the demo started.

---

## Optional extensions (if time allows)

Not core to the things this script proves in steps 2-7, but available if
the audience wants more:

- **Gap Analysis → upload an existing test.** Upload a sample pytest file,
  click Analyze, and show the AI deciding whether it *actually* verifies
  specific RFC requirements (not just topical overlap) — matched
  requirements move out of the "real gaps" list with a confidence + one-line
  rationale, never silently.
- **Semantic search (Overview tab).** Type a natural-language query (e.g.
  "hold timer expired") and show the same TF-IDF retrieval index that
  powers AI generation context, exposed directly.
- **Bulk-fill remaining gaps.** Gap Analysis → "Generate all remaining
  gaps" — mention this is the on-demand action used to raise coverage
  before a demo, run concurrently across several requirements at once
  rather than one at a time.
- **Run the OSPF tests too, right after step 7.** Click "Run deduplicated
  tests" again while OSPF is loaded — same mock, same button, same
  mechanism, and it'll pick OSPF-flavored default values for the mocked
  fields (`neighbor-state` → `"Full"`, etc.) with zero protocol-specific
  code in the runner itself. Good proof that the "run" feature is exactly
  as protocol-agnostic as generation is.
- **Peek at `backend/logs/generation.log`.** A real file on disk with
  every RFC ingest, every generated test's AI-backend-vs-heuristic-fallback
  outcome, and every test-run summary — useful if someone asks "how would
  I audit this after the fact, not just watch the dashboard live."
