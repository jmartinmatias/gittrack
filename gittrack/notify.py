"""Getting the digest to the user without the user having to come and look.

"Before anybody" is a property of being told, not of having a dashboard. Five
channels, all fire-and-forget: a failed notification must never fail the sweep
that produced it.

  macos    notification centre. Free and immediate, but easily missed on a
           laptop that sleeps, so rarely sufficient on its own.
  slack    an incoming webhook.
  ntfy     https://ntfy.sh - push to a phone with no account and no app config
           beyond subscribing to a topic. The best effort-to-reach ratio here.
  webhook  any URL, for Discord, Teams, n8n, Zapier and the rest.
  email    plain SMTP. The password is read from an environment variable named
           in the config, never stored in the file.
"""

from __future__ import annotations

import logging
import os
import platform
import smtplib
import subprocess
from email.message import EmailMessage

import httpx

log = logging.getLogger("gittrack.notify")


def format_items(items: list[dict], header: str) -> str:
    """One line per repo, reasons after a dash: readable in any channel."""
    lines = [header]
    for it in items:
        lines.append(f"* {it['full_name']} - {'; '.join(it['reasons'])}")
        lines.append(f"  {it.get('url', '')}")
    return "\n".join(lines).rstrip()


def macos(title: str, body: str, subtitle: str = "") -> bool:
    if platform.system() != "Darwin":
        return False
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
        httpx.post(webhook, json={"text": text}, timeout=10).raise_for_status()
        return True
    except httpx.HTTPError as exc:
        log.warning("Slack notification failed: %s", exc)
        return False


def ntfy(topic_url: str, title: str, body: str, click: str | None = None) -> bool:
    if not topic_url:
        return False
    headers = {"Title": title, "Tags": "chart_with_upwards_trend"}
    if click:
        headers["Click"] = click
    try:
        httpx.post(topic_url, content=body.encode(), headers=headers,
                   timeout=10).raise_for_status()
        return True
    except httpx.HTTPError as exc:
        log.warning("ntfy notification failed: %s", exc)
        return False


def webhook(url: str, payload: dict) -> bool:
    if not url:
        return False
    try:
        httpx.post(url, json=payload, timeout=10).raise_for_status()
        return True
    except httpx.HTTPError as exc:
        log.warning("webhook notification failed: %s", exc)
        return False


def email(cfg, subject: str, body: str) -> bool:
    n = cfg.notify
    if not (n.smtp_host and n.email_from and n.email_to):
        return False
    password = os.environ.get(n.smtp_password_env, "")
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, n.email_from, n.email_to
    msg.set_content(body)
    try:
        with smtplib.SMTP(n.smtp_host, n.smtp_port, timeout=20) as smtp:
            smtp.ehlo()
            if n.smtp_port != 25:
                smtp.starttls()
            if n.smtp_user:
                smtp.login(n.smtp_user, password)
            smtp.send_message(msg)
        return True
    except (OSError, smtplib.SMTPException) as exc:
        log.warning("email notification failed: %s", exc)
        return False


def send(cfg, items: list[dict], header: str) -> dict:
    """Dispatch to every configured channel; report what actually went out."""
    out = {"macos": False, "slack": False, "ntfy": False, "webhook": False, "email": False}
    if not items:
        return out
    text = format_items(items, header)
    first = items[0]
    short = (f"{first['full_name']}: {first['reasons'][0]}"
             + (f"  (+{len(items) - 1} more)" if len(items) > 1 else ""))
    n = cfg.notify
    if n.macos:
        out["macos"] = macos("gittrack", short, subtitle=header)
    if n.slack_webhook:
        out["slack"] = slack(n.slack_webhook, text)
    if n.ntfy:
        out["ntfy"] = ntfy(n.ntfy, header, text, click=first.get("url"))
    if n.webhook:
        out["webhook"] = webhook(n.webhook, {"title": header, "text": text, "items": items})
    if n.smtp_host:
        out["email"] = email(cfg, f"gittrack: {header}", text)
    return out


def channels_configured(cfg) -> list[str]:
    n = cfg.notify
    return [name for name, on in (("macos", n.macos), ("slack", bool(n.slack_webhook)),
                                  ("ntfy", bool(n.ntfy)), ("webhook", bool(n.webhook)),
                                  ("email", bool(n.smtp_host and n.email_to))) if on]
