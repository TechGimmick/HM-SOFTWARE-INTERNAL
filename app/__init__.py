from flask import Flask
from app.config import Config
from app.extensions import db, login_manager
import os
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
    login_manager.login_view = 'auth.login' # Note the 'auth.' prefix
    
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
     
    app.register_blueprint(purchase_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(sales_bp)
    app.register_blueprint(inventory_bp)
    app.register_blueprint(pdf_bp)
    app.register_blueprint(tally_bp)
    app.register_blueprint(cashbook_bp)
    
    with app.app_context():
        db.create_all()
        _ensure_sales_payment_date_column()
        _ensure_sale_payments_table()
    
    return app