"""cypherx-a1 — CypherX Autonomous Engineering Memory.

A first-class CypherX consuming app (peer to xAgent/ax-1) that ingests engineering
sources into a tenant-scoped knowledge graph + RAG corpus, runs an LLM knowledge
-extraction pipeline, and serves a cited hybrid-retrieval copilot plus an MCP query
surface. All domain (business) logic lives here; the SharedCore services (auth, llms,
guardrails, rag, memory) are consumed strictly through their versioned /v1 contracts.
"""

__version__ = "0.1.0"
