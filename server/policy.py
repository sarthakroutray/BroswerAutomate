"""
policy.py — Minimal action safety policy layer.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlparse


LOW_RISK_ACTIONS = {
    "click",
    "type",
    "clear",
    "select",
    "check",
    "uncheck",
    "press_key",
    "scroll",
    "hover",
    "wait",
    "double_click",
    "focus",
    "go_back",
    "go_forward",
    "reload",
}
MEDIUM_RISK_ACTIONS = {"close_tab", "switch_tab"}
HIGH_RISK_ACTIONS = {"submit", "click_at", "navigate", "new_tab"}


def _parse_allowlist() -> set[str]:
    raw = os.getenv("BROWSER_ALLOWED_DOMAINS", "").strip()
    if not raw:
        return {"localhost", "127.0.0.1"}
    return {entry.strip().lower() for entry in raw.split(",") if entry.strip()}


ALLOWED_DOMAINS = _parse_allowlist()


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    code: str
    message: str
    risk_level: str
    data: dict


def _hostname_from_url(url: str | None) -> str:
    if not url:
        return ""
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def _is_allowed_domain(hostname: str) -> bool:
    if not hostname:
        return False
    for allowed in ALLOWED_DOMAINS:
        if hostname == allowed or hostname.endswith(f".{allowed}"):
            return True
    return False


def get_action_risk(action: str) -> str:
    action_name = str(action or "").strip().lower()
    if action_name in HIGH_RISK_ACTIONS:
        return "high"
    if action_name in MEDIUM_RISK_ACTIONS:
        return "medium"
    return "low"


def evaluate_action(
    *,
    action: str,
    current_url: str = "",
    target_url: str = "",
    allow_unsafe: bool = False,
) -> PolicyDecision:
    action_name = str(action or "").strip().lower()
    risk_level = get_action_risk(action_name)
    current_host = _hostname_from_url(current_url)
    target_host = _hostname_from_url(target_url)
    effective_host = target_host or current_host

    if risk_level == "low":
        return PolicyDecision(True, "POLICY_ALLOWED", "Action allowed", risk_level, {
            "current_domain": current_host,
            "target_domain": target_host,
        })

    if risk_level == "medium" and allow_unsafe:
        return PolicyDecision(True, "POLICY_ALLOWED", "Action allowed by explicit override", risk_level, {
            "current_domain": current_host,
            "target_domain": target_host,
            "override": True,
        })

    if risk_level == "high":
        if allow_unsafe:
            return PolicyDecision(True, "POLICY_ALLOWED", "High-risk action allowed by explicit override", risk_level, {
                "current_domain": current_host,
                "target_domain": target_host,
                "override": True,
            })
        if effective_host and _is_allowed_domain(effective_host):
            return PolicyDecision(True, "POLICY_ALLOWED", "High-risk action allowed for allowlisted domain", risk_level, {
                "current_domain": current_host,
                "target_domain": target_host,
                "allowlisted_domain": effective_host,
            })
        return PolicyDecision(
            False,
            "POLICY_CONFIRMATION_REQUIRED",
            (
                f"High-risk action '{action_name}' is blocked for domain "
                f"'{effective_host or 'unknown'}'. Add the domain to BROWSER_ALLOWED_DOMAINS "
                "or pass allow_unsafe=true."
            ),
            risk_level,
            {
                "current_domain": current_host,
                "target_domain": target_host,
                "allowed_domains": sorted(ALLOWED_DOMAINS),
            },
        )

    if risk_level == "medium" and effective_host and not _is_allowed_domain(effective_host):
        return PolicyDecision(
            False,
            "POLICY_CONFIRMATION_REQUIRED",
            (
                f"Medium-risk action '{action_name}' is blocked for domain "
                f"'{effective_host}'. Add the domain to BROWSER_ALLOWED_DOMAINS "
                "or pass allow_unsafe=true."
            ),
            risk_level,
            {
                "current_domain": current_host,
                "target_domain": target_host,
                "allowed_domains": sorted(ALLOWED_DOMAINS),
            },
        )

    return PolicyDecision(True, "POLICY_ALLOWED", "Action allowed", risk_level, {
        "current_domain": current_host,
        "target_domain": target_host,
    })


def evaluate_action_batch(
    *,
    actions: list[dict],
    current_url: str = "",
    allow_unsafe: bool = False,
) -> PolicyDecision:
    highest_risk = "low"
    for step in actions:
        if not isinstance(step, dict):
            continue
        target_url = ""
        action_name = str(step.get("action", "")).strip().lower()
        if action_name in {"navigate", "new_tab"}:
            target_url = str(step.get("value") or step.get("url") or "").strip()
        decision = evaluate_action(
            action=action_name,
            current_url=current_url,
            target_url=target_url,
            allow_unsafe=allow_unsafe,
        )
        if not decision.allowed:
            return decision
        if decision.risk_level == "high":
            highest_risk = "high"
        elif decision.risk_level == "medium" and highest_risk != "high":
            highest_risk = "medium"

    return PolicyDecision(
        True,
        "POLICY_ALLOWED",
        "Action batch allowed",
        highest_risk,
        {
            "current_domain": _hostname_from_url(current_url),
            "allowed_domains": sorted(ALLOWED_DOMAINS),
        },
    )
