from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, session, make_response
from flask_login import login_required, current_user
from app.extensions import db
from app.models import Product, Supplier, Purchase, Sale, SaleItem, Customer, User
from sqlalchemy import func
import datetime
import re
import json
from collections import defaultdict
from fpdf import FPDF

inventory_bp = Blueprint('inventory', __name__)

@inventory_bp.route("/ping")
def ping():
    return '', 204  # No Content — keeps the connection alive without overhead

@inventory_bp.route("/dashboard")
@login_required
def dashboard():
    # --- 1. FETCH LOW STOCK ALERTS ---
    alerts = []
    try:
        low_stock_products = Product.query.filter(Product.quantity <= Product.min_stock).all()
        for p in low_stock_products:
            alerts.append({
                'name': p.name,
                'category': p.category,
                'current': p.quantity,
                'min': p.min_stock
            })
    except Exception as e:
        print(f"Alerts Error: {e}")

    # --- 2. FETCH RECENT SALES ---
    sales_data = []
    today_revenue = 0.0
    today_pending_count = 0
    try:
        recent_sales_objs = Sale.query.order_by(Sale.date.desc()).limit(5).all()
        for s in recent_sales_objs:
            # Fix: fallback to sum of items if grand_total is None
            total = s.grand_total if s.grand_total is not None else sum(i.total_price for i in s.items)
            status = s.payment_status or 'Payment Not Received'

            if status == 'Payment Received':
                status_label = 'Paid'
                status_class = 'status-received'
            elif status == 'Partial Payment':
                status_label = 'Partial'
                status_class = 'status-pending'
            else:
                status_label = 'Unpaid'
                status_class = 'status-unpaid'

            sales_data.append({
                'date': s.date.strftime('%d %b %Y'),
                'client': s.client_name,
                'total': total,
                'status_label': status_label,
                'status_class': status_class
            })

        # Today's Stats — use IST (UTC+5:30)
        today = (datetime.datetime.utcnow() + datetime.timedelta(hours=5, minutes=30)).date()
        today_sales = Sale.query.filter(func.date(Sale.date) == today).all()
        for s in today_sales:
            total = s.grand_total if s.grand_total is not None else sum(i.total_price for i in s.items)
            today_revenue += total
            if s.payment_status != 'Payment Received':
                today_pending_count += 1

    except Exception as e:
        print(f"Sales Data Error: {e}")

    # --- 3. FETCH RECENT PURCHASES ---
    purchases_data = []
    try:
        recent_purchases_objs = Purchase.query.order_by(Purchase.date.desc()).limit(5).all()
        for p in recent_purchases_objs:
            qty_display = 0
            if p.product_name:
                matches = re.findall(r'\(x(\d+)\)', p.product_name)
                qty_display = sum(int(m) for m in matches)
                if qty_display == 0: qty_display = 1

            purchases_data.append({
                'date': p.date.strftime('%d %b %Y'),
                'supplier': p.supplier_name,
                'qty': qty_display,
                'status': p.status
            })
    except Exception as e:
        print(f"Purchase Data Error: {e}")

    today_stats = {
        'revenue': today_revenue,
        'pending_bills': today_pending_count,
        'low_stock_count': len(alerts)
    }

    return render_template('dashboard.html', alerts=alerts, sales=sales_data, purchases=purchases_data, user=current_user, today_stats=today_stats)


@inventory_bp.route("/inventory")    
@login_required
def inventory():
    try:
        all_products = Product.query.all()
        inventory_summary = []
        alerts = []
        categories = sorted(list(set([p.category for p in all_products if p.category])))
        suppliers_list = Supplier.query.order_by(Supplier.name).all()

        for p in all_products:
            sup_name = p.supplier.name if p.supplier else "Unknown"
            inventory_summary.append({
                "Category": p.category, 
                "Product Name": p.name, 
                "Supplier": sup_name, 
                "Current Stock": p.quantity,
                "Min Stock": p.min_stock,
                "HSN": p.hsn_code,
                "Barcode": p.barcode,
                "MRP": p.mrp 
            })
            if p.quantity < p.min_stock:
                needed = p.max_stock - p.quantity
                alerts.append({
                    "product_name": p.name, "status": "Low Stock", "current": p.quantity, 
                    "msg": f"Below Min ({p.min_stock})", "deficit": needed
                })
        
        purchases = Purchase.query.order_by(Purchase.id.desc()).limit(50).all()
        purchase_log = []
        for p in purchases:
             purchase_log.append({
                'Purchase ID': p.id, 'Name': p.supplier_name, 'Category': p.category, 
                'Product Name': p.product_name, 'Qty': p.qty_purchased, 'Cost': p.unit_price, 
                'Date': p.date.strftime('%Y-%m-%d %H:%M'), 'Status': p.status
            })
        
        return render_template('inventory.html', alerts=alerts, summary=inventory_summary, purchase_log=purchase_log, categories=categories, suppliers=suppliers_list)
    except Exception as e:
        print(f"Inventory Error: {e}"); return render_template('inventory.html', alerts=[], summary=[], purchase_log=[])

@inventory_bp.route("/purchase")
@login_required
def purchase():
    if session.get("role") not in ["admin", "purchase"]:
        flash("Access Denied: Purchase Area is restricted.", "danger")
        return redirect(url_for('inventory.dashboard'))
    
    suppliers = Supplier.query.order_by(Supplier.name).all()
    products = Product.query.all()
    categories = sorted(list(set([p.category for p in products if p.category])))
    auto_supplier = request.args.get('selected_supplier'); auto_product = request.args.get('selected_product')
    return render_template('purchase.html', suppliers=suppliers, categories=categories, auto_supplier=auto_supplier, auto_product=auto_product)

@inventory_bp.route("/process_purchase", methods=["POST"])
@login_required
def process_purchase():
    try:
        supplier_id_form = request.form.get('supplier_id')
        cart_json = request.form.get('purchase_cart')
        
        if not cart_json: return redirect(url_for('inventory.purchase'))
        
        items = json.loads(cart_json)
        supplier_groups = defaultdict(list)
        
        for item in items:
            qty = int(item['qty'])
            if qty <= 0: continue
            
            product = None
            if 'id' in item and item['id']: 
                product = db.session.get(Product, item['id'])
            
            if not product: continue 
            
            new_p_price = float(item.get('purchase_price') or 0.0)
            new_s_price = float(item.get('sales_price') or 0.0)
            product.purchase_price = new_p_price
            product.mrp = new_s_price
            db.session.add(product)
            
            sup_id = product.supplier_id or supplier_id_form
            if not sup_id: continue 
            
            gst_rate = product.gst_rate or 0.0
            unit_cost_with_tax = new_p_price * (1 + gst_rate/100)
            
            supplier_groups[sup_id].append({
                'product': product, 
                'qty': qty, 
                'cost': unit_cost_with_tax
            })
            
        for sup_id, group_items in supplier_groups.items():
            supplier = db.session.get(Supplier, sup_id)
            sup_name = supplier.name if supplier else "Unknown"
            
            total_qty = sum(i['qty'] for i in group_items)
            total_cost = sum(i['cost'] * i['qty'] for i in group_items)
            
            name_list = []
            for i in group_items:
                p_name = i['product'].purchase_name if i['product'].purchase_name else i['product'].name
                name_list.append(f"{p_name} (x{i['qty']})")
                
            product_names_str = " || ".join(name_list)

            cat = group_items[0]['product'].category if len(group_items) == 1 else "Mixed Order"
            
            new_p = Purchase(
                supplier_name=sup_name, 
                supplier_id=sup_id, 
                category=cat, 
                product_name=product_names_str,
                product_id=None, 
                qty_purchased=total_qty, 
                unit_price=total_cost, 
                status='Pending'
            )
            db.session.add(new_p)
            
        db.session.commit()
        flash("Purchase(s) recorded as Pending.", "success")
        
    except Exception as e: 
        db.session.rollback()
        flash(f"Error: {e}", "danger")
        
    return redirect(url_for('inventory.purchase_log'))

@inventory_bp.route("/purchase_log")
@login_required
def purchase_log():
    if session.get("role") not in ['purchase', 'admin']:
        flash("Access Denied: Purchase Logs are restricted.", "danger")
        return redirect(url_for('inventory.dashboard'))
    
    purchases = Purchase.query.order_by(Purchase.date.desc()).all()
    
    pending_purchases = defaultdict(list)
    received_purchases = defaultdict(list)
    
    for p in purchases:
        date_key = p.date.strftime('%d %b %Y')
        items_parsed = []
        if p.product_name:
            raw_items = p.product_name.split(' || ') if ' || ' in p.product_name else p.product_name.split(', ')
            for r in raw_items:
                if ' (x' in r:
                    name_part, qty_part = r.rsplit(' (x', 1)
                    qty = qty_part.rstrip(')')
                    prod = Product.query.filter((Product.name == name_part) | (Product.purchase_name == name_part)).first()
                    cat = prod.category if prod else "-"
                    items_parsed.append({'Product Name': name_part, 'Qty': qty, 'Category': cat})
                else:
                    items_parsed.append({'Product Name': r, 'Qty': '?', 'Category': '-'})

        rcv_date_str = p.received_date.strftime('%d %b %Y, %I:%M %p') if p.received_date else None

        p_data = {
            'id': p.id,
            'date': date_key,
            'supplier': p.supplier_name,
            'status': p.status,
            'received_date': rcv_date_str,
            'items': items_parsed
        }
        
        if p.status == 'Received':
            received_purchases[date_key].append(p_data)
        else:
            pending_purchases[date_key].append(p_data)

    return render_template('purchase_log.html', pending_purchases=pending_purchases, received_purchases=received_purchases)

@inventory_bp.route("/update_product_inline", methods=["POST"])
@login_required
def update_product_inline():
    data = request.get_json()
    product_id = data.get('id')
    field = data.get('field')
    value = data.get('value')

    product = db.session.get(Product, product_id)
    if product:
        try:
            val_int = int(value)
            if field == 'quantity':
                product.quantity = val_int
            elif field == 'min_stock':
                product.min_stock = val_int
            elif field == 'max_stock':
                product.max_stock = val_int
            db.session.commit()
            return jsonify({'success': True})
        except ValueError:
            return jsonify({'success': False, 'error': 'Invalid number'})
    return jsonify({'success': False, 'error': 'Product not found'})

@inventory_bp.route("/add_supplier", methods=["POST"])
@login_required
def add_supplier():
    name = request.form.get('supplier_name')
    if name and not Supplier.query.filter_by(name=name).first(): 
        db.session.add(Supplier(name=name))
        db.session.commit()
        flash(f"Supplier '{name}' added.", "success")
    return redirect(url_for('inventory.purchase'))

@inventory_bp.route("/add_product_to_supplier", methods=["POST"])
@login_required
def add_product_to_supplier():
    try:
        supplier_id = request.form.get('modal_supplier_id')
        sales_name = request.form.get('product_name')
        purchase_name = request.form.get('purchase_name')
        
        category = request.form.get('category')
        unit = request.form.get('unit')
        p_price = request.form.get('purchase_price') or 0.0
        s_price = request.form.get('sales_price') or 0.0
        min_s = request.form.get('min_stock') or 10
        max_s = request.form.get('max_stock') or 100
        hsn = request.form.get('hsn_code')
        barcode = request.form.get('barcode') 
        gst = request.form.get('gst_rate') or 0.0
        
        has_sub = True if request.form.get('has_subcategory') == 'on' else False
        sub_type = request.form.get('subcategory_type') if has_sub else None
        sub_opts = request.form.get('subcategory_options') if has_sub else None
        
        if supplier_id and sales_name:
            final_p_name = purchase_name if purchase_name else sales_name
            
            new_prod = Product(
                name=sales_name, 
                purchase_name=final_p_name, 
                category=category, unit=unit, 
                purchase_price=float(p_price), mrp=float(s_price), 
                min_stock=int(min_s), max_stock=int(max_s), 
                hsn_code=hsn, gst_rate=float(gst),
                barcode=barcode, has_subcategory=has_sub, 
                subcategory_type=sub_type, subcategory_options=sub_opts, 
                supplier_id=supplier_id, quantity=0
            )
            db.session.add(new_prod)
            db.session.commit()
            flash(f"Product '{sales_name}' added.", "success")
            return redirect(url_for('inventory.purchase', selected_supplier=supplier_id))
            
    except Exception as e: 
        flash(f"Error: {e}", "danger")
    return redirect(url_for('inventory.purchase'))

@inventory_bp.route("/update_purchase_status", methods=["POST"])
@login_required
def update_purchase_status():
    try:
        pid = request.form.get('purchase_id')
        new_status = request.form.get('new_value')
        custom_time_str = request.form.get('received_time')

        purchase = db.session.get(Purchase, pid)
        
        if purchase and purchase.status != new_status:
            
            if new_status == 'Received':
                if custom_time_str:
                    purchase.received_date = datetime.datetime.strptime(custom_time_str, '%Y-%m-%dT%H:%M')
                else:
                    purchase.received_date = datetime.datetime.utcnow()
            else:
                purchase.received_date = None

            if purchase.product_name:
                if ' || ' in purchase.product_name:
                    items_list = purchase.product_name.split(' || ')
                else:
                    items_list = purchase.product_name.split(', ')
                
                for item_str in items_list:
                    if ' (x' in item_str:
                        try:
                            name_part, qty_part = item_str.rsplit(' (x', 1)
                            qty = int(qty_part.replace(')', '').strip())
                            name_clean = name_part.strip()
                            
                            product = None
                            product = Product.query.filter(Product.name == name_clean).first()
                            if not product: product = Product.query.filter(Product.purchase_name == name_clean).first()
                            if not product: product = Product.query.filter(Product.name.ilike(name_clean)).first()
                            if not product: product = Product.query.filter(Product.purchase_name.ilike(name_clean)).first()
                            if not product: product = Product.query.filter(Product.name.ilike(f"%{name_clean}%")).first()

                            if product:
                                if new_status == 'Received':
                                    product.quantity += qty
                                    product.last_purchased_date = purchase.received_date
                                
                                elif new_status == 'Pending' and purchase.status == 'Received':
                                    product.quantity -= qty
                                    product.last_purchased_date = None
                                    
                                db.session.add(product)
                            else:
                                print(f"WARNING: Product '{name_clean}' not found in DB.")
                                
                        except Exception as parse_error:
                            print(f"Error parsing item '{item_str}': {parse_error}")
                            continue

            purchase.status = new_status
            db.session.commit()
            flash(f"Purchase updated to {new_status}.", "success")
            
    except Exception as e:
        db.session.rollback()
        print(f"Error: {e}")
        flash(f"Error: {e}", "danger")
        
    return redirect(url_for('inventory.purchase_log'))