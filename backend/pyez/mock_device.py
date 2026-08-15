"""
pyez/mock_device.py — a mock stand-in for jnpr.junos (PyEZ), so generated
tests can actually be *run* without a real vJunos-router/vMX lab. PyEZ isn't
a real dependency of this project (nothing else imports it), so there's
nothing genuine to conflict with -- pipeline.run_deduplicated_tests() shims
`jnpr.junos.Device` / `jnpr.junos.utils.config.Config` with the classes here
via a conftest.py written alongside the deduplicated tests (see
pipeline.CONFTEST_CONTENT), before pytest imports the test files.

This is deliberately a demo/smoke-test double, not a protocol simulator:
MockRpcResponse.findtext() pattern-matches on the XPath string to return a
plausible value (session Established/Full, a placeholder AS number, etc.),
regardless of which RPC was called or what stimulus preceded it. That's
enough to prove the generate -> dedup -> run pipeline works end to end and
to exercise simple state-check assertions meaningfully, but it does NOT
simulate real negative/boundary protocol behavior -- a test that requires a
peer emulator (see requires_peer_emulator in the catalog) will still "run"
against this mock and may pass or fail without that meaning anything about
real conformance. Point tests like that at a real lab for a real signal;
this mock's job is proving the harness runs, not replacing the lab.
"""
import re


def _mock_value_for_xpath(xpath: str) -> str:
    """Best-effort plausible value for a given XPath/field-name string.
    Order matters -- more specific patterns are checked first so e.g.
    'neighbor-state' doesn't fall through to a generic 'state' match before
    its own OSPF-specific value is picked."""
    x = (xpath or "").lower()
    patterns = [
        (r"peer-state", "Established"),
        (r"neighbor-state", "Full"),
        (r"as-path", "65001"),
        (r"sequence-number", "0x80000001"),
        (r"\bage\b", "100"),
        (r"hold-?time", "90"),
        (r"flap-count", "0"),
        (r"last-error", "None"),
        (r"router-id", "10.0.0.1"),
        (r"lsa-type", "Router"),
        (r"cost\b", "10"),
        (r"\bstate\b", "Established"),
    ]
    for pattern, value in patterns:
        if re.search(pattern, x):
            return value
    return "mock-value"


class MockRpcResponse:
    """Stands in for the lxml.etree Element a real PyEZ RPC call returns.
    Only implements what the generated tests actually call (.findtext) --
    extend here if a new xpath pattern is worth recognizing."""

    def __init__(self, rpc_name: str, kwargs: dict):
        self.rpc_name = rpc_name
        self.kwargs = kwargs

    def findtext(self, xpath: str, default=None):
        return _mock_value_for_xpath(xpath)

    def findall(self, xpath: str):
        return []

    def __repr__(self):
        return f"<MockRpcResponse {self.rpc_name}({self.kwargs})>"


class MockRpcMeta:
    """Stands in for jnpr.junos.Device.rpc -- any attribute access returns a
    callable (mirroring PyEZ's dynamic RPC-method-per-XML-tag pattern) that
    returns a MockRpcResponse regardless of arguments."""

    def __init__(self, device):
        self._device = device

    def __getattr__(self, name):
        def _call(*args, **kwargs):
            return MockRpcResponse(name, kwargs)
        return _call


class MockJunosDevice:
    """Stands in for jnpr.junos.Device. Same constructor signature the
    generated fixtures use (host/user/password/port); .open()/.close() are
    no-ops -- no real SSH/NETCONF connection is ever attempted."""

    def __init__(self, host=None, user=None, password=None, port=22, **kwargs):
        self.host = host
        self.user = user
        self.port = port
        self.connected = False
        self.rpc = MockRpcMeta(self)

    def open(self, *args, **kwargs):
        self.connected = True
        return self

    def close(self, *args, **kwargs):
        self.connected = False

    def __repr__(self):
        return f"<MockJunosDevice host={self.host!r} connected={self.connected}>"


class MockConfig:
    """Stands in for jnpr.junos.utils.config.Config. Records the loaded
    config text (inspectable via .loaded_text/.loaded_format for anyone
    writing a real test against this mock) but never actually applies
    anything -- .commit() is a no-op that always reports success."""

    def __init__(self, dev, mode="exclusive", **kwargs):
        self.dev = dev
        self.mode = mode
        self.loaded_text = None
        self.loaded_format = None
        self.committed = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return False  # never suppress exceptions raised inside the `with` block

    def load(self, configuration, format="text", **kwargs):
        self.loaded_text = configuration
        self.loaded_format = format

    def commit(self, confirm=None, **kwargs):
        self.committed = True
        return True
