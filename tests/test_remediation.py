"""
tests/test_remediation.py — Core behavior tests for the LLM-free bridge.

Covers:
  - WebSocket auth handshake (enforced vs. optional token) and tab sync
  - Action batch validation and result parsing
  - URL scheme restrictions
  - browser_js passthrough
  - The four-tool surface (browser_see / browser_act / browser_js / browser_tabs)
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class FakeWebSocket:
    def __init__(self, auth_message, messages=None, origin="chrome-extension://test-extension"):
        self._auth_message = auth_message
        self._messages = list(messages or [])
        self.request_headers = {"Origin": origin}
        self.sent = []
        self.closed = None

    async def recv(self):
        return self._auth_message

    async def send(self, payload):
        self.sent.append(json.loads(payload))

    async def close(self, code, reason):
        self.closed = (code, reason)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._messages:
            return self._messages.pop(0)
        raise StopAsyncIteration


@pytest.fixture(autouse=True)
def reset_browser_manager():
    from server.browser_state import browser_manager

    browser_manager.set_browser_ws(None)
    browser_manager.clear_all()
    yield
    browser_manager.set_browser_ws(None)
    browser_manager.clear_all()


class TestWebSocketAuthAndTabSync:
    @pytest.mark.asyncio
    async def test_websocket_rejects_invalid_auth_token_when_enforced(self):
        from server.transport import handle_browser_websocket

        websocket = FakeWebSocket(json.dumps({"type": "AUTH", "token": "bad-token"}))

        with patch("server.transport.WS_AUTH_TOKEN", "expected-token"):
            await handle_browser_websocket(websocket)

        assert websocket.closed == (4401, "Authentication failed")

    @pytest.mark.asyncio
    async def test_websocket_accepts_any_token_when_auth_disabled(self):
        from server.browser_state import browser_manager
        from server.transport import handle_browser_websocket

        websocket = FakeWebSocket(json.dumps({"type": "AUTH", "token": ""}))

        with patch("server.transport.WS_AUTH_TOKEN", ""):
            await handle_browser_websocket(websocket)

        assert websocket.sent[0]["type"] == "AUTH_OK"
        assert browser_manager.connected is False  # cleared on disconnect after messages end

    @pytest.mark.asyncio
    async def test_websocket_auth_and_tab_events_sync_server_state(self):
        from server.browser_state import browser_manager
        from server.transport import handle_browser_websocket

        websocket = FakeWebSocket(
            json.dumps({"type": "AUTH", "token": "expected-token"}),
            messages=[
                json.dumps({
                    "type": "TAB_SNAPSHOT",
                    "tabs": [{"tab_id": "100", "url": "https://one.example", "title": "One"}],
                    "active_tab_id": "100",
                }),
                json.dumps({
                    "type": "TAB_CREATED",
                    "tab_id": "200",
                    "url": "https://two.example",
                    "title": "Two",
                }),
                json.dumps({
                    "type": "TAB_SWITCHED",
                    "tab_id": "200",
                    "url": "https://two.example",
                    "title": "Two",
                }),
                json.dumps({"type": "TAB_CLOSED", "tab_id": "100"}),
            ],
        )

        with patch("server.transport.WS_AUTH_TOKEN", "expected-token"):
            await handle_browser_websocket(websocket)

        assert websocket.sent[0]["type"] == "AUTH_OK"
        assert "200" in browser_manager.tabs
        assert "100" not in browser_manager.tabs
        assert browser_manager.active_tab_id == "200"
        assert browser_manager.tabs["200"].url == "https://two.example"

    @pytest.mark.asyncio
    async def test_dom_update_error_does_not_clobber_cached_dom(self):
        from server.browser_state import browser_manager
        from server.transport import handle_browser_websocket

        browser_manager.register_tab("100")
        browser_manager.update_dom("100", {"url": "https://ok.example", "title": "OK"})

        websocket = FakeWebSocket(
            json.dumps({"type": "AUTH", "token": ""}),
            messages=[
                json.dumps({
                    "type": "DOM_UPDATE",
                    "tab_id": "100",
                    "dom_state": {},
                    "error": "Could not establish connection.",
                }),
            ],
        )

        with patch("server.transport.WS_AUTH_TOKEN", ""):
            await handle_browser_websocket(websocket)

        assert browser_manager.tabs["100"].dom_state["url"] == "https://ok.example"


class TestActionResultParsing:
    @pytest.mark.asyncio
    async def test_handle_execute_actions_returns_deterministic_json(self):
        from server.tools.interaction import handle_act
        from server.tools.schemas import ActionStep, BrowserActionType

        mock_response = {
            "type": "ACTION_COMPLETE",
            "result": {
                "results": [
                    {"action": "click", "success": True},
                    {"action": "type", "success": False, "error": "element not found"},
                ]
            },
        }

        with patch("server.tools.interaction.browser_manager") as mock_bm:
            mock_bm.resolve_tab.return_value = MagicMock(tab_id="123", url="https://example.com", dom_state={})
            mock_bm.send = AsyncMock(return_value=mock_response)

            result = await handle_act(
                [
                    ActionStep(action=BrowserActionType.CLICK, selector="#btn"),
                    ActionStep(action=BrowserActionType.TYPE, selector="#input", value="hello"),
                ],
                tab_id="123",
            )

        parsed = json.loads(result)
        assert parsed["status"] == "error"
        assert parsed["data"]["summary"]["succeeded"] == 1
        assert parsed["data"]["summary"]["failed"] == 1

    @pytest.mark.asyncio
    async def test_set_code_step_passes_code_in_value(self):
        from server.tools.interaction import handle_act
        from server.tools.schemas import ActionStep, BrowserActionType

        mock_response = {
            "type": "ACTION_COMPLETE",
            "result": {"results": [{"action": "set_code", "success": True}]},
        }

        with patch("server.tools.interaction.browser_manager") as mock_bm:
            mock_bm.resolve_tab.return_value = MagicMock(tab_id="123")
            mock_bm.send = AsyncMock(return_value=mock_response)

            result = await handle_act(
                [ActionStep(action=BrowserActionType.SET_CODE, value="print(42)")],
                tab_id="123",
            )

        parsed = json.loads(result)
        assert parsed["status"] == "success"
        sent_steps = mock_bm.send.call_args[0][0]["steps"]
        assert sent_steps[0]["value"] == "print(42)"

    @pytest.mark.asyncio
    async def test_duplicate_clicks_are_not_auto_deduped(self):
        from server.tools.interaction import handle_act
        from server.tools.schemas import ActionStep, BrowserActionType

        mock_response = {
            "type": "ACTION_COMPLETE",
            "result": {"results": [{"action": "click", "success": True}] * 2},
        }

        with patch("server.tools.interaction.browser_manager") as mock_bm:
            mock_bm.resolve_tab.return_value = MagicMock(tab_id="123")
            mock_bm.send = AsyncMock(return_value=mock_response)

            await handle_act(
                [
                    ActionStep(action=BrowserActionType.CLICK, selector=".plus-btn"),
                    ActionStep(action=BrowserActionType.CLICK, selector=".plus-btn"),
                ],
                tab_id="123",
            )

        sent_steps = mock_bm.send.call_args[0][0]["steps"]
        assert all("idempotency_token" not in s for s in sent_steps)


class TestNavigationSafety:
    @pytest.mark.asyncio
    async def test_goto_rejects_javascript_url(self):
        from server.tools.navigation import handle_tabs

        result = await handle_tabs(action="goto", url="javascript:alert(1)")
        parsed = json.loads(result)
        assert parsed["status"] == "error"
        assert parsed["code"] == "UNSAFE_OPERATION"

    @pytest.mark.asyncio
    async def test_new_tab_allows_any_https_url(self):
        from server.tools.navigation import handle_tabs

        with patch("server.tools.navigation.browser_manager") as mock_bm:
            mock_bm.connected = True
            mock_bm.get_active_tab.return_value = MagicMock(url="https://current.example")
            mock_bm.send = AsyncMock(return_value={"tab_id": "999", "url": "https://example.com"})
            mock_bm.register_tab.return_value = MagicMock()

            result = await handle_tabs(action="new_tab", url="https://example.com")

        parsed = json.loads(result)
        assert parsed["status"] == "success"
        assert parsed["code"] == "TAB_OPENED"


class TestJsTool:
    @pytest.mark.asyncio
    async def test_handle_js_passes_script_and_returns_result(self):
        from server.tools.interaction import handle_js

        with patch("server.tools.interaction.browser_manager") as mock_bm:
            mock_bm.resolve_tab.return_value = MagicMock(tab_id="123")
            mock_bm.send = AsyncMock(return_value={"result": "42"})

            result = await handle_js("1 + 2", tab_id="123")

        parsed = json.loads(result)
        assert parsed["status"] == "success"
        assert parsed["data"]["result"] == "42"
        assert mock_bm.send.call_args[0][0]["script"] == "1 + 2"

    @pytest.mark.asyncio
    async def test_handle_js_reports_page_errors(self):
        from server.tools.interaction import handle_js

        with patch("server.tools.interaction.browser_manager") as mock_bm:
            mock_bm.resolve_tab.return_value = MagicMock(tab_id="123")
            mock_bm.send = AsyncMock(return_value={"error": "ReferenceError: x is not defined"})

            result = await handle_js("x", tab_id="123")

        parsed = json.loads(result)
        assert parsed["status"] == "error"
        assert parsed["code"] == "JS_EXECUTION_ERROR"


class TestSeeTool:
    @pytest.mark.asyncio
    async def test_invalid_mode_is_rejected(self):
        from server.tools.observation import handle_see

        with patch("server.tools.observation.browser_manager") as mock_bm:
            mock_bm.resolve_tab.return_value = MagicMock(tab_id="123")
            result = await handle_see(mode="nonsense", tab_id="123")

        parsed = json.loads(result)
        assert parsed["status"] == "error"
        assert parsed["code"] == "INVALID_ENUM"

    @pytest.mark.asyncio
    async def test_element_mode_requires_selector(self):
        from server.tools.observation import handle_see

        with patch("server.tools.observation.browser_manager") as mock_bm:
            mock_bm.resolve_tab.return_value = MagicMock(tab_id="123")
            result = await handle_see(mode="element", tab_id="123")

        parsed = json.loads(result)
        assert parsed["status"] == "error"
        assert parsed["code"] == "MISSING_REQUIRED_FIELD"

    @pytest.mark.asyncio
    async def test_page_mode_surfaces_bridge_error(self):
        """An extension-side error with no DOM must not be reported as success."""
        from server.tools.observation import handle_see

        with patch("server.tools.observation.browser_manager") as mock_bm, \
             patch("server.tools.observation.send_with_retries", new=AsyncMock(
                 return_value={"dom_state": {}, "error": "Could not establish connection."})):
            mock_bm.resolve_tab.return_value = MagicMock(tab_id="123", dom_state=None)
            result = await handle_see(mode="page", tab_id="123")

        parsed = json.loads(result)
        assert parsed["status"] == "error"
        assert parsed["code"] == "NO_DOM_STATE"
        assert "Could not establish connection." in parsed["message"]

    @pytest.mark.asyncio
    async def test_page_mode_falls_back_to_cache_with_error_message(self):
        from server.tools.observation import handle_see

        cached = {"url": "https://example.com", "title": "Example"}
        with patch("server.tools.observation.browser_manager") as mock_bm, \
             patch("server.tools.observation.send_with_retries", new=AsyncMock(
                 return_value={"dom_state": {}, "error": "bridge down"})):
            mock_bm.resolve_tab.return_value = MagicMock(tab_id="123", dom_state=cached)
            result = await handle_see(mode="page", tab_id="123")

        parsed = json.loads(result)
        assert parsed["status"] == "success"
        assert parsed["data"]["stale"] is True
        assert "bridge down" in parsed["message"]

    @pytest.mark.asyncio
    async def test_editor_mode_success_false_is_an_error(self):
        from server.tools.observation import handle_see

        mock_resp = {"type": "GET_CODE_RESULT", "success": False, "message": "No editor found"}
        with patch("server.tools.observation.browser_manager") as mock_bm, \
             patch("server.tools.observation.send_with_retries", new=AsyncMock(return_value=mock_resp)):
            mock_bm.resolve_tab.return_value = MagicMock(tab_id="123")
            result = await handle_see(mode="editor", tab_id="123")

        parsed = json.loads(result)
        assert parsed["status"] == "error"
        assert parsed["code"] == "EDITOR_EXTRACTION_FAILED"
        assert parsed["message"] == "No editor found"


class TestToolSurface:
    @pytest.mark.asyncio
    async def test_exactly_four_universal_tools_are_registered(self):
        from server.transport import create_mcp_server

        mcp = create_mcp_server()
        tools = await mcp.list_tools()
        names = sorted(t.name for t in tools)
        assert names == ["browser_act", "browser_js", "browser_see", "browser_tabs"]
