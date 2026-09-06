"""Getting the digest to the user without the user having to come and look.

"Before anybody" is a property of being told, not of having a dashboard. Two
channels to start: the macOS notification centre (free, local, immediate) and a
Slack incoming webhook (the one most people already have). Both are fire and
forget: a failed notification must never fail the sweep that produced it.
"""

from __future__ import annotations

import logging
import platform
import subprocess

import httpx

log = logging.getLogger("gittrack.notify")


def macos(title: str, body: str, subtitle: str = "") -> bool:
    if platform.system() != "Darwin":
        return False
    # osascript takes the strings as AppleScript literals: escape the quotes.
    q = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')  # noqa: E731
    script = (f'display notification "{q(body)}" with title "{q(title)}"'
              + (f' subtitle "{q(subtitle)}"' if subtitle else ""))
    try:
        subprocess.run(["osascript", "-e", script], check=True,
                       capture_output=True, timeout=10)
        return True
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("macOS notification failed: %s", exc)
        return False


def slack(webhook: str, text: str) -> bool:
    if not webhook:
        return False
    try:
        r = httpx.post(webhook, json={"text": text}, timeout=10)
        r.raise_for_status()
        return True
    except httpx.HTTPError as exc:
        log.warning("Slack notification failed: %s", exc)
        return False


def format_items(items: list[dict], header: str) -> str:
    """One line per repo, reasons after a dash - readable in a notification."""
    lines = [header]
    for it in items:
        lines.append(f"* {it['full_name']} - {'; '.join(it['reasons'])}")
    return "\n".join(lines)


def send(cfg, items: list[dict], header: str) -> dict:
    """Dispatch to every configured channel; report what actually went out."""
    if not items:
        return {"macos": False, "slack": False}
    text = format_items(items, header)
    out = {"macos": False, "slack": False}
    if cfg.notify.macos:
        first = items[0]
        body = (f"{first['full_name']}: {first['reasons'][0]}"
                + (f"  (+{len(items) - 1} more)" if len(items) > 1 else ""))
        out["macos"] = macos("gittrack", body, subtitle=header)
    if cfg.notify.slack_webhook:
        out["slack"] = slack(cfg.notify.slack_webhook, text)
    return out
