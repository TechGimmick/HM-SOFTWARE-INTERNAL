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
                flash("Invalid phone number format. Please enter a valid 10-digit number.", "danger")
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
        date_obj = datetime.utcnow()

        if payment_done == 'Yes':
            payment_date_str = request.form.get('payment_date')
            if payment_date_str:
                date_obj = datetime.strptime(payment_date_str, '%Y-%m-%d')
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
            p_id = item_data.get('id')
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
        flash(f"Tally Invoice #{invoice_number} successfully added and inventory deducted.", "success")
        
    except Exception as e:
        db.session.rollback()
        flash(f"Error saving Tally Bill: {e}", "danger")
        
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
                else:
                    tally.payment_status = "Payment Not Received"
                    tally.payment_mode = None
                    tally.paid_cash = 0.0
                    tally.paid_online = 0.0

                flash(f"Tally #{tally.invoice_number} payment status updated.", "success")
                
            # log_activity BEFORE commit so status change + audit log are atomic
            log_activity('UPDATE' if status_type == 'order' else 'PAYMENT', 'Tally',
                         f'Tally #{tally.invoice_number} {status_type} status updated',
                         ref_id=tally.id, ref_type='TallyBill')
            db.session.commit()   # single atomic commit: status + log together
            
    except Exception as e:
        db.session.rollback()
        flash(f"Error updating status: {e}", "danger")
        
    return redirect(url_for('tally.tally_sales_page'))

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
