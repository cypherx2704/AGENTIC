"""mcp-eng-memory — CypherX stateless MCP server for engineering-memory queries.

A thin, stateless Contract-4 facade (no DB, no Kafka, no outbox) that exposes read-only,
source-cited tools (who_owns / why_built / what_breaks_if_changed / experts_on /
graph_neighbors / incident_root_cause / how_does_x_work) by proxying to the cypherx-a1
product API. Per-invocation metering is the CALLER's (xAgent) outbox responsibility — this
server never meters tool invocations.
"""

__version__ = "1.0.0"
