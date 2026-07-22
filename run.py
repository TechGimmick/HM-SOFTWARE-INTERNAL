import os

# Load all secrets from .env (never committed to git)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv not installed — install with: pip install python-dotenv


from app import create_app

app = create_app()

if __name__ == "__main__":
    # reloader_type='stat' avoids the Python 3.13 + watchdog threading incompatibility
    app.run(host='0.0.0.0', port=5000, debug=True, reloader_type='stat')
