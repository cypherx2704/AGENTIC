package ai.cypherx.auth.service.s3

import java.io.InputStream
import java.net.URI
import java.net.http.HttpClient
import java.net.http.HttpRequest
import java.net.http.HttpResponse
import java.nio.charset.StandardCharsets
import java.security.MessageDigest
import java.time.Duration
import java.time.Instant
import java.time.ZoneOffset
import java.time.format.DateTimeFormatter
import javax.crypto.Mac
import javax.crypto.spec.SecretKeySpec

/**
 * S3 / MinIO [ObjectStore] backed by AWS Signature Version 4 over plain HTTP — deliberately with NO
 * AWS SDK dependency (the build only carries `software.amazon.awssdk:kms`). PUTs are header-signed;
 * read URLs are query-string presigned GET URLs. This keeps the audit pipeline self-contained and
 * avoids dragging the S3 SDK + its transitive surface into auth-service.
 *
 * Backend selection (env `AUDIT_EXPORT_STORE`): `s3` → real AWS S3 (virtual-hosted-style host
 * derived from bucket+region); `minio` → an S3-compatible store at a custom [endpoint] with
 * path-style addressing (`{endpoint}/{bucket}/{key}`).
 *
 * FAIL-SOFT: [isConfigured] is false when the bucket or credentials are blank — the factory only
 * constructs this class when they are present, but the guard is kept so a partially-wired deployment
 * degrades to 503 rather than emitting unsigned requests.
 *
 * Presigned GET URLs are signed for the configured [exportRegion]/`s3` service with the requested
 * TTL (Contract: 7 days for exports). SigV4 caps presigned validity at 7 days (604800s); longer TTLs
 * are clamped.
 */
class S3ObjectStore(
    private val bucket: String,
    private val region: String,
    private val accessKeyId: String,
    private val secretAccessKey: String,
    private val sessionToken: String?,
    endpoint: String?,
    httpTimeoutMs: Long,
) : ObjectStore {

    /** Normalised custom endpoint (MinIO/path-style) or null for real AWS S3 (virtual-hosted). */
    private val endpointBase: String? = endpoint?.takeIf { it.isNotBlank() }?.trimEnd('/')

    private val pathStyle: Boolean = endpointBase != null

    private val httpTimeout: Duration = Duration.ofMillis(httpTimeoutMs)

    private val http: HttpClient = HttpClient.newBuilder()
        .connectTimeout(httpTimeout)
        .build()

    override val backend: String = if (pathStyle) "minio" else "s3"

    override val isConfigured: Boolean =
        bucket.isNotBlank() && accessKeyId.isNotBlank() && secretAccessKey.isNotBlank()

    // ── Writes (header-signed PUT) ───────────────────────────────────────────────────────────

    override fun put(key: String, body: InputStream, contentLength: Long?, contentType: String): String {
        // The single-shot SigV4 PUT needs the exact body bytes to compute the payload hash; we
        // buffer here (audit export objects are bounded by exportMaxRows). For very large objects a
        // later phase would switch to streaming/multipart with UNSIGNED-PAYLOAD.
        val bytes = body.use { it.readAllBytes() }
        return putBytes(key, bytes, contentType)
    }

    override fun putBytes(key: String, bytes: ByteArray, contentType: String): String {
        ensureConfigured()
        val cleanKey = key.trim().trimStart('/')
        val uri = objectUri(cleanKey)
        val now = Instant.now()
        val payloadHashHex = sha256Hex(bytes)

        val headers = signedHeaders(
            method = "PUT",
            uri = uri,
            now = now,
            payloadHashHex = payloadHashHex,
            extraSignedHeaders = sortedMapOf("content-type" to contentType),
        )

        val builder = HttpRequest.newBuilder(uri)
            .timeout(httpTimeout)
            .PUT(HttpRequest.BodyPublishers.ofByteArray(bytes))
            .header("content-type", contentType)
        headers.forEach { (k, v) -> builder.header(k, v) }

        val resp = try {
            http.send(builder.build(), HttpResponse.BodyHandlers.ofString())
        } catch (ex: Exception) {
            throw ObjectStore.ObjectStoreException("S3 PUT failed for key=$cleanKey", ex)
        }
        if (resp.statusCode() !in 200..299) {
            throw ObjectStore.ObjectStoreException(
                "S3 PUT key=$cleanKey returned HTTP ${resp.statusCode()}: ${resp.body().take(500)}",
            )
        }
        return canonicalUri(cleanKey)
    }

    // ── Presigned GET (query-string-signed) ──────────────────────────────────────────────────

    override fun presignedGetUrl(key: String, ttl: Duration): String {
        ensureConfigured()
        val cleanKey = key.trim().trimStart('/')
        val uri = objectUri(cleanKey)
        val now = Instant.now()
        val expirySeconds = ttl.seconds.coerceIn(1, MAX_PRESIGN_SECONDS)

        val amzDate = AMZ_DATE_FMT.format(now)
        val dateStamp = DATE_STAMP_FMT.format(now)
        val credentialScope = "$dateStamp/$region/$SERVICE/aws4_request"
        val host = uri.host + (if (uri.port != -1) ":${uri.port}" else "")

        // Canonical query params (sorted) for a presigned URL.
        val query = sortedMapOf(
            "X-Amz-Algorithm" to ALGORITHM,
            "X-Amz-Credential" to "$accessKeyId/$credentialScope",
            "X-Amz-Date" to amzDate,
            "X-Amz-Expires" to expirySeconds.toString(),
            "X-Amz-SignedHeaders" to "host",
        )
        if (!sessionToken.isNullOrBlank()) query["X-Amz-Security-Token"] = sessionToken

        val canonicalQuery = query.entries.joinToString("&") { (k, v) ->
            "${uriEncode(k, true)}=${uriEncode(v, true)}"
        }
        val canonicalHeaders = "host:$host\n"
        val canonicalRequest = listOf(
            "GET",
            canonicalPath(uri),
            canonicalQuery,
            canonicalHeaders,
            "host",
            "UNSIGNED-PAYLOAD",
        ).joinToString("\n")

        val stringToSign = listOf(
            ALGORITHM,
            amzDate,
            credentialScope,
            sha256Hex(canonicalRequest.toByteArray(StandardCharsets.UTF_8)),
        ).joinToString("\n")

        val signature = hmacHex(signingKey(dateStamp), stringToSign)
        val base = uri.toString()
        return "$base?$canonicalQuery&X-Amz-Signature=$signature"
    }

    // ── SigV4 header signing (for PUT) ───────────────────────────────────────────────────────

    /**
     * Compute the full SigV4 header set (Authorization + x-amz-date + x-amz-content-sha256 [+
     * x-amz-security-token]) for [method] on [uri] with the given [payloadHashHex]. [extraSignedHeaders]
     * (e.g. content-type) are folded into the signed set; the returned map is added to the request.
     */
    private fun signedHeaders(
        method: String,
        uri: URI,
        now: Instant,
        payloadHashHex: String,
        extraSignedHeaders: Map<String, String>,
    ): Map<String, String> {
        val amzDate = AMZ_DATE_FMT.format(now)
        val dateStamp = DATE_STAMP_FMT.format(now)
        val host = uri.host + (if (uri.port != -1) ":${uri.port}" else "")

        val signed = sortedMapOf<String, String>()
        signed["host"] = host
        signed["x-amz-content-sha256"] = payloadHashHex
        signed["x-amz-date"] = amzDate
        if (!sessionToken.isNullOrBlank()) signed["x-amz-security-token"] = sessionToken
        extraSignedHeaders.forEach { (k, v) -> signed[k.lowercase()] = v.trim() }

        val signedHeaderNames = signed.keys.joinToString(";")
        val canonicalHeaders = signed.entries.joinToString("") { (k, v) -> "$k:$v\n" }

        val canonicalRequest = listOf(
            method,
            canonicalPath(uri),
            "", // no query for the PUT
            canonicalHeaders,
            signedHeaderNames,
            payloadHashHex,
        ).joinToString("\n")

        val credentialScope = "$dateStamp/$region/$SERVICE/aws4_request"
        val stringToSign = listOf(
            ALGORITHM,
            amzDate,
            credentialScope,
            sha256Hex(canonicalRequest.toByteArray(StandardCharsets.UTF_8)),
        ).joinToString("\n")

        val signature = hmacHex(signingKey(dateStamp), stringToSign)
        val authorization =
            "$ALGORITHM Credential=$accessKeyId/$credentialScope, " +
                "SignedHeaders=$signedHeaderNames, Signature=$signature"

        val out = linkedMapOf(
            "x-amz-date" to amzDate,
            "x-amz-content-sha256" to payloadHashHex,
            "Authorization" to authorization,
        )
        if (!sessionToken.isNullOrBlank()) out["x-amz-security-token"] = sessionToken
        return out
    }

    /** Derive the SigV4 per-request signing key: HMAC chain over date/region/service. */
    private fun signingKey(dateStamp: String): ByteArray {
        val kDate = hmac("AWS4$secretAccessKey".toByteArray(StandardCharsets.UTF_8), dateStamp)
        val kRegion = hmac(kDate, region)
        val kService = hmac(kRegion, SERVICE)
        return hmac(kService, "aws4_request")
    }

    // ── URI construction ─────────────────────────────────────────────────────────────────────

    /** The request URI for [key]: virtual-hosted (AWS) or path-style (MinIO/custom endpoint). */
    private fun objectUri(key: String): URI {
        val encodedKey = key.split("/").joinToString("/") { uriEncode(it, false) }
        return if (pathStyle) {
            URI.create("$endpointBase/$bucket/$encodedKey")
        } else {
            URI.create("https://$bucket.s3.$region.amazonaws.com/$encodedKey")
        }
    }

    /** The stable, store-canonical URI persisted on the job row (`s3://bucket/key`). */
    private fun canonicalUri(key: String): String = "s3://$bucket/$key"

    /** Canonical resource path used in the SigV4 canonical request (already percent-encoded). */
    private fun canonicalPath(uri: URI): String {
        val raw = uri.rawPath
        return if (raw.isNullOrEmpty()) "/" else raw
    }

    private fun ensureConfigured() {
        if (!isConfigured) {
            throw ObjectStore.ObjectStoreException("S3 object store is not configured (bucket/credentials missing)")
        }
    }

    // ── Crypto / encoding helpers ──────────────────────────────────────────────────────────────

    private fun sha256Hex(bytes: ByteArray): String =
        MessageDigest.getInstance("SHA-256").digest(bytes).toHex()

    private fun hmac(key: ByteArray, data: String): ByteArray {
        val mac = Mac.getInstance("HmacSHA256")
        mac.init(SecretKeySpec(key, "HmacSHA256"))
        return mac.doFinal(data.toByteArray(StandardCharsets.UTF_8))
    }

    private fun hmacHex(key: ByteArray, data: String): String = hmac(key, data).toHex()

    private fun ByteArray.toHex(): String = joinToString("") { "%02x".format(it) }

    /**
     * AWS-flavoured RFC 3986 percent-encoding. [encodeSlash] = false leaves `/` unescaped (object
     * key path segments are already split on `/`); true escapes it (query-param values).
     */
    private fun uriEncode(value: String, encodeSlash: Boolean): String {
        val sb = StringBuilder()
        for (b in value.toByteArray(StandardCharsets.UTF_8)) {
            val ch = b.toInt() and 0xFF
            val c = ch.toChar()
            when {
                c in 'A'..'Z' || c in 'a'..'z' || c in '0'..'9' ||
                    c == '_' || c == '-' || c == '~' || c == '.' -> sb.append(c)
                c == '/' && !encodeSlash -> sb.append('/')
                else -> sb.append('%').append("%02X".format(ch))
            }
        }
        return sb.toString()
    }

    private companion object {
        const val SERVICE = "s3"
        const val ALGORITHM = "AWS4-HMAC-SHA256"
        const val MAX_PRESIGN_SECONDS = 604_800L // SigV4 hard cap = 7 days

        val AMZ_DATE_FMT: DateTimeFormatter =
            DateTimeFormatter.ofPattern("yyyyMMdd'T'HHmmss'Z'").withZone(ZoneOffset.UTC)
        val DATE_STAMP_FMT: DateTimeFormatter =
            DateTimeFormatter.ofPattern("yyyyMMdd").withZone(ZoneOffset.UTC)
    }
}
