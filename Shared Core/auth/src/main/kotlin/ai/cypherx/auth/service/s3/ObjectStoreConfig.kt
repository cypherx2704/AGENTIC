package ai.cypherx.auth.service.s3

import ai.cypherx.auth.config.AuditPipelineProperties
import org.slf4j.LoggerFactory
import org.springframework.context.annotation.Bean
import org.springframework.context.annotation.Configuration

/**
 * Selects the single [ObjectStore] bean from `cypherx.auth.audit-pipeline.store`
 * (env `AUDIT_EXPORT_STORE`): `local` (default) | `s3` | `minio`.
 *
 * FAIL-SOFT (WP04): the bean is ALWAYS created so the application context wires cleanly, even with
 * no object backend configured:
 *  - `local` → [LocalObjectStore] (no AWS creds needed; the default for dev/test);
 *  - `s3` / `minio` → [S3ObjectStore] when bucket + creds are present, else a [NoopObjectStore]
 *    (its `isConfigured=false` makes the export endpoint return 503 and the mirror consumer skip,
 *    rather than crashing the app);
 *  - anything else → [NoopObjectStore].
 *
 * The store name is resolved case-insensitively so `S3` / `MinIO` env values work too.
 */
@Configuration
class ObjectStoreConfig {

    @Bean
    fun objectStore(props: AuditPipelineProperties): ObjectStore {
        val store = props.store.trim().lowercase()
        return when (store) {
            "local" -> {
                val s = LocalObjectStore(props.local.basePath)
                log.info("audit object store = local (basePath={}, configured={})", props.local.basePath, s.isConfigured)
                s
            }
            "s3", "minio" -> buildS3(store, props)
            else -> {
                log.warn("unknown AUDIT_EXPORT_STORE='{}' — audit object store disabled (no-op)", props.store)
                NoopObjectStore("unknown store '${props.store}'")
            }
        }
    }

    /** Build the S3/MinIO store, degrading to no-op when bucket/credentials are absent (fail-soft). */
    private fun buildS3(store: String, props: AuditPipelineProperties): ObjectStore {
        val s3 = props.s3
        if (s3.bucket.isBlank() || s3.accessKeyId.isBlank() || s3.secretAccessKey.isBlank()) {
            log.warn(
                "AUDIT_EXPORT_STORE={} but bucket/credentials are unset — audit object store disabled (no-op)",
                store,
            )
            return NoopObjectStore("$store store missing bucket/credentials")
        }
        // A custom endpoint (path-style) is honoured whenever set — typically for `minio`, but a
        // self-hosted S3-compatible `s3` deployment may set it too. Real AWS S3 leaves it blank
        // (virtual-hosted host is derived from bucket + region inside S3ObjectStore).
        val impl = S3ObjectStore(
            bucket = s3.bucket,
            region = s3.region,
            accessKeyId = s3.accessKeyId,
            secretAccessKey = s3.secretAccessKey,
            sessionToken = s3.sessionToken,
            endpoint = s3.endpoint,
            httpTimeoutMs = s3.httpTimeoutMs,
        )
        log.info("audit object store = {} (bucket={}, region={}, endpoint={})", impl.backend, s3.bucket, s3.region, s3.endpoint)
        return impl
    }

    private companion object {
        val log = LoggerFactory.getLogger(ObjectStoreConfig::class.java)
    }
}
