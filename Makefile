# Makefile for the RFC Conformance Test Generation prototype.
#
# Two ways to run the server:
#   make run        -- normal path, auto-seeds the bundled RFC 4271 text on
#                       first launch (see README).
#   make run-empty  -- starts with a genuinely empty knowledge base instead
#                       (SKIP_SEED_BOOTSTRAP=1, see backend/app.py) -- use
#                       this before `make demo-incremental`, which drives
#                       every ingest itself via the knowledge-library API
#                       and needs the DB empty to show real "new" vs
#                       "modified" test counts.
#
# All `curl`-based targets assume the server is already running in another
# terminal (`make run` or `make run-empty`) -- they just call its API.

PYTHON  ?= python3
BASE_URL ?= http://localhost:5000/api
JSON     := $(PYTHON) -m json.tool

.PHONY: install run run-empty status coverage catalog matrix library \
        ingest generate-all run-tests demo-incremental reset-demo check-server

install:
	cd backend && $(PYTHON) -m pip install -r requirements.txt

run:
	cd backend && $(PYTHON) app.py

run-empty:
	cd backend && SKIP_SEED_BOOTSTRAP=1 $(PYTHON) app.py

check-server:
	@curl -sf $(BASE_URL)/status > /dev/null || \
	  (echo "Server not reachable at $(BASE_URL) -- start it first with 'make run' or 'make run-empty'." && exit 1)

status: check-server
	@curl -s $(BASE_URL)/status | $(JSON)

coverage: check-server
	@curl -s $(BASE_URL)/coverage | $(JSON)

catalog: check-server
	@curl -s $(BASE_URL)/tests | $(JSON)

matrix: check-server
	@curl -s $(BASE_URL)/matrix | $(JSON)

library: check-server
	@curl -s $(BASE_URL)/knowledge-library | $(JSON)

# Ingest one file from backend/kb/rfc_library/, e.g.:
#   make ingest FILE=rfc4271_part1_sections1-6.3.txt
ingest: check-server
	@if [ -z "$(FILE)" ]; then echo "Usage: make ingest FILE=<filename from 'make library'>"; exit 1; fi
	@curl -s -X POST "$(BASE_URL)/knowledge-library/$(FILE)/ingest" | $(JSON)

generate-all: check-server
	@curl -s -X POST $(BASE_URL)/generate-all -H "Content-Type: application/json" -d "{}" | $(JSON)

run-tests: check-server
	@curl -s -X POST $(BASE_URL)/tests/run | $(JSON)

# The item #6 demo end to end, purely via API calls: ingest half the RFC,
# generate tests, ingest the rest, generate again -- prints created vs.
# modified counts each time so the "new knowledge changed existing tests"
# story is visible without opening the UI. Run against a server started
# with `make run-empty` on a freshly `make reset-demo`'d knowledge base,
# or the counts won't be meaningful (see run-empty's note above).
demo-incremental: check-server
	@echo "--- Ingesting part 1 (sections 1-6.3, 107 requirements) ---"
	@curl -s -X POST "$(BASE_URL)/knowledge-library/rfc4271_part1_sections1-6.3.txt/ingest" | $(JSON)
	@echo "--- Generating tests (batch 1) ---"
	@curl -s -X POST $(BASE_URL)/generate-all -H "Content-Type: application/json" -d "{}" | $(JSON)
	@echo "--- Ingesting part 2 (sections 6.4-10, 96 requirements) ---"
	@curl -s -X POST "$(BASE_URL)/knowledge-library/rfc4271_part2_sections6.4-10.txt/ingest" | $(JSON)
	@echo "--- Generating tests again (batch 2) -- watch for non-empty 'modified' ---"
	@curl -s -X POST $(BASE_URL)/generate-all -H "Content-Type: application/json" -d "{}" | $(JSON)
	@echo "--- Final coverage ---"
	@curl -s $(BASE_URL)/status | $(JSON)

# Destructive: wipes the knowledge base, generated tests, and process log so
# demo-incremental can be re-run from a clean state. Never invoked by any
# other target automatically -- run it yourself, then start the server with
# `make run-empty` before `make demo-incremental`.
reset-demo:
	rm -f backend/kb/knowledge.db backend/kb/retrieval_index.pkl
	rm -rf backend/generated_tests/docs backend/generated_tests/pytest backend/generated_tests/deduplicated
	rm -f backend/logs/generation.log
	mkdir -p backend/generated_tests/docs backend/generated_tests/pytest
	@echo "Knowledge base and generated tests cleared. Start the server with 'make run-empty' for the incremental demo, or 'make run' for the normal seeded start."
