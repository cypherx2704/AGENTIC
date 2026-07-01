package ai.cypherx.auth.service.s3

import java.io.InputStream
import java.time.Duration

/**
 * Pluggable object-storage abstraction for the audit pipeline (WP04).
 *
 * Two production-shaped backends are selected by env `AUDIT_EXPORT_STORE`
 * (see [ai.cypherx.auth.config.AuditPipelineProperties.store]):
 *  - [LocalObjectStore] — the dev/test filesystem store (no AWS creds; `file://` URLs);
 *  - [S3ObjectStore]    — S3 / MinIO via AWS SigV4-signed HTTP (no AWS SDK dependency).
 *
 * FAIL-SOFT: when the selected backend is UNCONFIGURED (e.g. `store=s3` but no bucket/creds), the
 * store factory hands out a [NoopObjectStore] whose [isConfigured] is false and whose write methods
 * throw [ObjectStoreException]. Callers (export endpoint, mirror consumer) MUST check [isConfigured]
 * and degrade gracefully (503 / log-and-skip) rather than crash — so local boots and tests are clean
 * even when no object backend is wired.
 *
 * All keys are store-relative (e.g. `audit-exports/{tenant}/{ts}.jsonl`); the implementation maps a
 * key to a filesystem path or an S3 object.
 */
interface ObjectStore {

    /** Backend tag for logs/metrics: `local` | `s3` | `minio` | `noop`. */
    val backend: String

    /**
     * True when the backend is fully wired (bucket + creds for S3, a writable base dir for local).
     * False on the no-op fallback — callers degrade gracefully instead of attempting a write.
     */
    val isConfigured: Boolean

    /**
     * Stream [body] to object [key] with the given [contentType]. [contentLength] is the exact byte
     * length when known (required for the single-shot S3 PUT signature); pass null only for the local
     * store. Returns the canonical store URI of the written object (`s3://…` or `file://…`).
     *
     * @throws ObjectStoreException on any write failure (the caller decides whether to fail-soft).
     */
    fun put(key: String, body: InputStream, contentLength: Long?, contentType: String): String

    /** Convenience: store the UTF-8 [bytes] under [key]. Returns the canonical store URI. */
    fun putBytes(key: String, bytes: ByteArray, contentType: String): String

    /**
     * Produce a time-limited read URL for [key] valid for [ttl]. For S3/MinIO this is a SigV4
     * query-signed GET URL; for the local store it is the object's `file://` URI (the TTL is
     * advisory — local objects are read directly off the mounted volume).
     */
    fun presignedGetUrl(key: String, ttl: Duration): String

    /** Raised on any object-store write/sign failure. Callers map to 503 or log-and-skip. */
    class ObjectStoreException(message: String, cause: Throwable? = null) : RuntimeException(message, cause)
}
