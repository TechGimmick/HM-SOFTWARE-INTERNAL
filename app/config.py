import os

class Config:
    # MUST be set as env var in production — the default here is a placeholder only
    SECRET_KEY = os.environ.get('SECRET_KEY', 'dev-only-change-in-production')
    
    # Database URL Logic (Handling the Port 6543 vs 5432 issue)
    SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URL', 'postgresql://localhost/papa_website')
    # Fix: SQLAlchemy 2.x needs 'postgresql://' not 'postgres://'
    if SQLALCHEMY_DATABASE_URI and SQLALCHEMY_DATABASE_URI.startswith('postgres://'):
        SQLALCHEMY_DATABASE_URI = SQLALCHEMY_DATABASE_URI.replace('postgres://', 'postgresql://', 1)
    # Fix: Supabase uses port 6543 (pgBouncer) but SQLAlchemy needs 5432 (direct).
    # Only replace the port portion, not any part of the password or hostname.
    if SQLALCHEMY_DATABASE_URI and ':6543/' in SQLALCHEMY_DATABASE_URI:
        SQLALCHEMY_DATABASE_URI = SQLALCHEMY_DATABASE_URI.replace(':6543/', ':5432/')
    
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # Tuned for EC2 t2.micro + 2 Gunicorn workers:
    # 3 pool + 5 overflow = 8 max connections per worker × 2 workers = 16 total
    SQLALCHEMY_ENGINE_OPTIONS = {
        'pool_size': 3,
        'max_overflow': 5,
        'pool_timeout': 30,
        'pool_recycle': 1800,
        'pool_pre_ping': True,   # Drop stale connections automatically
    }