"""
Gemini wrapper — single-call investigation.

Design for the free tier (gemini-2.5-flash-lite: RPM 10, RPD 20):
the whole investigation is ONE Gemini call, not three. The cluster
signals are gathered up-front by deterministic code (a fixed, broad
sweep of read tools), then a single prompt turns them into the RCA.

Why one call:
- RPD is only 20/day. Three calls per investigation = ~6 investigations
  a day. One call = ~20. 3x the daily budget for the same quota.
- RPM is 10, so a single call never risks the per-minute limit and we
  don't need artificial sleeps between steps.

The model name comes from env (GEMINI_MODEL); the usage tracker reads
the SAME env so the stats line never lies about which model / limit.
"""

from __future__ import annotations

import json
import os
import logging
import threading

import google.generativeai as genai
from google.api_core.exceptions import ResourceExhausted

from app.tools.registry import tool_catalog

logger = logging.getLogger(__name__)

_API_KEY = os.environ.get("GEMINI_API_KEY", "")
_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash-lite")

genai.configure(api_key=_API_KEY)
_model = genai.GenerativeModel(_MODEL)


# ---------------------------------------------------------------------
# API usage tracker (local, per-session)
# ---------------------------------------------------------------------
# Free-tier requests-per-day per model. Source: Google AI Studio rate
# limit page. These are the FREE-TIER RPD values; if you switch to a
# billed key the real limit is far higher and this estimate is moot.
_FREE_TIER_RPD = {
    "gemini-2.5-flash-lite": 20,
    "gemini-2.5-flash": 20,
    "gemini-3.5-flash": 20,
    "gemini-1.5-flash": 50,
}


class ApiUsageTracker:
    """Counts Gemini calls since process start, to estimate remaining
    daily free-tier quota. This is a LOCAL estimate -- it resets when the
    pod restarts and doesn't know about calls from other sessions, so
    treat it as a rough guide, not ground truth."""

    def __init__(self) -> None:
        self.session_requests = 0
        self.lock = threading.Lock()

    def increment(self) -> None:
        with self.lock:
            self.session_requests += 1

    def get_telegram_stats(self) -> str:
        with self.lock:
            rpd = _FREE_TIER_RPD.get(_MODEL)
            if rpd is None:
                return (
                    f"\n\nAPI usage (this session)\n"
                    f"- Model: {_MODEL}\n"
                    f"- Requests this session: {self.session_requests}"
                )
            remaining = max(rpd - self.session_requests, 0)
            return (
                f"\n\nAPI usage (this session)\n"
                f"- Model: {_MODEL}\n"
                f"- Requests this session: {self.session_requests}\n"
                f"- Est. free-tier quota left today: ~{remaining}/{rpd} "
                f"(local estimate, resets on pod restart)"
            )


usage_tracker = ApiUsageTracker()


# ---------------------------------------------------------------------
# JSON extraction
# ---------------------------------------------------------------------
def _extract_json(text: str) -> dict:
    """Strip markdown fences and parse the first JSON object."""
    t = text.strip()
    if "```" in t:
        chunks = t.split("```")
        for c in chunks:
            c = c.strip()
            if c.startswith("json"):
                c = c[4:].strip()
            if c.startswith("{") and c.endswith("}"):
                t = c
                break
    if not (t.startswith("{") and t.endswith("}")):
        start, end = t.find("{"), t.rfind("}")
        if start != -1 and end != -1:
            t = t[start : end + 1]
    return json.loads(t)


class QuotaExceeded(Exception):
    """Raised when the daily free-tier quota is exhausted. The bot turns
    this into a friendly message instead of a stack trace."""

    def __init__(self, retry_seconds=None):
        self.retry_seconds = retry_seconds
        super().__init__("Gemini daily free-tier quota exhausted")


# ---------------------------------------------------------------------
# Prompt -- ONE call does analysis over pre-gathered signals
# ---------------------------------------------------------------------
ANALYSE_SYSTEM = """You are a senior SRE producing a root-cause analysis for a Kubernetes issue on an OKE cluster.

The user asked:
{question}

A broad set of read-only signals has already been gathered from the cluster
in the namespace `{namespace}`. Here they are (JSON):

{collected}

Read tools that were available (all read-only):
{tools}

Analyse the signals and produce a concise, specific RCA. Cite actual pod
names, reasons, exit codes, restart counts and log lines you see in the
signals. Do NOT invent anything that is not present in the data. If the
signals are insufficient to be sure, say so in root_cause.

REMEDIATION -- you MAY propose exactly ONE safe action, but only if the
evidence clearly supports it:
  - rollout_restart(namespace, deployment)
      use when pods are wedged/stale and a fresh roll would clear it
      (e.g. stuck after a config/secret change, hung process, transient
      bad state) -- NOT for image-pull errors, OOM, missing env/config,
      or crashes that a restart won't fix.
  - patch_service_selector(namespace, service, selector)
      use ONLY when a Service has the wrong selector and its endpoints are
      empty because labels don't match the pods. Provide the corrected
      selector that matches the actual pod labels seen in the signals.

If no safe action clearly applies, set proposed_action to null and explain
the manual fix in suggested_fix instead.

Respond with STRICT JSON only, no prose, no markdown:
{{
  "root_cause": "<one or two sentences>",
  "timeline": ["<event>", "..."],
  "blast_radius": "<what is affected>",
  "evidence": ["<specific signal>", "..."],
  "suggested_fix": "<human steps>",
  "proposed_action": null
}}

OR, when a safe remediation applies, proposed_action becomes one of:
  {{"tool": "rollout_restart", "args": {{"namespace": "<ns>", "deployment": "<name>"}}, "why": "<reason>"}}
  {{"tool": "patch_service_selector", "args": {{"namespace": "<ns>", "service": "<name>", "selector": {{"app": "<value>"}}}}, "why": "<reason>"}}
"""


def _call(prompt: str) -> dict:
    """Single Gemini call with quota-aware error handling. No long retry:
    on the free tier a 429 is usually the DAILY limit, which won't recover
    in seconds -- so we surface it immediately as QuotaExceeded rather than
    blocking the bot for minutes."""
    usage_tracker.increment()
    logger.info("[API] Gemini call #%d (model=%s)", usage_tracker.session_requests, _MODEL)
    try:
        resp = _model.generate_content(prompt)
    except ResourceExhausted as e:
        secs = None
        try:
            secs = int(getattr(e, "retry_delay", None).seconds)
        except Exception:
            pass
        logger.warning("Gemini quota exhausted (429). retry approx %ss", secs)
        raise QuotaExceeded(secs) from e
    return _extract_json(resp.text)


def investigate(question: str, namespace: str, collected: str) -> dict:
    """The single LLM call: turn gathered signals into an RCA (+ optional
    safe remediation). Returns the analysis dict."""
    prompt = ANALYSE_SYSTEM.format(
        question=question,
        namespace=namespace,
        collected=collected,
        tools=tool_catalog(),
    )
    analysis = _call(prompt)
    if isinstance(analysis, dict) and "suggested_fix" in analysis:
        analysis["suggested_fix"] = str(analysis["suggested_fix"]) + usage_tracker.get_telegram_stats()
    return analysis
