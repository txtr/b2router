from __future__ import annotations

from flask import Flask, render_template_string

from b2router.services import account_pool


HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>B2 Router - File Cache</title>
    <style>
        body { font-family: system-ui, sans-serif; margin: 2rem; }
        h1 { color: #333; }
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
    </style>
</head>
<body>
    <h1>B2 Router - File Cache</h1>
    <p>Centralized view of files across all B2 accounts</p>

    <div class="stats">
        <div class="stat">Unique Files: <span class="stat-value">{{ stats.unique_files }}</span></div>
        <div class="stat">Total Size: <span class="stat-value">{{ stats.total_size_mb }} MB</span></div>
        <div class="stat">Total References: <span class="stat-value">{{ stats.total_account_references }}</span></div>
        <div class="stat">Accounts with Files: <span class="stat-value">{{ stats.accounts_with_files }}</span></div>
    </div>

    {% if files %}
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


def create_app() -> Flask:
    app = Flask(__name__)

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
            HTML_TEMPLATE,
            files=formatted_files,
            stats=stats_formatted,
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