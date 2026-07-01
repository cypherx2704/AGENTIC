-- =====================================================================================
-- tool-registry — first-cycle seed (WP11). PostgreSQL 16. Idempotent.
--
-- Seeds the platform `tool-web-search` tool (tenant_id IS NULL) + its first version
-- (Contract-4 manifest) + its capability/scope row + an initial health row.
--
-- The running service ALSO seeds this at startup (services/seed.py) from config so the
-- manifest base_url tracks the deployed env; this SQL is the reproducible DB-only seed
-- (the manifest base_url here is the in-cluster default). ON CONFLICT DO NOTHING makes
-- re-running safe and lets the runtime seed refresh it.
-- =====================================================================================

SET search_path = tools, public;

-- ── platform tool: tool-web-search ────────────────────────────────────────────────────
INSERT INTO tools.tools (tenant_id, name, status, latest_version)
VALUES (NULL, 'tool-web-search', 'active', '1.0.0')
ON CONFLICT (name) WHERE tenant_id IS NULL DO NOTHING;

-- ── version 1.0.0 with the Contract-4 manifest ────────────────────────────────────────
INSERT INTO tools.tool_versions (tenant_id, tool_id, version, manifest, status)
SELECT NULL, t.tool_id, '1.0.0',
       '{
          "schema_version": "1.0.0",
          "protocol_version": "mcp/1.0",
          "name": "tool-web-search",
          "display_name": "Web Search",
          "version": "1.0.0",
          "description": "Search the web and return ranked results with snippets.",
          "author": "CypherX Platform",
          "category": "research",
          "tags": ["search", "web", "information"],
          "auth_required": true,
          "required_scopes": ["tool:invoke", "tool:tool-web-search:invoke"],
          "base_url": "http://tool-web-search:8080",
          "tools": [
            {
              "name": "web_search",
              "description": "Perform a web search and return top results.",
              "input_schema": {
                "type": "object",
                "properties": {
                  "query": {"type": "string"},
                  "max_results": {"type": "integer", "default": 5, "maximum": 20}
                },
                "required": ["query"]
              },
              "timeout_seconds": 30,
              "idempotent": true,
              "estimated_cost_usd": 0.001,
              "rate_limit": {"rpm": 60, "rpd": 5000}
            }
          ],
          "health_endpoint": "/livez",
          "metrics_endpoint": "/metrics"
        }'::jsonb,
       'active'
  FROM tools.tools t
 WHERE t.name = 'tool-web-search' AND t.tenant_id IS NULL
ON CONFLICT (tool_id, version) DO NOTHING;

-- ── capability/scope row ───────────────────────────────────────────────────────────────
INSERT INTO tools.tool_capabilities (tenant_id, tool_id, capability, required_scope)
SELECT NULL, t.tool_id, 'web_search', 'tool:tool-web-search:invoke'
  FROM tools.tools t
 WHERE t.name = 'tool-web-search' AND t.tenant_id IS NULL
ON CONFLICT (tool_id, capability) DO NOTHING;

-- ── initial health row ─────────────────────────────────────────────────────────────────
INSERT INTO tools.tool_health (tenant_id, tool_id, status, consecutive_failures)
SELECT NULL, t.tool_id, 'active', 0
  FROM tools.tools t
 WHERE t.name = 'tool-web-search' AND t.tenant_id IS NULL
ON CONFLICT (tool_id) DO NOTHING;
