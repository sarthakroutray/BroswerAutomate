"""
tests/test_remediation.py — Validation tests for final hardening fixes.
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
    async def test_websocket_rejects_invalid_auth_token(self):
        from server.transport import handle_browser_websocket

        websocket = FakeWebSocket(json.dumps({"type": "AUTH", "token": "bad-token"}))

        with patch("server.transport.WS_AUTH_TOKEN", "expected-token"):
            await handle_browser_websocket(websocket)

        assert websocket.closed == (4401, "Authentication failed")

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


class TestActionResultParsing:
    @pytest.mark.asyncio
    async def test_handle_execute_actions_returns_deterministic_json(self):
        from server.tools.interaction import handle_execute_actions
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

            result = await handle_execute_actions(
                [
                    ActionStep(action=BrowserActionType.CLICK, selector="#btn"),
                    ActionStep(action=BrowserActionType.TYPE, selector="#input", value="hello"),
                ],
                tab_id="123",
                allow_unsafe=True,
            )

        parsed = json.loads(result)
        assert parsed["status"] == "error"
        assert parsed["data"]["summary"]["succeeded"] == 1
        assert parsed["data"]["summary"]["failed"] == 1

    @pytest.mark.asyncio
    async def test_click_first_selector_nested_results(self):
        from server.browser_state import browser_manager, click_first_selector

        mock_response = {
            "type": "ACTION_COMPLETE",
            "result": {"results": [{"action": "click", "success": True}]},
        }

        with patch.object(browser_manager, "send", new_callable=AsyncMock, return_value=mock_response):
            assert await click_first_selector("123", ["#btn1"]) is True


class TestNavigationPolicy:
    @pytest.mark.asyncio
    async def test_goto_rejects_javascript_url(self):
        from server.tools.navigation import handle_navigate

        result = await handle_navigate(action="goto", url="javascript:alert(1)")
        parsed = json.loads(result)
        assert parsed["status"] == "error"
        assert parsed["code"] == "UNSAFE_OPERATION"

    @pytest.mark.asyncio
    async def test_new_tab_requires_explicit_allow_for_non_allowlisted_domain(self):
        from server.tools.navigation import handle_navigate

        with patch("server.tools.navigation.browser_manager") as mock_bm:
            mock_bm.connected = True
            mock_bm.get_active_tab.return_value = MagicMock(url="https://current.example")

            result = await handle_navigate(action="new_tab", url="https://example.com")

        parsed = json.loads(result)
        assert parsed["status"] == "error"
        assert parsed["code"] == "POLICY_CONFIRMATION_REQUIRED"

    @pytest.mark.asyncio
    async def test_new_tab_allows_explicit_override(self):
        from server.tools.navigation import handle_navigate

        with patch("server.tools.navigation.browser_manager") as mock_bm:
            mock_bm.connected = True
            mock_bm.get_active_tab.return_value = MagicMock(url="https://current.example")
            mock_bm.send = AsyncMock(return_value={"tab_id": "999", "url": "https://example.com"})
            mock_bm.register_tab.return_value = MagicMock()

            result = await handle_navigate(
                action="new_tab",
                url="https://example.com",
                allow_unsafe=True,
            )

        parsed = json.loads(result)
        assert parsed["status"] == "success"
        assert parsed["code"] == "TAB_OPENED"


class TestMCPSamplingActivation:
    @pytest.mark.asyncio
    async def test_handle_task_from_extension_allows_mcp_session(self):
        from server.browser_state import _mcp_session_tracker, browser_manager
        from server.transport import handle_task_from_extension

        await _mcp_session_tracker.update_session(MagicMock())
        browser_manager.set_browser_ws(AsyncMock())
        browser_manager.register_tab("100")
        browser_manager.set_active_tab("100")

        with patch.dict("os.environ", {}, clear=True), patch("server.transport.agent") as mock_agent:
            mock_agent.run_task = AsyncMock(return_value=MagicMock(status="completed", summary="Done", error=""))
            await handle_task_from_extension({"goal": "test goal", "tab_id": "100"})
            mock_agent.run_task.assert_called_once()

        await _mcp_session_tracker.update_session(None)


class TestAgentToolUsage:
    @pytest.mark.asyncio
    async def test_agent_uses_tool_dispatch_instead_of_browser_send(self):
        from server.agent.orchestrator import AutonomousAgent
        from server.browser_state import BrowserManager, MCPSessionTracker

        browser = BrowserManager()
        browser.register_tab("123")
        browser.tabs["123"].url = "https://localhost"
        browser.send = AsyncMock(side_effect=AssertionError("browser.send should not be called"))

        agent = AutonomousAgent(browser, MCPSessionTracker())
        agent._llm.ask_json = AsyncMock(side_effect=[
            {"thinking": "Click the button", "actions": [{"action": "click", "selector": "#start"}], "done": False, "summary": ""},
            {"thinking": "Complete", "actions": [], "done": True, "summary": "Done"},
        ])

        async def fake_screenshot(**kwargs):
            return json.dumps({
                "status": "success",
                "code": "SCREENSHOT_CAPTURED",
                "message": "ok",
                "data": {"screenshot": "ZmFrZQ=="},
            })

        dom_payload = {
            "url": "https://localhost",
            "title": "Localhost",
            "inputs": [],
            "buttons": [{"text": "Start", "selector": "#start"}],
            "links": [],
            "selects": [],
            "checkboxes": [],
            "radioButtons": [],
            "textSummary": "Start page",
        }

        async def fake_dom(**kwargs):
            return json.dumps({
                "status": "success",
                "code": "PAGE_STATE_READY",
                "message": "ok",
                "data": {"dom_state": dom_payload},
            })

        execute_mock = AsyncMock(return_value=json.dumps({
            "status": "success",
            "code": "ACTIONS_EXECUTED",
            "message": "ok",
            "data": {"results": [{"action": "click", "success": True}]},
        }))

        with patch("server.agent.orchestrator.TOOL_DISPATCH", {
            "browser_take_screenshot": fake_screenshot,
            "browser_get_page_state": fake_dom,
            "browser_execute_actions": execute_mock,
        }), patch("server.agent.orchestrator.ENABLE_AGENT_ACTION_VERIFICATION", False):
            result = await agent.run_task("task-1", "Click start", "123", max_steps=2, allow_unsafe=True)

        assert result.status == "completed"
        execute_mock.assert_awaited()
        assert browser.send.await_count == 0
