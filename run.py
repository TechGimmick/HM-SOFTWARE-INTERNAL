import os

# DATABASE_URL must be set as an environment variable on the server.
# NEVER hardcode credentials here — they will be exposed in git history.
if not os.environ.get('DATABASE_URL'):
    raise RuntimeError(
        "DATABASE_URL environment variable is not set. "
        "Set it before starting the app (e.g. in your .env file or EC2 environment)."
    )

from app import create_app

app = create_app()

if __name__ == "__main__":
    # reloader_type='stat' avoids the Python 3.13 + watchdog threading incompatibility
    # (SystemError on threading shutdown). Hot-reload still works normally.
    app.run(debug=True, reloader_type='stat')
