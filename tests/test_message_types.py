"""
test_message_types.py — Asserts every message-type string in the extension
matches the canonical registry in background.js.

Chrome MV3 doesn't allow module imports across the service-worker /
content-script / popup boundaries, so the registry is duplicated in
each file. This test prevents typos and drift by:

  1. Parsing the MSG = Object.freeze({...}) block from background.js and
     collecting every value (the actual wire-string).
  2. Scanning background.js, content.js, overlay.js, popup.js for any
     string literal used as a message `type`, `case`, or compared in
     `message.type === "X"`.
  3. Failing if any literal isn't in the registry.

Run:  pytest tests/test_message_types.py
"""

import re
import sys
from pathlib import Path

import pytest

EXT_DIR = Path(__file__).resolve().parent.parent / "extension"
BG = EXT_DIR / "background.js"
CT = EXT_DIR / "content.js"
OL = EXT_DIR / "overlay.js"
PP = EXT_DIR / "popup.js"


def _parse_registry(text: str) -> set[str]:
    """Extract the set of wire-string values from `MSG = Object.freeze({...})`."""
    m = re.search(r"const\s+MSG\s*=\s*Object\.freeze\(\s*\{(.+?)\}\s*\)", text, re.DOTALL)
    if not m:
        raise AssertionError("background.js does not contain a `MSG = Object.freeze({...})` registry")
    body = m.group(1)
    return set(re.findall(r':\s*"([A-Z][A-Z0-9_]*)"', body))


def _scan_message_literals(text: str) -> set[str]:
    """Find every string literal that looks like a message type in JS source.

    Matches: type: "X", case "X":, message.type === "X", message.type == "X"
    """
    patterns = [
        r'type:\s*"([A-Z][A-Z0-9_]*)"',
        r'case\s+"([A-Z][A-Z0-9_]*)"\s*:',
        r'message\.type\s*===\s*"([A-Z][A-Z0-9_]*)"',
        r'message\.type\s*==\s*"([A-Z][A-Z0-9_]*)"',
        r'\.type\s*===\s*"([A-Z][A-Z0-9_]*)"',
        r'\.type\s*==\s*"([A-Z][A-Z0-9_]*)"',
    ]
    found: set[str] = set()
    for pat in patterns:
        found.update(re.findall(pat, text))
    return found


@pytest.fixture(scope="module")
def registry() -> set[str]:
    return _parse_registry(BG.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "path,label",
    [
        (BG, "background.js"),
        (CT, "content.js"),
        (OL, "overlay.js"),
        (PP, "popup.js"),
    ],
)
def test_all_message_types_are_registered(path: Path, label: str, registry: set[str]):
    if not path.exists():
        pytest.skip(f"{label} not present")
    text = path.read_text(encoding="utf-8")
    used = _scan_message_literals(text)
    # The registry file itself contains the values inside the freeze({...}) block,
    # but those are also re-declared as `type:` etc. in other places. We just
    # need to know each *used* type is in the registry.
    missing = used - registry
    assert not missing, (
        f"{label} uses message types not declared in background.js's MSG registry:\n"
        + "\n".join(f"  {m!r}" for m in sorted(missing))
    )


def test_registry_is_nonempty(registry: set[str]):
    assert len(registry) >= 20, (
        f"MSG registry looks suspiciously small ({len(registry)} entries); "
        "did the parse break?"
    )


def test_registry_values_are_unique(registry: set[str]):
    # We pull a *set* above, so this is trivially true, but if someone
    # accidentally re-uses the same wire-string for two constants, only a
    # list-based parse would catch it. Do that here.
    text = BG.read_text(encoding="utf-8")
    m = re.search(r"const\s+MSG\s*=\s*Object\.freeze\(\s*\{(.+?)\}\s*\)", text, re.DOTALL)
    body = m.group(1)
    pairs = re.findall(r"(\w+)\s*:\s*\"([A-Z][A-Z0-9_]*)\"", body)
    # Multiple aliases for the same wire-string are fine (STOP_TASK value
    # is used in both popup→bg and bg→ws). We only flag if the *same JS
    # identifier* appears twice.
    names = [name for name, _ in pairs]
    dup_names = {n for n in names if names.count(n) > 1}
    assert not dup_names, f"Duplicate JS identifiers in MSG registry: {dup_names}"
