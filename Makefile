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
#
# `make install` creates an isolated virtualenv at backend/.venv and installs
# backend/requirements.txt into it -- required, not optional, on any modern
# Debian/Ubuntu (including WSL2 Ubuntu 24.04+): those ship pip in "externally
# managed environment" mode (PEP 668), so a bare `pip install` fails outright
# with that exact error instead of quietly working. `run`/`run-empty` and
# every target below that needs the app's own code (ai-status, dedup-refresh,
# clear-uploads) use this same venv's interpreter (VENV_PYTHON below), not
# whatever `python3` happens to resolve to on PATH -- a bare system python3
# has none of backend/requirements.txt's packages (Flask, scikit-learn,
# python-dotenv, ...) installed. demo_1.md has no raw `python3 -c` left for
# exactly this reason -- everything routes through a target here instead.

PYTHON      ?= python3
VENV_DIR    := backend/.venv
VENV_PYTHON := $(VENV_DIR)/bin/python3
BASE_URL    ?= http://localhost:5000/api
# json.tool is stdlib -- pretty-printing curl output needs no project
# dependencies, so this uses the plain system interpreter (always present)
# rather than requiring 'make install' just to look at a response.
JSON        := $(PYTHON) -m json.tool

.PHONY: install run run-empty status coverage catalog matrix library \
        ingest generate-category generate-all run-tests demo-incremental \
        reset-demo check-server check-venv ai-status dedup-refresh clear-uploads

install:
	$(PYTHON) -m venv --clear $(VENV_DIR)
	$(VENV_PYTHON) -m pip install --upgrade pip
	$(VENV_PYTHON) -m pip install -r backend/requirements.txt
	@echo "Installed into $(VENV_DIR) -- 'make run'/'make run-empty' use this automatically."

check-venv:
	@test -x $(VENV_PYTHON) || \
	  (echo "$(VENV_PYTHON) not found -- run 'make install' first." && exit 1)

# No `cd backend` here -- app.py/pipeline.py resolve every path (kb/,
# generated_tests/, .env) relative to their own __file__, not the process's
# cwd, so invoking the venv's interpreter directly from the repo root works
# identically and avoids relative-path confusion between VENV_PYTHON
# (repo-root-relative) and a `cd`'d shell.
run: check-venv
	$(VENV_PYTHON) backend/app.py

run-empty: check-venv
	SKIP_SEED_BOOTSTRAP=1 $(VENV_PYTHON) backend/app.py

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

# Generate N tests (default 5) for one gap category, e.g.:
#   make generate-category CATEGORY=path_attribute
#   make generate-category CATEGORY=error_handling COUNT=10
generate-category: check-server
	@if [ -z "$(CATEGORY)" ]; then echo "Usage: make generate-category CATEGORY=<name from 'make coverage'> [COUNT=5]"; exit 1; fi
	@curl -s -X POST $(BASE_URL)/generate-by-category -H "Content-Type: application/json" \
	  -d "{\"category\": \"$(CATEGORY)\", \"count\": $(if $(COUNT),$(COUNT),5)}" | $(JSON)

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

# `-c` snippets need backend/ on PYTHONPATH explicitly -- unlike running
# backend/app.py as a script (where Python puts the script's own directory
# on sys.path automatically), `python3 -c "..."` puts the CURRENT WORKING
# DIRECTORY on sys.path instead, which is the repo root for every command
# in this Makefile, not backend/ -- so `import pipeline` would fail without
# this even though the exact same import works fine inside app.py itself.
PY_C := PYTHONPATH=backend $(VENV_PYTHON) -c

# Pre-flight check: confirms an AI backend will actually answer (not the
# heuristic fallback) before you start presenting. Doesn't need the server
# running -- imports pipeline.py directly.
ai-status: check-venv
	@$(PY_C) "import pipeline; print(pipeline.get_ai_status())"

# Rebuilds generated_tests/deduplicated/{docs,pytest} (and its conftest.py)
# from whatever's currently in kb/knowledge.db -- needed after manually
# restoring knowledge.db from a backup (cp doesn't touch the deduplicated
# view, it has to be recomputed), and prints the restored RFC/test count so
# you can confirm the restore actually matches what you backed up.
dedup-refresh: check-venv
	@$(PY_C) "\
import pipeline; \
print(pipeline.get_rfc_meta()); \
print(pipeline.get_coverage()['tests_generated'], 'tests in catalog'); \
print(pipeline.refresh_deduplicated_tests())"

# Removes every uploaded existing-test/artefact -- only needed if you used
# the Gap Analysis tab's optional upload feature during a demo and want the
# next run to start clean (see demo_1.md step 11).
clear-uploads: check-venv
	@$(PY_C) "\
import pipeline; \
[pipeline.delete_uploaded_test(t['id']) for t in pipeline.get_uploaded_tests()]; \
[pipeline.delete_artefact(a['id']) for a in pipeline.get_artefacts()]; \
print('uploaded tests/artefacts cleared')"

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
