# Contract 20 — External Onboarding ⚡

> **Status:** ⚡ First Cycle. Normative. Pins the funnel by which an external developer becomes a
> tenant **without going through px0**.

> **Critical for the "Externally Operable" principle.** Without this contract, the platform
> requires every external customer to also be a px0 customer — which contradicts the platform plan's
> intent that **every SharedCore service be a standalone product**.

Each entry point (website, marketplace, partner referral) lands on the **same flow**. A self-serve
tenant is created with `source = self-serve-signup` (Contract 13 §2); an SSO-originated tenant with
`source = sso-jit`.

---

## 1. Stages (the funnel)

```
[Signup form]
  → POST /v1/onboarding/signup { email, full_name, intended_use, terms_accepted_version }
  → Auth creates auth.signup_attempts row (status='pending_verification')
  → Email service sends verification link (link contains short-lived verification_token, TTL 24h)
  ↓
[Email verification]
  → GET  /v1/onboarding/verify?token=<...>
  → Auth marks signup_attempts.verified_at; creates auth.tenants row (source='self-serve-signup')
  → Auth seeds default plan in auth.tenant_quotas (free tier)
  → Auth emits cypherx.tenant.created
  → Each SharedCore service's bootstrap-tenant consumer seeds its tenant_config rows
  → Auth creates a default 'admin' role for the signup user
       (linked via auth.upstream_identity to verified email)
  → Auth mints a session JWT, returns to onboarding-redirect URL with ?token=<...>
  ↓
[First API key (sandbox)]
  → POST /v1/api-keys (with session JWT, scope='api_keys:write')
  → Returns cx_sandbox_auth_... (auto-scoped to sandbox environment — Phase 13 sandbox account)
  → Onboarding UI shows a 30s quickstart:
       curl -H 'Authorization: Bearer cx_sandbox_...' https://sandbox.cypherx.ai/v1/chat/completions
  ↓
[Upgrade to prod tenant]
  → POST /v1/onboarding/upgrade { billing_method: 'stripe' | 'px0' | 'manual-invoice', billing_payload }
  → Auth creates billing_account via the configured billing emitter (Contract 19 / billing-bridge)
  → On billing setup success, auth.tenants.plan transitions free → pro;
       emits cypherx.tenant.plan_changed
  → User can now mint cx_prod_... keys
```

### Stage detail

| Stage | Endpoint | Effect |
|-------|----------|--------|
| Signup | `POST /v1/onboarding/signup` | Body `{ email, full_name, intended_use, terms_accepted_version }`. Creates `auth.signup_attempts` (`status='pending_verification'`). Email service sends a verification link containing a short-lived `verification_token` (**TTL 24h**). |
| Verify | `GET /v1/onboarding/verify?token=<...>` | Marks `signup_attempts.verified_at`; creates `auth.tenants` row (`source='self-serve-signup'`); seeds default plan in `auth.tenant_quotas` (**free tier**); emits `cypherx.tenant.created`; every service's `bootstrap-tenant` consumer seeds `tenant_config`; creates a default **`admin`** role for the signup user (linked via `auth.upstream_identity` to the verified email); mints a session JWT and redirects to the onboarding-redirect URL with `?token=<...>`. |
| First key (sandbox) | `POST /v1/api-keys` (session JWT, `scope='api_keys:write'`) | Returns `cx_sandbox_auth_...`, **auto-scoped to the sandbox environment** (Phase 13 sandbox account). UI shows a 30-second quickstart curl. |
| Upgrade to prod | `POST /v1/onboarding/upgrade` | Body `{ billing_method: 'stripe' \| 'px0' \| 'manual-invoice', billing_payload }`. Creates a `billing_account` via the configured billing emitter (Contract 19 / billing-bridge). On success, `auth.tenants.plan` transitions **free → pro** and emits `cypherx.tenant.plan_changed`. User can now mint `cx_prod_...` keys. |

---

## 2. Sandbox vs prod isolation

- **Sandbox tenant** gets a **shadow tenant row** in the **sandbox EKS cluster** (Phase 13 Domain 5).
  API keys carry the `_sandbox_` segment; **Kong routes them to sandbox**. **Sandbox data auto-purges
  after 7 days.**
- **Production tenant** gets a **normal tenant row in the prod cluster**.
- **No data sharing between sandbox and prod.**

---

## 3. Anti-abuse (mandatory in onboarding flow)

- **Disposable-email blocklist** — Auth gates `signup` on a vetted block-list.
- **Per-IP rate limit** on `/onboarding/signup` — **10/hour via Kong**.
- **Captcha** — Cloudflare Turnstile or hCaptcha — gating `signup`.
- **Soft signal:** heuristic flags (**TLD reputation, ASN reputation**) →
  `auth.signup_attempts.risk_score`.
  - `risk_score ≥ 0.8` → **manual review queue**.
  - below threshold → **auto-provision**.

---

## 4. Termination

- `POST /v1/onboarding/close-account` → tenant transitions to **`status='pending_deletion'`**;
  **30-day grace**; then **`cypherx.tenant.deleted`** fires (Contract 13 §3).
- **During grace:** all **writes rejected** with `403 TENANT_PENDING_DELETION`; **reads allowed for
  data export**.

---

## 5. Data export (GDPR)

- `POST /v1/data/export` (per service or global aggregator) → produces a downloadable archive
  (**S3 pre-signed, TTL 7 days**).
- Export includes **all rows where `tenant_id` matches across every service schema**.

---

## 6. Verification of admin handover

- The signup user becomes the **initial tenant admin**.
- **Adding a second admin requires verification** (re-confirm email) so that a **compromised single
  admin can be recovered**.

---

## 7. Identity providers other than self-serve email (SSO-JIT variant)

- Signup may begin from an **upstream IdP** (SSO-JIT, Contract 13).
- The flow becomes: **first successful SSO** from a configured IdP with `auto_provision: true` →
  Auth creates the **tenant row (`source='sso-jit'`)** + initial admin user → user is redirected to
  the onboarding **completion page to set plan/billing**.
- **No email verification step** (the IdP already verified the identity).

---

## 8. Standard error codes (added to Contract 2)

| Code | HTTP Status | Meaning |
|------|-------------|---------|
| `SIGNUP_DISPOSABLE_EMAIL` | 422 | Email domain on the disposable-email blocklist |
| `SIGNUP_VERIFICATION_EXPIRED` | 410 | Verification link is older than 24h |
| `SIGNUP_RATE_LIMITED` | 429 | Per-IP signup rate limit hit |
| `TENANT_PENDING_DELETION` | 403 | Tenant in 30-day deletion grace; writes blocked |

---

## 9. Cross-references

- **Contract 2 (Error format):** the four error codes above.
- **Contract 13 (Tenant):** `source` values `self-serve-signup` / `sso-jit`; `cypherx.tenant.*`
  lifecycle events; well-known UUIDs.
- **Contract 18 (API keys):** `cx_sandbox_*` and `cx_prod_*` key formats.
- **Contract 19 (Usage/Quotas):** free-tier quota seeding; billing emitter / billing-bridge.
