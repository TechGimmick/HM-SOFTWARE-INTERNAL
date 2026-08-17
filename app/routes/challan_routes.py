from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify
from flask_login import login_required, current_user
from app.extensions import db
from app.models import Challan, ChallanItem, Product, Customer
from app.activity_service import log_activity
from sqlalchemy import func
import json
from datetime import datetime, timedelta
from collections import defaultdict

challan_bp = Blueprint('challan', __name__)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _next_challan_number():
    """Generate the next challan number e.g. CH-0001."""
    last = db.session.query(Challan).order_by(Challan.id.desc()).first()
    if last:
        # Extract numeric part from last challan_number (e.g. 'CH-0042' → 42)
        try:
            n = int(last.challan_number.split('-')[1]) + 1
        except (IndexError, ValueError):
            n = last.id + 1
    else:
        n = 1
    return f'CH-{n:04d}'


def _get_all_products_json():
    """Return all products as a JSON-serialisable list for the search autocomplete."""
    products = db.session.query(
        Product.id, Product.name, Product.unit,
        Product.has_subcategory, Product.subcategory_type, Product.subcategory_options
    ).all()
    result = []
    for p in products:
        result.append({
            'id': p.id,
            'name': p.name,
            'unit': p.unit or '',
            'has_sub': p.has_subcategory,
            'sub_type': p.subcategory_type,
            'sub_opts': p.subcategory_options.split(',') if p.subcategory_options else []
        })
    return json.dumps(result)


# ── Routes ─────────────────────────────────────────────────────────────────────

@challan_bp.route('/challan')
@login_required
def challan_book():
    """Challan Book list page."""
    if current_user.role not in ['admin', 'sales']:
        flash('Access Denied: Challan Book is for Sales Staff only.', 'danger')
        return redirect(url_for('inventory.dashboard'))

    # Filters
    filter_status = request.args.get('filter_status', 'all')
    filter_date_str = request.args.get('filter_date', '')
    filter_customer = request.args.get('filter_customer', '')

    query = Challan.query

    if filter_customer:
        query = query.filter(Challan.customer_name.ilike(f'%{filter_customer}%'))

    if filter_date_str:
        try:
            filter_date = datetime.strptime(filter_date_str, '%Y-%m-%d').date()
            query = query.filter(func.date(Challan.date) == filter_date)
        except ValueError:
            flash('Invalid date format.', 'danger')

    if filter_status and filter_status != 'all':
        query = query.filter(Challan.status == filter_status)

    challans = query.order_by(Challan.date.desc(), Challan.id.desc()).all()

    # Group by IST date
    challans_by_date = defaultdict(list)
    for c in challans:
        date_key = c.date_ist.strftime('%d %b %Y') if c.date_ist else 'Unknown Date'
        challans_by_date[date_key].append(c)

    all_products_json = _get_all_products_json()

    # Customer list for autocomplete in create modal
    customers = Customer.query.with_entities(Customer.name, Customer.phone).order_by(Customer.name).all()
    customer_list = []
    for c in customers:
        if c.phone:
            customer_list.append({'name': c.name, 'phone': c.phone, 'display': f'{c.name} - {c.phone}'})
        else:
            customer_list.append({'name': c.name, 'phone': '', 'display': c.name})

    return render_template(
        'challan_book.html',
        challans_by_date=dict(challans_by_date),
        all_products_json=all_products_json,
        customer_list=customer_list,
        current_filter_status=filter_status,
        current_filter_date=filter_date_str,
        current_filter_customer=filter_customer,
    )


@challan_bp.route('/challan/new', methods=['POST'])
@login_required
def new_challan():
    """Save a newly created challan."""
    if current_user.role not in ['admin', 'sales']:
        flash('Access Denied.', 'danger')
        return redirect(url_for('inventory.dashboard'))

    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'

    try:
        customer_name = (request.form.get('customer_name') or '').strip()
        customer_phone = (request.form.get('customer_phone') or '').strip()
        customer_address = (request.form.get('customer_address') or '').strip()
        dispatched_by = (request.form.get('dispatched_by') or '').strip()
        notes = (request.form.get('notes') or '').strip()
        challan_date_str = request.form.get('challan_date') or ''
        cart_json = request.form.get('challan_cart', '[]')

        if not customer_name:
            raise ValueError('Customer name is required.')

        cart_items = json.loads(cart_json)
        if not cart_items:
            raise ValueError('Please add at least one product to the challan.')

        # Parse date (IST → UTC)
        challan_date_utc = datetime.utcnow()
        if challan_date_str:
            try:
                local_dt = datetime.strptime(challan_date_str, '%Y-%m-%d')
                challan_date_utc = local_dt - timedelta(hours=5, minutes=30)
            except ValueError:
                pass

        challan_number = _next_challan_number()

        new_ch = Challan(
            challan_number=challan_number,
            date=challan_date_utc,
            customer_name=customer_name,
            customer_phone=customer_phone or None,
            customer_address=customer_address or None,
            dispatched_by=dispatched_by or None,
            notes=notes or None,
            status='Open',
            created_by_id=current_user.id,
        )
        db.session.add(new_ch)
        db.session.flush()  # get new_ch.id

        for item in cart_items:
            qty = int(item.get('qty', 0))
            if qty <= 0:
                continue
            
            # Parse optional price
            price_str = item.get('price', '')
            price_val = None
            if price_str:
                try:
                    price_val = float(price_str)
                except ValueError:
                    pass

            db.session.add(ChallanItem(
                challan_id=new_ch.id,
                product_name=item['name'],
                qty=qty,
                unit=item.get('unit', '') or None,
                price=price_val,
            ))

        log_activity('CREATE', 'Challan',
                     f'Challan {challan_number} created for {customer_name}',
                     ref_id=new_ch.id, ref_type='Challan')
        db.session.commit()

        success_msg = f'Challan {challan_number} created successfully.'
        flash(success_msg, 'success')
        if is_ajax:
            return jsonify({'success': True, 'redirect': url_for('challan.challan_book'), 'message': success_msg})
        return redirect(url_for('challan.challan_book'))

    except Exception as e:
        db.session.rollback()
        err_msg = f'Error creating challan: {e}'
        flash(err_msg, 'danger')
        if is_ajax:
            return jsonify({'success': False, 'error': err_msg}), 400
        return redirect(url_for('challan.challan_book'))


@challan_bp.route('/challan/<int:challan_id>/status', methods=['POST'])
@login_required
def update_challan_status(challan_id):
    """Update the status of a challan."""
    if current_user.role not in ['admin', 'sales']:
        return jsonify({'success': False, 'error': 'Access Denied.'}), 403

    challan = db.session.get(Challan, challan_id)
    if not challan:
        return jsonify({'success': False, 'error': 'Challan not found.'}), 404

    new_status = request.form.get('new_status') or request.json and request.json.get('new_status')
    if not new_status:
        # Try JSON body
        try:
            data = request.get_json(force=True) or {}
            new_status = data.get('new_status', '')
        except Exception:
            new_status = ''

    valid_statuses = ['Open', 'Accepted', 'Withdrawn', 'Bill Created']
    if new_status not in valid_statuses:
        return jsonify({'success': False, 'error': f'Invalid status: {new_status}'}), 400

    old_status = challan.status
    challan.status = new_status
    log_activity('UPDATE', 'Challan',
                 f'Challan {challan.challan_number}: {old_status} → {new_status}',
                 ref_id=challan.id, ref_type='Challan')
    db.session.commit()
    return jsonify({'success': True, 'new_status': new_status})


@challan_bp.route('/challan/<int:challan_id>/delete', methods=['POST'])
@login_required
def delete_challan(challan_id):
    """Delete a challan and all its items."""
    if current_user.role not in ['admin', 'sales']:
        flash('Access Denied.', 'danger')
        return redirect(url_for('challan.challan_book'))

    challan = db.session.get(Challan, challan_id)
    if not challan:
        flash('Challan not found.', 'danger')
        return redirect(url_for('challan.challan_book'))

    ch_num = challan.challan_number
    log_activity('DELETE', 'Challan',
                 f'Deleted challan {ch_num} ({challan.customer_name})',
                 ref_id=challan_id, ref_type='Challan')
    db.session.delete(challan)
    db.session.commit()
    flash(f'Challan {ch_num} deleted.', 'success')
    return redirect(url_for('challan.challan_book'))


@challan_bp.route('/challan/<int:challan_id>/print')
@login_required
def print_challan(challan_id):
    """Printable challan view."""
    if current_user.role not in ['admin', 'sales']:
        flash('Access Denied.', 'danger')
        return redirect(url_for('inventory.dashboard'))

    challan = db.session.get(Challan, challan_id)
    if not challan:
        flash('Challan not found.', 'danger')
        return redirect(url_for('challan.challan_book'))

    return render_template('challan_print.html', challan=challan)
