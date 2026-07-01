"""Prometheus metrics (Contract 7)."""

from __future__ import annotations

from prometheus_client import Counter, Histogram

invoke_total = Counter("mcp_eng_memory_invoke_total", "Tool invocations.", ["tool", "outcome"])
invoke_rejected_total = Counter("mcp_eng_memory_invoke_rejected_total", "Rejected invocations.", ["reason"])
invoke_duration_seconds = Histogram("mcp_eng_memory_invoke_duration_seconds", "Invoke latency.", ["tool"])
manifest_served_total = Counter("mcp_eng_memory_manifest_served_total", "Manifest responses.", ["status"])
revocation_checks_total = Counter("mcp_eng_memory_revocation_checks_total", "Revocation checks.", ["outcome"])
