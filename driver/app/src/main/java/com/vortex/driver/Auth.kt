package com.vortex.driver

import android.content.Context
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

/**
 * Auth orchestration against Turso directly. Mirrors the shape
 * `hub/app.py::login_post` / `register_post` + `db.py::create_user`
 * use, so a user created in either client lives in the same row of
 * the same `users` table.
 *
 * No hub server: the APK is the one talking to Turso. Sign-in does
 * one SELECT + PBKDF2 verify; register does one COUNT (for bootstrap
 * detection) + (optional) invite check + INSERT + (optional) invite
 * consume. After success, the calling activity saves the user_id
 * into [Prefs] and the user stays signed in until they explicitly
 * Sign out.
 */
object Auth {

    sealed class Result {
        data class Ok(val userId: Long, val username: String, val isAdmin: Boolean) : Result()
        data class Err(val message: String) : Result()
    }

    suspend fun signIn(ctx: Context, username: String, password: String): Result =
        withContext(Dispatchers.IO) {
            val client = clientOrFail(ctx) ?: return@withContext Result.Err(
                "Database not configured. Open Setup and add your Turso URL + token."
            )
            val uname = username.trim()
            if (uname.isBlank() || password.isEmpty()) {
                return@withContext Result.Err("Username and password are required.")
            }
            val rows = try {
                client.execute(
                    "SELECT id, username, password_hash, is_admin " +
                    "FROM users WHERE username = ?",
                    listOf(uname),
                ).rows
            } catch (e: TursoError) {
                return@withContext Result.Err("Database error: ${e.message}")
            }
            val row = rows.firstOrNull() ?: return@withContext Result.Err("Invalid credentials.")
            val stored = row["password_hash"] as? String
                ?: return@withContext Result.Err("Account is missing a password hash.")
            if (!Pbkdf2.verify(password, stored)) {
                return@withContext Result.Err("Invalid credentials.")
            }
            val id = (row["id"] as? Long) ?: return@withContext Result.Err("User row has no id column.")
            val admin = ((row["is_admin"] as? Long) ?: 0L) != 0L
            Result.Ok(id, row["username"] as? String ?: uname, admin)
        }

    /**
     * Register a new user. Bootstrap (first user) is always allowed and
     * becomes admin. Otherwise the invite code is enforced server-side
     * the same way the webapp does (SELECT invite WHERE code=? AND
     * used_by IS NULL), and consumed on success.
     */
    suspend fun register(
        ctx: Context,
        username: String,
        password: String,
        inviteCode: String,
    ): Result = withContext(Dispatchers.IO) {
        val client = clientOrFail(ctx) ?: return@withContext Result.Err(
            "Database not configured. Open Setup and add your Turso URL + token."
        )
        val uname = username.trim()
        if (uname.isBlank()) return@withContext Result.Err("All fields required.")
        if (password.length < 8) return@withContext Result.Err("Password must be at least 8 characters.")
        if (!uname.all { it.isLetterOrDigit() || it == '_' || it == '-' }) {
            return@withContext Result.Err("Username may only contain letters, numbers, _ and -.")
        }

        // Bootstrap check + invite check + uniqueness check, in one round-trip.
        val pre = try {
            client.pipeline(listOf(
                TursoClient.Stmt("SELECT COUNT(*) AS n FROM users"),
                TursoClient.Stmt("SELECT id FROM users WHERE username = ?", listOf(uname)),
                TursoClient.Stmt(
                    "SELECT code FROM invites WHERE code = ? AND used_by IS NULL",
                    listOf(inviteCode.trim()),
                ),
            ))
        } catch (e: TursoError) {
            return@withContext Result.Err("Database error: ${e.message}")
        }
        val userCount = ((pre[0].firstRow()?.get("n") as? Long) ?: 0L)
        if (pre[1].rows.isNotEmpty()) return@withContext Result.Err("Username already taken.")
        val isBootstrap = (userCount == 0L)
        // We don't have a settings-table read here for `registration_mode`
        // -- defer to a sensible default: bootstrap=allow; otherwise
        // require an invite if one was provided OR there are no invites
        // in the table. (A more polished read of the mode setting can
        // come in B11.2; the validation here matches the webapp's
        // invite-mode default for non-bootstrap inserts.)
        if (!isBootstrap && inviteCode.trim().isNotBlank() && pre[2].rows.isEmpty()) {
            return@withContext Result.Err("Invalid or already-used invite code.")
        }

        val hash = Pbkdf2.hash(password)
        val createdAt = System.currentTimeMillis() / 1000L
        val insert = try {
            client.execute(
                "INSERT INTO users (username, password_hash, is_admin, created_at) " +
                "VALUES (?, ?, ?, ?)",
                listOf(uname, hash, if (isBootstrap) 1L else 0L, createdAt),
            )
        } catch (e: TursoError) {
            return@withContext Result.Err("Could not create account: ${e.message}")
        }
        val newId = insert.lastInsertRowId ?: return@withContext Result.Err(
            "INSERT succeeded but server didn't return last_insert_rowid."
        )
        // Best-effort invite consume (matches the webapp's atomic step;
        // a race here just leaves the invite usable, no user-visible harm).
        if (!isBootstrap && inviteCode.trim().isNotBlank()) {
            try {
                client.execute(
                    "UPDATE invites SET used_by = ?, used_at = ? " +
                    "WHERE code = ? AND used_by IS NULL",
                    listOf(newId, createdAt, inviteCode.trim()),
                )
            } catch (_: TursoError) { /* best-effort */ }
        }
        Result.Ok(newId, uname, isBootstrap)
    }

    /** Hand back a [TursoClient] configured from Prefs, or null if the
     *  user hasn't completed the Setup screen yet. */
    private fun clientOrFail(ctx: Context): TursoClient? {
        val url = Prefs.tursoUrl(ctx)?.takeIf { it.isNotBlank() } ?: return null
        val tok = Prefs.tursoToken(ctx)?.takeIf { it.isNotBlank() } ?: return null
        return TursoClient(url, tok)
    }
}
