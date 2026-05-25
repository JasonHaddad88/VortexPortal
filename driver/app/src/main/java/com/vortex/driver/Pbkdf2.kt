package com.vortex.driver

import java.security.SecureRandom
import javax.crypto.SecretKeyFactory
import javax.crypto.spec.PBEKeySpec

/**
 * Password hashing that matches `hub/db.py::hash_password` /
 * `verify_password` byte-for-byte.
 *
 *   pbkdf2_sha256${iters}${salt-hex}${digest-hex}
 *
 * - algo:    PBKDF2-HMAC-SHA256
 * - iters:   200_000 (matches hub)
 * - salt:    16 random bytes (matches `os.urandom(16)`)
 * - digest:  32 bytes (SHA-256 output width); we ask PBKDF2 for 256 bits.
 *
 * That symmetry is the whole point: a user created in the webapp can
 * sign in via the APK, and vice versa. There's only one users table.
 */
object Pbkdf2 {

    private const val ALGO = "PBKDF2WithHmacSHA256"
    private const val ITERS = 200_000
    private const val SALT_BYTES = 16
    private const val DIGEST_BITS = 256

    fun hash(password: String): String {
        val salt = ByteArray(SALT_BYTES).also { SecureRandom().nextBytes(it) }
        val digest = pbkdf2(password, salt, ITERS, DIGEST_BITS)
        return "pbkdf2_sha256\$$ITERS\$${salt.toHex()}\$${digest.toHex()}"
    }

    fun verify(password: String, stored: String): Boolean {
        val parts = stored.split('$', limit = 4)
        if (parts.size != 4 || parts[0] != "pbkdf2_sha256") return false
        val iters = parts[1].toIntOrNull() ?: return false
        val salt = parts[2].fromHexOrNull() ?: return false
        val expected = parts[3].fromHexOrNull() ?: return false
        val candidate = try {
            pbkdf2(password, salt, iters, expected.size * 8)
        } catch (_: Throwable) { return false }
        return constantTimeEquals(candidate, expected)
    }

    // ----- internals -----

    private fun pbkdf2(password: String, salt: ByteArray, iters: Int, bits: Int): ByteArray {
        val spec = PBEKeySpec(password.toCharArray(), salt, iters, bits)
        return try {
            SecretKeyFactory.getInstance(ALGO).generateSecret(spec).encoded
        } finally {
            spec.clearPassword()
        }
    }

    private fun constantTimeEquals(a: ByteArray, b: ByteArray): Boolean {
        if (a.size != b.size) return false
        var r = 0
        for (i in a.indices) r = r or (a[i].toInt() xor b[i].toInt())
        return r == 0
    }

    private fun ByteArray.toHex(): String {
        val sb = StringBuilder(this.size * 2)
        for (b in this) {
            val v = b.toInt() and 0xFF
            sb.append(HEX[v ushr 4]); sb.append(HEX[v and 0x0F])
        }
        return sb.toString()
    }

    private fun String.fromHexOrNull(): ByteArray? {
        if (this.length % 2 != 0) return null
        val out = ByteArray(this.length / 2)
        for (i in out.indices) {
            val hi = Character.digit(this[i * 2], 16)
            val lo = Character.digit(this[i * 2 + 1], 16)
            if (hi < 0 || lo < 0) return null
            out[i] = ((hi shl 4) or lo).toByte()
        }
        return out
    }

    private val HEX = "0123456789abcdef".toCharArray()
}
