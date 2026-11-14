"""Shared development and production launch settings for the web services."""

import os
from pathlib import Path

from dotenv import dotenv_values


def run_app(app, default_port=5000):
    """Use Waitress when SERVER=TRUE, otherwise use Flask's development server."""
    file_settings = dotenv_values(Path(__file__).parent / ".env")

    def setting(name, default):
        return os.environ.get(name, file_settings.get(name) or default)

    port = int(setting("PORT", str(default_port)))
    production = setting("SERVER", "FALSE") == "TRUE"
    if production:
        from waitress import serve

        app.debug = False
        serve(app, host="0.0.0.0", port=port, threads=8, channel_timeout=600)
    else:
        app.run(host="0.0.0.0", port=port, debug=True)
