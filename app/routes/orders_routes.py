"""
orders_routes.py
----------------
Blueprint for the Orders module.

Retail Orders  — auto-created when a sales bill is saved.
  Status flow : Pending → Packaging → Done → Sent

Supplier Orders — admin places order, workers pack, admin dispatches.
  Status flow : Draft → Packing → Packed → Dispatched
"""

import os
import datetime
from flask import (Blueprint, render_template, request, redirect, url_for,
                   flash, jsonify, current_app)
from flask_login import login_required, current_user
from app.extensions import db
from app.models import RetailOrder, RetailOrderItem, SupplierOrder, SupplierOrderItem, Product, Supplier, Sale
from app.activity_service import log_activity
from sqlalchemy import func

orders_bp = Blueprint('orders', __name__, url_prefix='/orders')

# ── Helpers ───────────────────────────────────────────────────────────────────

RETAIL_STATUS_FLOW   = ['Pending', 'Packaging', 'Done', 'SentToSalesGuy', 'SentToCustomer']
SUPPLIER_STATUS_FLOW = ['Draft', 'Packing', 'Packed', 'Dispatched', 'Received']

UPLOAD_SUBDIR        = os.path.join('uploads', 'dispatch')    # relative to static/
INVOICE_UPLOAD_SUBDIR = os.path.join('uploads', 'invoices')   # relative to static/
ALLOWED_IMAGE_EXT = {'png', 'jpg', 'jpeg', 'webp', 'gif'}
ALLOWED_VIDEO_EXT = {'mp4', 'mov', 'avi', 'mkv', 'webm'}
ALLOWED_INVOICE_EXT = {'png', 'jpg', 'jpeg', 'webp', 'gif', 'pdf'}


def _allowed_file(filename, allowed):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in allowed


def _save_upload(file, allowed_exts, prefix='file', subdir=None):
    """Save an uploaded file to static/uploads/<subdir>/ and return the relative path."""
    if not file or not file.filename:
        return None
    if not _allowed_file(file.filename, allowed_exts):
        return None
    ext = file.filename.rsplit('.', 1)[1].lower()
    ts = datetime.datetime.utcnow().strftime('%Y%m%d%H%M%S%f')
    safe_name = f"{prefix}_{ts}.{ext}"
    folder = subdir if subdir else UPLOAD_SUBDIR
    upload_dir = os.path.join(current_app.static_folder, folder)
    os.makedirs(upload_dir, exist_ok=True)
    file.save(os.path.join(upload_dir, safe_name))
    return os.path.join(folder, safe_name).replace('\\', '/')   # always forward-slash


def _ist_now():
    return datetime.datetime.utcnow() + datetime.timedelta(hours=5, minutes=30)


# ══════════════════════════════════════════════════════════════════════════════
#  RETAIL ORDERS — WORKER QUEUE
# ══════════════════════════════════════════════════════════════════════════════

@orders_bp.route('/retail/queue')
@login_required
def retail_queue():
    """
    Live worker queue page — auto-refreshes every 8 seconds via JSON poll.
    Shows ONLY: order number, item names, quantities.
    No prices, no amounts, no customer phone, nothing financial.
    """
    return render_template('orders/retail_queue.html')


@orders_bp.route('/api/retail/queue_data')
def retail_queue_data():
    """
    JSON endpoint polled every 8 seconds by the queue page.
    Returns orders grouped by status column.
    Always forces a fresh read from the database — bypasses SQLAlchemy's
    session identity-map cache so status changes are always reflected.

    NOTE: @login_required is intentionally NOT used here because it issues
    a 302 HTML redirect when the session expires. fetch() follows that redirect,
    receives the login HTML, and res.json() silently throws — making the queue
    appear empty. Instead we check auth manually and return 401 JSON.
    """
    if not current_user.is_authenticated:
        return jsonify({'error': 'session_expired', 'message': 'Session expired. Please refresh the page.'}), 401

    ist_offset = datetime.timedelta(hours=5, minutes=30)

    # ── Force SQLAlchemy to discard any cached objects from this session ──────
    db.session.expire_all()

    # Only load warehouse-active orders (Pending / Packaging / Done).
    # SentToSalesGuy orders have left the warehouse — they live in the Sales Log.
    # SentToCustomer orders are deleted from the DB entirely.
    ACTIVE_COLS = ['Pending', 'Packaging', 'Done']
    orders = (
        RetailOrder.query
        .filter(RetailOrder.status.in_(ACTIVE_COLS))
        .execution_options(populate_existing=True)   # always re-load from DB
        .order_by(RetailOrder.created_at.asc())
        .all()
    )

    def _serialize(ro):
        return {
            'id': ro.id,
            'order_number': ro.order_number,
            'status': ro.status,
            'created_at': ((ro.created_at + ist_offset).strftime('%I:%M %p')
                           if ro.created_at else '—'),
            'items': [
                {'name': it.product_name, 'qty': it.qty, 'unit': it.unit or ''}
                for it in ro.items
            ],
        }

    grouped = {s: [] for s in ACTIVE_COLS}
    for ro in orders:
        grouped[ro.status].append(_serialize(ro))

    return jsonify({
        'columns': ACTIVE_COLS,
        'orders': grouped,
        'server_time': _ist_now().strftime('%I:%M:%S %p'),
    })


@orders_bp.route('/api/retail/<int:order_id>/status', methods=['POST'])
def retail_advance_status(order_id):
    """
    Advance a RetailOrder status by one step (warehouse-side).
    Role gates:
      Pending     → Packaging    : sales | admin  (salesperson clicks Start Packaging)
      Packaging   → Done         : any role       (worker marks Done on queue screen)
      Done        → SentToSalesGuy : sales | admin  (warehouse marks order dispatched to sales rep)

    SentToSalesGuy → SentToCustomer is handled by the separate
    /sent_to_customer endpoint, only callable from the Sales Log.

    NOTE: @login_required is intentionally NOT used here — same reason as
    retail_queue_data. Returns JSON 401 on unauthenticated requests instead
    of an HTML redirect that would break the AJAX caller.
    """
    # Manual auth check — avoid HTML redirect on AJAX calls
    if not current_user.is_authenticated:
        return jsonify({'error': 'session_expired', 'message': 'Session expired. Please refresh the page.'}), 401

    ro = db.session.get(RetailOrder, order_id)
    if not ro:
        return jsonify({'error': 'Order not found'}), 404

    # Expire the cached object so SQLAlchemy re-reads from the DB
    db.session.expire(ro)
    # Re-access attributes to trigger the fresh load
    _ = ro.status

    current_idx = RETAIL_STATUS_FLOW.index(ro.status) if ro.status in RETAIL_STATUS_FLOW else -1
    if current_idx == -1 or current_idx >= len(RETAIL_STATUS_FLOW) - 1:
        return jsonify({'error': 'Already at final status'}), 400

    next_status = RETAIL_STATUS_FLOW[current_idx + 1]

    # Role gate
    if next_status in ('Packaging', 'SentToSalesGuy') and current_user.role not in ('admin', 'sales', 'store'):
        return jsonify({'error': 'Only sales or store staff can perform this action'}), 403

    # SentToCustomer can ONLY be triggered from the Sales Log (separate endpoint).
    # The Retail Queue advances up to SentToSalesGuy only.
    if next_status == 'SentToCustomer':
        return jsonify({'error': 'Use the Sales Log to mark the order as Sent to Customer.'}), 403

    # ── When marking SentToSalesGuy: keep the order in the DB so the Sales Log
    # can still show it and let the sales rep mark it Sent to Customer.
    # The order is NOT deleted here.
    if next_status == 'SentToSalesGuy':
        ro.status = 'SentToSalesGuy'
        ro.updated_at = datetime.datetime.utcnow()
        # Also update the linked Sale's order_status so the summary badge reflects dispatch
        linked_sale = db.session.get(Sale, ro.sale_id)
        if linked_sale:
            linked_sale.order_status = 'Order Dispatched'
        log_activity('UPDATE', 'Orders',
                     f'Retail order {ro.order_number} → Sent to Sales Guy (dispatched from warehouse)',
                     ref_id=ro.id, ref_type='RetailOrder')
        db.session.commit()
        return jsonify({'success': True, 'new_status': 'SentToSalesGuy', 'order_id': ro.id, 'deleted': False})

    ro.status = next_status
    ro.updated_at = datetime.datetime.utcnow()
    log_activity('UPDATE', 'Orders',
                 f'Retail order {ro.order_number} → {next_status}',
                 ref_id=ro.id, ref_type='RetailOrder')
    db.session.commit()

    return jsonify({'success': True, 'new_status': next_status, 'order_id': ro.id})


@orders_bp.route('/api/retail/<int:order_id>/sent_to_customer', methods=['POST'])
def retail_mark_sent_to_customer(order_id):
    """
    Mark a RetailOrder as SentToCustomer — the final step triggered ONLY from
    the Sales Log page (sales rep confirms goods delivered to customer).

    This deletes the order from the DB to keep the queue table lean.
    The Sale record is unaffected — it lives in the 'sales' table.

    NOTE: @login_required is intentionally NOT used here — same reason as
    retail_queue_data. Returns JSON 401 on unauthenticated requests.
    """
    if not current_user.is_authenticated:
        return jsonify({'error': 'session_expired', 'message': 'Session expired. Please refresh the page.'}), 401

    if current_user.role not in ('admin', 'sales', 'store'):
        return jsonify({'error': 'Only sales or store staff can mark orders as Sent to Customer'}), 403

    ro = db.session.get(RetailOrder, order_id)
    if not ro:
        return jsonify({'error': 'Order not found'}), 404

    db.session.expire(ro)
    _ = ro.status  # trigger fresh load

    if ro.status != 'SentToSalesGuy':
        return jsonify({'error': f'Order must be in "Sent to Sales Guy" status to mark as Sent to Customer (current: {ro.status})'}), 400

    order_number = ro.order_number
    order_id_log = ro.id
    sale_id_log   = ro.sale_id
    # Update the linked Sale's order_status to 'Order Sent' BEFORE deleting the RetailOrder
    linked_sale = db.session.get(Sale, sale_id_log)
    if linked_sale:
        linked_sale.order_status = 'Order Sent'
    log_activity('UPDATE', 'Orders',
                 f'Retail order {order_number} → Sent to Customer (completed & removed)',
                 ref_id=order_id_log, ref_type='RetailOrder')
    db.session.delete(ro)
    db.session.commit()
    return jsonify({'success': True, 'new_status': 'SentToCustomer', 'order_id': order_id_log, 'deleted': True})


# ══════════════════════════════════════════════════════════════════════════════
#  ORDERS BOOK (bulk / outside supply orders — independent of retail sales)
# ══════════════════════════════════════════════════════════════════════════════

@orders_bp.route('/invoice_orders')
@login_required
def invoice_orders_list():
    """View all Orders Book entries and create new ones."""
    if current_user.role not in ('admin', 'sales', 'store', 'worker'):
        flash('Only authorized staff can access the Orders Book.', 'danger')
        return redirect(url_for('inventory.dashboard'))

    orders = SupplierOrder.query.order_by(SupplierOrder.created_at.desc()).all()
    suppliers = Supplier.query.order_by(Supplier.name).all()
    products  = Product.query.order_by(Product.name).all()

    return render_template('orders/invoice_orders_list.html',
                           orders=orders,
                           suppliers=suppliers,
                           products=products)


@orders_bp.route('/invoice_orders/create', methods=['POST'])
@login_required
def invoice_order_create():
    """Create a new order in the Orders Book (admin, sales and store only)."""
    if current_user.role not in ('admin', 'sales', 'store'):
        flash('Access denied.', 'danger')
        return redirect(url_for('orders.invoice_orders_list'))

    invoice_number    = (request.form.get('invoice_number') or '').strip()
    supplier_name     = (request.form.get('supplier_name') or '').strip()
    notes             = (request.form.get('notes') or '').strip()
    place_of_delivery = (request.form.get('place_of_delivery') or '').strip()
    mode_of_dispatch  = (request.form.get('mode_of_dispatch') or '').strip()
    transport_name    = (request.form.get('transport_name') or '').strip()
    transport_destination = (request.form.get('transport_destination') or '').strip()
    is_immediate      = request.form.get('is_immediate') == '1'

    # Parse target dispatch date
    dispatch_date_val = None
    dispatch_date_str = request.form.get('dispatch_date', '').strip()
    if dispatch_date_str:
        try:
            dispatch_date_val = datetime.datetime.strptime(dispatch_date_str, '%Y-%m-%dT%H:%M')
        except ValueError:
            try:
                dispatch_date_val = datetime.datetime.strptime(dispatch_date_str, '%Y-%m-%d')
            except ValueError:
                pass

    if not supplier_name:
        flash('Customer / Party name is required.', 'warning')
        return redirect(url_for('orders.invoice_orders_list'))

    # Collect line items
    names = request.form.getlist('item_name[]')
    qtys  = request.form.getlist('item_qty[]')
    units = request.form.getlist('item_unit[]')

    line_items = []
    for name, qty_str, unit in zip(names, qtys, units):
        name = name.strip()
        if not name:
            continue
        try:
            qty = int(qty_str)
        except (ValueError, TypeError):
            qty = 1
        if qty < 1:
            qty = 1
        line_items.append({'name': name, 'qty': qty, 'unit': (unit or '').strip()})


    # Handle invoice photo upload
    invoice_photo_path = None
    inv_file = request.files.get('invoice_photo')
    if inv_file and inv_file.filename:
        saved = _save_upload(inv_file, ALLOWED_INVOICE_EXT,
                             prefix=f'inv_{supplier_name[:10].replace(" ","_")}',
                             subdir=INVOICE_UPLOAD_SUBDIR)
        if saved:
            invoice_photo_path = saved

    so = SupplierOrder(
        supplier_name=supplier_name,
        invoice_number=invoice_number or None,
        notes=notes or None,
        place_of_delivery=place_of_delivery or None,
        mode_of_dispatch=mode_of_dispatch or None,
        transport_name=transport_name or None,
        transport_destination=transport_destination or None,
        is_immediate=is_immediate,
        dispatch_date=dispatch_date_val,
        invoice_photo=invoice_photo_path,
        status='Draft',
        created_by_id=current_user.id,
    )
    db.session.add(so)
    db.session.flush()

    for li in line_items:
        db.session.add(SupplierOrderItem(
            order_id=so.id,
            product_name=li['name'],
            qty=li['qty'],
            unit=li['unit'],
        ))

    inv_tag = f' [{invoice_number}]' if invoice_number else ''
    urgent_tag = ' 🔴 IMMEDIATE' if is_immediate else ''
    log_activity('CREATE', 'Orders',
                 f'Order{inv_tag} created for {supplier_name} ({len(line_items)} items){urgent_tag}',
                 ref_id=so.id, ref_type='SupplierOrder')
    db.session.commit()
    flash(f'Order #{so.id} created successfully.', 'success')
    return redirect(url_for('orders.invoice_order_detail', order_id=so.id))


@orders_bp.route('/invoice_orders/<int:order_id>')
@login_required
def invoice_order_detail(order_id):
    """Detail view of a single Orders Book entry."""
    so = db.session.get(SupplierOrder, order_id)
    if not so:
        flash('Order not found.', 'danger')
        return redirect(url_for('orders.invoice_orders_list'))
    return render_template('orders/invoice_order_detail.html', order=so)


@orders_bp.route('/invoice_orders/<int:order_id>/edit', methods=['GET', 'POST'])
@login_required
def invoice_order_edit(order_id):
    """Edit an Orders Book entry — only allowed when status is Draft."""
    if current_user.role not in ('admin', 'sales', 'store'):
        flash('Access denied.', 'danger')
        return redirect(url_for('orders.invoice_order_detail', order_id=order_id))

    so = db.session.get(SupplierOrder, order_id)
    if not so:
        flash('Order not found.', 'danger')
        return redirect(url_for('orders.invoice_orders_list'))

    if so.status != 'Draft':
        flash('Only Draft orders can be edited.', 'warning')
        return redirect(url_for('orders.invoice_order_detail', order_id=order_id))

    if request.method == 'GET':
        products = Product.query.order_by(Product.name).all()
        suppliers = Supplier.query.order_by(Supplier.name).all()
        return render_template('orders/invoice_order_edit.html', order=so,
                               products=products, suppliers=suppliers)

    # POST — save edits
    so.invoice_number     = (request.form.get('invoice_number') or '').strip() or None
    so.supplier_name      = (request.form.get('supplier_name') or '').strip()
    so.notes              = (request.form.get('notes') or '').strip() or None
    so.place_of_delivery  = (request.form.get('place_of_delivery') or '').strip() or None
    so.mode_of_dispatch   = (request.form.get('mode_of_dispatch') or '').strip() or None
    so.transport_name     = (request.form.get('transport_name') or '').strip() or None
    so.transport_destination = (request.form.get('transport_destination') or '').strip() or None
    so.is_immediate       = request.form.get('is_immediate') == '1'
    so.updated_at         = datetime.datetime.utcnow()

    dispatch_date_str = request.form.get('dispatch_date', '').strip()
    if dispatch_date_str:
        try:
            so.dispatch_date = datetime.datetime.strptime(dispatch_date_str, '%Y-%m-%dT%H:%M')
        except ValueError:
            try:
                so.dispatch_date = datetime.datetime.strptime(dispatch_date_str, '%Y-%m-%d')
            except ValueError:
                pass
    else:
        so.dispatch_date = None

    if not so.supplier_name:
        flash('Customer / Party name is required.', 'warning')
        return redirect(url_for('orders.invoice_order_edit', order_id=order_id))

    # Rebuild items
    for item in list(so.items):
        db.session.delete(item)
    db.session.flush()

    names = request.form.getlist('item_name[]')
    qtys  = request.form.getlist('item_qty[]')
    units = request.form.getlist('item_unit[]')

    line_items = []
    for name, qty_str, unit in zip(names, qtys, units):
        name = name.strip()
        if not name:
            continue
        try:
            qty = int(qty_str)
        except (ValueError, TypeError):
            qty = 1
        if qty < 1:
            qty = 1
        line_items.append({'name': name, 'qty': qty, 'unit': (unit or '').strip()})


    for li in line_items:
        db.session.add(SupplierOrderItem(
            order_id=so.id,
            product_name=li['name'],
            qty=li['qty'],
            unit=li['unit'],
        ))

    # Handle invoice photo replacement
    inv_file = request.files.get('invoice_photo')
    if inv_file and inv_file.filename:
        saved = _save_upload(inv_file, ALLOWED_INVOICE_EXT,
                             prefix=f'inv_{so.id}',
                             subdir=INVOICE_UPLOAD_SUBDIR)
        if saved:
            so.invoice_photo = saved

    log_activity('UPDATE', 'Orders',
                 f'Order #{so.id} ({so.supplier_name}) edited',
                 ref_id=so.id, ref_type='SupplierOrder')
    db.session.commit()
    flash(f'Order #{so.id} updated successfully.', 'success')
    return redirect(url_for('orders.invoice_order_detail', order_id=so.id))


@orders_bp.route('/invoice_orders/<int:order_id>/delete', methods=['POST'])
@login_required
def invoice_order_delete(order_id):
    """Delete an Orders Book entry (admin only)."""
    if current_user.role != 'admin':
        flash('Only admins can delete orders.', 'danger')
        return redirect(url_for('orders.invoice_order_detail', order_id=order_id))

    so = db.session.get(SupplierOrder, order_id)
    if not so:
        flash('Order not found.', 'danger')
        return redirect(url_for('orders.invoice_orders_list'))

    snap = f'Order #{so.id} ({so.supplier_name})'
    db.session.delete(so)
    log_activity('DELETE', 'Orders', f'Deleted {snap}', ref_id=order_id, ref_type='SupplierOrder')
    db.session.commit()
    flash(f'{snap} deleted.', 'success')
    return redirect(url_for('orders.invoice_orders_list'))


@orders_bp.route('/invoice_orders/<int:order_id>/status', methods=['POST'])
@login_required
def invoice_order_advance_status(order_id):
    """
    Advance a SupplierOrder status.
    Draft → Packing   : admin / sales
    Packing → Packed  : any authenticated user (warehouse worker)
    Packed → Dispatched : handled by /dispatch endpoint
    """
    so = db.session.get(SupplierOrder, order_id)
    if not so:
        flash('Order not found.', 'danger')
        return redirect(url_for('orders.invoice_orders_list'))

    if so.status in ('Dispatched', 'Received'):
        flash('Order is already dispatched/received.', 'info')
        return redirect(url_for('orders.invoice_order_detail', order_id=order_id))

    current_idx = SUPPLIER_STATUS_FLOW.index(so.status) if so.status in SUPPLIER_STATUS_FLOW else -1

    if current_idx == -1:
        flash('Unknown order status.', 'danger')
        return redirect(url_for('orders.invoice_order_detail', order_id=order_id))

    next_status = SUPPLIER_STATUS_FLOW[current_idx + 1]

    # Role gate: only admin/sales/store can trigger Draft → Packing
    if so.status == 'Draft' and current_user.role not in ('admin', 'sales', 'store'):
        flash('Only admin, sales or store staff can start packing.', 'danger')
        return redirect(url_for('orders.invoice_order_detail', order_id=order_id))

    # Packed → Dispatched is handled by the /dispatch endpoint (needs form data)
    if next_status == 'Dispatched':
        flash('Use the Dispatch form below to record dispatch details.', 'info')
        return redirect(url_for('orders.invoice_order_detail', order_id=order_id))

    so.status = next_status
    so.updated_at = datetime.datetime.utcnow()
    log_activity('UPDATE', 'Orders',
                 f'Order #{so.id} ({so.supplier_name}) → {next_status}',
                 ref_id=so.id, ref_type='SupplierOrder')
    db.session.commit()
    flash(f'Order status updated to {next_status}.', 'success')
    return redirect(url_for('orders.invoice_order_detail', order_id=order_id))


@orders_bp.route('/invoice_orders/<int:order_id>/dispatch', methods=['POST'])
@login_required
def invoice_order_dispatch(order_id):
    """
    Record dispatch details and mark order as Dispatched (admin, sales, store).
    """
    if current_user.role not in ('admin', 'sales', 'store'):
        flash('Only admin, sales or store staff can dispatch orders.', 'danger')
        return redirect(url_for('orders.invoice_order_detail', order_id=order_id))

    so = db.session.get(SupplierOrder, order_id)
    if not so:
        flash('Order not found.', 'danger')
        return redirect(url_for('orders.invoice_orders_list'))

    if so.status != 'Packed':
        flash('Order must be in Packed status before dispatching.', 'warning')
        return redirect(url_for('orders.invoice_order_detail', order_id=order_id))

    person_name    = (request.form.get('dispatch_person_name') or '').strip()
    person_phone   = (request.form.get('dispatch_person_phone') or '').strip()
    driver_name    = (request.form.get('dispatch_driver_name') or '').strip()
    vehicle_number = (request.form.get('dispatch_vehicle_number') or '').strip()
    vehicle_name   = (request.form.get('dispatch_vehicle_name') or '').strip()
    dispatch_mode  = (request.form.get('dispatch_mode') or '').strip()
    dispatched_at_str = request.form.get('dispatched_at')

    if not person_name:
        flash('Person in charge name is required.', 'warning')
        return redirect(url_for('orders.invoice_order_detail', order_id=order_id))

    # Parse dispatch date-time
    dispatched_at_val = datetime.datetime.utcnow()
    if dispatched_at_str:
        try:
            dispatched_at_val = datetime.datetime.strptime(dispatched_at_str, '%Y-%m-%dT%H:%M')
        except ValueError:
            pass

    # Handle file uploads
    bill_photo_path = so.dispatch_bill_photo
    video_path      = so.dispatch_video

    bill_file  = request.files.get('dispatch_bill_photo')
    video_file = request.files.get('dispatch_video')

    saved_photo = _save_upload(bill_file, ALLOWED_IMAGE_EXT, prefix=f'bill_{order_id}')
    if saved_photo:
        bill_photo_path = saved_photo

    saved_video = _save_upload(video_file, ALLOWED_VIDEO_EXT, prefix=f'video_{order_id}')
    if saved_video:
        video_path = saved_video

    so.dispatch_person_name      = person_name
    so.dispatch_person_phone     = person_phone
    so.dispatch_driver_name      = driver_name
    so.dispatch_vehicle_number   = vehicle_number
    so.dispatch_vehicle_name     = vehicle_name
    so.dispatch_mode             = dispatch_mode or None
    so.dispatch_bill_photo       = bill_photo_path
    so.dispatch_video            = video_path
    so.status                    = 'Dispatched'
    so.dispatched_at             = dispatched_at_val
    so.updated_at                = datetime.datetime.utcnow()

    log_activity('UPDATE', 'Orders',
                 f'Order #{so.id} dispatched via {dispatch_mode or vehicle_name or "—"} by {person_name}',
                 ref_id=so.id, ref_type='SupplierOrder')
    db.session.commit()
    flash(f'Order #{so.id} marked as Dispatched.', 'success')
    return redirect(url_for('orders.invoice_order_detail', order_id=order_id))


@orders_bp.route('/invoice_orders/<int:order_id>/dispatch_detail')
@login_required
def invoice_order_dispatch_detail(order_id):
    """Show full dispatch proof page."""
    so = db.session.get(SupplierOrder, order_id)
    if not so:
        flash('Order not found.', 'danger')
        return redirect(url_for('orders.invoice_orders_list'))
    return render_template('orders/invoice_order_dispatch.html', order=so)


@orders_bp.route('/invoice_orders/<int:order_id>/received', methods=['POST'])
@login_required
def invoice_order_mark_received(order_id):
    """Mark a dispatched order as received — records receiver name, receiving photo and transport bill photo."""
    if current_user.role not in ('admin', 'sales', 'store'):
        flash('Only admin, sales or store staff can mark orders as received.', 'danger')
        return redirect(url_for('orders.invoice_order_detail', order_id=order_id))

    so = db.session.get(SupplierOrder, order_id)
    if not so:
        flash('Order not found.', 'danger')
        return redirect(url_for('orders.invoice_orders_list'))

    if so.status != 'Dispatched':
        flash('Order must be in Dispatched status to mark as Received.', 'warning')
        return redirect(url_for('orders.invoice_order_detail', order_id=order_id))

    received_by = (request.form.get('received_by') or '').strip()
    delivery_note = (request.form.get('delivery_note') or '').strip()

    # ── Handle receiving photo upload ──
    recv_photo_file = request.files.get('receiving_photo')
    saved_recv_photo = _save_upload(recv_photo_file, ALLOWED_IMAGE_EXT,
                                    prefix=f'recv_{order_id}',
                                    subdir=UPLOAD_SUBDIR)
    if saved_recv_photo:
        so.receiving_photo         = saved_recv_photo
        so.receiving_photo_submitted = True   # auto-flag when file uploaded

    # ── Handle transport bill (LR) photo upload ──
    tb_photo_file = request.files.get('transport_bill_photo')
    saved_tb_photo = _save_upload(tb_photo_file, ALLOWED_IMAGE_EXT,
                                  prefix=f'lr_{order_id}',
                                  subdir=UPLOAD_SUBDIR)
    if saved_tb_photo:
        so.transport_bill_photo    = saved_tb_photo
        so.transport_bill_submitted  = True   # auto-flag when file uploaded

    so.status      = 'Received'
    so.received_at = datetime.datetime.utcnow()
    so.received_by = received_by or None
    so.delivery_note = delivery_note or None
    so.updated_at  = datetime.datetime.utcnow()

    log_activity('UPDATE', 'Orders',
                 f'Order #{so.id} ({so.supplier_name}) received by customer' +
                 (f' — signed by {received_by}' if received_by else ''),
                 ref_id=so.id, ref_type='SupplierOrder')
    db.session.commit()
    flash(f'Order #{so.id} marked as Received by customer.', 'success')
    return redirect(url_for('orders.invoice_order_detail', order_id=order_id))

