from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager

# Initialize instances (but don't bind to app yet)
db = SQLAlchemy()
login_manager = LoginManager()