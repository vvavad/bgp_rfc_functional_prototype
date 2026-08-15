# Ideas — Making the Demo More Awesome

Suggestions for future enhancements, ranked by impact-to-effort given what's
already built (RFC ingestion, AI-reasoned Test Intent generation,
deduplication, protocol-agnostic profiles for BGP/OSPF, and now real test
execution against a mocked PyEZ layer). None of these are implemented yet —
this is a backlog to pick from, not a plan.

## High-impact, fits what's already built

1. **Surface the retrieval context in the UI.** The AI already gets 3
   semantically-related requirements as grounding for every test — that's a
   real RAG story, but it's invisible right now (only used internally, see
   `pipeline.semantic_search` called from `_generate_one`). Showing "here's
   exactly what Claude saw" in the test detail modal would make that
   concrete instead of just described in `design.md`.
2. **Group mock test-run failures by why they failed.** `requires_peer_emulator`
   and `needs_review` already exist as signals on every test. A small panel
   that says "3 failed: 2 are flagged `requires_peer_emulator` (expected), 1
   is a real mock-limitation edge case" connects dots the presenter
   currently has to explain by hand after every "Run deduplicated tests"
   click.
3. **A 3rd protocol profile.** IS-IS is a natural next pick — same
   link-state family as OSPF, different enough to be a convincing third data
   point. Right now "protocol-agnostic" rests on 2 profiles (`bgp`, `ospf`
   in `protocol_profiles.py`); a 3rd, added live during a demo, is a much
   stronger claim than showing the same 2 every time.

## Good visual/narrative wins

4. **Stream the AI's reasoning live** in the UI while a test generates
   (even simple incremental text) instead of a static spinner — audiences
   respond much more to watching something "think" than waiting on a
   loader.
5. **A coverage-over-time chart** using the existing `ingestion_log`/batch
   timestamps — "watch the coverage number climb" is a much better live
   moment than a single static percentage.
6. **Side-by-side BGP vs. OSPF comparison view** — same requirement
   category, same template fields, rendered next to each other — reinforces
   the protocol-agnostic story faster than tab-switching between two full
   ingests.

## Bigger investments, if there's runway

7. **A real ExaBGP/Scapy peer-emulator integration** for at least one
   negative test end to end — this is the one remaining "deliberately
   stubbed" gap the README calls out by name (tests flagged
   `requires_peer_emulator` don't have generated emulator scripts). Closing
   even a single example would be a strong capstone.
8. **An optional real-lab mode** alongside the mock — if a vJunos-router/vMX
   is ever reachable, showing "same test, mock vs. real" side by side is
   the single most convincing thing this tool could do. Would mean
   swapping `pyez/mock_device.py` back out for real PyEZ + real device
   credentials for that run.
9. **Dockerize it** (`docker-compose up`) so the demo is a one-command
   reproducible setup on someone else's laptop, not dependent on this exact
   Python/venv setup.

## Where to start

#1 and #2 are the cheapest — both are backend data that's already computed
(retrieval context, `requires_peer_emulator`/`needs_review` flags), just not
surfaced in the UI yet — before reaching for bigger swings like the peer
emulator or real-lab mode.
