from __future__ import annotations

from datetime import datetime
from typing import Any

from flask import (
    Flask,
    Response,
    abort,
    make_response,
    render_template,
    request,
    stream_with_context,
)

from b2router.services import account_pool


def _format_files(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Map raw cache entries (from FileCacheEntry.to_dict) into template dicts."""
    def _fmt_ts(ts):
        """Format upload timestamp to human-readable string."""
        if ts is None:
            return "-"
        try:
            # ts is milliseconds since epoch (13-digit number)
            dt = datetime.fromtimestamp(ts / 1000)
            return dt.strftime("%Y-%m-%d %H:%M")
        except (ValueError, OSError):
            return str(ts)

    formatted = []
    for f in raw:
        formatted.append({
            "file_name": f["file_name"],
            "file_id": f["file_id"],
            "size": f["size"],
            "size_mb": round(f["size"] / (1024 * 1024), 2),
            "content_type": f["content_type"],
            "upload_time": _fmt_ts(f["upload_timestamp"]),
            "action": f["action"],
            "accounts": f["accounts"],
        })
    return formatted
def _format_stats(stats: dict[str, Any]) -> dict[str, Any]:
    free_bytes = stats["total_free_bytes"]
    used_bytes = stats["total_used_bytes"]
    capacity_bytes = stats["total_capacity_bytes"]
    free_mb = round(free_bytes / (1024 * 1024), 1)
    used_mb = round(used_bytes / (1024 * 1024), 1)
    return {
        "total_free_space": "Unlimited" if capacity_bytes == 0 else f"{free_mb} MB",
        "total_used_space": f"{used_mb} MB",
        "number_of_files": stats["number_of_files"],
        "number_of_accounts": stats["number_of_accounts"],
    }


def create_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = "dev-secret-change-in-production"

    @app.route("/")
    def index():
        files = _format_files(account_pool.list_files())
        stats = _format_stats(account_pool.file_cache_stats())
        return render_template("index.html", files=files, stats=stats)

    @app.route("/files")
    def files_fragment():
        """HTMX fragment: full file table (optionally filtered by ?q=)."""
        q = request.args.get("q", "").strip()
        files = _format_files(account_pool.list_files())
        if q:
            files = [f for f in files if q.lower() in f["file_name"].lower()]
        return render_template("fragments/files_table.html", files=files, query=q)

    @app.route("/stats")
    def stats_fragment():
        """HTMX fragment: account-pool statistics."""
        stats = _format_stats(account_pool.file_cache_stats())
        return render_template("fragments/stats.html", stats=stats)

    @app.route("/upload", methods=["POST"])
    def upload():
        """Handle HTMX file upload: flash messages + OOB refresh of table/stats."""
        messages: list[dict[str, Any]] = []
        for file in request.files.getlist("file"):
            if not file.filename:
                continue
            content = file.read()
            content_type = file.content_type or "application/octet-stream"
            try:
                account = account_pool.upload_file(
                    file.filename, content, content_type
                )
                messages.append({
                    "type": "success",
                    "text": f"Uploaded “{file.filename}” to {account.name} ({len(content):,} bytes)",
                })
            except AssertionError as e:
                messages.append({"type": "error", "text": f"“{file.filename}”: {e}"})
            except Exception as e:
                messages.append({"type": "error", "text": f"“{file.filename}”: {e}"})

        # Refresh the file table and stats out-of-band; re-render the form
        # (with messages) into #upload-region.
        table_html = render_template(
            "fragments/files_table.html",
            files=_format_files(account_pool.list_files()),
        )
        stats_html = render_template(
            "fragments/stats.html",
            stats=_format_stats(account_pool.file_cache_stats()),
        )
        form_html = render_template("fragments/upload_form.html", messages=messages)

        body = (
            f'<div id="file-table" hx-swap-oob="true">{table_html}</div>'
            f'<div id="stats-region" hx-swap-oob="true">{stats_html}</div>'
            f"{form_html}"
        )
        resp = make_response(body)
        resp.headers["Content-Type"] = "text/html; charset=utf-8"
        return resp

    @app.route("/download/<path:file_name>")
    def download_file(file_name: str):
        """Stream file download from B2."""
        accounts = account_pool.get_file_accounts(file_name)
        if not accounts:
            abort(404, description=f"File '{file_name}' not found in any account")
        account_name = accounts[0]
        account = account_pool[account_name]

        downloaded = account.bucket.download_file_by_name(file_name)
        file_info = account_pool.get_file(file_name)
        content_type = file_info.content_type if file_info else "application/octet-stream"

        def generate():
            with downloaded.response as r:
                r.raise_for_status()
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        yield chunk

        return Response(
            stream_with_context(generate()),
            headers={
                "Content-Type": content_type,
                "Content-Disposition": f'attachment; filename="{file_name}"',
            },
        )

    @app.route("/api/files")
    def api_files():
        """JSON: file cache + stats."""
        return {"files": account_pool.list_files(), "stats": account_pool.file_cache_stats()}

    @app.route("/api/accounts")
    def api_accounts():
        """JSON: active accounts + stats."""
        return {"active": account_pool.names(), "stats": account_pool.stats()}

    return app


def run_server(host: str = "0.0.0.0", port: int = 8000, debug: bool = False) -> None:
    """Run the Flask development server."""
    app = create_app()
    app.run(host=host, port=port, debug=debug)
