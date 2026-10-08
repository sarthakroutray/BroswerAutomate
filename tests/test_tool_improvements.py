"""
test_tool_improvements.py — Coverage for the Phase 1/2 tool improvements:

  - browser_see(mode="find") validation + passthrough
  - login-before-form page-type ordering
  - browser_see pagination params
  - browser_act: trusted/chord/new-action validation, truncation warnings
  - browser_wait: selector/text/stability routing
  - browser_session: status/stop/reconnect
  - browser_tabs: wait_until validation + unconfirmed partial results
  - dom_fingerprint sensitivity to values/disabled state
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def reset_browser_manager():
    from server.browser_state import browser_manager

    browser_manager.set_browser_ws(None)
    browser_manager.clear_all()
    yield
    browser_manager.set_browser_ws(None)
    browser_manager.clear_all()


def _tab(tab_id="123"):
    return MagicMock(tab_id=tab_id, url="https://example.com", dom_state={})


class TestSeeFindMode:
    @pytest.mark.asyncio
    async def test_find_requires_query(self):
        from server.tools.observation import handle_see

        with patch("server.tools.observation.browser_manager") as mock_bm:
            mock_bm.resolve_tab.return_value = _tab()
            result = await handle_see(mode="find", tab_id="123")

        parsed = json.loads(result)
        assert parsed["status"] == "error"
        assert parsed["code"] == "MISSING_REQUIRED_FIELD"

    @pytest.mark.asyncio
    async def test_find_rejects_bad_find_mode(self):
        from server.tools.observation import handle_see

        with patch("server.tools.observation.browser_manager") as mock_bm:
            mock_bm.resolve_tab.return_value = _tab()
            result = await handle_see(mode="find", query="Buy", find_mode="fuzzy", tab_id="123")

        parsed = json.loads(result)
        assert parsed["status"] == "error"
        assert parsed["code"] == "INVALID_ENUM"

    @pytest.mark.asyncio
    async def test_find_forwards_text_mode_tag_role(self):
        from server.tools.observation import handle_see

        mock_resp = {
            "candidates": [{"selector": "#buy", "text": "Buy now"}],
            "primary": {"selector": "#buy"},
        }
        with patch("server.tools.observation.browser_manager") as mock_bm, \
             patch("server.tools.observation.send_with_retries", new=AsyncMock(return_value=mock_resp)) as mock_send:
            mock_bm.resolve_tab.return_value = _tab()
            result = await handle_see(
                mode="find", query="Buy", find_mode="startsWith",
                selector="tag=button", tab_id="123",
            )

        parsed = json.loads(result)
        assert parsed["status"] == "success"
        assert parsed["code"] == "FIND_RESULTS_READY"
        sent = mock_send.call_args[0][0]
        assert sent["type"] == "FIND_BY_TEXT"
        assert sent["text"] == "Buy"
        assert sent["mode"] == "startsWith"
        assert sent["tag"] == "button"

    @pytest.mark.asyncio
    async def test_find_error_without_candidates_is_no_match(self):
        from server.tools.observation import handle_see

        with patch("server.tools.observation.browser_manager") as mock_bm, \
             patch("server.tools.observation.send_with_retries",
                   new=AsyncMock(return_value={"error": "No element found"})):
            mock_bm.resolve_tab.return_value = _tab()
            result = await handle_see(mode="find", query="Nope", tab_id="123")

        parsed = json.loads(result)
        assert parsed["status"] == "error"
        assert parsed["code"] == "FIND_NO_MATCH"

    @pytest.mark.asyncio
    async def test_page_pagination_params_validated(self):
        from server.tools.observation import handle_see

        with patch("server.tools.observation.browser_manager") as mock_bm:
            mock_bm.resolve_tab.return_value = _tab()
            bad_limit = await handle_see(mode="page", limit=500, tab_id="123")
            bad_offset = await handle_see(mode="page", offset=-1, tab_id="123")

        assert json.loads(bad_limit)["code"] == "INVALID_ARGUMENT"
        assert json.loads(bad_offset)["code"] == "INVALID_ARGUMENT"


class TestPageTypeOrdering:
    def test_login_beats_generic_form(self):
        from server.tools.observation import analyze_page_type

        dom = {
            "title": "Sign in",
            "inputs": [
                {"name": "username", "type": "text"},
                {"name": "password", "type": "password"},
            ],
            "buttons": [{"text": "Submit"}],
            "radioButtons": [],
            "checkboxes": [],
            "selects": [],
            "links": [],
            "tables": [],
        }
        assert analyze_page_type(dom)["type"] == "login"

    def test_runtime_signals_rendered(self):
        from server.tools.observation import format_dom_for_display

        dom = {
            "title": "T", "url": "https://x.example",
            "runtimeSignals": {
                "iframeCount": 2, "crossOriginCount": 1,
                "shadowHostCount": 3, "modalLikeCount": 1,
                "domHash": "abc123",
            },
        }
        display = format_dom_for_display(dom)
        assert "iframes=2" in display
        assert "shadow_hosts=3" in display
        assert "abc123" in display

    def test_pagination_windows_lists(self):
        from server.tools.observation import format_dom_for_display

        dom = {
            "title": "T", "url": "https://x.example",
            "links": [
                {"text": f"L{i}", "href": f"https://x.example/{i}", "selector": f"#l{i}"}
                for i in range(10)
            ],
        }
        first = format_dom_for_display(dom, limit=3, offset=0)
        second = format_dom_for_display(dom, limit=3, offset=3)
        assert "showing 1-3 of 10" in first
        assert "showing 4-6 of 10" in second
        assert "#l0" in first
        assert "#l0" not in second


class TestActImprovements:
    @pytest.mark.asyncio
    async def test_trusted_click_forwarded(self):
        from server.tools.interaction import handle_act
        from server.tools.schemas import ActionStep, BrowserActionType

        mock_response = {"type": "ACTION_COMPLETE",
                         "result": {"results": [{"action": "click", "success": True}]}}
        with patch("server.tools.interaction.browser_manager") as mock_bm:
            mock_bm.resolve_tab.return_value = _tab()
            mock_bm.send = AsyncMock(return_value=mock_response)
            result = await handle_act(
                [ActionStep(action=BrowserActionType.CLICK, selector="#c", trusted=True)],
                tab_id="123",
            )

        assert json.loads(result)["status"] == "success"
        assert mock_bm.send.call_args[0][0]["steps"][0]["trusted"] is True

    @pytest.mark.asyncio
    async def test_key_chord_parsed_to_modifiers(self):
        from server.tools.interaction import handle_act
        from server.tools.schemas import ActionStep, BrowserActionType

        mock_response = {"type": "ACTION_COMPLETE",
                         "result": {"results": [{"action": "press_key", "success": True}]}}
        with patch("server.tools.interaction.browser_manager") as mock_bm:
            mock_bm.resolve_tab.return_value = _tab()
            mock_bm.send = AsyncMock(return_value=mock_response)
            await handle_act(
                [ActionStep(action=BrowserActionType.PRESS_KEY, value="Ctrl+Shift+T")],
                tab_id="123",
            )

        step = mock_bm.send.call_args[0][0]["steps"][0]
        assert step["modifiers"] == {"ctrl": True, "shift": True, "alt": False, "meta": False}
        assert step["key"] == "T"

    @pytest.mark.asyncio
    async def test_new_actions_validate(self):
        from server.tools.interaction import handle_act
        from server.tools.schemas import ActionStep

        with patch("server.tools.interaction.browser_manager") as mock_bm:
            mock_bm.resolve_tab.return_value = _tab()

            missing = await handle_act(
                [ActionStep(action="right_click")], tab_id="123")
            assert json.loads(missing)["code"] == "INVALID_ARGUMENT"

            bad_drag = await handle_act(
                [ActionStep(action="drag", value="10,20")], tab_id="123")
            assert "drag" in json.loads(bad_drag)["message"]

            bad_scroll = await handle_act(
                [ActionStep(action="scroll_element", selector=".x", value="sideways")],
                tab_id="123")
            assert "scroll_element value" in json.loads(bad_scroll)["message"]

            no_file = await handle_act(
                [ActionStep(action="upload", selector="input[type=file]")], tab_id="123")
            assert "upload requires a file path" in json.loads(no_file)["message"]

    @pytest.mark.asyncio
    async def test_batch_truncation_warns(self):
        from server.tools.interaction import handle_act
        from server.tools.schemas import ActionStep, BrowserActionType

        steps = [ActionStep(action=BrowserActionType.HOVER, selector=f"#x{i}") for i in range(25)]
        mock_response = {"type": "ACTION_COMPLETE",
                         "result": {"results": [{"action": "hover", "success": True}] * 20}}
        with patch("server.tools.interaction.browser_manager") as mock_bm:
            mock_bm.resolve_tab.return_value = _tab()
            mock_bm.send = AsyncMock(return_value=mock_response)
            result = await handle_act(steps, tab_id="123")

        parsed = json.loads(result)
        assert len(mock_bm.send.call_args[0][0]["steps"]) == 20
        assert parsed["data"]["warnings"]
        assert "truncated" in parsed["message"]

    @pytest.mark.asyncio
    async def test_custom_timeout_used(self):
        from server.tools.interaction import handle_act, handle_js
        from server.tools.schemas import ActionStep, BrowserActionType

        mock_response = {"type": "ACTION_COMPLETE",
                         "result": {"results": [{"action": "hover", "success": True}]}}
        with patch("server.tools.interaction.browser_manager") as mock_bm:
            mock_bm.resolve_tab.return_value = _tab()
            mock_bm.send = AsyncMock(return_value=mock_response)
            await handle_act(
                [ActionStep(action=BrowserActionType.HOVER, selector="#x")],
                tab_id="123", timeout=99.0,
            )
        assert mock_bm.send.call_args[1]["timeout"] == 99.0

        with patch("server.tools.interaction.browser_manager") as mock_bm:
            mock_bm.resolve_tab.return_value = _tab()
            mock_bm.send = AsyncMock(return_value={"result": "ok"})
            await handle_js("1", tab_id="123", timeout=42.0)
        assert mock_bm.send.call_args[1]["timeout"] == 42.0


class TestWaitTool:
    @pytest.mark.asyncio
    async def test_wait_requires_target(self):
        from server.tools.wait import handle_wait

        with patch("server.tools.wait.browser_manager") as mock_bm:
            mock_bm.resolve_tab.return_value = _tab()
            result = await handle_wait(tab_id="123")

        assert json.loads(result)["code"] == "MISSING_REQUIRED_FIELD"

    @pytest.mark.asyncio
    async def test_wait_selector_success(self):
        from server.tools.wait import handle_wait

        mock_resp = {"found": True, "tag": "button", "text": "Pay",
                     "visible": True, "interactable": True, "elapsed": 321}
        with patch("server.tools.wait.browser_manager") as mock_bm, \
             patch("server.tools.wait.send_with_retries", new=AsyncMock(return_value=mock_resp)):
            mock_bm.resolve_tab.return_value = _tab()
            result = await handle_wait(selector="#pay", require_interactable=True, tab_id="123")

        parsed = json.loads(result)
        assert parsed["status"] == "success"
        assert parsed["code"] == "WAIT_SATISFIED"
        assert parsed["data"]["interactable"] is True

    @pytest.mark.asyncio
    async def test_wait_timeout_is_error(self):
        from server.tools.wait import handle_wait

        with patch("server.tools.wait.browser_manager") as mock_bm, \
             patch("server.tools.wait.send_with_retries",
                   new=AsyncMock(return_value={"found": False, "elapsed": 5000,
                                               "error": "Timed out"})):
            mock_bm.resolve_tab.return_value = _tab()
            result = await handle_wait(selector="#ghost", tab_id="123")

        parsed = json.loads(result)
        assert parsed["status"] == "error"
        assert parsed["code"] == "WAIT_TIMEOUT"

    @pytest.mark.asyncio
    async def test_wait_stability_route(self):
        from server.tools.wait import handle_wait

        with patch("server.tools.wait.browser_manager") as mock_bm, \
             patch("server.tools.wait.send_with_retries",
                   new=AsyncMock(return_value={"stable": True, "elapsed": 800})) as mock_send:
            mock_bm.resolve_tab.return_value = _tab()
            result = await handle_wait(require_stable=True, tab_id="123")

        parsed = json.loads(result)
        assert parsed["code"] == "WAIT_STABLE"
        assert mock_send.call_args[0][0]["type"] == "WAIT_FOR_STABLE"


class TestSessionTool:
    @pytest.mark.asyncio
    async def test_status_when_disconnected(self):
        from server.tools.session import handle_session

        with patch("server.tools.session.browser_manager") as mock_bm:
            mock_bm.connected = False
            mock_bm.tabs = {}
            mock_bm.active_tab_id = None
            result = await handle_session("status")

        parsed = json.loads(result)
        assert parsed["status"] == "success"
        assert parsed["data"]["connected"] is False

    @pytest.mark.asyncio
    async def test_status_queries_extension(self):
        from server.tools.session import handle_session

        tab = MagicMock(tab_id="7", url="https://example.com", title="Example")
        tab.last_updated = 0
        tab.dom_state = {"url": "https://example.com"}
        with patch("server.tools.session.browser_manager") as mock_bm, \
             patch("server.tools.session.send_with_retries",
                   new=AsyncMock(return_value={"connected": True, "tabCount": 1})) as mock_send:
            mock_bm.connected = True
            mock_bm.tabs = {"7": tab}
            mock_bm.active_tab_id = "7"
            result = await handle_session("status")

        parsed = json.loads(result)
        assert parsed["data"]["extension"]["tabCount"] == 1
        assert mock_send.call_args[0][0]["type"] == "GET_STATUS"

    @pytest.mark.asyncio
    async def test_stop_cancels_and_fails_pending(self):
        from server.tools.session import handle_session

        with patch("server.tools.session.browser_manager") as mock_bm, \
             patch("server.tools.session.send_with_retries",
                   new=AsyncMock(return_value={"cancelled": True})):
            mock_bm.connected = True
            mock_bm.resolve_tab.return_value = _tab()
            result = await handle_session("stop", tab_id="123")

        parsed = json.loads(result)
        assert parsed["code"] == "ACTIONS_CANCELLED"
        assert mock_bm.fail_tab_pending.called

    @pytest.mark.asyncio
    async def test_reconnect_guidance_when_down(self):
        from server.tools.session import handle_session

        with patch("server.tools.session.browser_manager") as mock_bm:
            mock_bm.connected = False
            mock_bm.tabs = {}
            result = await handle_session("reconnect")

        parsed = json.loads(result)
        assert parsed["status"] == "error"
        assert "ws://localhost:8000" in parsed["message"]

    @pytest.mark.asyncio
    async def test_invalid_session_action(self):
        from server.tools.session import handle_session

        result = await handle_session("explode")
        assert json.loads(result)["code"] == "INVALID_ENUM"


class TestTabsHonesty:
    @pytest.mark.asyncio
    async def test_unconfirmed_goto_is_partial_error(self):
        from server.tools.navigation import handle_tabs

        with patch("server.tools.navigation.browser_manager") as mock_bm, \
             patch("server.tools.navigation._capture_nav_snapshot",
                   new=AsyncMock(return_value={"url": "https://a.example", "hash": "h1"})), \
             patch("server.tools.navigation._verify_transition",
                   new=AsyncMock(return_value=(False, {"url": "https://a.example", "hash": "h1"}))):
            mock_bm.resolve_tab.return_value = _tab()
            mock_bm.send = AsyncMock(return_value={})
            result = await handle_tabs(action="goto", url="https://b.example", timeout=1.0)

        parsed = json.loads(result)
        assert parsed["status"] == "error"
        assert parsed["code"] == "NAVIGATION_UNCONFIRMED"
        assert parsed["data"]["partial"] is True
        assert parsed["data"]["confirmed"] is False

    @pytest.mark.asyncio
    async def test_bad_wait_until_rejected(self):
        from server.tools.navigation import handle_tabs

        result = await handle_tabs(action="goto", url="https://b.example", wait_until="soon")
        assert json.loads(result)["code"] == "INVALID_ENUM"


class TestFingerprint:
    def test_value_and_disabled_changes_move_hash(self):
        from server.browser_state import dom_fingerprint

        base = {
            "url": "https://x.example", "title": "T",
            "inputs": [{"name": "q", "value": "a"}],
            "buttons": [{"text": "Go"}],
            "links": [], "selects": [], "checkboxes": [],
            "radioButtons": [], "headings": [], "tables": [],
            "textSummary": "hello",
        }
        changed_value = {**base, "inputs": [{"name": "q", "value": "b"}]}
        changed_disabled = {**base, "buttons": [{"text": "Go", "disabled": True}]}
        assert dom_fingerprint(base) != dom_fingerprint(changed_value)
        assert dom_fingerprint(base) != dom_fingerprint(changed_disabled)
        assert dom_fingerprint(base) == dom_fingerprint(dict(base))
