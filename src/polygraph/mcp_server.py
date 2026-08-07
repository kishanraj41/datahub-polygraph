"""Polygraph as an MCP server.

``mcp-server-datahub`` lets an agent read what the catalog *claims*. This server
lets the same agent read what the runtime *proved*, and the difference between
them. Run both and an agent can answer "can I trust this asset's lineage?" with
evidence instead of with the catalog's own testimony.

Every tool reads **live from DataHub** -- the tags, structured properties and
documents Polygraph wrote -- rather than from a local report file. That matters:
an agent asking about trust should see the current state of the catalog, not a
snapshot from whenever someone last ran the CLI. Where a local report adds
detail the catalog does not carry (the operation paths, for instance), it is
used as an optional enrichment and its absence is reported rather than hidden.

Tools deliberately return *structured evidence plus a plain-language verdict*.
An agent that only relays the verdict is still correct; one that reads the
evidence can disagree with it. Both should be possible.

Run it:

    python -m polygraph.mcp_server                    # stdio, for Claude Desktop / Cursor
    POLYGRAPH_GMS=http://localhost:8080 python -m polygraph.mcp_server

Register with Claude Desktop by adding to ``claude_desktop_config.json``:

    {
      "mcpServers": {
        "polygraph": {
          "command": "python",
          "args": ["-m", "polygraph.mcp_server"],
          "env": {"PYTHONPATH": "/path/to/datahub-polygraph/src"}
        }
      }
    }
"""

from __future__ import annotations

from fastmcp import FastMCP

from . import tools

# NOTE: deliberately no `GMS = tools.GMS` / `REPORT_PATH = tools.REPORT_PATH`
# aliases here. A module-level copy is snapshotted at import time, so it looks
# like configuration but silently ignores any later change to the real value --
# which is exactly how a monkeypatched test kept passing against the wrong path.
# Read tools.GMS / tools.REPORT_PATH directly.

mcp = FastMCP(
    name="polygraph",
    instructions=(
        "Polygraph reconciles what a DataHub catalog CLAIMS about lineage against what "
        "a pipeline's runtime actually DID. Use it whenever a question depends on "
        "whether lineage can be trusted, rather than on what lineage says.\n\n"
        "Key distinction: DataHub's own tools report declared lineage. These tools "
        "report whether those declarations survived contact with a real run.\n\n"
        "Every verdict describes ONE captured run. Do not restate them as universal "
        "properties of the pipeline. If a tool returns evidence_available=false, say "
        "so rather than answering from the catalog alone."
    ),
)



# The implementations live in polygraph.tools so the MCP server and the
# `polygraph ask` CLI cannot drift apart. Registration only, below.
for _fn in (
    tools.can_i_trust,
    tools.get_integrity_score,
    tools.list_undeclared_sources,
    tools.list_phantom_edges,
    tools.get_incident_report,
    tools.explain_verdict_semantics,
):
    mcp.tool(_fn)


def main() -> None:
    # show_banner=False: on stdio the banner is pure noise in the client's log,
    # and anything written near the protocol stream is a risk not worth taking.
    mcp.run(show_banner=False)


if __name__ == "__main__":
    main()
