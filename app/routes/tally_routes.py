from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required, current_user
from app.extensions import db
from app.models import TallyBill, TallyBillItem, Product, Warehouse, WarehouseStock
from app.activity_service import log_activity
from datetime import datetime, timedelta
from sqlalchemy import func, or_
from collections import defaultdict
from flask import jsonify
import json
import smtplib
import os
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

tally_bp = Blueprint('tally', __name__)

@tally_bp.route("/tally", methods=["GET"])
@login_required
def tally_sales_page():
    now_ist = datetime.utcnow() + timedelta(hours=5, minutes=30)
    filter_date_str = request.args.get('filter_date')
    filter_status = request.args.get('filter_status')
    filter_inv = request.args.get('filter_inv')
    
    query = TallyBill.query
    
    if filter_inv:
        query = query.filter(or_(TallyBill.invoice_number.ilike(f"%{filter_inv}%"), TallyBill.client_name.ilike(f"%{filter_inv}%")))
    
    has_date_filter = False
    if filter_date_str:
        try:
            filter_date = datetime.strptime(filter_date_str, '%Y-%m-%d').date()
            # Convert local IST filter date to UTC range (local 00:00 to 23:59 minus 5h 30m)
            start_utc = datetime.combine(filter_date, datetime.min.time()) - timedelta(hours=5, minutes=30)
            end_utc = datetime.combine(filter_date, datetime.max.time()) - timedelta(hours=5, minutes=30)
            
            query = query.filter(TallyBill.date >= start_utc, TallyBill.date <= end_utc)
            has_date_filter = True
            flash(f"Showing tally bills for {filter_date.strftime('%d %b %Y')}", "info")
        except ValueError:
            flash("Invalid date format.", "danger")

    if filter_status and filter_status != 'all':
        if filter_status == 'partial':
            query = query.filter(TallyBill.payment_status.in_(['Partial Payment', 'Payment Not Received']))
        elif filter_status == 'received':
            query = query.filter(TallyBill.payment_status == 'Payment Received')
        elif filter_status == 'full_cash':
            query = query.filter(TallyBill.payment_status == 'Payment Received', func.coalesce(TallyBill.paid_online, 0.0) == 0.0)
        elif filter_status == 'full_online':
            query = query.filter(TallyBill.payment_status == 'Payment Received', func.coalesce(TallyBill.paid_cash, 0.0) == 0.0)
            
        if not has_date_filter:
             flash(f"Showing all '{filter_status}' tally bills found.", "info")

    query = query.order_by(TallyBill.date.desc(), TallyBill.id.desc())
    if not filter_inv and not filter_date_str and (not filter_status or filter_status == 'all'):
        query = query.limit(10)
        
    tally_bills_raw = query.all()
    
    tally_bills = defaultdict(list)
    for t in tally_bills_raw:
        date_key = t.date_ist.strftime('%d %b %Y')
        tally_bills[date_key].append(t)
    
    # Pre-load full product inventory for autocomplete 
    products = db.session.query(Product.id, Product.name, Product.category, Product.unit, Product.quantity).all()
    all_list = []
    for p in products:
        all_list.append({
            'id': p.id,
            'name': p.name,
            'category': p.category if p.category else "Uncategorized",
            'unit': p.unit if p.unit else "Units",
            'quantity': p.quantity if p.quantity else 0
        })
    
    all_products_json = json.dumps(all_list)

    return render_template('tally_sales.html', 
                            tally_bills=tally_bills, 
                            all_products_json=all_products_json,
                            current_filter_date=filter_date_str,
                            current_filter_status=filter_status,
                            current_filter_inv=filter_inv,
                            current_filter_overdue=None,
                            warehouses=Warehouse.query.order_by(Warehouse.name).all())

@tally_bp.route("/save_tally", methods=["POST"])
@login_required
def save_tally():
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    try:
        invoice_number = request.form.get('invoice_number')
        credit_period = int(request.form.get('credit_period', 0))
        payment_done = request.form.get('payment_done')
        warehouse_id = request.form.get('warehouse_id')
        
        customer_email = request.form.get('customer_email')
        customer_phone = request.form.get('customer_phone')
        
        if customer_phone:
            customer_phone = customer_phone.strip()
            import re
            if not re.match(r'^\d{10}$', customer_phone):
                err = "Invalid phone number format. Please enter a valid 10-digit number."
                flash(err, "danger")
                if is_ajax:
                    return jsonify({'success': False, 'error': err}), 400
                return redirect(url_for('tally.tally_sales_page'))
        
        tally_cart_str = request.form.get('tally_cart')
        tally_cart = []
        if tally_cart_str:
            import json
            tally_cart = json.loads(tally_cart_str)

        grand_total = float(request.form.get('grand_total', 0.0))

        payment_status = "Payment Not Received"
        payment_mode = None
        paid_cash = 0.0
        paid_online = 0.0

        # Read manually provided bill date if any
        bill_date_str = request.form.get('bill_date')
        if bill_date_str:
            try:
                ist_now = datetime.utcnow() + timedelta(hours=5, minutes=30)
                date_ist = datetime.combine(datetime.strptime(bill_date_str, '%Y-%m-%d').date(), ist_now.time())
                date_obj = date_ist - timedelta(hours=5, minutes=30)
            except Exception:
                date_obj = datetime.utcnow()
        else:
            date_obj = datetime.utcnow()

        if payment_done == 'Yes':
            payment_date_str = request.form.get('payment_date')
            # If user didn't specify bill_date, fall back to payment_date for date_obj
            if payment_date_str and not bill_date_str:
                try:
                    date_obj = datetime.strptime(payment_date_str, '%Y-%m-%d')
                except Exception:
                    pass
            payment_mode = request.form.get('payment_mode')
            if payment_mode == 'Both':
                paid_cash = float(request.form.get('cash_amount') or 0.0)
                paid_online = float(request.form.get('online_amount') or 0.0)
                modes = []
                if paid_cash > 0: modes.append("Cash")
                if paid_online > 0: modes.append("Online")
                payment_mode = " + ".join(modes) if modes else "Both"
            elif payment_mode == 'Cash':
                paid_cash = float(request.form.get('cash_amount') or 0.0)
            elif payment_mode == 'Online':
                paid_online = float(request.form.get('online_amount') or 0.0)

            total_paid = paid_cash + paid_online
            if total_paid > 0:
                if grand_total > 0 and total_paid < grand_total:
                    payment_status = "Partial Payment"
                else:
                    payment_status = "Payment Received"

        new_tally = TallyBill(
            client_name=request.form.get('customer_name'),
            invoice_number=invoice_number,
            date=date_obj,
            payment_status=payment_status,
            order_status='Order Sent', # Auto-mark sent to immediately deduct
            credit_period=credit_period,
            payment_mode=payment_mode,
            paid_cash=paid_cash,
            paid_online=paid_online,
            grand_total=grand_total,
            customer_email=customer_email,
            customer_phone=customer_phone,
            warehouse_id=warehouse_id
        )
        
        if payment_status == "Payment Received":
            new_tally.credit_period = 0
            
        db.session.add(new_tally)
        db.session.flush()

        for item_data in tally_cart:
            raw_id = item_data.get('id')
            p_id = int(raw_id) if raw_id not in (None, '', 'null') else None
            p_name = item_data.get('name')
            p_qty = int(item_data.get('qty', 1))
            
            new_item = TallyBillItem(
                tally_bill_id=new_tally.id,
                product_name=p_name,
                qty=p_qty,
                product_id=p_id
            )
            db.session.add(new_item)
            
            # Deduct inventory instantly
            product = None
            if p_id:
                product = db.session.get(Product, p_id)
            else:
                base_name = p_name.split(' (')[0].split(' - ')[0].strip()
                product = Product.query.filter(db.func.lower(Product.name) == base_name.lower()).first()
                
            if product:
                product.quantity -= p_qty
                if warehouse_id:
                    ws = WarehouseStock.query.filter_by(warehouse_id=warehouse_id, product_id=product.id).first()
                    if ws: ws.quantity -= p_qty
            
        # log_activity BEFORE commit so audit log is atomic with the bill creation
        log_activity('CREATE', 'Tally', f'New Tally Bill #{invoice_number} — ₹{grand_total:.0f}', ref_id=new_tally.id, ref_type='TallyBill')
        db.session.commit()   # single atomic commit: bill + log together
        success_msg = f"Tally Invoice #{invoice_number} successfully added and inventory deducted."
        flash(success_msg, "success")
        if is_ajax:
            return jsonify({'success': True, 'redirect': url_for('tally.tally_sales_page'), 'message': success_msg})
        
    except Exception as e:
        db.session.rollback()
        err_msg = f"Error saving Tally Bill: {e}"
        flash(err_msg, "danger")
        if is_ajax:
            return jsonify({'success': False, 'error': err_msg}), 400
        
    return redirect(url_for('tally.tally_sales_page'))

@tally_bp.route("/update_tally_status", methods=["POST"])
@login_required
def update_tally_status():
    try:
        tally_id = request.form.get('tally_id')
        status_type = request.form.get('status_type') # 'order' or 'payment'
        warehouse_id = request.form.get('warehouse_id')
        
        tally = db.session.get(TallyBill, tally_id)
        if tally:
            if status_type == 'order':
                new_status = request.form.get('new_status')
                if tally.order_status != new_status:
                    if new_status == 'Order Sent' and tally.order_status == 'Order Pending':
                        # Deduct inventory
                        for item in tally.items:
                            product = None
                            if item.product_id:
                                product = db.session.get(Product, item.product_id)
                            else:
                                base_name = item.product_name.split(' (')[0].split(' - ')[0].strip()
                                product = Product.query.filter(db.func.lower(Product.name) == base_name.lower()).first()
                            if product:
                                product.quantity -= item.qty
                                w_id = warehouse_id or tally.warehouse_id
                                if w_id:
                                    ws = WarehouseStock.query.filter_by(warehouse_id=w_id, product_id=product.id).first()
                                    if ws: ws.quantity -= item.qty
                                    
                    if warehouse_id and new_status == 'Order Sent':
                        tally.warehouse_id = warehouse_id
                    elif new_status == 'Order Pending' and tally.order_status == 'Order Sent':
                        # Revert inventory if sent back to Pending
                        for item in tally.items:
                            product = None
                            if item.product_id:
                                product = db.session.get(Product, item.product_id)
                            else:
                                base_name = item.product_name.split(' (')[0].split(' - ')[0].strip()
                                product = Product.query.filter(db.func.lower(Product.name) == base_name.lower()).first()
                            if product:
                                product.quantity += item.qty
                                w_id = tally.warehouse_id or warehouse_id
                                if w_id:
                                    ws = WarehouseStock.query.filter_by(warehouse_id=w_id, product_id=product.id).first()
                                    if ws: ws.quantity += item.qty
                                
                    tally.order_status = new_status
                    flash(f"Tally #{tally.invoice_number} order status updated to '{new_status}'", "success")

            elif status_type == 'payment':
                p_mode = request.form.get('payment_mode')
                cash_amt = float(request.form.get('cash_amount') or 0.0)
                online_amt = float(request.form.get('online_amount') or 0.0)
                new_grand_total = float(request.form.get('grand_total') or tally.grand_total)
                payment_date_str = request.form.get('payment_date')

                tally.grand_total = new_grand_total
                total_paid = cash_amt + online_amt
                
                if total_paid > 0:
                    tally.payment_mode = p_mode if p_mode else None
                    if p_mode == 'Cash':
                        tally.paid_cash = cash_amt
                        tally.paid_online = 0.0
                    elif p_mode == 'Online':
                        tally.paid_cash = 0.0
                        tally.paid_online = online_amt
                    elif p_mode == 'Both':
                        tally.paid_cash = cash_amt
                        tally.paid_online = online_amt
                    
                    if tally.paid_cash > 0 and tally.paid_online > 0:
                        tally.payment_mode = "Cash + Online"
                        
                    if new_grand_total > 0 and total_paid < new_grand_total:
                        tally.payment_status = "Partial Payment"
                    else:
                        tally.payment_status = "Payment Received"
                        tally.credit_period = 0
                        
                    if payment_date_str:
                        try:
                            # Assume 'YYYY-MM-DD' from the date picker
                            tally.payment_date = datetime.strptime(payment_date_str, '%Y-%m-%d')
                        except ValueError:
                            pass
                else:
                    tally.payment_status = "Payment Not Received"
                    tally.payment_mode = None
                    tally.paid_cash = 0.0
                    tally.paid_online = 0.0
                    tally.payment_date = None

                flash(f"Tally #{tally.invoice_number} payment status updated.", "success")
                
            elif status_type == 'customer':
                tally.customer_email = request.form.get('customer_email') or None
                tally.customer_phone = request.form.get('customer_phone') or None
                tally.credit_period = int(request.form.get('credit_period') or 0)
                flash(f"Tally #{tally.invoice_number} customer info updated.", "success")
                
            # log_activity BEFORE commit so status change + audit log are atomic
            log_type = 'UPDATE'
            if status_type == 'payment': log_type = 'PAYMENT'
            elif status_type == 'customer': log_type = 'UPDATE'
            
            log_activity(log_type, 'Tally',
                         f'Tally #{tally.invoice_number} {status_type} details updated',
                         ref_id=tally.id, ref_type='TallyBill')
            db.session.commit()   # single atomic commit: status + log together
            
    except Exception as e:
        db.session.rollback()
        flash(f"Error updating status: {e}", "danger")
        
    redirect_to = request.form.get('redirect_to')
    if redirect_to:
        return redirect(redirect_to)
    return redirect(url_for('tally.tally_sales_page'))

@tally_bp.route("/due-bills", methods=["GET"])
@login_required
def due_bills_page():
    now_ist = datetime.utcnow() + timedelta(hours=5, minutes=30)
    today = now_ist.date()
    from sqlalchemy.types import Integer
    from sqlalchemy import cast
    offset = timedelta(hours=5, minutes=30)

    # Bills due TODAY (maturity date == today)
    due_today_raw = TallyBill.query.filter(
        TallyBill.payment_status != 'Payment Received',
        TallyBill.credit_period > 0,
        (func.date(TallyBill.date + offset) + cast(TallyBill.credit_period, Integer)) == today
    ).order_by(TallyBill.date.asc()).all()

    for bill in due_today_raw:
        bill.amount_due = bill.grand_total - (bill.paid_cash or 0) - (bill.paid_online or 0)
        bill.maturity_date_str = (bill.date + timedelta(days=bill.credit_period)).strftime('%d %b %Y')

    # OVERDUE bills (past maturity date, not paid)
    overdue_raw = TallyBill.query.filter(
        TallyBill.payment_status != 'Payment Received',
        TallyBill.credit_period > 0,
        (func.date(TallyBill.date + offset) + cast(TallyBill.credit_period, Integer)) < today
    ).order_by(TallyBill.date.asc()).all()

    for bill in overdue_raw:
        maturity = (bill.date + timedelta(days=bill.credit_period)).date()
        bill.amount_due = bill.grand_total - (bill.paid_cash or 0) - (bill.paid_online or 0)
        bill.maturity_date_str = maturity.strftime('%d %b %Y')
        bill.days_overdue = (today - maturity).days

    # ALL pending bills (any unpaid, regardless of credit period or date)
    pending_raw = TallyBill.query.filter(
        TallyBill.payment_status != 'Payment Received'
    ).order_by(TallyBill.date.desc()).all()

    for bill in pending_raw:
        bill.amount_due = bill.grand_total - (bill.paid_cash or 0) - (bill.paid_online or 0)

    return render_template(
        'due_tally_bills.html',
        due_today_bills=due_today_raw,
        overdue_bills=overdue_raw,
        pending_bills=pending_raw,
        today_str=today.strftime('%d %b %Y'),
        warehouses=Warehouse.query.order_by(Warehouse.name).all()
    )

@tally_bp.route("/api/tally_due_bills", methods=["GET"])
@login_required
def get_due_bills():
    now_ist = datetime.utcnow() + timedelta(hours=5, minutes=30)
    from sqlalchemy.types import Integer
    from sqlalchemy import cast
    offset = timedelta(hours=5, minutes=30)
    
    # Due bills: Not fully paid, has a credit period > 0, and current date is past maturity date
    due_bills = TallyBill.query.filter(
        TallyBill.payment_status != 'Payment Received',
        TallyBill.credit_period > 0,
        (func.date(TallyBill.date + offset) + cast(TallyBill.credit_period, Integer)) <= now_ist.date()
    ).order_by(TallyBill.date.asc()).all()
    
    results = []
    for bill in due_bills:
        maturity_date = (bill.date + timedelta(days=bill.credit_period)).date()
        days_overdue = (now_ist.date() - maturity_date).days
        
        items_list = [{'product_name': i.product_name, 'qty': i.qty} for i in bill.items]
        
        results.append({
            'id': bill.id,
            'invoice_number': bill.invoice_number,
            'date_str': bill.date.strftime('%d %b %Y'),
            'maturity_date_str': maturity_date.strftime('%d %b %Y'),
            'days_overdue': days_overdue,
            'grand_total': bill.grand_total,
            'paid_amount': (bill.paid_cash or 0) + (bill.paid_online or 0),
            'customer_email': bill.customer_email or "Not Provided",
            'customer_phone': bill.customer_phone or "Not Provided",
            'items': items_list
        })
        
    return jsonify(results)

@tally_bp.route("/delete_tally_bill/<int:tally_id>", methods=["POST"])
@login_required
def delete_tally_bill(tally_id):
    try:
        tally = db.session.get(TallyBill, tally_id)
        if tally:
            # If the stock was already deducted, revert it
            if tally.order_status == 'Order Sent':
                for item in tally.items:
                    if item.product_id:
                        product = db.session.get(Product, item.product_id)
                    else:
                        base_name = item.product_name.split(' (')[0].split(' - ')[0].strip()
                        product = Product.query.filter(db.func.lower(Product.name) == base_name.lower()).first()
                    if product:
                        product.quantity += item.qty
                        # Revert to the assigned warehouse, or fallback if none was saved
                        w_id = tally.warehouse_id
                        if not w_id:
                            first_wh = Warehouse.query.first()
                            w_id = first_wh.id if first_wh else None
                            
                        if w_id:
                            ws = WarehouseStock.query.filter_by(warehouse_id=w_id, product_id=product.id).first()
                            if ws: ws.quantity += item.qty
            
            # Snapshot BEFORE delete — SQLAlchemy expires all attributes after commit
            invoice_snap = tally.invoice_number
            tally_id_snap = tally.id
            db.session.delete(tally)
            log_activity('DELETE', 'Tally', f'Deleted Tally Bill #{invoice_snap}',
                         ref_id=tally_id_snap, ref_type='TallyBill')
            db.session.commit()   # single atomic commit: delete + log together
            flash(f"Tally Bill #{invoice_snap} deleted permanently.", "warning")
    except Exception as e:
        db.session.rollback()
        flash(f"Error deleting Tally bill: {e}", "danger")
        
    return redirect(url_for('tally.tally_sales_page'))


@tally_bp.route("/api/send_due_reminder/<int:tally_id>", methods=["POST"])
@login_required
def send_due_reminder(tally_id):
    """Send a professional HTML payment reminder email to the customer."""
    try:
        bill = db.session.get(TallyBill, tally_id)
        if not bill:
            return jsonify({'success': False, 'error': 'Bill not found.'}), 404

        recipient_email = bill.customer_email
        if not recipient_email:
            return jsonify({'success': False, 'error': 'No email address on file for this customer.'}), 400

        # ── Compute amounts ──────────────────────────────────────
        now_ist = datetime.utcnow() + timedelta(hours=5, minutes=30)
        paid_total = (bill.paid_cash or 0) + (bill.paid_online or 0)
        balance_due = bill.grand_total - paid_total
        maturity_date = (bill.date + timedelta(days=bill.credit_period)).strftime('%d %b %Y') if bill.credit_period else 'N/A'
        days_overdue = (now_ist.date() - (bill.date + timedelta(days=bill.credit_period or 0)).date()).days

        items_rows = ''.join(
            f'<tr><td style="padding:8px 12px;border-bottom:1px solid #f0f0f0;">{item.product_name}</td>'
            f'<td style="padding:8px 12px;border-bottom:1px solid #f0f0f0;text-align:center;font-weight:600;">{item.qty}</td></tr>'
            for item in bill.items
        )

        status_color = '#dc2626' if days_overdue > 0 else '#d97706'
        status_label = f'Overdue by {days_overdue} day(s)' if days_overdue > 0 else 'Due Today'

        # ── Build HTML email body ────────────────────────────────
        html_body = f"""
        <!DOCTYPE html>
        <html>
        <head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"></head>
        <body style="margin:0;padding:0;background:#f4f7f6;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
          <table width="100%" cellpadding="0" cellspacing="0" style="background:#f4f7f6;padding:30px 0;">
            <tr><td align="center">
              <table width="600" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:12px;overflow:hidden;box-shadow:0 4px 20px rgba(0,0,0,0.08);">

                <!-- Header -->
                <tr>
                  <td style="background:linear-gradient(135deg,#1e3a5f,#0ea5e9);padding:32px 40px;text-align:center;">
                    <h1 style="margin:0;color:#ffffff;font-size:24px;font-weight:700;letter-spacing:-0.5px;">Safe Environment International</h1>
                    <p style="margin:6px 0 0;color:rgba(255,255,255,0.8);font-size:13px;">Payment Reminder Notice</p>
                  </td>
                </tr>

                <!-- Status Banner -->
                <tr>
                  <td style="background:{status_color};padding:14px 40px;text-align:center;">
                    <p style="margin:0;color:#ffffff;font-size:15px;font-weight:700;letter-spacing:0.3px;">
                      ⚠️ {status_label}
                    </p>
                  </td>
                </tr>

                <!-- Greeting -->
                <tr>
                  <td style="padding:32px 40px 16px;">
                    <p style="margin:0 0 16px;font-size:16px;color:#1e293b;">
                      Dear <strong>{bill.client_name or 'Customer'}</strong>,
                    </p>
                    <p style="margin:0;font-size:14px;color:#475569;line-height:1.7;">
                      This is a payment reminder from <strong>Safe Environment International</strong> regarding
                      Invoice <strong>#{bill.invoice_number}</strong>. The payment for this bill
                      is currently outstanding. Kindly arrange to clear the dues at the earliest
                      to avoid any inconvenience.
                    </p>
                  </td>
                </tr>

                <!-- Bill Summary Card -->
                <tr>
                  <td style="padding:0 40px 24px;">
                    <table width="100%" cellpadding="0" cellspacing="0"
                           style="background:#f8fafc;border-radius:10px;border:1px solid #e2e8f0;overflow:hidden;">
                      <tr><td colspan="2" style="padding:14px 20px;background:#e2e8f0;">
                        <span style="font-size:11px;font-weight:700;text-transform:uppercase;color:#64748b;letter-spacing:0.08em;">Bill Summary</span>
                      </td></tr>
                      <tr>
                        <td style="padding:12px 20px;font-size:13px;color:#64748b;border-bottom:1px solid #e2e8f0;">Invoice Number</td>
                        <td style="padding:12px 20px;font-size:13px;font-weight:600;color:#1e293b;text-align:right;border-bottom:1px solid #e2e8f0;">#{bill.invoice_number}</td>
                      </tr>
                      <tr>
                        <td style="padding:12px 20px;font-size:13px;color:#64748b;border-bottom:1px solid #e2e8f0;">Bill Date</td>
                        <td style="padding:12px 20px;font-size:13px;font-weight:600;color:#1e293b;text-align:right;border-bottom:1px solid #e2e8f0;">{bill.date_ist.strftime('%d %b %Y')}</td>
                      </tr>
                      <tr>
                        <td style="padding:12px 20px;font-size:13px;color:#64748b;border-bottom:1px solid #e2e8f0;">Due Date</td>
                        <td style="padding:12px 20px;font-size:13px;font-weight:600;color:{status_color};text-align:right;border-bottom:1px solid #e2e8f0;">{maturity_date}</td>
                      </tr>
                      <tr>
                        <td style="padding:12px 20px;font-size:13px;color:#64748b;border-bottom:1px solid #e2e8f0;">Total Amount</td>
                        <td style="padding:12px 20px;font-size:13px;font-weight:600;color:#1e293b;text-align:right;border-bottom:1px solid #e2e8f0;">₹{bill.grand_total:.2f}</td>
                      </tr>
                      <tr>
                        <td style="padding:12px 20px;font-size:13px;color:#64748b;border-bottom:1px solid #e2e8f0;">Amount Paid</td>
                        <td style="padding:12px 20px;font-size:13px;font-weight:600;color:#16a34a;text-align:right;border-bottom:1px solid #e2e8f0;">₹{paid_total:.2f}</td>
                      </tr>
                      <tr>
                        <td style="padding:14px 20px;font-size:14px;font-weight:700;color:#dc2626;">Balance Due</td>
                        <td style="padding:14px 20px;font-size:18px;font-weight:800;color:#dc2626;text-align:right;">₹{balance_due:.2f}</td>
                      </tr>
                    </table>
                  </td>
                </tr>


                <!-- CTA -->
                <tr>
                  <td style="padding:0 40px 32px;text-align:center;">
                    <p style="font-size:13px;color:#475569;line-height:1.7;margin:0 0 20px;">
                      Please contact us immediately to arrange payment or discuss a payment plan.
                      You can reach us at <strong>arth.hygienematrix@gmail.com</strong>.
                    </p>
                    <p style="margin:0;font-size:13px;color:#94a3b8;">
                      Thank you for your prompt attention to this matter.
                    </p>
                  </td>
                </tr>

                <!-- Footer -->
                <tr>
                  <td style="background:#f8fafc;padding:20px 40px;border-top:1px solid #e2e8f0;text-align:center;">
                    <p style="margin:0;font-size:12px;color:#94a3b8;">
                      SEI Software &nbsp;·&nbsp; Safe Environment International &nbsp;·&nbsp; This is an automated payment reminder.
                    </p>
                  </td>
                </tr>

              </table>
            </td></tr>
          </table>
        </body>
        </html>
        """

        # ── SMTP Send ────────────────────────────────────────────
        mail_user = os.environ.get('MAIL_USERNAME', 'arth.hygienematrix@gmail.com')
        mail_pass = os.environ.get('MAIL_PASSWORD', '')

        if not mail_pass:
            return jsonify({'success': False, 'error': 'Email password not configured in .env (MAIL_PASSWORD).'}), 500

        msg = MIMEMultipart('alternative')
        msg['Subject'] = f'Payment Reminder — Invoice #{bill.invoice_number} | Safe Environment International'
        msg['From'] = f'Safe Environment International <{mail_user}>'
        msg['To'] = recipient_email
        msg.attach(MIMEText(html_body, 'html', 'utf-8'))

        with smtplib.SMTP('smtp.gmail.com', 587) as server:
            server.ehlo()
            server.starttls()
            server.login(mail_user, mail_pass)
            server.sendmail(mail_user, recipient_email, msg.as_string())

        log_activity('EMAIL', 'Tally',
                     f'Reminder email sent for #{bill.invoice_number} to {recipient_email}',
                     ref_id=bill.id, ref_type='TallyBill')

        return jsonify({'success': True, 'message': f'Reminder sent to {recipient_email}'})

    except smtplib.SMTPAuthenticationError:
        return jsonify({'success': False, 'error': 'Gmail authentication failed. Check MAIL_PASSWORD in .env (use an App Password, not your regular password).'}), 500
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@tally_bp.route("/api/send_bulk_due_reminder", methods=["POST"])
@login_required
def send_bulk_due_reminder():
    """Send ONE combined payment reminder email listing multiple outstanding bills for a customer."""
    try:
        data = request.get_json()
        bill_ids = data.get('bill_ids', [])

        if not bill_ids or len(bill_ids) < 1:
            return jsonify({'success': False, 'error': 'No bill IDs provided.'}), 400

        bills = TallyBill.query.filter(TallyBill.id.in_(bill_ids)).all()
        if not bills:
            return jsonify({'success': False, 'error': 'No bills found.'}), 404

        # All bills must share the same customer email
        emails = set(b.customer_email for b in bills if b.customer_email)
        if not emails:
            return jsonify({'success': False, 'error': 'None of the selected bills have a customer email on file.'}), 400

        recipient_email = emails.pop()  # use first common email

        # ── Build per-bill rows ──────────────────────────────────────────────
        now_ist = datetime.utcnow() + timedelta(hours=5, minutes=30)
        total_balance = 0.0

        bill_rows_html = ''
        for b in bills:
            paid = (b.paid_cash or 0) + (b.paid_online or 0)
            balance = b.grand_total - paid
            total_balance += balance
            due_date = (b.date + timedelta(days=b.credit_period)).strftime('%d %b %Y') if b.credit_period else 'N/A'
            days_overdue = (now_ist.date() - (b.date + timedelta(days=b.credit_period or 0)).date()).days
            status_label = f'Overdue {days_overdue}d' if days_overdue > 0 else ('Due Today' if days_overdue == 0 else 'Upcoming')
            status_color = '#dc2626' if days_overdue > 0 else ('#d97706' if days_overdue == 0 else '#475569')
            bill_rows_html += (
                f'<tr>'
                f'<td style="padding:10px 14px;border-bottom:1px solid #e2e8f0;font-weight:600;color:#0ea5e9;">#{b.invoice_number}</td>'
                f'<td style="padding:10px 14px;border-bottom:1px solid #e2e8f0;color:#475569;">{b.date_ist.strftime("%d %b %Y")}</td>'
                f'<td style="padding:10px 14px;border-bottom:1px solid #e2e8f0;color:{status_color};font-weight:600;">{status_label} — {due_date}</td>'
                f'<td style="padding:10px 14px;border-bottom:1px solid #e2e8f0;text-align:right;color:#1e293b;">₹{b.grand_total:.2f}</td>'
                f'<td style="padding:10px 14px;border-bottom:1px solid #e2e8f0;text-align:right;color:#16a34a;">₹{paid:.2f}</td>'
                f'<td style="padding:10px 14px;border-bottom:1px solid #e2e8f0;text-align:right;font-weight:800;color:#dc2626;">₹{balance:.2f}</td>'
                f'</tr>'
            )

        client_name = bills[0].client_name or 'Customer'

        html_body = f"""
        <!DOCTYPE html>
        <html>
        <head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"></head>
        <body style="margin:0;padding:0;background:#f4f7f6;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
          <table width="100%" cellpadding="0" cellspacing="0" style="background:#f4f7f6;padding:30px 0;">
            <tr><td align="center">
              <table width="620" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:12px;overflow:hidden;box-shadow:0 4px 20px rgba(0,0,0,0.08);">

                <!-- Header -->
                <tr>
                  <td style="background:linear-gradient(135deg,#1e3a5f,#0ea5e9);padding:32px 40px;text-align:center;">
                    <h1 style="margin:0;color:#ffffff;font-size:24px;font-weight:700;letter-spacing:-0.5px;">Safe Environment International</h1>
                    <p style="margin:6px 0 0;color:rgba(255,255,255,0.8);font-size:13px;">Consolidated Payment Reminder</p>
                  </td>
                </tr>

                <!-- Alert Banner -->
                <tr>
                  <td style="background:#dc2626;padding:14px 40px;text-align:center;">
                    <p style="margin:0;color:#ffffff;font-size:15px;font-weight:700;letter-spacing:0.3px;">
                      ⚠️ {len(bills)} Outstanding Bill(s) Require Your Attention
                    </p>
                  </td>
                </tr>

                <!-- Greeting -->
                <tr>
                  <td style="padding:32px 40px 20px;">
                    <p style="margin:0 0 14px;font-size:16px;color:#1e293b;">
                      Dear <strong>{client_name}</strong>,
                    </p>
                    <p style="margin:0;font-size:14px;color:#475569;line-height:1.75;">
                      This is a consolidated payment reminder from <strong>Safe Environment International</strong>.
                      Our records indicate that you have <strong>{len(bills)} outstanding invoice(s)</strong> with a
                      combined balance of <strong style="color:#dc2626;">₹{total_balance:.2f}</strong>.
                      Kindly arrange to clear all dues at the earliest to avoid any inconvenience.
                    </p>
                  </td>
                </tr>

                <!-- Bills Summary Table -->
                <tr>
                  <td style="padding:0 40px 28px;">
                    <table width="100%" cellpadding="0" cellspacing="0"
                           style="border:1px solid #e2e8f0;border-radius:10px;overflow:hidden;font-size:13px;">
                      <thead>
                        <tr style="background:#1e3a5f;">
                          <th style="padding:12px 14px;color:#fff;text-align:left;font-weight:600;">Invoice</th>
                          <th style="padding:12px 14px;color:#fff;text-align:left;font-weight:600;">Bill Date</th>
                          <th style="padding:12px 14px;color:#fff;text-align:left;font-weight:600;">Due Date / Status</th>
                          <th style="padding:12px 14px;color:#fff;text-align:right;font-weight:600;">Total</th>
                          <th style="padding:12px 14px;color:#fff;text-align:right;font-weight:600;">Paid</th>
                          <th style="padding:12px 14px;color:#fff;text-align:right;font-weight:600;">Balance</th>
                        </tr>
                      </thead>
                      <tbody>{bill_rows_html}</tbody>
                      <tfoot>
                        <tr style="background:#fef2f2;">
                          <td colspan="5" style="padding:14px;font-weight:700;color:#dc2626;font-size:14px;">Total Outstanding Balance</td>
                          <td style="padding:14px;font-size:18px;font-weight:900;color:#dc2626;text-align:right;">₹{total_balance:.2f}</td>
                        </tr>
                      </tfoot>
                    </table>
                  </td>
                </tr>

                <!-- CTA -->
                <tr>
                  <td style="padding:0 40px 32px;text-align:center;">
                    <p style="font-size:13px;color:#475569;line-height:1.7;margin:0 0 10px;">
                      Please contact us immediately to arrange payment or discuss a payment plan.<br>
                      You can reach us at <strong>arth.hygienematrix@gmail.com</strong>.
                    </p>
                    <p style="margin:0;font-size:13px;color:#94a3b8;">Thank you for your prompt attention to this matter.</p>
                  </td>
                </tr>

                <!-- Footer -->
                <tr>
                  <td style="background:#f8fafc;padding:20px 40px;border-top:1px solid #e2e8f0;text-align:center;">
                    <p style="margin:0;font-size:12px;color:#94a3b8;">
                      SEI Software &nbsp;·&nbsp; Safe Environment International &nbsp;·&nbsp; This is an automated payment reminder.
                    </p>
                  </td>
                </tr>

              </table>
            </td></tr>
          </table>
        </body>
        </html>
        """

        # ── SMTP Send ────────────────────────────────────────────────────────
        mail_user = os.environ.get('MAIL_USERNAME', 'arth.hygienematrix@gmail.com')
        mail_pass = os.environ.get('MAIL_PASSWORD', '')

        if not mail_pass:
            return jsonify({'success': False, 'error': 'Email password not configured in .env (MAIL_PASSWORD).'}), 500

        invoice_list = ', '.join(f'#{b.invoice_number}' for b in bills)
        msg = MIMEMultipart('alternative')
        msg['Subject'] = f'Consolidated Payment Reminder ({len(bills)} Invoices) | Safe Environment International'
        msg['From']    = f'Safe Environment International <{mail_user}>'
        msg['To']      = recipient_email
        msg.attach(MIMEText(html_body, 'html', 'utf-8'))

        with smtplib.SMTP('smtp.gmail.com', 587) as server:
            server.ehlo()
            server.starttls()
            server.login(mail_user, mail_pass)
            server.sendmail(mail_user, recipient_email, msg.as_string())

        log_activity('EMAIL', 'Tally',
                     f'Bulk reminder sent for {invoice_list} to {recipient_email}',
                     ref_type='TallyBill')

        return jsonify({'success': True, 'message': f'Combined reminder for {len(bills)} bills sent to {recipient_email}'})

    except smtplib.SMTPAuthenticationError:
        return jsonify({'success': False, 'error': 'Gmail authentication failed. Use an App Password in MAIL_PASSWORD.'}), 500
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500
