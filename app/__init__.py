from flask import Flask
from app.config import Config
from app.extensions import db, login_manager
import os
import datetime
from sqlalchemy import inspect, text




def _ensure_sales_payment_date_column():
    inspector = inspect(db.engine)
    if not inspector.has_table('sales'):
        return

    existing_columns = {col['name'] for col in inspector.get_columns('sales')}
    if 'payment_date' not in existing_columns:
        with db.engine.begin() as conn:
            conn.execute(text("ALTER TABLE sales ADD COLUMN payment_date TIMESTAMP NULL"))

def _ensure_sale_payments_table():
    inspector = inspect(db.engine)
    if not inspector.has_table('sale_payments'):
        with db.engine.begin() as conn:
            conn.execute(text("""
                CREATE TABLE sale_payments (
                    id SERIAL PRIMARY KEY,
                    sale_id INTEGER NOT NULL REFERENCES sales(id) ON DELETE CASCADE,
                    amount_cash DOUBLE PRECISION DEFAULT 0.0,
                    amount_online DOUBLE PRECISION DEFAULT 0.0,
                    payment_date TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP
                )
            """))


def _ensure_activity_log_table():
    """Idempotent: creates activity_logs table if it doesn't exist yet.
    Works on both fresh installs and existing Postgres deployments."""
    inspector = inspect(db.engine)
    if not inspector.has_table('activity_logs'):
        with db.engine.begin() as conn:
            conn.execute(text("""
                CREATE TABLE activity_logs (
                    id          SERIAL PRIMARY KEY,
                    timestamp   TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                    user_id     INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    username    VARCHAR(50),
                    action      VARCHAR(50)  NOT NULL,
                    module      VARCHAR(50)  NOT NULL,
                    description VARCHAR(500) NOT NULL,
                    ref_id      INTEGER,
                    ref_type    VARCHAR(50),
                    extra       TEXT
                )
            """))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_activity_logs_timestamp ON activity_logs (timestamp DESC)"
            ))

def _auto_cleanup_old_logs(app):
    """
    Silently prune logs older than 90 days on startup.
    Only runs if the table has grown past 5 000 rows — avoids touching
    the DB on every restart of a small/dev instance.
    Industry standard: combine with a weekly scheduled task for reliability.
    """
    try:
        inspector = inspect(db.engine)
        if not inspector.has_table('activity_logs'):
            return
        from app.models import ActivityLog
        row_count = db.session.query(ActivityLog).count()
        if row_count > 5000:
            cutoff = datetime.datetime.utcnow() - datetime.timedelta(days=90)
            n = ActivityLog.query.filter(ActivityLog.timestamp < cutoff).delete()
            db.session.commit()
            if n:
                app.logger.info(f'[auto-cleanup] Pruned {n} activity log entries older than 90 days.')
    except Exception as exc:
        app.logger.warning(f'[auto-cleanup] Could not prune activity logs: {exc}')


def create_app():
    # Specify the template and static folders relative to the project root
    template_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'templates'))
    static_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'static'))
    
    app = Flask(__name__, template_folder=template_dir, static_folder=static_dir)
    app.config.from_object(Config)

    # 1. Initialize Extensions
    db.init_app(app)
    login_manager.init_app(app)

    # 2. Configure Login Manager
    login_manager.login_view = 'auth.login'

    # Inject now_date into every template (used by activity sidebar date picker)
    @app.context_processor
    def inject_globals():
        ist_now = datetime.datetime.utcnow() + datetime.timedelta(hours=5, minutes=30)
        return {'now_date': ist_now.strftime('%Y-%m-%d')}

    # Move the user_loader inside or import it to avoid circulars
    from app.models import User
    @login_manager.user_loader
    def load_user(user_id):
        return db.session.get(User, int(user_id))

    # 3. Register Blueprints (The Routes)
    from app.routes.auth_routes import auth_bp
    from app.routes.sales_routes import sales_bp
    from app.routes.inventory_routes import inventory_bp
    from app.routes.pdf_routes import pdf_bp
    from app.routes.purchase_routes import purchase_bp
    from app.routes.tally_routes import tally_bp
    from app.routes.cashbook_routes import cashbook_bp
    from app.routes.activity_routes import activity_bp
     
    app.register_blueprint(purchase_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(sales_bp)
    app.register_blueprint(inventory_bp)
    app.register_blueprint(pdf_bp)
    app.register_blueprint(tally_bp)
    app.register_blueprint(cashbook_bp)
    app.register_blueprint(activity_bp)
    with app.app_context():
        db.create_all()
        _ensure_sales_payment_date_column()
        _ensure_sale_payments_table()
        _ensure_activity_log_table()
        _auto_cleanup_old_logs(app)

    # Flask CLI command: `flask cleanup-logs`
    # Run this from a Windows Task Scheduler or cron job weekly.
    @app.cli.command('cleanup-logs')
    def cleanup_logs_cmd():
        """Delete activity log entries older than 90 days (run weekly via scheduler)."""
        from app.models import ActivityLog
        cutoff = datetime.datetime.utcnow() - datetime.timedelta(days=90)
        with app.app_context():
            n = ActivityLog.query.filter(ActivityLog.timestamp < cutoff).delete()
            db.session.commit()
        print(f'[cleanup-logs] Deleted {n} log entries older than 90 days (cutoff: {cutoff.date()}).')
    
    return app