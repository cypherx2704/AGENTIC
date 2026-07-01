package ai.cypherx.auth.service.s3

import org.slf4j.LoggerFactory
import java.io.InputStream
import java.nio.file.Files
import java.nio.file.Path
import java.nio.file.StandardCopyOption
import java.time.Duration

/**
 * Local-filesystem [ObjectStore] for dev/test (selected by `AUDIT_EXPORT_STORE=local`, the default).
 *
 * Objects are written under a configured base directory; the object key maps to a relative path
 * below it. No AWS credentials are required, so a local boot / the test profile works with no cloud
 * wiring at all (the WP04 fail-soft requirement).
 *
 * Presigned URLs are the object's `file://` URI — there is nothing to sign on a local volume; the
 * 7-day TTL is advisory (a local export is read directly off the mounted path). Writes are atomic
 * (write to a sibling `*.tmp` then `ATOMIC_MOVE`) so a reader never observes a half-written object.
 *
 * [isConfigured] is false only when the base directory cannot be created (e.g. a read-only mount) —
 * the caller then degrades gracefully rather than crashing.
 */
class LocalObjectStore(basePath: String) : ObjectStore {

    private val base: Path = Path.of(basePath).toAbsolutePath().normalize()

    /** Resolved once at construction: can we create/use the base dir? Drives [isConfigured]. */
    private val ready: Boolean = runCatching { Files.createDirectories(base) }
        .onFailure { log.warn("local object store base dir {} not writable: {}", base, it.message) }
        .isSuccess

    override val backend: String = "local"
    override val isConfigured: Boolean get() = ready

    override fun put(key: String, body: InputStream, contentLength: Long?, contentType: String): String {
        ensureReady()
        val target = resolveKey(key)
        return try {
            Files.createDirectories(target.parent)
            val tmp = target.resolveSibling(target.fileName.toString() + ".tmp")
            body.use { Files.copy(it, tmp, StandardCopyOption.REPLACE_EXISTING) }
            runCatching { Files.move(tmp, target, StandardCopyOption.ATOMIC_MOVE) }
                .recoverCatching { Files.move(tmp, target, StandardCopyOption.REPLACE_EXISTING) }
                .getOrThrow()
            target.toUri().toString()
        } catch (ex: Exception) {
            throw ObjectStore.ObjectStoreException("local object write failed for key=$key", ex)
        }
    }

    override fun putBytes(key: String, bytes: ByteArray, contentType: String): String =
        put(key, bytes.inputStream(), bytes.size.toLong(), contentType)

    /** A local object has no signed URL — return its `file://` URI (TTL advisory). */
    override fun presignedGetUrl(key: String, ttl: Duration): String {
        ensureReady()
        return resolveKey(key).toUri().toString()
    }

    /** Resolve a store-relative key under [base], rejecting any traversal outside it. */
    private fun resolveKey(key: String): Path {
        val clean = key.trim().trimStart('/')
        val resolved = base.resolve(clean).normalize()
        if (!resolved.startsWith(base)) {
            throw ObjectStore.ObjectStoreException("object key escapes base directory: $key")
        }
        return resolved
    }

    private fun ensureReady() {
        if (!ready) throw ObjectStore.ObjectStoreException("local object store base dir is not writable: $base")
    }

    private companion object {
        val log = LoggerFactory.getLogger(LocalObjectStore::class.java)
    }
}
