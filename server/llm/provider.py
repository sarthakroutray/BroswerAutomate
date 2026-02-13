"""
provider.py — Unified LLM interface.

Consolidates MCP sampling, Anthropic, OpenAI, and Gemini into one call path.
"""

import os
import json
import re
import asyncio
import base64
import logging
from typing import Optional

from ..config import MCP_SAMPLING_TIMEOUT, get_mcp_model_hints

logger = logging.getLogger("browser-agent")

# ── SDK availability flags ────────────────────────────────────────────────────

try:
    from mcp.types import (
        SamplingMessage,
        TextContent,
        ImageContent,
        ModelPreferences,
        ModelHint,
    )
    MCP_SAMPLING_AVAILABLE = True
except ImportError:
    try:
        from mcp.types import SamplingMessage, TextContent, ImageContent
        ModelPreferences = None
        ModelHint = None
        MCP_SAMPLING_AVAILABLE = True
    except ImportError:
        MCP_SAMPLING_AVAILABLE = False
        TextContent = None
        ImageContent = None

try:
    import anthropic
    ANTHROPIC_AVAILABLE = True
except ImportError:
    ANTHROPIC_AVAILABLE = False

try:
    import openai as openai_mod
    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False

try:
    import google.generativeai as genai
    GEMINI_AVAILABLE = True
except ImportError:
    GEMINI_AVAILABLE = False


# ── Response parsing ──────────────────────────────────────────────────────────

def parse_ai_response(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r'^```(?:json)?\s*', '', text)
        text = re.sub(r'\s*```$', '', text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r'\{[\s\S]*\}', text)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
        return {"thinking": text, "actions": [], "done": False}


def extract_code_from_response(response_text: str) -> str:
    cleaned = (response_text or "").strip()
    if not cleaned:
        return ""
    fenced = re.search(r"```(?:[a-zA-Z0-9_+.-]+)?\s*([\s\S]*?)```", cleaned)
    if fenced:
        cleaned = fenced.group(1).strip()
    cleaned = re.sub(r'^```(?:\w+)?\s*\n?', '', cleaned)
    cleaned = re.sub(r'\n?\s*```\s*$', '', cleaned)
    cleaned = cleaned.strip()
    if not cleaned:
        return ""
    markers = [
        "#include", "int main", "using namespace", "public class", "class ",
        "def ", "import ", "from ", "fn main", "package ",
    ]
    for marker in markers:
        idx = cleaned.find(marker)
        if idx > 0:
            cleaned = cleaned[idx:]
            break
    return cleaned.strip()


def looks_like_source_code(candidate: str) -> bool:
    if not candidate:
        return False
    lines = [l for l in candidate.splitlines() if l.strip()]
    if len(lines) < 2:
        return False
    tokens = [";", "{", "}", "#include", "def ", "class ", "import ", "return ", "main("]
    lowered = candidate.lower()
    return any(t.lower() in lowered for t in tokens)


# ── MCP message creation with model-hint fallback ─────────────────────────────

async def create_mcp_message(session, *, messages, max_tokens: int, system_prompt: str):
    base_kwargs = {
        "messages": messages,
        "max_tokens": max_tokens,
        "system_prompt": system_prompt,
    }
    errors = []
    if ModelPreferences is not None and ModelHint is not None:
        for hint in get_mcp_model_hints():
            try:
                kw = dict(base_kwargs)
                kw["model_preferences"] = ModelPreferences(hints=[ModelHint(name=hint)])
                return await session.create_message(**kw)
            except Exception as e:
                errors.append(f"hint '{hint}' failed: {e}")
    try:
        return await session.create_message(**base_kwargs)
    except Exception as e:
        errors.append(f"default sampling failed: {e}")
    raise ValueError("; ".join(errors[-4:]))


def _extract_text_from_result(result) -> str:
    if hasattr(result, "content"):
        if hasattr(result.content, "text"):
            return result.content.text
        if isinstance(result.content, str):
            return result.content
        return str(result.content)
    return str(result)


# ── Unified LLM provider ─────────────────────────────────────────────────────

class LLMProvider:
    """Single entry point for all LLM calls — routes through MCP sampling
    then falls back to direct API keys."""

    def __init__(self, mcp_session_tracker):
        self._tracker = mcp_session_tracker
        self._mcp_session = None

    def set_mcp_session(self, session):
        self._mcp_session = session

    async def ask(
        self,
        *,
        text: str,
        system_prompt: str,
        max_tokens: int = 2048,
        screenshot: Optional[str] = None,
    ) -> str:
        """Returns raw text response from the best available LLM."""
        raw = await self._route(text, system_prompt, max_tokens, screenshot)
        return raw

    async def ask_json(
        self,
        *,
        text: str,
        system_prompt: str,
        max_tokens: int = 2048,
        screenshot: Optional[str] = None,
    ) -> dict:
        """Returns parsed JSON dict from the best available LLM."""
        raw = await self._route(text, system_prompt, max_tokens, screenshot)
        return parse_ai_response(raw)

    async def ask_code(
        self,
        *,
        text: str,
        system_prompt: str,
        max_tokens: int = 4096,
    ) -> str:
        """Returns clean source code from the best available LLM."""
        raw = await self._route(text, system_prompt, max_tokens, None)
        return extract_code_from_response(raw)

    # ── Internal routing ──────────────────────────────────────────────────────

    async def _route(self, text: str, system_prompt: str, max_tokens: int, screenshot: Optional[str]) -> str:
        mcp_errors = []

        # Collect MCP sessions to try
        sessions = []
        if self._mcp_session is not None:
            sessions.append(self._mcp_session)
        global_session = await self._tracker.get_session()
        if global_session is not None and global_session is not self._mcp_session:
            sessions.append(global_session)

        if sessions and MCP_SAMPLING_AVAILABLE:
            for session in sessions:
                # Try with image
                if screenshot:
                    try:
                        result = await asyncio.wait_for(
                            self._via_mcp(text, screenshot, system_prompt, max_tokens, session),
                            timeout=MCP_SAMPLING_TIMEOUT,
                        )
                        return result
                    except Exception as e:
                        mcp_errors.append(f"vision: {e}")
                        logger.warning(f"MCP sampling vision failed: {e}")

                # Try text-only
                try:
                    result = await asyncio.wait_for(
                        self._via_mcp(text, None, system_prompt, max_tokens, session),
                        timeout=MCP_SAMPLING_TIMEOUT,
                    )
                    return result
                except Exception as e:
                    mcp_errors.append(f"text-only: {e}")
                    logger.warning(f"MCP sampling text-only failed: {e}")

        # Direct API fallbacks
        anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
        openai_key = os.environ.get("OPENAI_API_KEY")
        gemini_key = os.environ.get("GEMINI_API_KEY")

        if anthropic_key and ANTHROPIC_AVAILABLE:
            return await self._via_anthropic(text, screenshot, system_prompt, max_tokens, anthropic_key)
        if openai_key and OPENAI_AVAILABLE:
            return await self._via_openai(text, screenshot, system_prompt, max_tokens, openai_key)
        if gemini_key and GEMINI_AVAILABLE:
            return await self._via_gemini(text, screenshot, system_prompt, max_tokens, gemini_key)

        if mcp_errors:
            raise ValueError(
                "MCP sampling failed and no fallback API key is set. "
                f"Last errors: {' | '.join(mcp_errors[-2:])}\n"
                "Set ANTHROPIC_API_KEY / OPENAI_API_KEY / GEMINI_API_KEY."
            )
        raise ValueError(
            "No LLM available. Either:\n"
            "  1. Invoke from your MCP client so sampling session is active\n"
            "  2. Set ANTHROPIC_API_KEY, OPENAI_API_KEY, or GEMINI_API_KEY env var"
        )

    async def _via_mcp(self, text, screenshot, system_prompt, max_tokens, session) -> str:
        msgs = []
        if screenshot and ImageContent is not None:
            msgs.append(SamplingMessage(
                role="user",
                content=ImageContent(type="image", data=screenshot, mimeType="image/png"),
            ))
        msgs.append(SamplingMessage(
            role="user",
            content=TextContent(type="text", text=text),
        ))
        result = await create_mcp_message(
            session, messages=msgs, max_tokens=max_tokens, system_prompt=system_prompt,
        )
        return _extract_text_from_result(result)

    async def _via_anthropic(self, text, screenshot, system_prompt, max_tokens, api_key) -> str:
        client = anthropic.AsyncAnthropic(api_key=api_key)
        content = []
        if screenshot:
            content.append({"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": screenshot}})
        content.append({"type": "text", "text": text})
        resp = await client.messages.create(
            model="claude-sonnet-4-20250514", max_tokens=max_tokens,
            system=system_prompt, messages=[{"role": "user", "content": content}],
        )
        return resp.content[0].text

    async def _via_openai(self, text, screenshot, system_prompt, max_tokens, api_key) -> str:
        client = openai_mod.AsyncOpenAI(api_key=api_key)
        content = []
        if screenshot:
            content.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{screenshot}", "detail": "high"}})
        content.append({"type": "text", "text": text})
        resp = await client.chat.completions.create(
            model="gpt-4o", max_tokens=max_tokens,
            messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": content}],
        )
        return resp.choices[0].message.content

    async def _via_gemini(self, text, screenshot, system_prompt, max_tokens, api_key) -> str:
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel(
            model_name="gemini-2.0-flash-exp",
            system_instruction=system_prompt,
        )
        contents = []
        if screenshot:
            import PIL.Image
            import io
            image_data = base64.b64decode(screenshot)
            image = PIL.Image.open(io.BytesIO(image_data))
            contents.append(image)
        contents.append(text)
        resp = await asyncio.to_thread(
            model.generate_content,
            contents,
            generation_config={"max_output_tokens": max_tokens},
        )
        return resp.text
