"""
activity_service.py
-------------------
Lightweight helper for recording user activity across all modules.

Usage (anywhere inside a request context):
    from app.activity_service import log_activity
    log_activity('CREATE', 'Sales', f'New sale #{sale.id} for {client}', ref_id=sale.id, ref_type='Sale')
"""

import json
from app.extensions import db
from app.models import ActivityLog


def log_activity(action: str, module: str, description: str,
                 ref_id: int = None, ref_type: str = None, extra: dict = None):
    """
    Record one activity log entry for the current logged-in user.

    Parameters
    ----------
    action      : str  — e.g. 'CREATE', 'UPDATE', 'DELETE', 'PAYMENT', 'LOGIN', 'LOGOUT'
    module      : str  — e.g. 'Sales', 'Purchase', 'Inventory', 'CashBook', 'Auth'
    description : str  — human-readable summary shown in the sidebar
    ref_id      : int  — primary key of the affected record (optional)
    ref_type    : str  — model name of the affected record (optional)
    extra       : dict — additional JSON detail (optional)
    """
    try:
        from flask_login import current_user
        uid = None
        uname = 'System'
        if current_user and current_user.is_authenticated:
            uid = current_user.id
            uname = current_user.username

        entry = ActivityLog(
            user_id=uid,
            username=uname,
            action=action.upper(),
            module=module,
            description=description[:500],
            ref_id=ref_id,
            ref_type=ref_type,
            extra=json.dumps(extra) if extra else None,
        )
        db.session.add(entry)
        db.session.flush()   # write to DB within current transaction, no separate commit needed
    except Exception as e:
        # Never let audit logging crash the main request
        print(f"[ActivityLog] Failed to write log: {e}")
