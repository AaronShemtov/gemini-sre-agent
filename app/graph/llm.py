"""
Gemini wrapper.

Two distinct LLM calls in the investigation flow:
1. plan_investigation
2. analyse

Includes a local API Usage Tracker to monitor Free Tier requests
and appends the statistics directly to the Telegram output.
"""

from __future__ import annotations

import json
import os
import logging
import threading

import google.generativeai as genai
from google.api_core.exceptions import ResourceExhausted
from tenacity import retry, wait_exponential, retry_if_exception_type, stop_after_delay, before_sleep_log

from app.tools.registry import tool_catalog

logger = logging.getLogger(__name__)

_API_KEY = os.environ.get("GEMINI_API_KEY", "")
_MODEL = os.environ.get("GEMINI_MODEL", "gemini-1.5-flash")

genai.configure(api_key=_API_KEY)
_model = genai.GenerativeModel(_MODEL)


# ─────────────────────────────────────────────────────────────────────
# API Usage Tracker (Local Counter)
# ─────────────────────────────────────────────────────────────────────
class ApiUsageTracker:
    """Tracks local API calls since the bot started to estimate quota usage."""
    def __init__(self):
        self.session_requests = 0
        self.lock = threading.Lock()

    def increment(self) -> None:
        with self.lock:
            self.session_requests += 1

    def get_telegram_stats(self) -> str:
        with self.lock:
            return (
                f"\n\n📊 API Usage Stats:\n"
                f"• Requests this session: {self.session_requests}\n"
                f"• Estimated daily quota remaining (1.5-flash): {1500 - self.session_requests}/1500"
            )

# Global tracker instance
usage_tracker = ApiUsageTracker()
# ─────────────────────────────────────────────────────────────────────


retry_429 = retry(
    retry=retry_if_exception_type(ResourceExhausted),
    wait=wait_exponential(multiplier=2, min=4, max=60),
    stop=stop_after_delay(180), # Wait up to 3 minutes for quota recovery
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True
)

def _extract_json(text: str) -> dict:
    """Strips markdown fences and parses JSON."""
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


PLAN_SYSTEM = """You are an SRE assistant investigating a Kubernetes issue on an OKE cluster.

Available READ-ONLY tools:
{tools}

Given the user's question, produce an investigation plan.
Rules:
- Use ONLY read-only tools in the plan.
- Resolve the namespace from the question or use: {namespaces}

Respond with STRICT JSON only:
{{"namespace": "<ns>", "calls": [{{"tool": "<name>", "args": {{...}}}}]}}
"""

REPLAN_SYSTEM = """You are continuing a Kubernetes investigation.

Results so far:
{collected}
Available READ-ONLY tools: {tools}

Decide what to inspect next.
Respond with STRICT JSON only:
{{"calls": [{{"tool": "<name>", "args": {{...}}}}], "enough": <true|false>}}
"""

ANALYSE_SYSTEM = """You are a senior SRE producing a root-cause analysis for an OKE Kubernetes issue.

Question: {question}
Signals: {collected}

Produce a concise, structured analysis.

Respond with STRICT JSON only:
{{
  "root_cause": "<one or two sentences>",
  "timeline": ["<event>", "..."],
  "blast_radius": "<what is affected>",
  "evidence": ["<specific signal>", "..."],
  "suggested_fix": "<human steps>",
  "proposed_action": null
}}
"""

@retry_429
def plan_investigation(question: str, namespaces: list[str]) -> dict:
    usage_tracker.increment()
    logger.info(f"[API Tracker] Executing plan_investigation (Total: {usage_tracker.session_requests})")
    
    prompt = PLAN_SYSTEM.format(
        tools=tool_catalog(), namespaces=", ".join(namespaces)
    ) + f"\n\nUser question: {question}"
    
    resp = _model.generate_content(prompt)
    return _extract_json(resp.text)

@retry_429
def replan(collected: str) -> dict:
    usage_tracker.increment()
    logger.info(f"[API Tracker] Executing replan (Total: {usage_tracker.session_requests})")
    
    prompt = REPLAN_SYSTEM.format(collected=collected, tools=tool_catalog())
    resp = _model.generate_content(prompt)
    return _extract_json(resp.text)

@retry_429
def analyse(question: str, collected: str) -> dict:
    usage_tracker.increment()
    logger.info(f"[API Tracker] Executing analyse (Total: {usage_tracker.session_requests})")
    
    prompt = ANALYSE_SYSTEM.format(question=question, collected=collected)
    resp = _model.generate_content(prompt)
    
    # Parse the LLM response
    analysis_data = _extract_json(resp.text)
    
    # Inject API usage stats directly into the Telegram output field
    if "suggested_fix" in analysis_data:
        analysis_data["suggested_fix"] += usage_tracker.get_telegram_stats()
        
    return analysis_data