from __future__ import annotations

from flask import Flask, render_template_string, Response, stream_with_context, abort, request, redirect, url_for, flash

from b2router.services import account_pool


INDEX_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>B2 Router - File Cache</title>
    <style>
        body { font-family: system-ui, sans-serif; margin: 2rem; }
        h1 { color: #333; }
        h2 { color: #555; margin-top: 2rem; }
        table { border-collapse: collapse; width: 100%; margin-top: 1rem; }
        th, td { border: 1px solid #ddd; padding: 0.75rem; text-align: left; }
        th { background-color: #f4f4f4; font-weight: 600; }
        tr:nth-child(even) { background-color: #fafafa; }
        tr:hover { background-color: #f0f0f0; }
        .stats { display: flex; gap: 2rem; margin: 1rem 0; padding: 1rem; background: #f9f9f9; border-radius: 4px; }
        .stat { font-weight: 500; }
        .stat-value { font-weight: 700; color: #2c3e50; }
        .accounts-badge { display: inline-block; background: #e0e0e0; padding: 0.2rem 0.5rem; border-radius: 3px; margin-right: 0.25rem; font-size: 0.85rem; }
        .size { font-family: monospace; }
        .btn { display: inline-block; padding: 0.4rem 0.8rem; background: #0066cc; color: white; text-decoration: none; border-radius: 4px; font-size: 0.85rem; border: none; cursor: pointer; }
        .btn:hover { background: #0052a3; }
        .btn:disabled { background: #999; cursor: not-allowed; }
        .btn-success { background: #28a745; }
        .btn-success:hover { background: #218838; }
        .upload-form { background: #f8f9fa; padding: 1.5rem; border-radius: 4px; margin-bottom: 2rem; }
        .form-group { margin-bottom: 1rem; }
        label { display: block; margin-bottom: 0.5rem; font-weight: 500; }
        input[type="file"] { padding: 0.5rem; }
        input[type="text"] { padding: 0.5rem; width: 300px; }
        .alert { padding: 1rem; border-radius: 4px; margin-bottom: 1rem; }
        .alert-success { background: #d4edda; color: #155724; border: 1px solid #c3e6cb; }
        .alert-error { background: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; }
        .progress { margin-top: 1rem; }
    </style>
</head>
<body>
    <h1>B2 Router - File Cache</h1>
    <p>Centralized view of files across all B2 accounts</p>
    <p><a href="/upload">Upload Files</a></p>

    <div class="stats">
        <div class="stat">Unique Files: <span class="stat-value">{{ stats.unique_files }}</span></div>
        <div class="stat">Total Size: <span class="stat-value">{{ stats.total_size_mb }} MB</span></div>
        <div class="stat">Total References: <span class="stat-value">{{ stats.total_account_references }}</span></div>
        <div class="stat">Accounts with Files: <span class="stat-value">{{ stats.accounts_with_files }}</span></div>
    </div>

    {% if files %}
    <h2>Files</h2>
    <table>
        <thead>
            <tr>
                <th>File Name</th>
                <th>File ID</th>
                <th>Size</th>
                <th>Content Type</th>
                <th>Upload Time</th>
                <th>Action</th>
                <th>Accounts ({{ stats.accounts_with_files }})</th>
                <th>Download</th>
            </tr>
        </thead>
        <tbody>
            {% for file in files %}
            <tr>
                <td>{{ file.file_name }}</td>
                <td><code>{{ file.file_id }}</code></td>
                <td class="size">{{ file.size_mb }} MB ({{ file.size }} bytes)</td>
                <td>{{ file.content_type }}</td>
                <td>{{ file.upload_time }}</td>
                <td>{{ file.action }}</td>
                <td>
                    {% for acc in file.accounts %}
                    <span class="accounts-badge">{{ acc }}</span>
                    {% endfor %}
                </td>
                <td>
                    <a href="/download/{{ file.file_name }}" class="btn">Download</a>
                </td>
            </tr>
            {% endfor %}
        </tbody>
    </table>
    {% else %}
    <p>No files found in any account.</p>
    {% endif %}
</body>
</html>
"""

UPLOAD_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Upload Files - B2 Router</title>
    <style>
        body { font-family: system-ui, sans-serif; margin: 2rem; }
        h1 { color: #333; }
        h2 { color: #555; margin-top: 2rem; }
        table { border-collapse: collapse; width: 100%; margin-top: 1rem; }
        th, td { border: 1px solid #ddd; padding: 0.75rem; text-align: left; }
        th { background-color: #f4f4f4; font-weight: 600; }
        tr:nth-child(even) { background-color: #fafafa; }
        tr:hover { background-color: #f0f0f0; }
        .stats { display: flex; gap: 2rem; margin: 1rem 0; padding: 1rem; background: #f9f9f9; border-radius: 4px; }
        .stat { font-weight: 500; }
        .stat-value { font-weight: 700; color: #2c3e50; }
        .accounts-badge { display: inline-block; background: #e0e0e0; padding: 0.2rem 0.5rem; border-radius: 3px; margin-right: 0.25rem; font-size: 0.85rem; }
        .size { font-family: monospace; }
        .btn { display: inline-block; padding: 0.4rem 0.8rem; background: #0066cc; color: white; text-decoration: none; border-radius: 4px; font-size: 0.85rem; border: none; cursor: pointer; }
        .btn:hover { background: #0052a3; }
        .btn:disabled { background: #999; cursor: not-allowed; }
        .btn-success { background: #28a745; }
        .btn-success:hover { background: #218838; }
        .upload-form { background: #f8f9fa; padding: 1.5rem; border-radius: 4px; margin-bottom: 2rem; }
        .form-group { margin-bottom: 1rem; }
        label { display: block; margin-bottom: 0.5rem; font-weight: 500; }
        input[type="file"] { padding: 0.5rem; }
        input[type="text"] { padding: 0.5rem; width: 300px; }
        .alert { padding: 1rem; border-radius: 4px; margin-bottom: 1rem; }
        .alert-success { background: #d4edda; color: #155724; border: 1px solid #c3e6cb; }
        .alert-error { background: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; }
        .progress { margin-top: 1rem; }
    </style>
</head>
<body>
    <h1>B2 Router - Upload Files</h1>
    <p>Upload one or more files to the B2 Router pool (best-fit routing).</p>

    {% with messages = get_flashed_messages(with_categories=true) %}
      {% if messages %}
        {% for category, message in messages %}
          <div class="alert alert-{{ category }}">{{ message }}</div>
        {% endfor %}
      {% endif %}
    {% endwith %}

    <h2>Upload Files</h2>
    <div class="upload-form">
        <form action="/upload" method="POST" enctype="multipart/form-data" id="uploadForm">
            <div class="form-group">
                <label for="file">Select Files:</label>
                <input type="file" name="file" id="file" multiple required>
            </div>
            <p>Files will be uploaded with their original filenames. If a file with the same name already exists, the upload will be rejected.</p>
            <button type="submit" class="btn btn-success">Upload</button>
        </form>
    </div>

    <p><a href="/">Back to File List</a></p>
</body>
</html>
"""


def create_app() -> Flask:
    app = Flask(__name__)
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

        return render_template_string(
            INDEX_TEMPLATE,
            files=formatted_files,
            stats=stats_formatted,
        )

    @app.route("/upload", methods=["GET", "POST"])
    def upload_file():
        if request.method == "GET":
            return render_template_string(UPLOAD_TEMPLATE)

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