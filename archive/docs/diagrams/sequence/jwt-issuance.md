# JWT Issuance — Sequence Diagram

> Mermaid source. Shows the full JWT issuance and verification lifecycle.

```mermaid
sequenceDiagram
    autonumber
    participant Client as API Client
    participant Auth as auth-service
    participant DB as Neon (auth schema)
    participant Valkey as Valkey
    participant Kafka as Redpanda
    participant Services as Downstream Services

    Note over Client,Auth: Phase 1: API Key Exchange → JWT

    Client->>Auth: POST /v1/agents/{agent_id}/token\n{api_key: "cx_live_abc123..."}
    Auth->>DB: SELECT * FROM api_keys\nWHERE key_prefix = 'cx_live_ab'\nAND agent_id = {agent_id}
    DB-->>Auth: {key_hash, scopes, expires_at, revoked}
    Auth->>Auth: Argon2id.verify(api_key, key_hash)\n(CPU-intensive, ~50-200ms)
    Auth->>Auth: Check: expires_at > now()\nCheck: revoked = false\nCheck: agent status = active
    Auth->>DB: SELECT current_value FROM quota_usage\nWHERE tenant_id = ... AND resource_type = 'requests_per_day'
    DB-->>Auth: {current_value: 450, limit: 1000}
    Auth->>Auth: Check: current_value < limit

    Auth->>DB: SELECT * FROM signing_keys\nWHERE status = 'active'\nORDER BY created_at DESC LIMIT 1
    DB-->>Auth: {kid: "key-2026-06", encrypted_private_key, public_key_pem}
    Auth->>Auth: AES-256-GCM decrypt private key\nMint RS256 JWT:\n{iss, sub=agent_id, aud, iat, exp, jti, tenant_id, scopes, plan}
    Auth->>Valkey: SET cypherx:rev:jti:{jti} 0 EX 3600\n(placeholder; becomes 1 on revocation)
    Auth->>DB: INSERT audit_log\n{action=token.issued, agent_id, jti, exp}
    Auth->>DB: UPDATE quota_usage\nSET current_value = current_value + 1

    Auth-->>Client: 200\n{access_token: JWT, token_type: Bearer, expires_in: 3600}

    Note over Client,Services: Phase 2: JWT Usage at Downstream Service

    Client->>Services: POST /v1/tasks\nAuthorization: Bearer JWT

    Services->>Auth: GET /.well-known/jwks.json\n(cached in memory for 24h)
    Auth-->>Services: {keys: [{kid: "key-2026-06", kty: RSA, n: ..., e: AQAB}]}
    Services->>Services: Parse JWT header → kid = "key-2026-06"\nFind matching public key in JWKS\nVerify RS256 signature
    Services->>Services: Validate: exp > now()\nValidate: iss = AUTH_ISSUER_URL\nValidate: aud contains platform audience
    Services->>Valkey: GET cypherx:rev:jti:{jti}
    Valkey-->>Services: "0" (not revoked)
    Services->>Services: Check required scope for endpoint\nSet app.tenant_id from JWT claims
    Services-->>Client: (proceed with request)

    Note over Auth,Services: Phase 3: Token Revocation

    Auth->>DB: INSERT revoked_tokens {jti, reason, revoked_at}
    Auth->>Valkey: SET cypherx:rev:jti:{jti} 1 EX {jti_remaining_ttl}
    Auth->>DB: INSERT outbox {topic=cypherx.auth.token.revoked, payload={jti, agent_id, tenant_id}}
    DB->>Kafka: Outbox relay publishes token.revoked event
    Kafka->>Services: Consume token.revoked → SET cypherx:rev:jti:{jti} 1
    Note over Services: Next request with this JWT:\nGET cypherx:rev:jti:{jti} → "1" → 401 TOKEN_REVOKED

    Note over Services: Valkey outage scenario (FAIL OPEN)
    Services->>Valkey: GET cypherx:rev:jti:{jti}
    Valkey-->>Services: (timeout / error)
    Services->>Services: Log: WARN revocation_check_failed\nAccept token (availability wins)
```
