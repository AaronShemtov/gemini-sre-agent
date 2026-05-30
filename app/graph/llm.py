"""
Gemini wrapper.

Two distinct LLM calls in the investigation flow:

1. plan_investigation — given the user's question and the list of
   namespaces, decide which read-only tools to call and with what args.
   Returns strict JSON so the graph can execute the plan deterministically.

2. analyse — given everything collected, produce the RCA: probable root
   cause, timeline, blast radius, suggested fix, and optionally a proposed
   safe remediation (which tool + args) that the graph will gate.

We use gemini-2.0-flash (free tier, fast, good enough for this). The model
name is overridable via env in config.
"""

from __future__ import annotations

import json
import os
import logging

import google.generativeai as genai
from google.api_core.exceptions import ResourceExhausted
# Fixed import here: added stop_after_delay
from tenacity import retry, wait_exponential, retry_if_exception_type, stop_after_delay, before_sleep_log

from app.tools.registry import tool_catalog

logger = logging.getLogger(__name__)

_API_KEY = os.environ.get("GEMINI_API_KEY", "")
_MODEL = os.environ.get("GEMINI_MODEL", "gemini-1.5-flash")

genai.configure(api_key=_API_KEY)
_model = genai.GenerativeModel(_MODEL)

# Retry mechanism for 429 ResourceExhausted (Free Tier RPM Limits)
# Waits 4s, then 8s, 16s, 32s...
retry_429 = retry(
    retry=retry_if_exception_type(ResourceExhausted),
    wait=wait_exponential(multiplier=2, min=4, max=60),
    stop=stop_after_delay(180), # Wait up to 3 minutes for quota recovery
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True
)

def _extract_json(text: str) -> dict:
    """Gemini sometimes wraps JSON in ```json fences or adds prose.
    Strip that and parse. Raises ValueError if nothing parseable."""
    t = text.strip()
    # Remove markdown fences.
    if "```" in t:
        # take the content of the first fenced block
        chunks = t.split("```")
        for c in chunks:
            c = c.strip()
            if c.startswith("json"):
                c = c[4:].strip()
            if c.startswith("{") and c.endswith("}"):
                t = c
                break
    # Last resort: slice from first { to last }.
    if not (t.startswith("{") and t.endswith("}")):
        start, end = t.find("{"), t.rfind("}")
        if start != -1 and end != -1:
            t = t[start : end + 1]
    return json.loads(t)


PLAN_SYSTEM = """You are an SRE assistant investigating a Kubernetes issue on an OKE cluster.

Available READ-ONLY tools:
{tools}

Given the user's question, produce an investigation plan: an ordered list of
read-only tool calls to gather the signals needed for a root-cause analysis.

Rules:
- Use ONLY read-only tools in the plan (never mutating ones).
- Always start broad (get_pods, get_events) then narrow (describe_pod,
  get_pod_logs on the specific failing pod).
- You do not know pod names up front — plan get_pods / get_events first;
  the orchestrator will run a SECOND planning round once it has results.
- Resolve the namespace from the question. If unclear, use the most likely
  one from this list: {namespaces}

Respond with STRICT JSON only, no prose, no markdown:
{{"namespace": "<ns>", "calls": [{{"tool": "<name>", "args": {{...}}}}]}}
"""

REPLAN_SYSTEM = """You are continuing a Kubernetes investigation.

You already ran initial discovery. Here are the results so far:
{collected}

Available READ-ONLY tools:
{tools}

Decide what to inspect next to pin down the root cause. Typically: pick the
specific failing pod(s) from the results and call describe_pod and
get_pod_logs on them, and get_deployment / get_service / get_endpoints if
the issue looks like a rollout or routing problem.

If you already have enough to explain the issue, return an empty calls list.

Respond with STRICT JSON only:
{{"calls": [{{"tool": "<name>", "args": {{...}}}}], "enough": <true|false>}}
"""

ANALYSE_SYSTEM = """You are a senior SRE producing a root-cause analysis for an OKE Kubernetes issue.

User question:
{question}

All signals collected from the cluster:
{collected}

Produce a concise, structured analysis. Be specific — cite the actual pod
names, reasons, exit codes, and log lines you saw. Do not invent anything
not present in the signals.

If a SAFE remediation is warranted, you may propose exactly ONE of these
mutating actions:
- rollout_restart(namespace, deployment)
- patch_service_selector(namespace, service, selector)

Only propose remediation if the evidence clearly supports it. Many issues
(image pull errors, OOM, missing env, bad config) are NOT fixed by these
two actions — in those cases propose no action and explain the manual fix.

Respond with STRICT JSON only:
{{
  "root_cause": "<one or two sentences>",
  "timeline": ["<event>", "..."],
  "blast_radius": "<what is affected>",
  "evidence": ["<specific signal>", "..."],
  "suggested_fix": "<human steps>",
  "proposed_action": null OR {{"tool": "<rollout_restart|patch_service_selector>", "args": {{...}}, "why": "<reason>"}}
}}
"""


@retry_429
def plan_investigation(question: str, namespaces: list[str]) -> dict:
    logger.info(f"📡 [Gemini API] Отправка запроса: plan_investigation")
    prompt = PLAN_SYSTEM.format(
        tools=tool_catalog(), namespaces=", ".join(namespaces)
    ) + f"\n\nUser question: {question}"
    resp = _model.generate_content(prompt)
    return _extract_json(resp.text)

@retry_429
def replan(collected: str) -> dict:
    logger.info(f"📡 [Gemini API] Отправка запроса: replan")
    prompt = REPLAN_SYSTEM.format(collected=collected, tools=tool_catalog())
    resp = _model.generate_content(prompt)
    return _extract_json(resp.text)

@retry_429
def analyse(question: str, collected: str) -> dict:
    logger.info(f"📡 [Gemini API] Отправка запроса: analyse")
    prompt = ANALYSE_SYSTEM.format(question=question, collected=collected)
    resp = _model.generate_content(prompt)
    return _extract_json(resp.text)