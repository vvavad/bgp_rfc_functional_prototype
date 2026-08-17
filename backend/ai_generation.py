"""
ai_generation.py — AI-powered Test Intent generation.

Replaces the hardcoded STEPS_BY_TYPE/ASSERTION_BY_TYPE lookup tables with an
actual model call that reasons about the specific normative statement: what
BGP/Junos-specific stimulus proves it, whether Junos itself can even exhibit
the condition or a peer emulator is required, and what the precise pass/fail
assertion should be.

Design choice (mirrors the "Test Intent first" pattern from the proposal):
the model NEVER writes free-form Python. It returns a structured, schema-
validated Test Intent (JSON). pipeline.py's deterministic template renderer
is still the only thing that produces doc/pytest text. This keeps a human-
reviewable, non-hallucinated boundary between "AI reasoning" and "executed
code" -- if the model's JSON doesn't validate, generation falls back to the
heuristic path rather than emitting untrusted code.

A Test Intent carries a LIST of independent "checks" (description +
assertion_code), not a single assertion -- real conformance tests usually
need to verify more than one fact (session state AND a specific attribute
value AND the absence of some condition), and collapsing all of that into
one combined boolean expression is exactly what made early tests feel
"basic": most ended up with nothing but a generic state check because
there was only ever one assertion slot to begin with. Each check is
promoted to an executable `assert` line independently, gated on safety,
not confidence: any check whose assertion_code passes
_safe_assertion_expr (no imports/dangerous calls, and every name it
references is guaranteed to exist -- see known_names) gets promoted,
regardless of confidence, so a real check runs instead of nothing -- a
check that fails validation stays a commented note while its sibling
checks in the same test still run. Confidence still drives needs_review
in the catalog independently of any of this -- a safe-to-run assertion
from a low-confidence Test Intent is still flagged for a human to
double-check the reasoning behind it, even though it's guaranteed not to
crash.
"""
import os
import re
import ast
import json
import logging
import threading
from pathlib import Path
from string import Template

from dotenv import load_dotenv

import ai_backends

# Load backend/.env before reading any environment variable below, so the
# app always picks up ANTHROPIC_API_KEY / AI_MODEL / AI_BACKEND from .env
# regardless of which module gets imported first or how the process was
# started. Real process environment variables (if already set) still take
# precedence.
load_dotenv(Path(__file__).resolve().parent / ".env")

logger = logging.getLogger(__name__)

AI_MODEL = os.environ.get("AI_MODEL", "claude-sonnet-5")

# Which backend(s) to try, in order. "auto" (default) prefers the local
# Claude Code CLI -- already authenticated via that session's own login, no
# separate key to provision -- and falls back to a direct Anthropic API call
# (ANTHROPIC_API_KEY) for standalone runs outside Claude Code. "cli" / "api"
# force one path only (and count as unavailable, not an error, if that path
# isn't reachable); "heuristic" disables AI entirely regardless of what's
# configured, useful for demoing/inspecting the fallback path on purpose.
AI_BACKEND_MODE = os.environ.get("AI_BACKEND", "auto").strip().lower()

_backend = None
_backend_checked = False
_backend_lock = threading.Lock()


def _select_backend():
    """Lazy singleton, resolved at most once even under concurrent callers
    (pipeline.generate_tests fans out across a thread pool -- without the
    lock, two threads racing the first call could both see
    _backend_checked flip True before _backend is assigned and incorrectly
    fall back to heuristic). Returns None (and logs once) if no backend is
    available under the configured AI_BACKEND_MODE -- callers must treat
    that as 'use the heuristic path'."""
    global _backend, _backend_checked
    if _backend_checked:
        return _backend
    with _backend_lock:
        if _backend_checked:
            return _backend
        _backend_checked = True
        _resolve_backend()
    return _backend


def _resolve_backend():
    global _backend
    cli_backend = ai_backends.ClaudeCodeCLIBackend(model=AI_MODEL)
    api_backend = ai_backends.AnthropicAPIBackend(model=AI_MODEL)

    if AI_BACKEND_MODE == "cli":
        _backend = cli_backend if cli_backend.available() else None
    elif AI_BACKEND_MODE == "api":
        _backend = api_backend if api_backend.available() else None
    elif AI_BACKEND_MODE == "heuristic":
        _backend = None
    else:
        if AI_BACKEND_MODE != "auto":
            logger.warning(f"Unrecognized AI_BACKEND={AI_BACKEND_MODE!r} -- treating as 'auto'.")
        if cli_backend.available():
            _backend = cli_backend
        elif api_backend.available():
            _backend = api_backend
        else:
            _backend = None

    if _backend is None:
        logger.warning(f"No AI backend available (AI_BACKEND={AI_BACKEND_MODE}) -- using heuristic templates.")
    else:
        logger.info(f"AI backend selected: {_backend.key}")
    return _backend


def ai_available() -> bool:
    return _select_backend() is not None


def get_active_backend_key():
    """'claude-code-cli' / 'anthropic-api' / None -- for status reporting
    and per-test provenance (see pipeline.get_ai_status / generate_tests)."""
    backend = _select_backend()
    return backend.key if backend else None


# Templated (string.Template, $-style) rather than a plain f-string/`.format()`
# target, deliberately -- the JSON schema block below is dense with literal
# { } characters that would collide with str.format()'s field-delimiter
# syntax (see pipeline.py's config_stanza rendering for the same issue hit
# and fixed with the same approach).
SYSTEM_PROMPT_TEMPLATE = Template("""You are a principal network protocol conformance test architect with deep, \
current expertise in routing protocols (BGP, OSPF, IS-IS, MPLS, EVPN, segment routing), \
Juniper Networks' Junos OS, PyEZ automation, YANG/NETCONF, and precise interpretation of IETF RFCs. \
You are currently reasoning about $expertise_note.

You are given ONE normative statement (a MUST/SHOULD/MAY-class requirement) extracted from an \
RFC, plus a few semantically related requirements from the same RFC for context. You may also be \
given excerpts from uploaded product specs or other reference material for the actual target \
device -- use that only to refine what the real implementation can/cannot do; the RFC statement \
remains the source of truth for what's normatively required. Your job is to design a conformance \
test for that specific statement -- not a generic template, but reasoning tied to the actual \
protocol mechanics it describes.

Think concretely about:
- Whether the target device (a conformant Junos implementation) would ever exhibit the invalid/edge \
  condition itself, or whether a scriptable peer/attacker ($emulator_tool_examples) is \
  required to construct the stimulus. Be honest about this -- Junos will not originate malformed \
  messages on request.
- What is externally, protocol-level observable (wire messages, NOTIFICATION codes, FSM state, \
  RIB/Adj-RIB contents, timers) versus purely internal/implementation-defined and therefore not \
  independently testable.
- The minimum topology needed (default is $topology_description, Junos vJunos-router \
  / vMX via PyEZ over NETCONF) -- only ask for more (e.g. a third router, a multi-area/mesh \
  topology, a route reflector) if the requirement genuinely needs it.
- The precise PyEZ/NETCONF observation point: which RPC or config/operational data would show the result \
  (e.g. $observation_example, a specific XML element).
- Whether the requirement implies MULTIPLE distinct, separately-verifiable facts (e.g. "the session \
  stays Established AND the specific attribute value is correct AND some other condition is absent") \
  rather than just one -- if so, that's several entries in "checks" below, not one combined condition.

IMPORTANT -- what data is actually available when you write a check's assertion_code:
The generated test ALWAYS fetches one value for you automatically, before your checks run: \
$observation_example, stored in a Python variable called `$result_var`. That is the ONLY variable \
that exists unless you declare more. If the requirement needs to inspect something else \
(e.g. AS_PATH content, a specific route attribute, an LSA field) -- something $result_var alone \
can't show -- declare it in the "observations" array below instead of assuming a variable name \
exists. Each entry names a NEW variable, picks where to get it from, and gives the XPath to extract:
  - "source": "primary" pulls from the response already fetched for $result_var (a different field of the same RPC reply) -- no extra network call.
  - "source": one of these additional trusted calls available for this protocol -- $secondary_menu -- issues that call once and extracts your XPath from its response.
Declare at most 3 observations, shared across all your checks. Each check's assertion_code may ONLY \
reference `$result_var` and the variable names you declare in "observations" -- nothing else. If a \
specific check can't be expressed that way, leave that check's assertion_code as an empty string and \
say why in "notes" rather than inventing a variable that will never exist -- its description still \
stands as a documented, human-reviewable check even without executable code.

IMPORTANT -- "checks" is a LIST, not one combined assertion:
Prefer several small, independently-checkable conditions over one large ANDed boolean expression --
e.g. a session/adjacency-state check, a specific-field-value check, and (where the requirement implies
it) a negative/absence check, each as ITS OWN entry. Each is graded and promoted independently, so if
one can't be safely expressed the others still run. Declare at least 1 and at most 4 checks -- a
single well-justified check is completely fine for a genuinely simple requirement; don't pad with
redundant or trivial checks just to reach 4.

Respond with ONLY a single JSON object, no prose, no markdown fences, matching exactly this schema:
{
  "test_type": "positive" | "negative" | "boundary" | "policy" | "recovery",
  "risk": "high" | "medium" | "low",
  "confidence": "high" | "medium" | "low",
  "protocol_reasoning": "2-3 sentences of the specific protocol mechanics behind this test, in your own words",
  "requires_peer_emulator": true | false,
  "emulator_tool": $emulator_tool_enum,
  "topology_note": "short note on topology if it differs from the default, else empty string",
  "steps": ["specific step 1", "specific step 2", "specific step 3 (optional)"],
  "pyez_observation": "the specific PyEZ RPC call or XML field to inspect, e.g. $observation_example",
  "observations": [
    {"var_name": "snake_case_identifier", "source": "primary" or a key from the menu above, "xpath": "XPath string to extract with .findtext()"}
  ],
  "checks": [
    {"description": "precise, specific description of what this ONE check verifies",
     "assertion_code": "a single Python boolean expression referencing ONLY $result_var and/or your declared observations[].var_name (no imports, no function calls beyond simple attribute/dict access) -- empty string if no such expression is safely possible"}
  ],
  "notes": "any caveats, edge cases, or reasons for lower confidence"
}

Set confidence to "low" if the requirement is ambiguous, internally-scoped, or you are not sure \
Junos exposes the needed observation point. Do not inflate confidence to seem more useful.""")


def _build_system_prompt(profile) -> str:
    """profile is a protocol_profiles.ProtocolProfile for the RFC currently
    being reasoned about -- see pipeline.get_active_profile(). Keeps the
    deep multi-protocol expertise framing (still genuinely true regardless
    of which RFC is loaded) while making the topology/tooling defaults, the
    emulator_tool enum, and the available-variable/observations grounding
    reflect the active protocol instead of always assuming BGP."""
    tools = [] if profile.default_emulator_tool == "none" else profile.default_emulator_tool.split("/")
    emulator_tool_enum = " | ".join(f'"{t}"' for t in tools + ["none"])
    emulator_tool_examples = (", ".join(tools) + ", a raw protocol-speaking peer") if tools else \
        "a protocol-appropriate packet-crafting tool"
    if profile.secondary_observations:
        secondary_menu = ", ".join(f'"{k}" ({v})' for k, v in profile.secondary_observations.items())
    else:
        secondary_menu = "(none available for this protocol -- only \"primary\" is valid)"
    return SYSTEM_PROMPT_TEMPLATE.substitute(
        expertise_note=profile.expertise_note,
        emulator_tool_examples=emulator_tool_examples,
        topology_description=profile.topology_description,
        observation_example=profile.observation_hint(),
        emulator_tool_enum=emulator_tool_enum,
        result_var=profile.result_var,
        secondary_menu=secondary_menu,
    )


def _build_user_prompt(rfc_label, req_row, related, artefact_context=""):
    related_block = "\n".join(
        f"- [{r['requirement_id']}] ({r['keyword']}) {r['statement']}"
        for r in related
    ) or "(none found)"
    artefact_block = (
        f"\nAdditional grounding -- product/implementation context uploaded by the test engineer "
        f"(use this to refine what the ACTUAL target product supports; it does not override the "
        f"RFC statement above):\n{artefact_context}\n"
        if artefact_context else ""
    )
    return f"""RFC: {rfc_label}
Section: {req_row['section_id']} ({req_row['section_title']})
Category (heuristically pre-classified, you may override via test_type/reasoning): {req_row['category']}

TARGET REQUIREMENT [{req_row['requirement_id']}] ({req_row['keyword']}):
"{req_row['statement']}"

Related requirements from the same RFC (for context only, do not generate tests for these):
{related_block}
{artefact_block}
Design the conformance test intent for the TARGET REQUIREMENT only. Respond with the JSON object only."""


def _strip_fences(text: str) -> str:
    text = text.strip()
    text = re.sub(r'^```(json)?', '', text).strip()
    text = re.sub(r'```$', '', text).strip()
    return text


REQUIRED_KEYS = {"test_type", "risk", "confidence", "protocol_reasoning", "requires_peer_emulator",
                 "emulator_tool", "topology_note", "steps", "pyez_observation",
                 "checks", "notes"}

# "observations" is deliberately NOT in REQUIRED_KEYS -- it's new and optional
# so a response that omits it entirely (rather than sending an empty list)
# still validates; _validate_and_filter_observations treats a missing/
# malformed value as "no extra observations" rather than failing the intent.
MAX_OBSERVATIONS = 3
MAX_CHECKS = 4
_VALID_IDENTIFIER_RE = re.compile(r'^[a-z_][a-z0-9_]*$')
_RESERVED_VAR_NAMES = {"_info", "r1", "r2", "cu", "dev", "pytest", "Device", "Config",
                        "len", "str", "int", "None", "True", "False"}
_ALLOWED_BUILTIN_NAMES = {"len", "str", "int"}


def _validate_and_filter_observations(observations_raw, profile) -> list:
    """Keeps only well-formed observation declarations: a legal, non-reserved
    Python identifier for var_name, a source that's either "primary" or one
    of this protocol's secondary_observations keys, and a string xpath.
    Silently drops anything malformed instead of failing the whole intent --
    matches the existing "generation always succeeds" contract. Caps at
    MAX_OBSERVATIONS and de-dupes var_name collisions (first one wins)."""
    if not isinstance(observations_raw, list):
        return []
    valid_sources = {"primary"} | set(profile.secondary_observations.keys())
    reserved = _RESERVED_VAR_NAMES | {profile.result_var}
    seen_names = set()
    out = []
    for item in observations_raw:
        if len(out) >= MAX_OBSERVATIONS:
            break
        if not isinstance(item, dict):
            continue
        var_name, source, xpath = item.get("var_name"), item.get("source"), item.get("xpath")
        if not isinstance(var_name, str) or not _VALID_IDENTIFIER_RE.match(var_name):
            continue
        if var_name in reserved or var_name in seen_names:
            continue
        if source not in valid_sources:
            continue
        if not isinstance(xpath, str) or not xpath or len(xpath) > 300:
            continue
        seen_names.add(var_name)
        out.append({"var_name": var_name, "source": source, "xpath": xpath})
    return out


def _validate_and_filter_checks(checks_raw) -> list:
    """Keeps only well-formed check entries: a non-empty string
    "description" and a string "assertion_code" (may be empty -- that's a
    documented-but-not-executable check, still valuable). Drops malformed
    entries individually rather than failing the whole intent, and caps at
    MAX_CHECKS. Safety validation of assertion_code happens later, per
    check, in generate_ai_test_intent -- this function only enforces shape."""
    if not isinstance(checks_raw, list):
        return []
    out = []
    for item in checks_raw:
        if len(out) >= MAX_CHECKS:
            break
        if not isinstance(item, dict):
            continue
        description, assertion_code = item.get("description"), item.get("assertion_code", "")
        if not isinstance(description, str) or not description.strip():
            continue
        if not isinstance(assertion_code, str):
            assertion_code = ""
        out.append({"description": description.strip(), "assertion_code": assertion_code})
    return out


def _validate_intent(obj: dict, profile) -> bool:
    if not REQUIRED_KEYS.issubset(obj.keys()):
        return False
    if obj["test_type"] not in ("positive", "negative", "boundary", "policy", "recovery"):
        return False
    if obj["risk"] not in ("high", "medium", "low"):
        return False
    if obj["confidence"] not in ("high", "medium", "low"):
        return False
    if not isinstance(obj["steps"], list) or not obj["steps"]:
        return False
    # Filter in place -- pipeline.py and _safe_assertion_expr both read
    # obj["observations"] afterward and expect it to already be validated.
    obj["observations"] = _validate_and_filter_observations(obj.get("observations", []), profile)
    obj["checks"] = _validate_and_filter_checks(obj.get("checks", []))
    if not obj["checks"]:
        return False
    return True


def _safe_assertion_expr(code: str, known_names: set) -> bool:
    """Only promote AI-suggested assertion code to an executable assert line
    if it (a) parses as a single, simple boolean-ish expression with no
    calls to anything beyond attribute/subscript access, AND (b) references
    only variable names guaranteed to exist in the rendered test
    (known_names: the profile's result_var plus any validated
    observations[].var_name) -- otherwise the promoted assert would raise
    NameError at run time the moment someone tries to use it, which is
    exactly the bug this parameter closes. A lightweight allowlist, not a
    full sandbox. Anything that fails either check stays a commented
    suggestion instead."""
    if not code or len(code) > 200:
        return False
    try:
        tree = ast.parse(code, mode="eval")
    except SyntaxError:
        return False
    banned = (ast.Import, ast.ImportFrom, ast.Lambda)
    for node in ast.walk(tree):
        if isinstance(node, banned):
            return False
        if isinstance(node, ast.Call):
            # allow only simple calls like str.strip(), .lower(), len(), no arbitrary names
            if isinstance(node.func, ast.Attribute):
                if node.func.attr not in ("strip", "lower", "upper", "findtext", "get",
                                           "split", "count", "startswith", "endswith", "replace"):
                    return False
            elif isinstance(node.func, ast.Name):
                if node.func.id not in _ALLOWED_BUILTIN_NAMES:
                    return False
            else:
                return False
        if isinstance(node, ast.Name):
            if node.id in ("eval", "exec", "os", "sys", "open", "__import__"):
                return False
            if (isinstance(node.ctx, ast.Load) and node.id not in _ALLOWED_BUILTIN_NAMES
                    and node.id not in known_names):
                return False
    return True


def generate_ai_test_intent(rfc_label: str, req_row: dict, related: list, profile, artefact_context: str = ""):
    """Returns (intent_dict, mode_str) where mode_str is one of
    'ai-high' / 'ai-medium' / 'ai-low' / 'heuristic-fallback:<reason>'.
    profile is a protocol_profiles.ProtocolProfile (see
    pipeline.get_active_profile()) -- shapes the system prompt's topology/
    tooling defaults instead of always assuming BGP. artefact_context is
    optional grounding text built from uploaded product specs / other
    reference material (see pipeline.get_artefact_context)."""
    backend = _select_backend()
    if backend is None:
        return None, "heuristic-fallback:no-ai-backend"

    try:
        text = backend.complete(_build_system_prompt(profile),
                                 _build_user_prompt(rfc_label, req_row, related, artefact_context),
                                 max_tokens=1000)
        obj = json.loads(_strip_fences(text))
    except Exception as e:
        logger.warning(f"AI generation failed for {req_row['requirement_id']}: {e}")
        return None, f"heuristic-fallback:api-error"

    if not _validate_intent(obj, profile):
        logger.warning(f"AI response failed schema validation for {req_row['requirement_id']}")
        return None, "heuristic-fallback:invalid-schema"

    known_names = {profile.result_var} | {o["var_name"] for o in obj["observations"]}
    for check in obj["checks"]:
        check["assertion_code_is_safe"] = _safe_assertion_expr(check["assertion_code"], known_names)
    return obj, f"ai-{obj['confidence']}"


# ------------------------------------------------------------------ #
# Existing-test coverage review -- "does this uploaded test actually
# verify this RFC requirement?" Same non-hallucinated-JSON discipline as
# Test Intent generation above: the model only ever returns a list of
# {requirement_id, confidence, rationale}; pipeline.py is the only thing
# that writes that into the coverage map, and only for candidate IDs it
# actually offered the model -- a hallucinated ID can't sneak in.
# ------------------------------------------------------------------ #

COVERAGE_SYSTEM_PROMPT_TEMPLATE = Template("""You are a principal test architect auditing an EXISTING test suite for \
conformance coverage against an IETF RFC, for a $protocol_display_name implementation on Juniper Junos (vJunos-router / vMX).

You are given the content of one existing/uploaded test artifact (this may be pytest/PyEZ source code, \
a documented test case, an exported test-case description, or similar), plus a shortlist of candidate RFC \
requirements that a semantic search judged as topically related to it. You may also be given excerpts \
from uploaded product specs for grounding on what the real target device supports.

Your job: for each candidate requirement, decide whether this specific test ACTUALLY verifies it -- not \
just mentions related terms. Look for real evidence: an assertion, an observation point (RPC call, wire \
capture, log check), a specific stimulus that would exercise that exact behavior. A test that merely \
brings up a protocol session/adjacency does not, by itself, verify every requirement about that session -- \
only the ones its assertions and checks concretely establish.

Be conservative: if the evidence is thin or ambiguous, either omit the requirement entirely or include it \
with "low" confidence and say why in the rationale. Do not pad the list to look thorough.

Respond with ONLY a single JSON object, no prose, no markdown fences, matching exactly this schema:
{
  "covered": [
    {"requirement_id": "<one of the candidate requirement IDs given to you>",
     "confidence": "high" | "medium" | "low",
     "rationale": "1-2 sentences: what in the test actually establishes coverage of this requirement"}
  ]
}

Only include requirement IDs that were in the candidate list you were given. If none are genuinely
covered, return {"covered": []}.""")


def _build_coverage_system_prompt(profile) -> str:
    return COVERAGE_SYSTEM_PROMPT_TEMPLATE.substitute(protocol_display_name=profile.display_name)


def _build_coverage_user_prompt(rfc_label, filename, test_content, candidates, artefact_context=""):
    candidate_block = "\n".join(
        f"- [{c['requirement_id']}] ({c['keyword']}) {c['statement']}"
        for c in candidates
    ) or "(no candidates found)"
    artefact_block = (
        f"\nProduct/implementation context uploaded by the test engineer (grounding only, does not "
        f"override the RFC):\n{artefact_context}\n"
        if artefact_context else ""
    )
    return f"""RFC: {rfc_label}

EXISTING TEST ARTIFACT: {filename}
--- begin test content ---
{test_content}
--- end test content ---

Candidate RFC requirements (from semantic search over the persisted requirement corpus -- judge ONLY
whether the test above actually covers each one; do not invent requirement IDs outside this list):
{candidate_block}
{artefact_block}
Respond with the JSON object only."""


def _validate_coverage_result(obj: dict, candidate_ids: set) -> bool:
    if not isinstance(obj, dict) or "covered" not in obj or not isinstance(obj["covered"], list):
        return False
    for item in obj["covered"]:
        if not isinstance(item, dict):
            return False
        if not {"requirement_id", "confidence", "rationale"}.issubset(item.keys()):
            return False
        if item["requirement_id"] not in candidate_ids:
            return False
        if item["confidence"] not in ("high", "medium", "low"):
            return False
    return True


_SIGNIFICANT_WORD_RE = re.compile(r"[A-Za-z_]{6,}")


def _heuristic_coverage_match(test_content: str, candidates: list) -> list:
    """No-API-key / failure fallback: crude keyword overlap, always flagged
    low-confidence so it's visibly a guess, not an AI judgment. Better than
    nothing for a first pass, but every match here is explicitly labeled
    for manual review rather than silently trusted."""
    content_lower = test_content.lower()
    matches = []
    for c in candidates:
        if c["requirement_id"].lower() in content_lower:
            matches.append({"requirement_id": c["requirement_id"], "confidence": "low",
                             "rationale": "Heuristic match: requirement ID string found verbatim in the "
                                          "test content (no AI reasoning available -- verify manually)."})
            continue
        statement_words = {w.lower() for w in _SIGNIFICANT_WORD_RE.findall(c["statement"])}
        overlap = sum(1 for w in statement_words if w in content_lower)
        if overlap >= 3:
            matches.append({"requirement_id": c["requirement_id"], "confidence": "low",
                             "rationale": f"Heuristic match: {overlap} distinctive keyword(s) from the "
                                          f"requirement text appear in the test content (no AI reasoning "
                                          f"available -- verify manually)."})
    return matches


def analyze_existing_test_coverage(rfc_label: str, filename: str, test_content: str, candidates: list,
                                    profile, artefact_context: str = ""):
    """Returns (matches, mode_str). matches is a list of
    {requirement_id, confidence, rationale} dicts, restricted to the given
    candidates. mode_str mirrors generate_ai_test_intent's convention:
    'ai-reviewed' or 'heuristic-fallback:<reason>'. profile is a
    protocol_profiles.ProtocolProfile (see pipeline.get_active_profile())."""
    if not candidates:
        return [], "skipped:no-candidates"

    candidate_ids = {c["requirement_id"] for c in candidates}
    backend = _select_backend()
    if backend is None:
        return _heuristic_coverage_match(test_content, candidates), "heuristic-fallback:no-ai-backend"

    try:
        text = backend.complete(
            _build_coverage_system_prompt(profile),
            _build_coverage_user_prompt(rfc_label, filename, test_content, candidates, artefact_context),
            max_tokens=1500,
        )
        obj = json.loads(_strip_fences(text))
    except Exception as e:
        logger.warning(f"AI coverage review failed for {filename}: {e}")
        return _heuristic_coverage_match(test_content, candidates), "heuristic-fallback:api-error"

    if not _validate_coverage_result(obj, candidate_ids):
        logger.warning(f"AI coverage response failed schema validation for {filename}")
        return _heuristic_coverage_match(test_content, candidates), "heuristic-fallback:invalid-schema"

    return obj["covered"], "ai-reviewed"
