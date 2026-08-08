"""Probe what this GMS can actually answer. Read-only. Changes nothing.

Gate 10 went red on a server-side 500:

    Failed to generate PointInTime Identifier.. Root cause: search
    path: ['searchAcrossLineage']

That is not Polygraph's code and it is not the MCP Server's code -- both send an
ordinary ``searchAcrossLineage`` query. This script establishes, against the
running stack, which GraphQL surfaces work and which do not, so the fix targets
the real fault instead of the first thing that looked broken.

It matters beyond the MCP integration: the DataHub UI's Lineage tab issues the
same ``searchAcrossLineage`` query. If that resolver is broken, the "immaculate
catalog" beat of the demo is broken too, and nobody has looked at the UI yet.

Run:  python scripts/probe_gms.py
Exit: 0 always -- this is a diagnostic, not a gate. Read the verdicts.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import requests

JOB_URN = "urn:li:dataJob:(urn:li:dataFlow:(polygraph,fraud_scoring,PROD),train_fraud_model)"
DATASET_URN = "urn:li:dataset:(urn:li:dataPlatform:file,polygraph.demo.raw_claims,PROD)"
TIMEOUT = 30


# --------------------------------------------------------------------------
# credentials
# --------------------------------------------------------------------------

def resolve_gms() -> tuple[str, str | None]:
    """GMS URL and token from the environment, else ~/.datahubenv, else default.

    ``datahub init`` writes ~/.datahubenv. Parsed leniently on purpose: a
    malformed line should degrade to "no token" (quickstart usually has auth
    off) rather than crash a diagnostic.
    """
    url = os.environ.get("DATAHUB_GMS_URL")
    token = os.environ.get("DATAHUB_GMS_TOKEN")
    if url:
        return url.rstrip("/"), token

    env_file = Path.home() / ".datahubenv"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
            stripped = line.strip()
            if stripped.startswith("server:") and not url:
                url = stripped.split(":", 1)[1].strip().strip("'\"")
            elif stripped.startswith("token:") and not token:
                candidate = stripped.split(":", 1)[1].strip().strip("'\"")
                token = candidate or None

    return (url or "http://localhost:8080").rstrip("/"), token


def gql(url: str, token: str | None, query: str, variables: dict) -> dict[str, Any]:
    """POST a GraphQL query and return a classified outcome.

    Never raises. Every failure mode -- transport, HTTP status, GraphQL errors
    array -- comes back as a dict so the caller can print all of them in one
    pass instead of dying on the first one.
    """
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        r = requests.post(
            f"{url}/api/graphql",
            json={"query": query, "variables": variables},
            headers=headers,
            timeout=TIMEOUT,
        )
    except Exception as e:  # noqa: BLE001 - diagnostic; the exception IS the result
        return {"ok": False, "kind": "transport", "detail": f"{type(e).__name__}: {e}"}

    try:
        body = r.json()
    except Exception:
        return {"ok": False, "kind": "http", "detail": f"HTTP {r.status_code}: {r.text[:400]}"}

    if body.get("errors"):
        messages = [e.get("message", "") for e in body["errors"]]
        joined = " | ".join(messages)
        kind = "pit" if "PointInTime" in joined else "graphql"
        return {"ok": False, "kind": kind, "detail": joined[:600], "data": body.get("data")}

    return {"ok": True, "kind": "ok", "data": body.get("data")}


# --------------------------------------------------------------------------
# the four probes
# --------------------------------------------------------------------------

Q_LINEAGE = """
query P($input: SearchAcrossLineageInput!) {
  searchAcrossLineage(input: $input) {
    total
    searchResults { entity { urn type } degree }
  }
}
"""

# The MCP Server's get_lineage sends exactly this shape: degree filter, count,
# skipHighlighting. Reproducing it here proves the 500 is the resolver, not the
# client.
V_LINEAGE = {
    "input": {
        "urn": JOB_URN,
        "direction": "UPSTREAM",
        "query": "*",
        "start": 0,
        "count": 100,
        "types": [],
        "orFilters": [
            {"and": [{"field": "degree", "condition": "EQUAL", "values": ["1"]}]}
        ],
        "searchFlags": {"skipHighlighting": True, "maxAggValues": 3},
    }
}

# `OwnerType` is a GraphQL union of CorpUser and CorpGroup, so `owner { urn }` is
# a validation error -- the field has to be selected inside an inline fragment
# per concrete type. Getting this wrong the first time produced
# "Field 'urn' in type 'OwnerType' is undefined", which reads like a broken
# server and is in fact a broken query.
Q_ENTITIES = """
query P($urns: [String!]!) {
  entities(urns: $urns) {
    urn
    type
    ... on DataJob {
      jobId
      properties { name description }
      ownership { owners { owner { ... on CorpUser { urn } ... on CorpGroup { urn } } } }
    }
    ... on Dataset {
      name
      properties { name description }
      ownership { owners { owner { ... on CorpUser { urn } ... on CorpGroup { urn } } } }
    }
  }
}
"""

# Does this GMS's GraphQL schema expose the declared inputs at all? The MCP
# Server never asks for them -- `inputOutput` appears nowhere in its .gql files.
# If GMS answers this, the gap is in the MCP Server's query, not in DataHub, and
# that is a one-file upstream fix worth proposing.
Q_INPUTOUTPUT = """
query P($urn: String!) {
  entity(urn: $urn) {
    urn
    ... on DataJob {
      inputOutput {
        inputDatasets { urn }
        outputDatasets { urn }
      }
    }
  }
}
"""

Q_SEARCH = """
query P($input: SearchAcrossEntitiesInput!) {
  searchAcrossEntities(input: $input) {
    total
    searchResults { entity { urn type } }
  }
}
"""

V_SEARCH = {"input": {"types": ["DATASET"], "query": "polygraph", "start": 0, "count": 10}}


def main() -> int:
    url, token = resolve_gms()
    print("=" * 72)
    print("Polygraph -- GMS capability probe (read-only)")
    print("=" * 72)
    print(f"GMS   : {url}")
    print(f"token : {'present' if token else 'none (quickstart default)'}")
    print()

    # --- version -----------------------------------------------------------
    try:
        cfg = requests.get(f"{url}/config", timeout=TIMEOUT).json()
        print(f"version   : {cfg.get('versions', {}).get('acryldata/datahub', {}).get('version', '?')}")
        print(f"statefulIngestion / graphql cfg keys: {sorted(cfg.keys())[:8]}")
    except Exception as e:  # noqa: BLE001
        print(f"/config unreachable: {type(e).__name__}: {e}")
        print("\nDataHub is not up. Start it before probing.")
        return 0
    print()

    results: dict[str, dict] = {}

    print("-" * 72)
    print("1. searchAcrossLineage  -- used by get_lineage AND by the UI Lineage tab")
    results["lineage"] = gql(url, token, Q_LINEAGE, V_LINEAGE)
    _report(results["lineage"])

    print("-" * 72)
    print("2. entities(urns:)      -- used by get_entities. No search, no PIT.")
    results["entities"] = gql(url, token, Q_ENTITIES, {"urns": [JOB_URN, DATASET_URN]})
    _report(results["entities"])

    print("-" * 72)
    print("3. DataJob.inputOutput  -- the declared claim, straight from GraphQL")
    results["inputOutput"] = gql(url, token, Q_INPUTOUTPUT, {"urn": JOB_URN})
    _report(results["inputOutput"])

    print("-" * 72)
    print("4. searchAcrossEntities -- used by the MCP `search` tool")
    results["search"] = gql(url, token, Q_SEARCH, V_SEARCH)
    _report(results["search"])

    # --- what it means -----------------------------------------------------
    print("=" * 72)
    print("READING")
    print("=" * 72)

    lineage_ok = results["lineage"]["ok"]
    search_ok = results["search"]["ok"]
    entities_ok = results["entities"]["ok"]
    pit = results["lineage"].get("kind") == "pit"

    # The discriminator that matters. searchAcrossEntities is ordinary search --
    # no point-in-time, no graph traversal. If it fails too, the search backend
    # is unreachable, and the point-in-time error is a symptom rather than the
    # disease. Reading only the lineage failure would send you to fix a config
    # value on a service that has nothing to talk to.
    if not lineage_ok and not search_ok:
        print("* BOTH lineage and plain search fail.")
        print("  This is not a dialect or point-in-time config problem: ordinary")
        print("  search does not use point-in-time at all. GMS cannot reach its")
        print("  search backend.")
        print()
        print("  Most likely the OpenSearch/Elasticsearch container is not running.")
        print("  GMS still answers /config and still serves entity reads, because")
        print("  those come from MySQL -- which is why the rest of the demo works.")
        print()
        print("  NEXT: scripts/stack_status.ps1  (shows every container, running or not)")
        print("  Do NOT run fix_gms_search.ps1 yet -- it changes a setting on a")
        print("  service whose problem is a missing peer.")
    elif not lineage_ok and pit:
        print("* Lineage fails on point-in-time creation, but plain search works.")
        print("  That is the dialect mismatch: ELASTICSEARCH_IMPLEMENTATION defaults")
        print("  to 'elasticsearch' while the quickstart runs OpenSearch, and")
        print("  ELASTICSEARCH_SEARCH_GRAPH_POINT_IN_TIME_CREATION_ENABLED defaults")
        print("  to true, so GMS sends an Elasticsearch _pit call to OpenSearch.")
        print()
        print("  FIX: scripts/fix_gms_search.ps1")
    elif not lineage_ok:
        print("* Lineage fails for a reason that is NOT point-in-time, while plain")
        print("  search works. Read the detail above; none of the known fixes apply.")
    else:
        print("* searchAcrossLineage works. The UI Lineage tab is fine, and")
        print("  `reconcile --declared-via mcp` should work.")

    if not lineage_ok:
        print()
        print("  Either way, the DataHub UI's Lineage tab uses this same resolver.")
        print("  Check it before recording anything:")
        print(f"    http://localhost:9002/tasks/{JOB_URN}/Lineage")

    print()
    if entities_ok:
        print("* entities(urns:) works -> catalog context (owners, descriptions) can be")
        print("  read through the MCP Server's get_entities regardless of search.")
    else:
        print("* entities(urns:) failed. If the error mentions a GraphQL validation")
        print("  error, that is THIS PROBE's query being wrong, not GMS being broken --")
        print("  the MCP Server sends its own query. Only a non-validation failure here")
        print("  means get_entities is genuinely unavailable.")

    io_data = (results["inputOutput"].get("data") or {}).get("entity") or {}
    io_present = bool((io_data.get("inputOutput") or {}).get("inputDatasets"))
    print()
    if io_present:
        n = len(io_data["inputOutput"]["inputDatasets"])
        print(f"* DataJob.inputOutput IS exposed by this GMS ({n} declared inputs).")
        print("  The MCP Server simply never requests it -- `inputOutput` appears in")
        print("  none of its .gql files. That is a one-field upstream fix, not a")
        print("  DataHub limitation. Worth a PR.")
    elif results["inputOutput"]["ok"]:
        print("* DataJob.inputOutput resolved but is empty. Has demo/seed_catalog.py run?")
    else:
        print("* DataJob.inputOutput could not be queried; see the error above.")

    out = Path("probe_gms.json")
    out.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8", newline="\n")
    print()
    print(f"Raw results: {out.resolve()}")
    return 0


def _report(res: dict) -> None:
    if res["ok"]:
        payload = json.dumps(res.get("data"), default=str)
        print(f"   OK   {payload[:300]}")
    else:
        print(f"   FAIL [{res['kind']}] {res['detail'][:400]}")
    print()


if __name__ == "__main__":
    sys.exit(main())
