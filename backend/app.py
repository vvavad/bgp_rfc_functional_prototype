"""
app.py — Flask backend for the RFC Conformance Test Generation prototype.

Run with:
    pip install -r requirements.txt
    python app.py
Then open http://localhost:5000
"""
from pathlib import Path
from dotenv import load_dotenv

BASE = Path(__file__).resolve().parent

# Load backend/.env first, before pipeline/ai_generation are imported --
# ai_generation.py reads ANTHROPIC_API_KEY/AI_MODEL at import time, so this
# must run before that import happens. See .env.example for the full list
# of variables the app reads.
load_dotenv(BASE / ".env")

from flask import Flask, jsonify, request, send_from_directory

import pipeline

FRONTEND_DIR = BASE.parent / "frontend"

app = Flask(__name__, static_folder=str(FRONTEND_DIR), static_url_path="")


# ------------------------------------------------------------------ #
# Frontend
# ------------------------------------------------------------------ #

@app.route("/")
def index():
    return send_from_directory(FRONTEND_DIR, "index.html")


# ------------------------------------------------------------------ #
# Read APIs
# ------------------------------------------------------------------ #

@app.get("/api/status")
def api_status():
    meta = pipeline.get_rfc_meta()
    cov = pipeline.get_coverage()
    return jsonify({
        "rfc": meta,
        "ai": pipeline.get_ai_status(),
        "coverage_summary": {
            "total_requirements": cov["total_requirements"],
            "tests_generated": cov["tests_generated"],
            "overall_coverage_pct": cov["overall_coverage_pct"],
            "automatable_coverage_pct": cov["automatable_coverage_pct"],
            "gap_count": cov["gap_count"],
        },
    })


@app.get("/api/coverage")
def api_coverage():
    return jsonify(pipeline.get_coverage())


@app.get("/api/matrix")
def api_matrix():
    return jsonify(pipeline.get_matrix())


@app.get("/api/requirements")
def api_requirements():
    conn = pipeline.get_conn()
    rows = conn.execute("SELECT * FROM requirements").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.get("/api/tests")
def api_test_catalog():
    return jsonify(pipeline.get_test_catalog())


@app.get("/api/tests/<test_id>")
def api_test_detail(test_id):
    detail = pipeline.get_test_detail(test_id)
    if not detail:
        return jsonify({"error": "not found"}), 404
    return jsonify(detail)


@app.get("/api/batches")
def api_batches():
    return jsonify(pipeline.get_batches())


@app.get("/api/ingestion-log")
def api_ingestion_log():
    return jsonify(pipeline.get_ingestion_log())


@app.get("/api/artefacts")
def api_artefacts():
    return jsonify(pipeline.get_artefacts())


@app.get("/api/search")
def api_search():
    q = request.args.get("q", "")
    k = int(request.args.get("k", 10))
    if not q:
        return jsonify([])
    return jsonify(pipeline.semantic_search(q, k))


# ------------------------------------------------------------------ #
# Write / action APIs
# ------------------------------------------------------------------ #

@app.post("/api/generate")
def api_generate():
    """Generate tests for a list of requirement IDs. This is the live
    equivalent of the 'reuse' demo -- it only ever reads requirements.py's
    DB tables, never the raw RFC text, no matter how many times it's called."""
    body = request.get_json(force=True) or {}
    requirement_ids = body.get("requirement_ids", [])
    batch_label = body.get("label", "manual")
    derived_from = body.get("derived_from", "")
    if not requirement_ids:
        return jsonify({"error": "requirement_ids is required"}), 400
    result = pipeline.generate_tests(requirement_ids, batch_label=batch_label, derived_from=derived_from)
    return jsonify(result)


@app.post("/api/generate-by-category")
def api_generate_by_category():
    """Convenience action: generate N tests for a given category, picking
    from the current gap list. Powers the 'Close this gap' button."""
    body = request.get_json(force=True) or {}
    category = body.get("category")
    count = int(body.get("count", 5))
    if not category:
        return jsonify({"error": "category is required"}), 400
    cov = pipeline.get_coverage()
    candidate_ids = [g["requirement_id"] for g in cov["gaps"] if g["category"] == category][:count]
    if not candidate_ids:
        return jsonify({"batch_id": None, "created": [], "skipped_already_covered": []})
    result = pipeline.generate_tests(candidate_ids, batch_label=f"gap-close:{category}")
    return jsonify(result)


@app.post("/api/generate-all")
def api_generate_all():
    """Bulk-fill every remaining automatable gap in one call -- the action
    that actually raises the out-of-the-box coverage number, as opposed to
    the demo-scale seed package or the 5-at-a-time per-category buttons.
    Optional {limit} caps how many gaps get filled in this call."""
    body = request.get_json(force=True) or {}
    limit = body.get("limit")
    result = pipeline.generate_all_gaps(batch_label="bulk-fill-all-gaps", limit=int(limit) if limit else None)
    return jsonify(result)


@app.post("/api/ingest")
def api_ingest():
    """Re-ingest a new RFC (pasted text). Replaces the current knowledge base
    and clears previously generated tests -- this is the one operation that
    reads raw RFC text; every other endpoint reads from the DB it produces."""
    body = request.get_json(force=True) or {}
    rfc_number = body.get("rfc_number", "").strip()
    rfc_title = body.get("rfc_title", "").strip()
    raw_text = body.get("raw_text", "")
    protocol = body.get("protocol", "").strip()
    if not rfc_number or not raw_text:
        return jsonify({"error": "rfc_number and raw_text are required"}), 400
    result = pipeline.ingest_rfc(rfc_number, rfc_title or f"RFC {rfc_number}", raw_text, "user-pasted text (UI)",
                                  protocol_override=protocol)
    return jsonify(result)


@app.post("/api/ingest/upload")
def api_ingest_upload():
    """Same as /api/ingest but the RFC text comes from an uploaded file
    (.txt/.md/.pdf) instead of a pasted textarea."""
    f = request.files.get("rfc_file")
    rfc_number = request.form.get("rfc_number", "").strip()
    rfc_title = request.form.get("rfc_title", "").strip()
    protocol = request.form.get("protocol", "").strip()
    if not rfc_number or not f or not f.filename:
        return jsonify({"error": "rfc_number and rfc_file are required"}), 400
    text, note = pipeline.extract_text_from_file(f.filename, f.read())
    if not text:
        return jsonify({"error": f"could not extract text from {f.filename}: {note}"}), 400
    result = pipeline.ingest_rfc(rfc_number, rfc_title or f"RFC {rfc_number}", text, f"uploaded file: {f.filename}",
                                  protocol_override=protocol)
    return jsonify(result)


@app.post("/api/artefacts/upload")
def api_artefacts_upload():
    """Upload a product spec or other supporting artefact (.txt/.md/.pdf).
    Extracted text is stored and handed to the AI as extra grounding context
    on every later test-generation call -- it never touches the RFC parser,
    only the AI reasoning step in ai_generation.py."""
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "file is required"}), 400
    artefact_type = request.form.get("artefact_type", "other")
    if artefact_type not in ("product_spec", "other"):
        artefact_type = "other"
    result = pipeline.save_artefact(artefact_type, f.filename, f.read())
    return jsonify(result)


@app.delete("/api/artefacts/<int:artefact_id>")
def api_artefacts_delete(artefact_id):
    return jsonify(pipeline.delete_artefact(artefact_id))


@app.get("/api/existing-tests")
def api_existing_tests():
    return jsonify(pipeline.get_uploaded_tests())


@app.post("/api/existing-tests/upload")
def api_existing_tests_upload():
    """Upload an existing test artifact (pytest source, a documented test
    case, an exported test-case list -- .txt/.md/.py/.pdf). Not reviewed yet;
    call /api/existing-tests/<id>/analyze to map it against RFC requirements."""
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "file is required"}), 400
    result = pipeline.save_uploaded_test(f.filename, f.read())
    return jsonify(result)


@app.post("/api/existing-tests/<int:uploaded_test_id>/analyze")
def api_existing_tests_analyze(uploaded_test_id):
    """Has the AI review this one uploaded test against a semantically
    narrowed set of RFC requirements, grounded with any uploaded product
    specs. Updates the Gap Analysis tab's real-gap computation."""
    result = pipeline.analyze_uploaded_test_coverage(uploaded_test_id)
    if "error" in result:
        return jsonify(result), 404
    return jsonify(result)


@app.post("/api/existing-tests/analyze-all")
def api_existing_tests_analyze_all():
    """Convenience bulk action: (re-)analyze every uploaded existing test."""
    results = [pipeline.analyze_uploaded_test_coverage(t["id"]) for t in pipeline.get_uploaded_tests()]
    return jsonify({"analyzed": results})


@app.delete("/api/existing-tests/<int:uploaded_test_id>")
def api_existing_tests_delete(uploaded_test_id):
    return jsonify(pipeline.delete_uploaded_test(uploaded_test_id))


if __name__ == "__main__":
    # Bootstrap: if the DB has no requirements yet, ingest the bundled RFC 4271
    # text once so the app starts with a working demo state.
    conn = pipeline.get_conn()
    count = conn.execute("SELECT COUNT(*) AS n FROM requirements").fetchone()["n"]
    conn.close()
    if count == 0:
        raw = (Path(__file__).resolve().parent / "kb" / "rfc4271_raw.txt").read_text(encoding="utf-8", errors="ignore")
        print("No requirements in knowledge base yet -- ingesting bundled RFC 4271...")
        res = pipeline.ingest_rfc("4271", "A Border Gateway Protocol 4 (BGP-4)", raw,
                                   "kb/rfc4271_raw.txt (bundled seed)")
        print(f"Ingested {res['requirement_count']} requirements.")
        conn2 = pipeline.get_conn()
        all_reqs = conn2.execute(
            "SELECT requirement_id, category FROM requirements WHERE testability='automatable' "
            "ORDER BY requirement_id"
        ).fetchall()
        conn2.close()
        # Stratified pick: up to 3 per category for a demo-scale, category-diverse seed package
        by_cat = {}
        for r in all_reqs:
            by_cat.setdefault(r["category"], []).append(r["requirement_id"])
        seed_ids = []
        for cat, ids in by_cat.items():
            seed_ids.extend(ids[:3])
        pipeline.generate_tests(seed_ids, batch_label="seed-demo-package")
        print(f"Generated seed demo package of {len(seed_ids)} tests across {len(by_cat)} categories.")

    app.run(host="0.0.0.0", port=5000, debug=False)
