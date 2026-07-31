"""
ai_backends.py — pluggable AI completion backends for ai_generation.py.

Two backends implement the same tiny interface (`available()` /
`complete(system_prompt, user_prompt) -> str`), so ai_generation.py's prompt
construction, JSON parsing, and schema validation stay identical no matter
which one answered:

- AnthropicAPIBackend  — the original path: a direct Anthropic() client call,
  gated on ANTHROPIC_API_KEY. Needed for running this app standalone, outside
  a Claude Code session (a colleague on a plain terminal, CI, etc).
- ClaudeCodeCLIBackend — shells out to the `claude` binary already bundled
  with a Claude Code / VS Code session in non-interactive print mode
  (`-p --output-format json`). Authenticates via that session's existing
  OAuth login (~/.claude/.credentials.json) -- no separate API key to
  provision, store, or rotate for this POC.

Confirmed by a manual spike before writing this (see conversation/plan
history): the bundled binary supports `-p --output-format json` for clean,
single-line JSON on stdout (trust-dialog warnings and permission notices go
to stderr only, never mixed into stdout), and `--system-prompt` fully
replaces the default Claude Code system prompt rather than appending to it
-- both required for this to behave like a plain completion call instead of
an interactive coding session.
"""
import os
import glob
import json
import shutil
import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

CLI_TIMEOUT_SECONDS = 120


class AIBackend:
    """Minimal shared interface. `complete()` raises on any failure --
    callers (ai_generation.py) already wrap every AI call in a broad
    try/except and treat any exception as 'fall back to heuristic', so
    backends don't need their own fallback logic."""
    key = "none"

    def available(self) -> bool:
        raise NotImplementedError

    def complete(self, system_prompt: str, user_prompt: str, max_tokens: int = 1000) -> str:
        raise NotImplementedError


class AnthropicAPIBackend(AIBackend):
    """Direct Anthropic API call. Requires ANTHROPIC_API_KEY in the
    environment (loaded from backend/.env by ai_generation.py before this
    is constructed)."""
    key = "anthropic-api"

    def __init__(self, model: str):
        self.model = model
        self._client = None
        self._checked = False

    def _get_client(self):
        if self._checked:
            return self._client
        self._checked = True
        if not os.environ.get("ANTHROPIC_API_KEY"):
            return None
        try:
            from anthropic import Anthropic
            self._client = Anthropic()
        except ImportError:
            logger.warning("anthropic package not installed -- AnthropicAPIBackend unavailable.")
            self._client = None
        return self._client

    def available(self) -> bool:
        return self._get_client() is not None

    def complete(self, system_prompt: str, user_prompt: str, max_tokens: int = 1000) -> str:
        client = self._get_client()
        if client is None:
            raise RuntimeError("Anthropic client not available (no ANTHROPIC_API_KEY or SDK missing)")
        resp = client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        return "".join(block.text for block in resp.content if getattr(block, "type", None) == "text")


# Known install locations for the bundled Claude Code CLI binary, checked in
# order. Version-numbered extension directories mean these are globs, not
# exact paths -- sorted() picks the lexicographically-last (newest) match.
_BINARY_GLOB_PATTERNS = [
    "~/.vscode-server/extensions/anthropic.claude-code-*/resources/native-binary/claude",
    "~/.vscode/extensions/anthropic.claude-code-*/resources/native-binary/claude",
    "~/.cursor-server/extensions/anthropic.claude-code-*/resources/native-binary/claude",
    "~/.cursor/extensions/anthropic.claude-code-*/resources/native-binary/claude",
]


class ClaudeCodeCLIBackend(AIBackend):
    """Shells out to the local Claude Code CLI in non-interactive print
    mode. No API key required -- auth comes from the CLI's own existing
    login. `model` accepts the same values as the Anthropic API path
    (aliases like 'sonnet'/'opus', or a full model name)."""
    key = "claude-code-cli"

    def __init__(self, model: str = None, timeout: int = CLI_TIMEOUT_SECONDS):
        self.model = model
        self.timeout = timeout
        self._binary_path = None
        self._checked = False

    def _find_binary(self):
        # 1. Explicit override, for anyone whose install lives somewhere
        #    these patterns don't anticipate.
        override = os.environ.get("CLAUDE_CLI_PATH")
        if override and Path(override).is_file():
            return override

        # 2. This process's own launch environment, if the Flask app itself
        #    happened to be started from inside a Claude Code terminal.
        execpath = os.environ.get("CLAUDE_CODE_EXECPATH")
        if execpath and Path(execpath).is_file():
            return execpath

        # 3. Known VS Code / Cursor extension install directories.
        for pattern in _BINARY_GLOB_PATTERNS:
            matches = sorted(glob.glob(str(Path(pattern).expanduser())))
            if matches:
                return matches[-1]

        # 4. A standalone `claude` CLI install on PATH.
        found = shutil.which("claude")
        if found:
            return found

        return None

    def available(self) -> bool:
        if not self._checked:
            self._checked = True
            self._binary_path = self._find_binary()
            if self._binary_path:
                logger.info(f"ClaudeCodeCLIBackend: found binary at {self._binary_path}")
        return self._binary_path is not None

    def complete(self, system_prompt: str, user_prompt: str, max_tokens: int = 1000) -> str:
        if not self.available():
            raise RuntimeError("claude CLI binary not found (checked CLAUDE_CLI_PATH, "
                                "CLAUDE_CODE_EXECPATH, known extension install paths, and PATH)")
        cmd = [self._binary_path, "-p", "--output-format", "json",
               "--allowedTools", "", "--system-prompt", system_prompt]
        if self.model:
            cmd += ["--model", self.model]
        try:
            proc = subprocess.run(
                cmd, input=user_prompt, capture_output=True, text=True, timeout=self.timeout,
            )
        except subprocess.TimeoutExpired as e:
            raise RuntimeError(f"claude CLI timed out after {self.timeout}s") from e
        if proc.returncode != 0:
            raise RuntimeError(f"claude CLI exited {proc.returncode}: {proc.stderr[:500]}")
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"claude CLI produced non-JSON stdout: {proc.stdout[:300]}") from e
        if payload.get("is_error"):
            raise RuntimeError(f"claude CLI reported an error: {payload.get('result')}")
        return payload.get("result", "")
