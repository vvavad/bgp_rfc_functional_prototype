"""
protocol_profiles.py — per-protocol defaults for requirement classification
and test-template rendering.

Everything in pipeline.py that used to assume BGP outright (category
keyword rules, the two-router-eBGP topology description, the Junos config
stanza baked into the pytest template, the PyEZ observation RPC) now reads
those defaults from whichever profile is active for the currently-ingested
RFC. A profile is resolved once, at ingest time (see resolve_profile()),
and stored on rfc_meta.protocol_key.

Adding a new protocol means adding a new profile here -- nothing in
pipeline.py should need a protocol-specific `if` after that.

`bgp` is the flagship profile: its category_rules are copied verbatim from
the original hardcoded CATEGORY_RULES so ingesting RFC 4271 produces
byte-identical output to before this refactor (see the regression check in
todo.md item 1). `ospf` is the second profile that proves the abstraction
isn't secretly BGP-shaped -- its rules, topology, and templates are RFC
2328 vocabulary. `generic` is the fallback for any RFC that doesn't match a
known profile: it keeps the protocol-neutral common categories (timer,
message_format, error_handling, capability_negotiation) and otherwise
leaves everything as general_conformance, with clearly-marked placeholder
templates instead of silently guessing BGP.
"""

# Category rules shared across all profiles -- these describe RFC
# structure/vocabulary common to most IETF protocol specs, not anything
# BGP-specific. Kept as their own list so every profile can start from the
# same baseline instead of re-typing them.
COMMON_CATEGORY_RULES = [
    ("timer", [r'\btimer\b', r'\bexpir', r'\binterval\b']),
    ("message_format", [r'Message Format', r'\bheader\b', r'\bencod', r'\boctet', r'\bfield\b']),
    ("error_handling", [r'Error Handling', r'\berror\b']),
    ("capability_negotiation", [r'\bVersion Negotiation\b', r'\bcapabilit']),
]


class ProtocolProfile:
    def __init__(self, key, display_name, rfc_numbers, title_keywords, category_rules,
                 topology_key, topology_description, timer_fields, timer_category_override,
                 config_template, observation_call, observation_field, result_var,
                 default_emulator_tool, expertise_note):
        self.key = key
        self.display_name = display_name
        self.rfc_numbers = rfc_numbers
        self.title_keywords = title_keywords
        self.category_rules = category_rules
        self.topology_key = topology_key
        self.topology_description = topology_description
        self.timer_fields = timer_fields  # [{"label":..., "key":..., "default":...}, ...]
        self.timer_category_override = timer_category_override  # {"timer_key": value} when category=="timer"
        self.config_template = config_template  # single-brace .format() template, keyed by timer field keys
        self.observation_call = observation_call  # trusted literal PyEZ call, e.g. "rpc.get_bgp_neighbor_information()"
        self.observation_field = observation_field  # e.g. ".//peer-state"
        self.result_var = result_var  # variable name used in the rendered pytest stub
        self.default_emulator_tool = default_emulator_tool
        self.expertise_note = expertise_note  # short phrase for the AI system prompt

    def timers_for(self, category: str) -> dict:
        timers = {f["key"]: f["default"] for f in self.timer_fields}
        if category == "timer":
            timers.update(self.timer_category_override)
        return timers

    def timers_line(self, timers: dict) -> str:
        return ", ".join(f"{f['label']} = {timers[f['key']]}" for f in self.timer_fields)

    def observation_hint(self) -> str:
        return f"{self.observation_call} -> {self.observation_field}"


BGP_PROFILE = ProtocolProfile(
    key="bgp",
    display_name="BGP (Border Gateway Protocol)",
    rfc_numbers={"4271", "1771", "4360", "4893", "7606"},
    title_keywords=["border gateway protocol", "bgp-4", "bgp4", " bgp "],
    category_rules=[
        ("fsm_state", [r'\bFSM\b', r'\bstate machine\b', r'\bIdle\b', r'\bConnect\b', r'\bActive\b',
                        r'\bOpenSent\b', r'\bOpenConfirm\b', r'\bEstablished\b', r'transition']),
        ("timer", [r'\btimer\b', r'\bHold Timer\b', r'\bKeepAlive\b', r'\bConnectRetry\b', r'\bexpir']),
        ("message_format", [r'Message Format', r'\bheader\b', r'\bencod', r'\boctet', r'\bfield\b']),
        ("path_attribute", [r'\bpath attribute\b', r'ORIGIN', r'AS_PATH', r'NEXT_HOP',
                             r'MULTI_EXIT_DISC', r'LOCAL_PREF', r'AGGREGATOR', r'ATOMIC_AGGREGATE']),
        ("error_handling", [r'Error Handling', r'\bNOTIFICATION\b', r'\berror\b', r'\bCease\b']),
        ("capability_negotiation", [r'\bVersion Negotiation\b', r'\bcapabilit', r'\bOPEN Message\b']),
        ("decision_process", [r'Decision Process', r'Route Selection', r'Degree of Preference',
                               r'Route Dissemination', r'Route Resolvability']),
        ("update_handling", [r'UPDATE Message', r'route advertise', r'withdraw', r'Update-Send']),
        ("connection_management", [r'Connection Collision', r'TCP Connection', r'peer']),
    ],
    topology_key="two-router-ebgp",
    topology_description="R1 AS 65001 ↔ R2 AS 65002",
    timer_fields=[
        {"label": "Hold", "key": "hold", "default": 90},
        {"label": "KeepAlive", "key": "keepalive", "default": 30},
        {"label": "ConnectRetry", "key": "connect_retry", "default": 120},
    ],
    timer_category_override={"hold": 6},
    # NOTE: rendered via string.Template (see pipeline._generate_one), NOT
    # str.format() -- Junos config syntax is dense with literal { } braces
    # that would collide with str.format()'s field-delimiter syntax, so
    # placeholders here use Template's $name form instead.
    config_template=(
        "protocols {\n"
        "    bgp {\n"
        "        group EBGP-PEER {\n"
        "            type external;\n"
        "            peer-as 65002;\n"
        "            neighbor R2_LAB_IP {\n"
        "                hold-time ${hold};\n"
        "            }\n"
        "        }\n"
        "    }\n"
        "}"
    ),
    observation_call="rpc.get_bgp_neighbor_information()",
    observation_field=".//peer-state",
    result_var="peer_state",
    default_emulator_tool="ExaBGP/Scapy",
    expertise_note="BGP peering, path attributes, and the BGP FSM",
)

OSPF_PROFILE = ProtocolProfile(
    key="ospf",
    display_name="OSPF (Open Shortest Path First)",
    rfc_numbers={"2328", "5340"},
    title_keywords=["open shortest path first", "ospf"],
    category_rules=[
        ("neighbor_adjacency", [r'\bHello\b', r'\bneighbor\b', r'\badjacenc', r'\bDR\b', r'\bBDR\b',
                                 r'Designated Router', r'Backup Designated Router']),
        ("timer", [r'\btimer\b', r'\bexpir', r'HelloInterval', r'RouterDeadInterval', r'Dead Interval',
                   r'RxmtInterval', r'Retransmit Interval']),
        ("message_format", [r'Message Format', r'packet format', r'\bheader\b', r'\bencod', r'\bfield\b']),
        ("lsa_flooding", [r'\bLSA\b', r'Link State Advertisement', r'\bflood', r'Link State Database',
                           r'\bLSDB\b', r'LS sequence number', r'LS age']),
        ("spf_calculation", [r'Shortest Path', r'\bSPF\b', r'Dijkstra', r'routing table', r'\bcost\b',
                              r'shortest[- ]path tree']),
        ("area_management", [r'\barea\b', r'stub area', r'virtual link', r'\bABR\b', r'\bASBR\b', r'backbone']),
        ("error_handling", [r'\berror\b', r'checksum', r'\bdiscard']),
        ("capability_negotiation", [r'\boptions\b', r'\bE-bit\b', r'external routing capability']),
        ("authentication", [r'authentication', r'\bpassword\b', r'cryptographic']),
    ],
    topology_key="two-router-single-area",
    topology_description="R1 ↔ R2, both in Area 0.0.0.0",
    timer_fields=[
        {"label": "Hello Interval", "key": "hello_interval", "default": 10},
        {"label": "Dead Interval", "key": "dead_interval", "default": 40},
        {"label": "Retransmit Interval", "key": "retransmit_interval", "default": 5},
    ],
    timer_category_override={"hello_interval": 1},
    # Rendered via string.Template, not str.format() -- see the BGP profile's
    # config_template comment above for why.
    config_template=(
        "protocols {\n"
        "    ospf {\n"
        "        area 0.0.0.0 {\n"
        "            interface ge-0/0/0.0 {\n"
        "                hello-interval ${hello_interval};\n"
        "                dead-interval ${dead_interval};\n"
        "            }\n"
        "        }\n"
        "    }\n"
        "}"
    ),
    observation_call="rpc.get_ospf_neighbor_information()",
    observation_field=".//neighbor-state",
    result_var="neighbor_state",
    default_emulator_tool="Scapy",
    expertise_note="OSPF neighbor discovery, LSA flooding, and SPF-based route computation",
)

GENERIC_PROFILE = ProtocolProfile(
    key="generic",
    display_name="Unrecognized protocol",
    rfc_numbers=set(),
    title_keywords=[],
    category_rules=[],  # falls through to COMMON_CATEGORY_RULES only, see _classify_category
    topology_key="two-peer-default",
    topology_description="two conformant peers (adjust to the protocol's actual topology needs)",
    timer_fields=[
        {"label": "Session Timer", "key": "session_timer", "default": 30},
    ],
    timer_category_override={},
    config_template=(
        "protocols {\n"
        "    <protocol> {\n"
        "        /* TODO: protocol-specific peering configuration -- no profile\n"
        "           was recognized for this RFC, see rfc_meta.protocol_key */\n"
        "    }\n"
        "}"
    ),
    observation_call="rpc.get_TODO_protocol_status_information()  # TODO: replace with the real PyEZ RPC",
    observation_field=".//state",
    result_var="protocol_state",
    default_emulator_tool="Scapy",
    expertise_note="the protocol described by this RFC (no specific profile recognized)",
)

PROFILES = {p.key: p for p in (BGP_PROFILE, OSPF_PROFILE, GENERIC_PROFILE)}


def resolve_profile(rfc_number: str, rfc_title: str) -> ProtocolProfile:
    """RFC number match wins outright; otherwise a case-insensitive keyword
    scan of the title; otherwise the generic fallback. Never raises --
    an unrecognized RFC gets the generic profile, not a guess."""
    rfc_number = (rfc_number or "").strip()
    title_lower = (rfc_title or "").lower()
    for profile in (BGP_PROFILE, OSPF_PROFILE):
        if rfc_number in profile.rfc_numbers:
            return profile
    for profile in (BGP_PROFILE, OSPF_PROFILE):
        if any(kw in title_lower for kw in profile.title_keywords):
            return profile
    return GENERIC_PROFILE


def get_profile(protocol_key: str) -> ProtocolProfile:
    """Look up an already-resolved profile key (from rfc_meta.protocol_key).
    Falls back to generic for an unset/unrecognized key rather than raising
    -- a profile lookup should never crash a read path."""
    return PROFILES.get(protocol_key, GENERIC_PROFILE)
