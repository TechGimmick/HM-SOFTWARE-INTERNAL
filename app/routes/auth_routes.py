from flask import Blueprint, render_template, redirect, url_for, request, flash, session
from flask_login import login_user, logout_user, login_required, current_user
from app.extensions import db
from app.models import User
from app.activity_service import log_activity
from datetime import datetime

# Define Blueprint
auth_bp = Blueprint('auth', __name__)

@auth_bp.route("/")
def index():
    return redirect(url_for('auth.login'))  # Correct endpoint with 'auth.' prefix

@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('inventory.dashboard')) # Redirect to Dashboard in inventory

    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        user = User.query.filter_by(username=username).first()
        
        if user and user.check_password(password):
            login_user(user)
            session['role'] = user.role
            session['login_time'] = datetime.utcnow().isoformat()  # UTC ISO — used by activity log
            log_activity('LOGIN', 'Auth', f'{user.username} logged in', ref_type='User')
            db.session.commit()
            flash('Logged in successfully.', 'success')
            return redirect(url_for('inventory.dashboard'))
                
        flash('Invalid username or password', 'danger')
    return render_template('login.html')

@auth_bp.route('/logout')
@login_required
def logout():
    username_snapshot = current_user.username   # capture before logout_user() clears identity
    log_activity('LOGOUT', 'Auth', f'{username_snapshot} logged out', ref_type='User')
    db.session.commit()                          # single atomic commit
    session.pop('role', None)  # Clear role on logout
    session.pop('login_time', None)   # clear session start marker
    logout_user()
    flash('You have been logged out.', 'info')
    return redirect(url_for('auth.login'))