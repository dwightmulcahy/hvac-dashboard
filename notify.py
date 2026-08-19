"""
HVAC Dashboard — generic outbound notification webhook.

Separate from the `watchtower_webhook` setting in auth.py/system.py,
which specifically forwards Watchtower's own "image updated" POST.
This module is invoked directly by our own code for events the user
opted into via the `notification_webhook` setting: device offline,
maintenance overdue, schedule command failures.

Only depends on state.py and httpx, so it can be imported from worker.py
without violating the one-way dependency graph in docs/ARCHITECTURE.md.
"""

import httpx

from state import _state, _add_log


async def notify(text: str, title: str = "HVAC Dashboard"):
    """Best-effort POST to the user's configured notification webhook.
    No-ops silently if none is configured. Never raises — a failed
    notification should never take down the caller (worker loop,
    request handler, etc.)."""
    url = _state["settings"].get("notification_webhook", "")
    if not url:
        return
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(url, json={"text": text, "title": title})
    except Exception as e:
        _add_log(f"Notification webhook failed: {e}", "warn")
