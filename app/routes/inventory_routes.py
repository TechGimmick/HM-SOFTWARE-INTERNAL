from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, session, make_response
from flask_login import login_required, current_user
from app.extensions import db
from app.models import Product, Supplier, Purchase, Sale, SaleItem, Customer, User, Warehouse, WarehouseStock, StockTransfer
from app.activity_service import log_activity
from sqlalchemy import func
from sqlalchemy.orm import joinedload
import datetime
import re
import json
from collections import defaultdict

inventory_bp = Blueprint('inventory', __name__)

@inventory_bp.route("/dashboard")
@login_required
def dashboard():
    # Fast render — no DB queries here. All data is loaded via /dashboard_data after page load.
    return render_template('dashboard.html', user=current_user)


@inventory_bp.route("/dashboard_data")
@login_required
def dashboard_data():
    """API endpoint — returns recent sales & purchases as JSON. No product queries."""
    sales_data = []
    purchases_data = []
    today_revenue = 0.0
    today_pending_count = 0
    store_retail_count = 0
    store_invoice_count = 0

    if current_user.role == 'store':
        try:
            from app.models import RetailOrder, SupplierOrder
            store_retail_count = RetailOrder.query.filter_by(status='Pending').count()
            store_invoice_count = SupplierOrder.query.filter(SupplierOrder.status.in_(['Draft', 'Packing', 'Packed'])).count()
        except Exception as e:
            print(f"Store Data Error: {e}")

    try:
        recent_sales_objs = Sale.query.order_by(Sale.date.desc()).limit(5).all()
        for s in recent_sales_objs:
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

        today = (datetime.datetime.utcnow() + datetime.timedelta(hours=5, minutes=30)).date()
        today_sales = Sale.query.filter(func.date(Sale.date) == today).all()
        for s in today_sales:
            total = s.grand_total if s.grand_total is not None else sum(i.total_price for i in s.items)
            today_revenue += total
            if s.payment_status != 'Payment Received':
                today_pending_count += 1
    except Exception as e:
        print(f"Sales Data Error: {e}")

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

    return jsonify({
        'sales': sales_data,
        'purchases': purchases_data,
        'today_stats': {
            'revenue': today_revenue,
            'pending_bills': today_pending_count,
            'store_retail_count': store_retail_count,
            'store_invoice_count': store_invoice_count
        },
        'role': current_user.role
    })


@inventory_bp.route("/inventory")    
@login_required
def inventory():
    try:
        all_products = Product.query.options(joinedload(Product.supplier)).all()
        # Query all warehouses and warehouse stocks
        all_warehouses = Warehouse.query.order_by(Warehouse.name).all()
        stocks = WarehouseStock.query.all()
        
        # Build a dictionary {product_id: {warehouse_name: quantity}}
        stock_map = defaultdict(dict)
        for ws in stocks:
            w_name = next((w.name for w in all_warehouses if w.id == ws.warehouse_id), "Unknown")
            stock_map[ws.product_id][w_name] = ws.quantity

        inventory_summary = []
        alerts = []
        categories = sorted(list(set([p.category for p in all_products if p.category])))
        suppliers_list = Supplier.query.order_by(Supplier.name).all()

        for p in all_products:
            sup_name = p.supplier.name if p.supplier else "Unknown"
            
            # Recalculate total quantity dynamically to be safe
            p_total_qty = sum(stock_map[p.id].values()) if p.id in stock_map else p.quantity
            
            inventory_summary.append({
                "Product ID": p.id,
                "Category": p.category, 
                "Product Name": p.name, 
                "Supplier": sup_name, 
                "Current Stock": p_total_qty,
                "Min Stock": p.min_stock,
                "HSN": p.hsn_code,
                "Barcode": p.barcode,
                "MRP": p.mrp,
                "WarehouseStocks": stock_map.get(p.id, {})
            })
            if p_total_qty < p.min_stock:
                needed = p.max_stock - p_total_qty
                alerts.append({
                    "product_name": p.name, "status": "Low Stock", "current": p_total_qty, 
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
        
        return render_template('inventory.html', alerts=alerts, summary=inventory_summary, purchase_log=purchase_log, categories=categories, suppliers=suppliers_list, warehouses=all_warehouses)

    except Exception as e:
        print(f"Inventory Error: {e}"); return render_template('inventory.html', alerts=[], summary=[], purchase_log=[])



@inventory_bp.route("/purchase")
@login_required
def purchase():
    if session.get("role") not in ["admin", "purchase"]:
        flash("Access Denied: Purchase Area is restricted.", "danger")
        return redirect(url_for('inventory.dashboard'))
    # Zero DB queries — suppliers and categories load via JS after page render
    auto_supplier = request.args.get('selected_supplier')
    auto_product = request.args.get('selected_product')
    return render_template('purchase.html', auto_supplier=auto_supplier, auto_product=auto_product)


@inventory_bp.route("/get_suppliers")
@login_required
def get_suppliers():
    """Lightweight API: returns supplier list as JSON for JS dropdowns."""
    suppliers = Supplier.query.order_by(Supplier.name).with_entities(Supplier.id, Supplier.name).all()
    return jsonify([{'id': s.id, 'name': s.name} for s in suppliers])


@inventory_bp.route("/get_categories")
@login_required
def get_categories():
    """Lightweight API: returns distinct category names as JSON for JS dropdowns."""
    raw = db.session.query(Product.category).distinct().order_by(Product.category).all()
    return jsonify(sorted([c[0] for c in raw if c[0]]))

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
        log_activity('CREATE', 'Purchase', f'New PO from {sup_name} — {sum(len(g) for g in supplier_groups.values())} item(s)', ref_type='Purchase')
        db.session.commit()   # single atomic commit: PO + log together
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

    date_range = request.args.get('date_range', '30')
    supplier_filter = request.args.get('supplier', '')
    status_filter = request.args.get('status', 'all')

    query = Purchase.query
    today = datetime.date.today()
    if date_range != 'all':
        try:
            days = int(date_range)
            cutoff = datetime.datetime.combine(today - datetime.timedelta(days=days), datetime.time.min)
            query = query.filter(Purchase.date >= cutoff)
        except ValueError:
            pass
    if supplier_filter:
        query = query.filter(Purchase.supplier_name == supplier_filter)

    purchases = query.order_by(Purchase.date.desc()).all()
    all_suppliers = db.session.query(Purchase.supplier_name).distinct().order_by(Purchase.supplier_name).all()
    supplier_names = [s[0] for s in all_suppliers if s[0]]

    import json

    def parse_items_from_po(p):
        if p.status == 'Partial Received' and p.received_details:
            details = json.loads(p.received_details)
            pending_items, received_items, total_damaged = [], [], 0
            for item in details:
                prod = Product.query.filter((Product.name == item.get('name')) | (Product.purchase_name == item.get('name'))).first()
                cat = prod.category if prod else "-"
                unit = prod.unit if prod else "-"
                ord_qty = item.get('ordered_qty', 0)
                good = item.get('good_qty', 0)
                dmg = item.get('damaged_qty', 0)
                pending_qty = ord_qty - (good + dmg)
                if pending_qty > 0:
                    pending_items.append({'Product Name': item.get('name'), 'Qty': str(pending_qty), 'Category': cat, 'Unit': unit})
                if good > 0 or dmg > 0:
                    qty_str = f"{good + dmg}/{ord_qty}"
                    if dmg > 0:
                        qty_str += f" ({dmg} Dmg)"
                        total_damaged += dmg
                    received_items.append({'Product Name': item.get('name'), 'Qty': qty_str, 'Category': cat, 'Unit': unit})
            return pending_items, received_items, total_damaged

        items_parsed, total_damaged = [], 0
        if p.received_details:
            details = json.loads(p.received_details)
            for item in details:
                prod = Product.query.filter((Product.name == item.get('name')) | (Product.purchase_name == item.get('name'))).first()
                cat = prod.category if prod else "-"
                unit = prod.unit if prod else "-"
                qty_str = str(item.get('ordered_qty', 0))
                if p.status == 'Received':
                    good = item.get('good_qty', 0)
                    dmg = item.get('damaged_qty', 0)
                    ord_qty = item.get('ordered_qty', 0)
                    qty_str = f"{good + dmg}/{ord_qty}"
                    if dmg > 0:
                        qty_str += f" ({dmg} Dmg)"
                        total_damaged += dmg
                items_parsed.append({'Product Name': item.get('name'), 'Qty': qty_str, 'Category': cat, 'Unit': unit})
        elif p.product_name:
            raw_items = p.product_name.split(' || ') if ' || ' in p.product_name else p.product_name.split(', ')
            for r in raw_items:
                if ' (x' in r:
                    name_part, qty_part = r.rsplit(' (x', 1)
                    qty = qty_part.rstrip(')')
                    prod = Product.query.filter((Product.name == name_part) | (Product.purchase_name == name_part)).first()
                    cat = prod.category if prod else "-"
                    unit = prod.unit if prod else "-"
                    items_parsed.append({'Product Name': name_part, 'Qty': qty, 'Category': cat, 'Unit': unit})
                else:
                    items_parsed.append({'Product Name': r, 'Qty': '?', 'Category': '-', 'Unit': '-'})
        return items_parsed, [], total_damaged

    all_pos = []
    stats = {'total': 0, 'pending': 0, 'partial': 0, 'received': 0, 'total_damaged': 0}

    for p in purchases:
        date_key = p.date.strftime('%d %b %Y')
        rcv_date_str = p.received_date.strftime('%d %b %Y, %I:%M %p') if p.received_date else None
        pending_items, received_items, total_damaged = parse_items_from_po(p)
        stats['total_damaged'] += total_damaged
        stats['total'] += 1

        if p.status == 'Partial Received':
            if pending_items:
                all_pos.append({'id': p.id, 'date': date_key, 'supplier': p.supplier_name,
                    'status': 'Partial Pending', 'received_date': None, 'line_items': pending_items,
                    'contact_info': '', 'total_damaged': 0, 'item_count': len(pending_items)})
                stats['pending'] += 1
            if received_items:
                all_pos.append({'id': p.id, 'date': date_key, 'supplier': p.supplier_name,
                    'status': 'Partial Received', 'received_date': rcv_date_str, 'line_items': received_items,
                    'contact_info': '', 'total_damaged': total_damaged, 'item_count': len(received_items)})
                stats['partial'] += 1
        else:
            all_pos.append({'id': p.id, 'date': date_key, 'supplier': p.supplier_name,
                'status': p.status, 'received_date': rcv_date_str,
                'line_items': pending_items if pending_items else received_items,
                'contact_info': '', 'total_damaged': total_damaged,
                'item_count': len(pending_items) if pending_items else len(received_items)})
            if p.status == 'Received':
                stats['received'] += 1
            else:
                stats['pending'] += 1

    status_map = {'pending': ['Pending','Partial Pending'], 'partial': ['Partial Received','Partial Pending'], 'received': ['Received','Partial Received']}
    if status_filter in status_map:
        all_pos = [po for po in all_pos if po['status'] in status_map[status_filter]]

    return render_template('purchase_log.html', all_pos=all_pos, stats=stats,
                           supplier_names=supplier_names, active_date_range=date_range,
                           active_supplier=supplier_filter, active_status=status_filter,
                           warehouses=Warehouse.query.order_by(Warehouse.name).all())

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
            elif field == 'pack_size':
                product.pack_size = val_int
            field_labels = {
                'quantity': 'Stock qty', 'min_stock': 'Min stock',
                'max_stock': 'Max stock', 'pack_size': 'Pack size'
            }
            label = field_labels.get(field, field)
            log_activity('UPDATE', 'Inventory',
                         f'{label} changed to {val_int} for "{product.name}"',
                         ref_id=product.id, ref_type='Product')
            db.session.commit()   # single atomic commit: field change + log
            return jsonify({'success': True})
        except ValueError:
            return jsonify({'success': False, 'error': 'Invalid number'})
    return jsonify({'success': False, 'error': 'Product not found'})

@inventory_bp.route("/add_supplier", methods=["POST"])
@login_required
def add_supplier():
    name = request.form.get('supplier_name')
    if name and not Supplier.query.filter_by(name=name).first():
        supplier = Supplier(name=name)
        db.session.add(supplier)
        db.session.flush()   # get ID before commit
        log_activity('CREATE', 'Inventory', f'New supplier "{name}" added',
                     ref_id=supplier.id, ref_type='Supplier')
        db.session.commit()  # single atomic commit: supplier + log together
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
        pack_size = request.form.get('pack_size') or 1
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
                pack_size=int(pack_size),
                hsn_code=hsn, gst_rate=float(gst),
                barcode=barcode, has_subcategory=has_sub, 
                subcategory_type=sub_type, subcategory_options=sub_opts, 
                supplier_id=supplier_id, quantity=0
            )
            db.session.add(new_prod)
            log_activity('CREATE', 'Inventory', f'New product "{sales_name}" added', ref_id=new_prod.id, ref_type='Product')
            db.session.commit()   # single atomic commit: product + log together
            flash(f"Product '{sales_name}' added.", "success")
            return redirect(url_for('inventory.purchase', selected_supplier=supplier_id))
            
    except Exception as e: 
        flash(f"Error: {e}", "danger")
    return redirect(url_for('inventory.purchase'))

import json

@inventory_bp.route("/update_purchase_status", methods=["POST"])
@login_required
def update_purchase_status():
    try:
        pid = request.form.get('purchase_id')
        new_status_req = request.form.get('new_value')
        custom_time_str = request.form.get('received_time')
        warehouse_id = request.form.get('warehouse_id')
        item_count = int(request.form.get('item_count', 0))

        purchase = db.session.get(Purchase, pid)
        
        if not purchase:
            return redirect(url_for('inventory.purchase_log'))
            
        if purchase.status == new_status_req and new_status_req != 'Received':
            return redirect(url_for('inventory.purchase_log'))
            
        if new_status_req == 'Pending' and purchase.status in ['Received', 'Partial Received']:
            if purchase.received_details:
                details = json.loads(purchase.received_details)
                for item in details:
                    name_clean = item.get('name')
                    qty_added = item.get('good_qty', 0)
                    if qty_added > 0:
                        product = Product.query.filter(Product.name == name_clean).first()
                        if not product: product = Product.query.filter(Product.purchase_name == name_clean).first()
                        if not product: product = Product.query.filter(Product.name.ilike(name_clean)).first()
                        if not product: product = Product.query.filter(Product.purchase_name.ilike(name_clean)).first()
                        
                        if product:
                            product.quantity -= qty_added
                            
                            # Determine previous date if any
                            history_pos = Purchase.query.filter(
                                Purchase.status.in_(['Received', 'Partial Received']),
                                Purchase.id != pid,
                                Purchase.received_date != None
                            ).order_by(Purchase.received_date.desc()).all()
                            
                            found_prev_date = None
                            for h_po in history_pos:
                                if (product.name and product.name.lower() in h_po.product_name.lower()) or \
                                   (product.purchase_name and product.purchase_name.lower() in h_po.product_name.lower()) or \
                                   (name_clean.lower() in h_po.product_name.lower()):
                                    found_prev_date = h_po.received_date
                                    break 
                            product.last_purchased_date = found_prev_date
                            db.session.add(product)
            else:
                # Fallback for old orders
                if purchase.product_name:
                    items_list = purchase.product_name.split(' || ') if ' || ' in purchase.product_name else purchase.product_name.split(', ')
                    for item_str in items_list:
                        if ' (x' in item_str:
                            try:
                                name_part, qty_part = item_str.rsplit(' (x', 1)
                                qty = int(qty_part.replace(')', '').strip())
                                name_clean = name_part.strip()
                                product = Product.query.filter(Product.name == name_clean).first()
                                if not product: product = Product.query.filter(Product.purchase_name == name_clean).first()
                                if product:
                                    product.quantity -= qty
                                    db.session.add(product)
                            except Exception:
                                pass
                                
            purchase.status = 'Pending'
            purchase.received_date = None
            purchase.received_details = None
            log_activity('UPDATE', 'Purchase', f'PO #{pid} reverted to Pending', ref_id=int(pid) if pid else None, ref_type='Purchase')
            db.session.commit()   # single atomic commit: status + log together
            flash("Purchase reverted to Pending.", "success")
            
        elif new_status_req == 'Received':
            is_partial = False
            
            if custom_time_str:
                purchase.received_date = datetime.datetime.strptime(custom_time_str, '%Y-%m-%dT%H:%M')
            else:
                purchase.received_date = datetime.datetime.utcnow()

            if item_count > 0:
                # Load existing details if any to accumulate
                existing_details = []
                if purchase.received_details:
                    existing_details = json.loads(purchase.received_details)
                    
                # Create a map for easy updating
                details_map = { d['name']: d for d in existing_details }
                
                for i in range(item_count):
                    name = request.form.get(f'item_name_{i}')
                    received_qty_input = int(request.form.get(f'received_qty_{i}', 0))
                    damaged_qty_input = int(request.form.get(f'damaged_qty_{i}', 0))
                    
                    if damaged_qty_input > received_qty_input:
                        damaged_qty_input = received_qty_input
                        
                    good_qty = received_qty_input - damaged_qty_input
                    damaged_qty = damaged_qty_input
                    
                    if name in details_map:
                        details_map[name]['good_qty'] += good_qty
                        details_map[name]['damaged_qty'] += damaged_qty
                    else:
                        ordered_qty = int(request.form.get(f'ordered_qty_{i}', 0))
                        details_map[name] = {
                            'name': name,
                            'ordered_qty': ordered_qty,
                            'good_qty': good_qty,
                            'damaged_qty': damaged_qty
                        }
                        
                    # Add to inventory
                    if good_qty > 0:
                        product = Product.query.filter(Product.name == name).first()
                        if not product: product = Product.query.filter(Product.purchase_name == name).first()
                        if not product: product = Product.query.filter(Product.name.ilike(name)).first()
                        
                        if product:
                            w_stock = None
                            if warehouse_id:
                                w_stock = WarehouseStock.query.filter_by(warehouse_id=warehouse_id, product_id=product.id).first()
                                if not w_stock:
                                    w_stock = WarehouseStock(warehouse_id=warehouse_id, product_id=product.id, quantity=0)
                                    db.session.add(w_stock)

                            product.quantity += good_qty
                            if w_stock: w_stock.quantity += good_qty
                            product.last_purchased_date = purchase.received_date
                            db.session.add(product)
                            
                # Re-evaluate is_partial based on ALL items
                final_details = list(details_map.values())
                for item in final_details:
                    if (item['good_qty'] + item['damaged_qty']) < item['ordered_qty']:
                        is_partial = True
                        
                purchase.received_details = json.dumps(final_details)
                purchase.status = 'Partial Received' if is_partial else 'Received'
                
            else:
                # Old fallback logic if item_count == 0
                purchase.status = 'Received'
                if purchase.product_name:
                    items_list = purchase.product_name.split(' || ') if ' || ' in purchase.product_name else purchase.product_name.split(', ')
                    for item_str in items_list:
                        if ' (x' in item_str:
                            try:
                                name_part, qty_part = item_str.rsplit(' (x', 1)
                                qty = int(qty_part.replace(')', '').strip())
                                name_clean = name_part.strip()
                                product = Product.query.filter(Product.name == name_clean).first()
                                if not product: product = Product.query.filter(Product.purchase_name == name_clean).first()
                                if product:
                                    product.quantity += qty
                                    product.last_purchased_date = purchase.received_date
                                    db.session.add(product)
                            except Exception:
                                pass
                                
            status_snap = purchase.status   # snapshot before commit expires the object
            log_activity('UPDATE', 'Purchase', f'PO #{pid} updated to {status_snap}', ref_id=int(pid) if pid else None, ref_type='Purchase')
            db.session.commit()   # single atomic commit: status + log together
            flash(f"Purchase updated to {status_snap}.", "success")
            
    except Exception as e:
        db.session.rollback()
        print(f"Error: {e}")
        flash(f"Error: {e}", "danger")
        
    return redirect(url_for('inventory.purchase_log'))

@inventory_bp.route("/add_warehouse", methods=["POST"])
@login_required
def add_warehouse():
    try:
        if current_user.role not in ['admin', 'manager', 'purchase']:
            flash("Access Denied.", "danger")
            return redirect(url_for('inventory.inventory'))
            
        name = request.form.get('warehouse_name')
        location = request.form.get('warehouse_location')
        if name and not Warehouse.query.filter_by(name=name).first():
            wh = Warehouse(name=name, location=location)
            db.session.add(wh)
            db.session.flush()   # get ID before commit
            log_activity('CREATE', 'Inventory',
                         f'New warehouse "{name}" added' + (f' ({location})' if location else ''),
                         ref_id=wh.id, ref_type='Warehouse')
            db.session.commit()  # single atomic commit: warehouse + log
            flash(f"Warehouse '{name}' added successfully.", "success")
        else:
            flash("Warehouse name already exists or invalid.", "danger")
    except Exception as e:
        db.session.rollback()
        flash(f"Error adding warehouse: {e}", "danger")
    return redirect(url_for('inventory.inventory'))

@inventory_bp.route("/transfer_stock", methods=["POST"])
@login_required
def transfer_stock():
    try:
        product_id = request.form.get('product_id')
        from_wh_id = request.form.get('from_warehouse_id')
        to_wh_id = request.form.get('to_warehouse_id')
        qty = int(request.form.get('transfer_qty') or 0)
        
        transfer_date_str = request.form.get('transfer_date')
        if transfer_date_str:
            import datetime
            try:
                t_date = datetime.datetime.strptime(transfer_date_str, '%Y-%m-%dT%H:%M')
            except ValueError:
                t_date = datetime.datetime.strptime(transfer_date_str, '%Y-%m-%d')
        else:
            import datetime
            t_date = datetime.datetime.utcnow()
            
        delivery_method = request.form.get('delivery_method', '')
        
        if qty <= 0 or from_wh_id == to_wh_id:
            flash("Invalid transfer quantity or same warehouse selected.", "danger")
            return redirect(url_for('inventory.inventory'))
            
        from_stock = WarehouseStock.query.filter_by(warehouse_id=from_wh_id, product_id=product_id).first()
        to_stock = WarehouseStock.query.filter_by(warehouse_id=to_wh_id, product_id=product_id).first()
        
        if not from_stock or from_stock.quantity < qty:
            flash("Insufficient stock in source warehouse.", "danger")
            return redirect(url_for('inventory.inventory'))
            
        # Deduct from source
        from_stock.quantity -= qty
        
        # Add to destination
        if to_stock:
            to_stock.quantity += qty
        else:
            new_stock = WarehouseStock(warehouse_id=to_wh_id, product_id=product_id, quantity=qty)
            db.session.add(new_stock)
            
        # Log transfer
        transfer = StockTransfer(
            product_id=product_id,
            from_warehouse_id=from_wh_id,
            to_warehouse_id=to_wh_id,
            quantity=qty,
            date=t_date,
            delivery_method=delivery_method,
            reference=request.form.get('reference', '')
        )
        db.session.add(transfer)

        # Build log message while objects are still in-session (before commit)
        product_obj = db.session.get(Product, product_id)
        from_wh_obj = db.session.get(Warehouse, from_wh_id)
        to_wh_obj   = db.session.get(Warehouse, to_wh_id)
        p_name   = product_obj.name if product_obj else str(product_id)
        from_name = from_wh_obj.name if from_wh_obj else '?'
        to_name   = to_wh_obj.name   if to_wh_obj   else '?'
        log_activity('UPDATE', 'Inventory', f'Stock transfer: {qty}× {p_name} from {from_name} → {to_name}', ref_type='StockTransfer')
        db.session.commit()   # single atomic commit: transfer + log together
        flash(f"Successfully transferred {qty} units.", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"Error transferring stock: {e}", "danger")
    return redirect(url_for('inventory.inventory'))

@inventory_bp.route("/api/last_transfer/<int:product_id>")
@login_required
def get_last_transfer(product_id):
    last_transfer = StockTransfer.query.filter_by(product_id=product_id).order_by(StockTransfer.date.desc()).first()
    if last_transfer:
        return jsonify({
            'found': True,
            'date': last_transfer.date.strftime('%d %b %Y'),
            'time': last_transfer.date.strftime('%I:%M %p'),
            'qty': last_transfer.quantity,
            'delivery_method': last_transfer.delivery_method or 'None',
            'reference': last_transfer.reference or 'None',
            'from_warehouse': last_transfer.from_warehouse.name if last_transfer.from_warehouse else 'N/A',
            'to_warehouse': last_transfer.to_warehouse.name if last_transfer.to_warehouse else 'N/A'
        })
    return jsonify({'found': False})

@inventory_bp.route("/edit_warehouse", methods=["POST"])
@login_required
def edit_warehouse():
    try:
        if current_user.role not in ['admin', 'manager', 'purchase']:
            flash("Access Denied.", "danger")
            return redirect(url_for('inventory.inventory'))
            
        wh_id = request.form.get('warehouse_id')
        name = request.form.get('warehouse_name')
        location = request.form.get('warehouse_location')
        
        warehouse = db.session.get(Warehouse, wh_id)
        if warehouse:
            old_name = warehouse.name
            warehouse.name = name
            warehouse.location = location
            log_activity('UPDATE', 'Inventory',
                         f'Warehouse "{old_name}" renamed/updated to "{name}"' + (f' ({location})' if location else ''),
                         ref_id=int(wh_id), ref_type='Warehouse')
            db.session.commit()  # single atomic commit: warehouse update + log
            flash(f"Warehouse updated successfully.", "success")
        else:
            flash("Warehouse not found.", "danger")
    except Exception as e:
        db.session.rollback()
        flash(f"Error updating warehouse: {e}", "danger")
    return redirect(url_for('inventory.inventory'))