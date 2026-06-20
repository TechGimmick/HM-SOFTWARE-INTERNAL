import os

# DATABASE_URL must be set as an environment variable on the server.
# NEVER hardcode credentials here — they will be exposed in git history.
os.environ['DATABASE_URL'] = 'postgresql://postgres.eaeihcittnyzbkmbgasr:Gohan%402635@aws-1-ap-south-1.pooler.supabase.com:6543/postgres'

from app import create_app

app = create_app()

if __name__ == "__main__":
    # reloader_type='stat' avoids the Python 3.13 + watchdog threading incompatibility
    # (SystemError on threading shutdown). Hot-reload still works normally.
    app.run(host='0.0.0.0', port=5000, debug=True, reloader_type='stat')
