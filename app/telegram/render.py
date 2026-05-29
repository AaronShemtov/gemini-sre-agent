"""
Render the analysis dict into a Telegram-friendly HTML message.

Telegram HTML parse_mode supports a small tag set: <b> <i> <code> <pre>
<a>. We keep messages compact — Telegram hard-limits a message to 4096
chars, so we trim evidence/timeline if needed.
"""

from __future__ import annotations

import html
from typing import Any


def _esc(s: Any) -> str:
    return html.escape(str(s))


def render_analysis(analysis: dict[str, Any], namespace: str) -> str:
    lines: list[str] = []
    lines.append(f"🔍 <b>RCA</b> · <code>{_esc(namespace)}</code>")
    lines.append("")
    lines.append(f"<b>Root cause</b>\n{_esc(analysis.get('root_cause', 'n/a'))}")

    blast = analysis.get("blast_radius")
    if blast:
        lines.append(f"\n<b>Blast radius</b>\n{_esc(blast)}")

    timeline = analysis.get("timeline") or []
    if timeline:
        lines.append("\n<b>Timeline</b>")
        for t in timeline[:8]:
            lines.append(f"• {_esc(t)}")

    evidence = analysis.get("evidence") or []
    if evidence:
        lines.append("\n<b>Evidence</b>")
        for e in evidence[:8]:
            lines.append(f"• {_esc(e)}")

    fix = analysis.get("suggested_fix")
    if fix:
        lines.append(f"\n<b>Suggested fix</b>\n{_esc(fix)}")

    msg = "\n".join(lines)
    # Telegram limit guard.
    if len(msg) > 3500:
        msg = msg[:3490] + "\n…(truncated)"
    return msg


def render_action_prompt(action: dict[str, Any]) -> str:
    """The approval prompt shown with Yes/No buttons."""
    args = action.get("args", {})
    arg_str = ", ".join(f"{k}={v}" for k, v in args.items())
    return (
        f"⚠️ <b>Proposed remediation</b>\n\n"
        f"<code>{_esc(action.get('tool'))}({_esc(arg_str)})</code>\n\n"
        f"<b>Why</b>: {_esc(action.get('why', 'n/a'))}\n\n"
        f"Approve this action?"
    )


def render_action_result(action: dict[str, Any], result: dict[str, Any], verification: str) -> str:
    args = action.get("args", {})
    arg_str = ", ".join(f"{k}={v}" for k, v in args.items())
    out = [f"✅ <b>Executed</b>: <code>{_esc(action.get('tool'))}({_esc(arg_str)})</code>"]
    if isinstance(result, dict) and result.get("error"):
        out = [f"❌ <b>Action failed</b>: <code>{_esc(action.get('tool'))}</code>",
               f"<code>{_esc(result['error'])}</code>"]
        return "\n".join(out)
    if verification:
        out.append(f"\n<b>Verification</b>: {_esc(verification)}")
    return "\n".join(out)
