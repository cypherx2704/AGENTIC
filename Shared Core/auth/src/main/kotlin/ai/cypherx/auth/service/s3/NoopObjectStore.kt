package ai.cypherx.auth.service.s3

import java.io.InputStream
import java.time.Duration

/**
 * Fail-soft no-op [ObjectStore] handed out by [ObjectStoreConfig] when the selected backend is
 * UNCONFIGURED (e.g. `AUDIT_EXPORT_STORE=s3` with no bucket/credentials, or an unknown store name).
 *
 * Its sole job is to let the application boot cleanly with NO object backend wired (WP04 fail-soft
 * requirement): [isConfigured] is false, so callers (export endpoint, mirror consumer) skip or
 * return 503 instead of attempting a write. Any direct write/sign attempt throws so a mis-wired
 * caller fails loudly rather than silently dropping data.
 */
class NoopObjectStore(private val reason: String) : ObjectStore {

    override val backend: String = "noop"
    override val isConfigured: Boolean = false

    override fun put(key: String, body: InputStream, contentLength: Long?, contentType: String): String =
        fail()

    override fun putBytes(key: String, bytes: ByteArray, contentType: String): String = fail()

    override fun presignedGetUrl(key: String, ttl: Duration): String = fail()

    private fun fail(): Nothing =
        throw ObjectStore.ObjectStoreException("object store not configured: $reason")
}
