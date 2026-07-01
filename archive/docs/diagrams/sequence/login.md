# Admin Console Login — Sequence Diagram

> Mermaid source. Shows the BFF session-based login flow.

```mermaid
sequenceDiagram
    autonumber
    participant Browser as Browser (SPA)
    participant Edge as Edge (Caddy)
    participant BFF as frontend-bff
    participant Auth as auth-service
    participant Valkey as Valkey

    Note over Browser: User visits admin console

    Browser->>Edge: GET https://localhost:8000/
    Edge-->>Browser: SPA HTML + JS bundle (Next.js)

    Note over Browser: User enters agent_id + api_key on login form

    Browser->>BFF: POST /bff/login\n{agent_id: "...", api_key: "cx_live_..."}\n(no auth required — permit-all route)
    BFF->>Auth: POST /v1/agents/{agent_id}/token\n{api_key: "cx_live_..."}
    Auth->>Auth: Argon2id verify api_key hash\nCheck agent status = active\nCheck quota not exceeded
    Auth->>Auth: Load active signing key\nMint RS256 JWT {sub, tenant_id, scopes, exp}
    Auth->>Valkey: SET cypherx:jwt_cache:{jti} 1 EX 3600
    Auth-->>BFF: 200 {access_token: JWT, expires_in: 3600}

    BFF->>BFF: Generate session_id (UUID v4)\nGenerate csrf_token (random 32 bytes)\nBuild session payload:\n{jwt, tenant_id, agent_id, csrf_token}
    BFF->>Valkey: AES-256-GCM encrypt session payload\nSET session:{session_id} encrypted_blob EX 86400

    BFF-->>Browser: 200 {tenant_id, agent_id, scopes}\nSet-Cookie: session={session_id}; HttpOnly; Secure; SameSite=Lax; Max-Age=86400\nSet-Cookie: cypherx_csrf={csrf_token}; SameSite=Lax; Max-Age=86400
    Note over Browser: session cookie: httpOnly (JS cannot read)\ncypherx_csrf cookie: readable by JS for CSRF header

    Browser->>BFF: GET /bff/me\nCookie: session={session_id}; cypherx_csrf={csrf_token}
    BFF->>Valkey: GET session:{session_id}
    Valkey-->>BFF: encrypted_blob
    BFF->>BFF: AES-256-GCM decrypt → {jwt, tenant_id, agent_id}
    BFF-->>Browser: 200 {agent_id, tenant_id, scopes: [...], plan: "pro"}

    Note over Browser: User is now logged in\nSPA renders dashboard

    Note over Browser,BFF: Subsequent API calls

    Browser->>BFF: POST /bff/api/tasks\nCookie: session={session_id}; cypherx_csrf={csrf_token}\nX-CSRF-Token: {csrf_token}\n{agent_id, input: {...}}
    BFF->>BFF: Verify X-CSRF-Token header\n== cypherx_csrf cookie\n== session.csrfToken\n(triple match: double-submit + session binding)
    BFF->>Valkey: GET session:{session_id} → decrypt → JWT
    BFF->>BFF: Strip: Authorization, X-Tenant-ID, X-Agent-ID (client-supplied)\nInject: Authorization: Bearer JWT\nInject: X-Tenant-ID: {tenant_id}\nInject: X-Request-ID: new UUID\nInject: traceparent
    BFF->>xA: POST /v1/tasks (proxied with injected headers)
```
