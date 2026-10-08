"""
test_transport_lazy.py — Verifies that server.transport is importable
without paying the cost of constructing the FastMCP server.

The full FastMCP server pulls in the entire tool registry, the LLM
provider (with all its SDK fallbacks), and the agent orchestrator. None
of that should happen on a bare `import server.transport`. Tests,
scripts, and tools that only need the helpers in transport.py
(constants, helper functions) should be able to import it cheaply.

We check this by spawning a fresh subprocess that does nothing but
import server.transport and inspect the singleton. A regular
`import server.transport` in the test process would carry over state
from earlier tests, so the subprocess is the cleanest signal.
"""

import subprocess
import sys


def test_transport_mcp_server_is_lazy():
    code = (
        "import server.transport; "
        "import sys; "
        "sys.exit(0 if server.transport._mcp_server_instance is None else 1)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, (
        "server.transport eagerly constructs the FastMCP server on import; "
        "use server.transport.get_mcp_server() to obtain it on demand.\n"
        f"stderr: {result.stderr}"
    )


def test_get_mcp_server_returns_singleton():
    """Two calls to get_mcp_server() must return the same instance."""
    import server.transport
    a = server.transport.get_mcp_server()
    b = server.transport.get_mcp_server()
    assert a is b, "get_mcp_server() should memoize; got a new instance on second call"


def test_get_mcp_server_returns_none_when_sdk_missing(monkeypatch):
    """If the FastMCP SDK is unavailable, get_mcp_server() returns None
    rather than raising."""
    import server.transport
    monkeypatch.setattr(server.transport, "MCP_AVAILABLE", False)
    # Reset the singleton so the patched flag takes effect
    monkeypatch.setattr(server.transport, "_mcp_server_instance", None)
    assert server.transport.get_mcp_server() is None
