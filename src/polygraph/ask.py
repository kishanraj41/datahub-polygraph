"""``polygraph ask`` — two backends over one set of tools.

**Deterministic (default).** A keyword router. It is not an agent and this
module does not pretend otherwise: it matches the question against a small
intent table, calls the matching tool, and renders the result. It needs no API
key, produces identical output for identical input, and therefore survives the
fresh-clone gate. When it cannot classify a question it says so and lists what
it *can* answer, rather than guessing at the closest intent — a router that
silently answers a different question than the one asked is worse than one that
declines.

**LLM (``--llm``).** A real tool-use loop against the Anthropic API. The model
gets the six functions Polygraph's own MCP server exposes **plus two tools that
proxy to DataHub's MCP Server**, decides which to call, and writes the answer
from what they return. Requires ``ANTHROPIC_API_KEY``. It is gated behind a flag
rather than being the default precisely so that a judge cloning the repo can
reproduce every documented result without credentials.

Why two servers in one loop. Polygraph's tools return *evidence* -- what a
runtime capture proved. DataHub's tools return *testimony* -- what the catalog
claims. The interesting questions need both: "this undeclared source is real,
is it registered in the catalog under some other name?" is a Polygraph verdict
followed by a DataHub search. Composing them is the point, and keeping the two
kinds of answer distinguishable is the discipline -- see rule 6 in ``SYSTEM``.

Both backends call ``polygraph.tools`` for the evidence side. There is one
implementation of "can I trust this asset", not two.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from . import tools

MAX_TOOL_ROUNDS = 6

# Assets the demo knows by name, so a question can say "fee schedule" instead of
# pasting a URN. Explicit, like urn_map.yaml -- no fuzzy matching.
KNOWN_ASSETS: dict[str, str] = {
    "fee_schedule": "urn:li:dataset:(urn:li:dataPlatform:file,polygraph.demo.fee_schedule,PROD)",
    "fee schedule": "urn:li:dataset:(urn:li:dataPlatform:file,polygraph.demo.fee_schedule,PROD)",
    "raw_claims": "urn:li:dataset:(urn:li:dataPlatform:file,polygraph.demo.raw_claims,PROD)",
    "raw claims": "urn:li:dataset:(urn:li:dataPlatform:file,polygraph.demo.raw_claims,PROD)",
    "legacy_claims_archive": "urn:li:dataset:(urn:li:dataPlatform:file,polygraph.demo.legacy_claims_archive,PROD)",
    "legacy archive": "urn:li:dataset:(urn:li:dataPlatform:file,polygraph.demo.legacy_claims_archive,PROD)",
    "archive": "urn:li:dataset:(urn:li:dataPlatform:file,polygraph.demo.legacy_claims_archive,PROD)",
    "predictions": "urn:li:dataset:(urn:li:dataPlatform:file,polygraph.demo.fraud_predictions,PROD)",
}

JOB_ALIASES = ("fraud_scoring", "fraud scoring", "train_fraud_model", "the job", "the model",
               "the pipeline")


@dataclass
class Answer:
    question: str
    backend: str
    intent: str
    tool_calls: list[str] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)
    text: str = ""
    understood: bool = True


# --------------------------------------------------------------------------
# Deterministic backend
# --------------------------------------------------------------------------

def _extract_urn(question: str) -> str | None:
    """Pull an asset URN out of the question. Explicit matches only."""
    raw = re.search(r"urn:li:[a-zA-Z]+:\S+", question)
    if raw:
        return raw.group(0).rstrip(".,?\"'")
    lowered = question.lower()
    # Longest alias first, so "legacy_claims_archive" wins over "archive".
    for alias in sorted(KNOWN_ASSETS, key=len, reverse=True):
        if alias in lowered:
            return KNOWN_ASSETS[alias]
    return None


def _mentions_job(question: str) -> bool:
    lowered = question.lower()
    return any(a in lowered for a in JOB_ALIASES)


# Intent table. Ordered: the first matching intent wins, so put the specific
# patterns above the general ones.
INTENTS: list[tuple[str, tuple[str, ...]]] = [
    ("incident", ("incident", "what broke", "root cause", "why did", "collapse",
                  "went wrong", "f1 drop", "degraded")),
    ("undeclared", ("undeclared", "shadow", "hidden", "not declared", "missing source",
                    "missing input", "what else does it read", "reads that")),
    ("phantom", ("phantom", "stale", "dead edge", "no longer", "not real", "unused")),
    ("score", ("score", "integrity", "how trustworthy", "how accurate", "how good",
               "how bad", "precision", "recall")),
    ("semantics", ("what does verified mean", "what do the verdicts mean", "verdict mean",
                   "semantics", "what does phantom mean", "what does undeclared mean")),
    ("trust", ("can i trust", "should i trust", "is it trustworthy", "trust", "reliable",
               "is the lineage", "verified")),
]

ANSWERABLE = [
    "Can I trust <asset>?",
    "What's the integrity score for the fraud scoring job?",
    "What undeclared sources does the pipeline read?",
    "Which declared edges are phantom?",
    "What caused the incident?",
    "What do the verdicts mean?",
]


def classify(question: str) -> str | None:
    lowered = question.lower()
    for intent, patterns in INTENTS:
        if any(p in lowered for p in patterns):
            return intent
    return None


def answer_deterministic(question: str) -> Answer:
    intent = classify(question)

    if intent is None:
        return Answer(
            question=question,
            backend="deterministic",
            intent="unrecognised",
            understood=False,
            text=(
                "I could not classify that question, and I will not guess at the closest "
                "match — answering a different question than the one asked is worse than "
                "declining.\n\nThis backend is a keyword router, not an agent. It can answer:\n"
                + "\n".join(f"  - {q}" for q in ANSWERABLE)
                + "\n\nFor open-ended questions, run with --llm (needs ANTHROPIC_API_KEY), "
                "or register the MCP server with an agent client."
            ),
        )

    if intent == "incident":
        data = tools.get_incident_report()
        text = _render_incident(data)
        call = "get_incident_report"
    elif intent == "undeclared":
        data = tools.list_undeclared_sources()
        text = _render_edges(data, "undeclared source")
        call = "list_undeclared_sources"
    elif intent == "phantom":
        data = tools.list_phantom_edges()
        text = _render_edges(data, "phantom edge")
        call = "list_phantom_edges"
    elif intent == "score":
        data = tools.get_integrity_score(tools.DEFAULT_JOB)
        text = _render_score(data)
        call = "get_integrity_score"
    elif intent == "semantics":
        data = tools.explain_verdict_semantics()
        text = _render_semantics(data)
        call = "explain_verdict_semantics"
    else:  # trust
        urn = _extract_urn(question)
        if urn is None and _mentions_job(question):
            urn = tools.DEFAULT_JOB
        if urn is None:
            return Answer(
                question=question, backend="deterministic", intent="trust", understood=False,
                text=(
                    "I need to know which asset. Name one of: "
                    + ", ".join(sorted({k for k in KNOWN_ASSETS if '_' in k}))
                    + " — or paste a URN."
                ),
            )
        data = tools.can_i_trust(urn)
        text = _render_trust(data)
        call = "can_i_trust"

    return Answer(question=question, backend="deterministic", intent=intent,
                  tool_calls=[call], data=data, text=text)


def _render_trust(d: dict[str, Any]) -> str:
    if not d.get("evidence_available"):
        return f"{d['answer']}\n\n(asset: {d['asset']})"
    lines = [d["answer"], "", f"Verdicts: {', '.join(d['verdicts'])}"]
    if d.get("implicated_in_incident"):
        lines.append("This asset is also tagged as implicated in an incident.")
    for e in d.get("edge_detail", []):
        ops = " → ".join(e["operations_observed"]) or "no operations recorded"
        lines.append(f"  {e['verdict']}: {_short(e['upstream'])} → {_short(e['downstream'])}")
        lines.append(f"    {ops}")
    lines += ["", d["caveat"]]
    return "\n".join(lines)


def _render_score(d: dict[str, Any]) -> str:
    if not d.get("evidence_available"):
        return d["answer"]
    return (
        f"Lineage Integrity Score: {d['lineage_integrity_score']}\n"
        f"  precision {d['precision']}  recall {d['recall']}\n\n"
        f"{d['diagnosis']}\n\n{d['definition']}"
    )


def _render_edges(d: dict[str, Any], label: str) -> str:
    if not d.get("evidence_available"):
        return d["answer"]
    if d["count"] == 0:
        return f"No {label}s found in the captured run."
    lines = [f"{d['count']} {label}(s):", ""]
    for e in d["edges"]:
        lines.append(f"  {_short(e['upstream'])} → {_short(e['downstream'])}")
        ops = " → ".join(e["operations_observed"])
        lines.append(f"    {ops or 'no operations recorded — nothing flowed'}")
    lines += ["", d["caveat"]]
    return "\n".join(lines)


def _render_incident(d: dict[str, Any]) -> str:
    if not d.get("evidence_available"):
        return d["answer"]
    return (
        f"{d['title']}\n\n"
        f"Root operation: {d.get('root_operation')}\n"
        f"{d.get('baseline')} → {d.get('degraded')}\n"
        f"sha256: {d.get('sha256')}\n\n"
        f"{d.get('markdown','')[:1200]}"
    )


def _render_semantics(d: dict[str, Any]) -> str:
    lines = []
    for verdict in ("VERIFIED", "PHANTOM", "UNDECLARED"):
        lines += [verdict, f"  means:         {d[verdict]['means']}",
                  f"  does NOT mean: {d[verdict]['does_not_mean']}", ""]
    lines += [f"unmapped: {d['unmapped']}", "", f"scope: {d['scope']}"]
    return "\n".join(lines)


def _short(urn: str) -> str:
    from .reconcile import _short as s
    return s(urn)


# --------------------------------------------------------------------------
# LLM backend
# --------------------------------------------------------------------------

# --- tools that proxy to DataHub's MCP Server ----------------------------
# Imported lazily: the deterministic backend is the default and must not pay
# for fastmcp, nor require DataHub to be reachable.
#
# Deliberately NOT added to polygraph.tools. That module is Polygraph's own
# query surface and is mirrored one-for-one by Polygraph's MCP server; putting
# DataHub's tools in it would have Polygraph's server re-advertise another
# server's tools as its own.

def _datahub_get_entities(urns: list[str]) -> dict:
    """Ask DataHub's catalog what it knows about these assets: registered name,
    description, and owning team.

    This is the catalog's TESTIMONY, read through DataHub's MCP Server. It is
    not evidence and Polygraph does not verify it. Use it to name a responsible
    team or to quote how an asset is described, never to support a claim about
    what a pipeline actually did.

    Args:
        urns: DataHub URNs to look up.
    """
    from . import catalog_mcp

    context = catalog_mcp.fetch_catalog_context(urns)
    return {
        "source": "mcp-server-datahub:get_entities",
        "assets": [c.to_dict() for c in context.values()],
    }


def _datahub_search(query: str) -> dict:
    """Search DataHub's catalog for registered assets, through DataHub's MCP Server.

    The question this is for: Polygraph reports an UNDECLARED source -- runtime
    proved the pipeline reads it, the catalog never declared the edge. Is the
    asset registered in the catalog at all, under some name? A hit means the
    catalog knows the asset but not the edge. A miss means the asset is
    invisible to the catalog entirely, which is worse.

    Args:
        query: Search text. DataHub's structured syntax works: prefix with /q
            for boolean queries, e.g. "/q fee+schedule".
    """
    from . import catalog_mcp

    return catalog_mcp.search_catalog(query)


TOOL_FUNCS: dict[str, Callable[..., dict]] = {
    "can_i_trust": tools.can_i_trust,
    "get_integrity_score": tools.get_integrity_score,
    "list_undeclared_sources": tools.list_undeclared_sources,
    "list_phantom_edges": tools.list_phantom_edges,
    "get_incident_report": tools.get_incident_report,
    "explain_verdict_semantics": tools.explain_verdict_semantics,
    "datahub_get_entities": _datahub_get_entities,
    "datahub_search": _datahub_search,
}

TOOL_SCHEMAS = [
    {
        "name": "can_i_trust",
        "description": tools.can_i_trust.__doc__,
        "input_schema": {
            "type": "object",
            "properties": {"asset_urn": {"type": "string", "description": "A DataHub URN."}},
            "required": ["asset_urn"],
        },
    },
    {
        "name": "get_integrity_score",
        "description": tools.get_integrity_score.__doc__,
        "input_schema": {
            "type": "object",
            "properties": {"job_urn": {"type": "string", "description": "A dataJob URN."}},
            "required": ["job_urn"],
        },
    },
    {
        "name": "list_undeclared_sources",
        "description": tools.list_undeclared_sources.__doc__,
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_phantom_edges",
        "description": tools.list_phantom_edges.__doc__,
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_incident_report",
        "description": tools.get_incident_report.__doc__,
        "input_schema": {
            "type": "object",
            "properties": {"document_urn": {"type": "string"}},
        },
    },
    {
        "name": "explain_verdict_semantics",
        "description": tools.explain_verdict_semantics.__doc__,
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "datahub_get_entities",
        "description": _datahub_get_entities.__doc__,
        "input_schema": {
            "type": "object",
            "properties": {
                "urns": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "DataHub URNs to look up.",
                }
            },
            "required": ["urns"],
        },
    },
    {
        "name": "datahub_search",
        "description": _datahub_search.__doc__,
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search text."},
            },
            "required": ["query"],
        },
    },
]

SYSTEM = (
    "You answer questions about whether a DataHub catalog's lineage can be trusted. "
    "Polygraph compares what a catalog CLAIMS against what a pipeline's runtime "
    "actually DID.\n\n"
    "You have tools from two servers:\n"
    "  * Polygraph's tools (can_i_trust, get_integrity_score, list_undeclared_sources, "
    "list_phantom_edges, get_incident_report, explain_verdict_semantics) return "
    "EVIDENCE -- what a runtime capture proved.\n"
    "  * DataHub's tools (datahub_get_entities, datahub_search), which reach the "
    "catalog through DataHub's own MCP Server, return TESTIMONY -- what the catalog "
    "claims. Polygraph has not verified any of it.\n\n"
    "Rules you must follow:\n"
    "1. Answer only from tool results. Never fill gaps from general knowledge about "
    "DataHub or ML pipelines.\n"
    "2. Every Polygraph verdict describes ONE captured run. Never restate a verdict as a "
    "universal property of the pipeline.\n"
    "3. If a tool returns evidence_available=false, say plainly that there is no evidence. "
    "Do not present absence of findings as a clean result.\n"
    "4. Quote the concrete numbers and operation names the tools return. A reader should be "
    "able to disagree with your conclusion from the evidence you showed.\n"
    "5. Keep the two kinds of answer distinguishable. Say 'the catalog says' for anything "
    "from a datahub_* tool and 'Polygraph observed' for anything from a Polygraph tool. "
    "Never let a catalog description stand in for evidence about what ran -- the gap "
    "between those two is the entire subject.\n"
    f"6. The demo's training job URN is {tools.DEFAULT_JOB}\n"
    "Be concise."
)


def answer_llm(question: str, model: str = "claude-opus-4-20250514") -> Answer:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return Answer(
            question=question, backend="llm", intent="unavailable", understood=False,
            text=(
                "ANTHROPIC_API_KEY is not set, so the LLM backend cannot run.\n\n"
                "Everything documented in the README reproduces without it — use the "
                "default deterministic backend, or register the MCP server with an agent "
                "client. The LLM path is deliberately optional so a judge never needs "
                "credentials to verify a claim."
            ),
        )

    try:
        import anthropic
    except ImportError:
        return Answer(
            question=question, backend="llm", intent="unavailable", understood=False,
            text="The `anthropic` package is not installed. `pip install anthropic`.",
        )

    client = anthropic.Anthropic()
    messages: list[dict[str, Any]] = [{"role": "user", "content": question}]
    calls: list[str] = []
    collected: dict[str, Any] = {}

    for _ in range(MAX_TOOL_ROUNDS):
        resp = client.messages.create(
            model=model, max_tokens=2000, system=SYSTEM, tools=TOOL_SCHEMAS, messages=messages
        )
        messages.append({"role": "assistant", "content": resp.content})

        tool_uses = [b for b in resp.content if getattr(b, "type", None) == "tool_use"]
        if not tool_uses:
            text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
            return Answer(question=question, backend="llm", intent="llm",
                          tool_calls=calls, data=collected, text=text.strip())

        results = []
        for use in tool_uses:
            fn = TOOL_FUNCS.get(use.name)
            calls.append(use.name)
            try:
                out = fn(**use.input) if fn else {"error": f"unknown tool {use.name}"}
            except Exception as e:  # noqa: BLE001 - surfaced to the model, not swallowed
                out = {"error": f"{type(e).__name__}: {e}"}
            collected[use.name] = out
            results.append({
                "type": "tool_result",
                "tool_use_id": use.id,
                "content": json.dumps(out, default=str),
            })
        messages.append({"role": "user", "content": results})

    return Answer(
        question=question, backend="llm", intent="llm", tool_calls=calls, data=collected,
        understood=False,
        text=(
            f"Stopped after {MAX_TOOL_ROUNDS} tool rounds without a final answer. "
            f"Tools called: {', '.join(calls)}. Reporting this rather than returning a "
            "partial answer as if it were complete."
        ),
    )


def ask(question: str, use_llm: bool = False, model: str | None = None) -> Answer:
    if use_llm:
        return answer_llm(question, model=model or "claude-opus-4-20250514")
    return answer_deterministic(question)
