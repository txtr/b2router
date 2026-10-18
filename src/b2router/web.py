from __future__ import annotations

from flask import Flask, render_template, Response, stream_with_context, abort, request, redirect, url_for, flash

from b2router.services import account_pool


def create_app() -> Flask:
    app = Flask(__name__, template_folder="templates")
    app.secret_key = "dev-secret-change-in-production"

    @app.route("/")
    def index():
        files = account_pool.list_files()
        stats = account_pool.file_cache_stats()

        # Format files for template
        formatted_files = []
        for f in files:
            formatted_files.append({
                "file_name": f["file_name"],
                "file_id": f["file_id"],
                "size": f["size"],
                "size_mb": round(f["size"] / (1024 * 1024), 2),
                "content_type": f["content_type"],
                "upload_time": f["upload_timestamp"],
                "action": f["action"],
                "accounts": f["accounts"],
            })

        stats_formatted = {
            "unique_files": stats["unique_files"],
            "total_size_mb": round(stats["total_size_bytes"] / (1024 * 1024), 2),
            "total_account_references": stats["total_account_references"],
            "accounts_with_files": stats["accounts_with_files"],
        }

        return render_template(
            "index.html",
            files=formatted_files,
            stats=stats_formatted,
        )

    @app.route("/upload", methods=["GET", "POST"])
    def upload_file():
        if request.method == "GET":
            return render_template("upload.html")

        # POST: handle multiple file upload
        uploaded_files = request.files.getlist("file")
        if not uploaded_files or all(not f.filename for f in uploaded_files):
            flash("No files selected", "error")
            return redirect(url_for("upload_file"))

        any_success = False
        for file in uploaded_files:
            if not file.filename:
                continue  # skip empty or None
            filename = file.filename
            content = file.read()
            content_type = file.content_type or "application/octet-stream"

            try:
                account = account_pool.upload_file(filename, content, content_type)
                flash(f"File '{filename}' uploaded to {account.name} ({len(content):,} bytes)", "success")
                any_success = True
            except AssertionError as e:
                flash(f"Failed to upload '{filename}': {e}", "error")
            except Exception as e:
                flash(f"Failed to upload '{filename}': {e}", "error")

        # After processing all files, stay on upload page to allow more uploads
        return redirect(url_for("upload_file"))

    @app.route("/download/<path:file_name>")
    def download_file(file_name: str):
        """Stream file download from B2."""
        # Find which account has this file
        accounts = account_pool.get_file_accounts(file_name)
        if not accounts:
            abort(404, description=f"File '{file_name}' not found in any account")

        # Use the first account that has the file
        account_name = accounts[0]
        account = account_pool[account_name]

        # Use B2 SDK to download (handles auth for private buckets)
        downloaded_file = account.bucket.download_file_by_name(file_name)

        # Get content type from cache
        file_info = account_pool.get_file(file_name)
        content_type = file_info.content_type if file_info else "application/octet-stream"

        def generate():
            # Stream from B2 response
            with downloaded_file.response as r:
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
        """JSON API endpoint for file cache."""
        return {"files": account_pool.list_files(), "stats": account_pool.file_cache_stats()}

    @app.route("/api/accounts")
    def api_accounts():
        """JSON API endpoint for account status."""
        return {
            "active": account_pool.names(),
            "stats": account_pool.stats(),
        }

    return app


def run_server(host: str = "0.0.0.0", port: int = 8000, debug: bool = False) -> None:
    """Run the Flask development server."""
    app = create_app()
    app.run(host=host, port=port, debug=debug)