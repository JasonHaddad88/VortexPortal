package com.vortex.driver

import android.content.Context
import android.os.Environment
import android.webkit.MimeTypeMap
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.io.IOException

/**
 * B11.5: file-browser ops served over the peer-WS / hub-WS surface.
 *
 * Wire shape matches `agent/agent.py::op_stat / op_list_dir /
 * op_read_file_stream` byte-for-byte, so the webapp's existing
 * `/devices/{id}/browse` HTML page works against a Driver APK with
 * zero hub-side changes, and the APK's own [PeerControlActivity]
 * Files tab speaks the same protocol.
 *
 *   stat       { exists, is_dir, size?, content_type? }
 *   list_dir   { entries: [{ name, is_dir, size?, is_image? }, ...] }
 *   read_file_stream     stream_start(size, content_type) + N chunks + stream_end
 *
 * Browsable root:
 *   `Environment.getExternalStorageDirectory()` (`/storage/emulated/0`
 *   on most phones). Path traversal is blocked the same way the
 *   Python agent's `_safe_resolve()` does it: the requested path is
 *   resolved + canonicalised, then verified to live under the root.
 *
 * Permission model:
 *   - On Android <= 12, broad read works via `READ_EXTERNAL_STORAGE`
 *     (declared in the manifest). If the user hasn't granted it
 *     yet, ops return a clean error pointing them at Settings.
 *   - On Android 13+, the legacy permission is replaced by
 *     `READ_MEDIA_IMAGES / VIDEO / AUDIO` (also in the manifest);
 *     general non-media file access requires
 *     `MANAGE_EXTERNAL_STORAGE` which Google Play restricts. Until
 *     a user grants All-Files access from system settings, only
 *     the app's own folders + media indexed by MediaStore are
 *     readable -- ops surface the limitation in the error string.
 */
object FileBrowserOps {

    fun register(ctx: Context, dispatcher: OpDispatcher) {
        dispatcher.register("stat") { args -> opStat(ctx, args) }
        dispatcher.register("list_dir") { args -> opListDir(ctx, args) }
        dispatcher.registerStream("read_file_stream") { args, sink ->
            opReadFileStream(ctx, args, sink)
        }
    }

    // ----- root + path safety -------------------------------------------

    /** Per-device browsable root. Same shape the Python agent uses
     *  (`STORAGE_ROOT` env var -> `~/storage/shared`). We don't make
     *  this user-configurable yet; B11.6 will add a Setup field. */
    private fun rootFor(ctx: Context): File {
        // Prefer shared external storage so the user's photos +
        // downloads are visible. Fall back to app-private external
        // (`getExternalFilesDir`) on devices where the broad path
        // isn't readable -- worst case the browser shows an empty
        // tree of the app's own scratch dir.
        val shared = Environment.getExternalStorageDirectory()
        return if (shared != null && shared.canRead()) shared
               else ctx.getExternalFilesDir(null) ?: ctx.filesDir
    }

    /** Resolve a relative request path against the root and refuse
     *  anything that escapes (`..` traversal, absolute paths to
     *  somewhere outside, symlinks pointing outside). */
    private fun safeResolve(ctx: Context, rel: String): File {
        val root = rootFor(ctx).canonicalFile
        val requested = if (rel.isBlank() || rel == "/") root
                        else File(root, rel.trimStart('/'))
        val canonical = try { requested.canonicalFile }
                        catch (_: IOException) { requested.absoluteFile }
        val rootPath = root.absolutePath.trimEnd(File.separatorChar) + File.separator
        if (canonical != root &&
            !(canonical.absolutePath + File.separator).startsWith(rootPath) &&
            canonical.absolutePath != root.absolutePath) {
            throw SecurityException("Path escapes browsable root")
        }
        return canonical
    }

    // ----- ops ----------------------------------------------------------

    private fun opStat(ctx: Context, args: JSONObject): JSONObject {
        val rel = args.optString("path", "")
        val p = safeResolve(ctx, rel)
        val out = JSONObject()
        if (!p.exists()) { out.put("exists", false); return out }
        val isDir = p.isDirectory
        out.put("exists", true)
        out.put("is_dir", isDir)
        if (!isDir) {
            try { out.put("size", p.length()) } catch (_: Throwable) {}
            out.put("content_type", guessMime(p.name))
        }
        return out
    }

    private fun opListDir(ctx: Context, args: JSONObject): JSONObject {
        val rel = args.optString("path", "")
        val p = safeResolve(ctx, rel)
        if (!p.exists()) throw RuntimeException("Path does not exist: ${p.absolutePath}")
        if (!p.isDirectory) throw RuntimeException("Not a directory: ${p.absolutePath}")
        if (!p.canRead()) throw RuntimeException(
            "Can't read this directory. On Android 11+ you may need to grant " +
            "All-Files access in Settings -> Apps -> Vortex Driver -> Permissions."
        )
        val children = try { p.listFiles() } catch (_: SecurityException) { null }
            ?: throw RuntimeException("Permission denied listing ${p.absolutePath}")
        // Match the Python agent's sort: dirs first, then files, both A-Z (case-insensitive).
        val sorted = children.sortedWith(
            compareBy<File>({ !it.isDirectory }, { it.name.lowercase() })
        )
        val arr = JSONArray()
        for (c in sorted) {
            val entry = JSONObject()
                .put("name", c.name)
                .put("is_dir", c.isDirectory)
            if (!c.isDirectory) {
                try { entry.put("size", c.length()) } catch (_: Throwable) {}
                val mime = guessMime(c.name)
                if (mime.startsWith("image/")) entry.put("is_image", true)
            }
            arr.put(entry)
        }
        return JSONObject().put("entries", arr)
    }

    private suspend fun opReadFileStream(
        ctx: Context, args: JSONObject, sink: WsStreamSink,
    ) {
        val rel = args.optString("path", "")
        val p = safeResolve(ctx, rel)
        if (!p.isFile) throw RuntimeException("Not a file: ${p.absolutePath}")
        if (!p.canRead()) throw RuntimeException("Permission denied reading ${p.absolutePath}")
        val size = p.length()
        val mime = guessMime(p.name)
        sink.sendStartWith { m ->
            m.put("size", size)
            m.put("content_type", mime)
        }
        // Same chunk size the Python agent uses (256 KiB) so the
        // peer's frame protocol fits in one WS message.
        val buf = ByteArray(256 * 1024)
        p.inputStream().use { input ->
            while (true) {
                val n = input.read(buf)
                if (n <= 0) break
                val chunk = if (n == buf.size) buf else buf.copyOf(n)
                if (!sink.sendChunk(chunk)) break   // backpressure / closed
            }
        }
        // sink.sendEnd is called by the dispatcher when the suspend
        // handler returns; no explicit close needed here.
    }

    // ----- helpers ------------------------------------------------------

    private fun guessMime(name: String): String {
        val ext = name.substringAfterLast('.', "").lowercase()
        if (ext.isBlank()) return "application/octet-stream"
        return MimeTypeMap.getSingleton().getMimeTypeFromExtension(ext)
            ?: "application/octet-stream"
    }
}
