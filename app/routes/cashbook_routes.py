from flask import Blueprint, render_template, request, jsonify, redirect, url_for, flash
from flask_login import login_required, current_user
from app.extensions import db
from app.models import Sale, TallyBill, CashBookEntry
from datetime import datetime, timedelta
from sqlalchemy import func

cashbook_bp = Blueprint('cashbook', __name__)


def _ist_today():
    """Return today's date in IST (UTC+5:30)."""
    return (datetime.utcnow() + timedelta(hours=5, minutes=30)).date()


def _get_cashbook_data(date_obj):
    """
    Build a complete cashbook snapshot for the given date.
    Returns a dict with:
      - retail_entries : list of cash/online retail sale records
      - tally_entries  : list of cash/online tally invoice records
      - expense_entries: list of manual expense entries
      - totals         : aggregated summary dict
    """
    # ── 1. Retail sales (not credit) on this date ──────────────────────────
    retail_sales = Sale.query.filter(
        func.date(Sale.date) == date_obj,
        Sale.payment_status.in_(['Payment Received', 'Partial Payment'])
    ).order_by(Sale.date.asc(), Sale.id.asc()).all()

    retail_entries = []
    for s in retail_sales:
        cash_in   = s.paid_cash   or 0.0
        online_in = s.paid_online or 0.0
        if cash_in <= 0 and online_in <= 0:
            continue  # pure credit — skip
        retail_entries.append({
            'id'          : s.id,
            'time'        : (s.date + timedelta(hours=5, minutes=30)).strftime('%I:%M %p'),
            'client'      : s.client_name or '—',
            'description' : ', '.join(
                              [f"{i.product_name} ×{i.qty_sold}" for i in s.items]
                          ) if s.items else 'Retail Bill',
            'cash_in'     : round(cash_in,   2),
            'online_in'   : round(online_in, 2),
            'type'        : 'retail',
        })

    # ── 2. Tally (GST) invoice sales (not credit) on this date ────────────
    tally_bills = TallyBill.query.filter(
        func.date(TallyBill.date) == date_obj,
        TallyBill.payment_status.in_(['Payment Received', 'Partial Payment'])
    ).order_by(TallyBill.date.asc(), TallyBill.id.asc()).all()

    tally_entries = []
    for t in tally_bills:
        cash_in   = t.paid_cash   or 0.0
        online_in = t.paid_online or 0.0
        if cash_in <= 0 and online_in <= 0:
            continue  # pure credit — skip
        tally_entries.append({
            'id'          : t.id,
            'invoice'     : t.invoice_number or f'#{t.id}',
            'time'        : (t.date + timedelta(hours=5, minutes=30)).strftime('%I:%M %p'),
            'client'      : t.client_name or '—',
            'description' : ', '.join(
                              [f"{i.product_name} ×{i.qty}" for i in t.items]
                          ) if t.items else 'Tally Invoice',
            'cash_in'     : round(cash_in,   2),
            'online_in'   : round(online_in, 2),
            'type'        : 'tally',
        })

    # ── 3. Manual expense entries ──────────────────────────────────────────
    raw_expenses = CashBookEntry.query.filter(
        CashBookEntry.date == date_obj
    ).order_by(CashBookEntry.created_at.asc()).all()

    expense_entries = []
    for e in raw_expenses:
        is_income = (e.amount < 0)
        expense_entries.append({
            'id'         : e.id,
            'time'       : e.created_at.strftime('%I:%M %p') if e.created_at else '—',
            'description': e.description,
            'amount'     : round(abs(e.amount), 2),
            'mode'       : e.payment_mode,  # 'Cash' | 'Online'
            'type'       : 'Income' if is_income else 'Expense'
        })

    # ── 4. Totals ──────────────────────────────────────────────────────────
    total_retail_cash   = sum(r['cash_in']   for r in retail_entries)
    total_retail_online = sum(r['online_in'] for r in retail_entries)
    total_tally_cash    = sum(t['cash_in']   for t in tally_entries)
    total_tally_online  = sum(t['online_in'] for t in tally_entries)
    
    total_expense_cash  = sum(e['amount'] for e in expense_entries if e['mode'] == 'Cash' and e['type'] == 'Expense')
    total_expense_online= sum(e['amount'] for e in expense_entries if e['mode'] == 'Online' and e['type'] == 'Expense')
    total_income_cash   = sum(e['amount'] for e in expense_entries if e['mode'] == 'Cash' and e['type'] == 'Income')
    total_income_online = sum(e['amount'] for e in expense_entries if e['mode'] == 'Online' and e['type'] == 'Income')

    gross_cash   = total_retail_cash   + total_tally_cash + total_income_cash
    gross_online = total_retail_online + total_tally_online + total_income_online
    net_cash     = gross_cash   - total_expense_cash
    net_online   = gross_online - total_expense_online

    totals = {
        'retail_cash'    : round(total_retail_cash,    2),
        'retail_online'  : round(total_retail_online,  2),
        'retail_total'   : round(total_retail_cash + total_retail_online, 2),
        'tally_cash'     : round(total_tally_cash,     2),
        'tally_online'   : round(total_tally_online,   2),
        'tally_total'    : round(total_tally_cash + total_tally_online, 2),
        'expense_cash'   : round(total_expense_cash,   2),
        'expense_online' : round(total_expense_online, 2),
        'expense_total'  : round(total_expense_cash + total_expense_online, 2),
        'income_cash'    : round(total_income_cash,    2),
        'income_online'  : round(total_income_online,  2),
        'income_total'   : round(total_income_cash + total_income_online, 2),
        'gross_cash'     : round(gross_cash,           2),
        'gross_online'   : round(gross_online,         2),
        'net_cash'       : round(net_cash,             2),
        'net_online'     : round(net_online,           2),
        'net_total'      : round(net_cash + net_online, 2),
    }

    return retail_entries, tally_entries, expense_entries, totals


# ─────────────────────────────────────────────────────────────────────────────
#  ROUTES
# ─────────────────────────────────────────────────────────────────────────────

@cashbook_bp.route('/cashbook')
@login_required
def cashbook_page():
    """Renders the cash book standalone window."""
    if current_user.role not in ['admin', 'sales']:
        flash("Access Denied: Cash Book is for Sales/Admin only.", "danger")
        return redirect(url_for('inventory.dashboard'))

    date_str = request.args.get('date', _ist_today().strftime('%Y-%m-%d'))
    try:
        date_obj = datetime.strptime(date_str, '%Y-%m-%d').date()
    except ValueError:
        date_obj = _ist_today()

    from datetime import timedelta as td

    retail, tally, expenses, totals = _get_cashbook_data(date_obj)

    return render_template(
        'cashbook.html',
        date_obj        = date_obj,
        date_str        = date_obj.strftime('%Y-%m-%d'),
        display_date    = date_obj.strftime('%d %B %Y'),
        today_date      = _ist_today().strftime('%Y-%m-%d'),
        timedelta_days  = td(days=1),
        retail_entries  = retail,
        tally_entries   = tally,
        expense_entries = expenses,
        totals          = totals,
    )


@cashbook_bp.route('/api/cashbook_data')
@login_required
def cashbook_data_api():
    """JSON API — returns full cashbook snapshot for a given date."""
    date_str = request.args.get('date', _ist_today().strftime('%Y-%m-%d'))
    try:
        date_obj = datetime.strptime(date_str, '%Y-%m-%d').date()
    except ValueError:
        return jsonify({'error': 'Invalid date'}), 400

    retail, tally, expenses, totals = _get_cashbook_data(date_obj)
    return jsonify({
        'date'           : date_str,
        'retail_entries' : retail,
        'tally_entries'  : tally,
        'expense_entries': expenses,
        'totals'         : totals,
    })


@cashbook_bp.route('/cashbook/add_expense', methods=['POST'])
@login_required
def add_expense():
    """Add a manual expense/debit or income/credit entry to the cash book."""
    if current_user.role not in ['admin', 'sales']:
        return jsonify({'error': 'Access denied'}), 403

    data = request.get_json()
    date_str    = data.get('date', _ist_today().strftime('%Y-%m-%d'))
    description = (data.get('description') or '').strip()
    amount      = float(data.get('amount') or 0)
    mode        = data.get('mode', 'Cash')  # 'Cash' | 'Online'
    entry_type  = data.get('type', 'Expense') # 'Expense' | 'Income'

    if not description or amount <= 0:
        return jsonify({'error': 'Description and a positive amount are required.'}), 400

    try:
        date_obj = datetime.strptime(date_str, '%Y-%m-%d').date()
    except ValueError:
        return jsonify({'error': 'Invalid date'}), 400

    # Store Income as a negative amount
    db_amount = amount if entry_type == 'Expense' else -amount

    entry = CashBookEntry(
        date        = date_obj,
        description = description,
        amount      = db_amount,
        payment_mode= mode,
        created_by  = current_user.username,
    )
    db.session.add(entry)
    db.session.commit()
    return jsonify({'success': True, 'id': entry.id})


@cashbook_bp.route('/cashbook/delete_expense/<int:entry_id>', methods=['POST'])
@login_required
def delete_expense(entry_id):
    """Delete a manual expense entry (admin only)."""
    if current_user.role != 'admin':
        return jsonify({'error': 'Only admins can delete entries.'}), 403

    entry = db.session.get(CashBookEntry, entry_id)
    if not entry:
        return jsonify({'error': 'Entry not found'}), 404

    db.session.delete(entry)
    db.session.commit()
    return jsonify({'success': True})
