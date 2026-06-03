"""
activity_routes.py
------------------
REST API endpoints for the Activity Log sidebar.

  GET  /api/activity-log            — paginated/filtered log feed
  GET  /api/activity-log/count      — unread count since session login
  POST /api/activity-log/cleanup    — admin-only: purge logs older than N days
"""

from flask import Blueprint, jsonify, request, session
from flask_login import login_required, current_user
from app.extensions import db
from app.models import ActivityLog
import datetime

activity_bp = Blueprint('activity', __name__)

# ── Industry standard: how long we keep logs in the hot table ──────────────
LOG_RETENTION_DAYS = 90


# ─────────────────────────────────────────────────────────────────────────────
#  MAIN FEED
# ─────────────────────────────────────────────────────────────────────────────
@activity_bp.route('/api/activity-log')
@login_required
def get_activity_log():
    """
    Returns filtered activity log entries as JSON.

    Query params:
      since   ISO-8601 UTC string  — only return entries after this time
                                     (defaults to session['login_time'])
      date    YYYY-MM-DD           — filter to a specific calendar day (IST)
      limit   int (max 200)        — number of entries (default 60)
    """
    ist_offset  = datetime.timedelta(hours=5, minutes=30)
    now_utc     = datetime.datetime.utcnow()
    limit       = min(int(request.args.get('limit', 60)), 200)

    # ── Time boundary ──────────────────────────────────────────────────────
    # Priority: ?date param > ?since param > session login_time > no filter
    date_str  = request.args.get('date', '').strip()   # YYYY-MM-DD (IST)
    since_str = request.args.get('since', '').strip()  # ISO UTC

    since_dt = None
    until_dt = None

    if date_str:
        # User picked a specific calendar date in IST — convert to UTC range
        try:
            day_ist   = datetime.datetime.strptime(date_str, '%Y-%m-%d')
            since_dt  = day_ist - ist_offset                          # IST midnight → UTC
            until_dt  = since_dt + datetime.timedelta(days=1)
        except ValueError:
            pass
    elif since_str:
        try:
            since_dt = datetime.datetime.fromisoformat(since_str)
        except ValueError:
            pass
    else:
        # Default: show logs since this session started
        login_time_str = session.get('login_time')
        if login_time_str:
            try:
                since_dt = datetime.datetime.fromisoformat(login_time_str)
            except ValueError:
                pass

    # ── Build query ────────────────────────────────────────────────────────
    query = ActivityLog.query

    # Role-based scope
    if current_user.role != 'admin':
        query = query.filter(ActivityLog.user_id == current_user.id)

    # Time filters
    if since_dt:
        query = query.filter(ActivityLog.timestamp >= since_dt)
    if until_dt:
        query = query.filter(ActivityLog.timestamp < until_dt)

    entries = query.order_by(ActivityLog.timestamp.desc()).limit(limit).all()

    # ── Serialize ──────────────────────────────────────────────────────────
    results = []
    for e in entries:
        ts_ist = (e.timestamp + ist_offset) if e.timestamp else (now_utc + ist_offset)
        diff   = now_utc - e.timestamp if e.timestamp else datetime.timedelta(0)

        if diff.total_seconds() < 60:
            rel = 'just now'
        elif diff.total_seconds() < 3600:
            rel = f"{int(diff.total_seconds() // 60)}m ago"
        elif diff.total_seconds() < 86400:
            rel = f"{int(diff.total_seconds() // 3600)}h ago"
        else:
            rel = ts_ist.strftime('%d %b')

        results.append({
            'id':          e.id,
            'timestamp':   ts_ist.strftime('%d %b %Y, %I:%M %p'),
            'rel_time':    rel,
            'username':    e.username or 'Unknown',
            'action':      e.action,
            'module':      e.module,
            'description': e.description,
            'ref_id':      e.ref_id,
            'ref_type':    e.ref_type,
        })

    return jsonify({
        'logs':     results,
        'total':    len(results),
        'is_admin': current_user.role == 'admin',
        'filtered': bool(since_dt or until_dt),
    })


# ─────────────────────────────────────────────────────────────────────────────
#  BADGE COUNT  — entries since THIS session started (not since last hour)
# ─────────────────────────────────────────────────────────────────────────────
@activity_bp.route('/api/activity-log/count')
@login_required
def get_activity_count():
    """Count of entries since the user logged in (used for badge)."""
    login_time_str = session.get('login_time')
    try:
        since_dt = datetime.datetime.fromisoformat(login_time_str) if login_time_str else \
                   datetime.datetime.utcnow() - datetime.timedelta(hours=1)
    except (ValueError, TypeError):
        since_dt = datetime.datetime.utcnow() - datetime.timedelta(hours=1)

    query = ActivityLog.query.filter(ActivityLog.timestamp >= since_dt)

    if current_user.role != 'admin':
        query = query.filter(ActivityLog.user_id == current_user.id)

    return jsonify({'count': query.count()})


# ─────────────────────────────────────────────────────────────────────────────
#  RETENTION MANAGEMENT  (industry standard: keep hot table lean)
# ─────────────────────────────────────────────────────────────────────────────
@activity_bp.route('/api/activity-log/cleanup', methods=['POST'])
@login_required
def cleanup_old_logs():
    """
    Admin-only endpoint. Deletes log entries older than LOG_RETENTION_DAYS.
    Industry standard: called by a scheduled task (cron / APScheduler) weekly,
    or manually by admin. Old logs should be archived to cold storage first
    in a real production system.
    """
    if current_user.role != 'admin':
        return jsonify({'error': 'Admin only'}), 403

    days = int(request.get_json(silent=True, force=True).get('days', LOG_RETENTION_DAYS))
    cutoff = datetime.datetime.utcnow() - datetime.timedelta(days=days)

    deleted = ActivityLog.query.filter(ActivityLog.timestamp < cutoff).delete()
    db.session.commit()

    return jsonify({
        'deleted': deleted,
        'cutoff':  cutoff.strftime('%d %b %Y'),
        'message': f'Removed {deleted} entries older than {days} days.'
    })
