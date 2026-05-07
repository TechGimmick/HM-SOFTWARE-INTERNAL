from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, make_response
from fpdf import FPDF
from flask_login import login_required, current_user
from app.extensions import db
from app.models import Product, Supplier, Purchase, Warehouse, WarehouseStock
from collections import defaultdict
import json
import datetime
import re

# Define the Blueprint
purchase_bp = Blueprint('purchase', __name__)

# --- CUSTOM PDF CLASS FOR FOOTER ---
class PO_PDF(FPDF):
    def footer(self):
        # Position at 3.0 cm from bottom
        self.set_y(-30)
        self.set_font('Helvetica', 'B', 10)
        
        # Signature Block
        self.cell(0, 5, "For Safe Environment International", 0, 1, 'R')
        self.ln(10)
        self.cell(0, 5, "Authorized Signatory", 0, 1, 'R')
        
        # Page Number
        self.set_y(-15)
        self.set_font('Helvetica', 'I', 8)
        self.cell(0, 10, f'Page {self.page_no()}', 0, 0, 'C')

# --- HELPER FUNCTIONS ---
def product_to_dict_full(p):
    last_date = p.last_purchased_date.strftime('%d %b %Y, %I:%M %p') if p.last_purchased_date else None
    return {
        'id': p.id,
        'name': p.name,
        'category': p.category,
        'stock': p.quantity,
        'unit': p.unit,
        'min_stock': p.min_stock,
        'max_stock': p.max_stock,
        'purchase_price': p.purchase_price,
        'sales_price': p.mrp,
        'hsn_code': p.hsn_code,
        'gst_rate': p.gst_rate,
        'barcode': p.barcode,
        'pack_size': p.pack_size or 1,
        'last_purchased': last_date
    }

def generate_product_pdf(products, title, filename):
    pdf = FPDF(); pdf.add_page(); pdf.set_font("Helvetica", 'B', 16); pdf.cell(0, 10, title, align='C', ln=1); pdf.ln(5)
    pdf.set_font("Helvetica", 'B', 10); pdf.cell(10, 10, "SNo", 1, 0, 'C'); pdf.cell(75, 10, "Product Name", 1, 0, 'L'); pdf.cell(25, 10, "HSN", 1, 0, 'C'); pdf.cell(30, 10, "Cost Price", 1, 0, 'R'); pdf.cell(25, 10, "Current Qty", 1, 0, 'C'); pdf.cell(25, 10, "Min/Max", 1, 1, 'C')
    pdf.set_font("Helvetica", size=10)
    for i, p in enumerate(products, 1):
        x_start = pdf.get_x(); y_start = pdf.get_y()
        if y_start > 270: pdf.add_page(); y_start = pdf.get_y(); x_start = pdf.get_x()
        pdf.set_xy(x_start + 10, y_start); pdf.multi_cell(75, 8, str(p.name), 1, 'L')
        y_end = pdf.get_y(); row_height = y_end - y_start
        pdf.set_xy(x_start, y_start); pdf.cell(10, row_height, str(i), 1, 0, 'C')
        pdf.set_xy(x_start + 10 + 75, y_start); pdf.cell(25, row_height, str(p.hsn_code or '-'), 1, 0, 'C')
        pdf.set_xy(x_start + 10 + 75 + 25, y_start); pdf.cell(30, row_height, f"{p.purchase_price:.2f}", 1, 0, 'R')
        pdf.set_xy(x_start + 10 + 75 + 25 + 30, y_start); pdf.cell(25, row_height, str(p.quantity), 1, 0, 'C')
        pdf.set_xy(x_start + 10 + 75 + 25 + 30 + 25, y_start); pdf.cell(25, row_height, f"{p.min_stock}/{p.max_stock}", 1, 1, 'C')
        pdf.set_y(y_end)
    pdf_content = pdf.output(dest='S'); response_data = bytes(pdf_content) if isinstance(pdf_content, (bytes, bytearray)) else pdf_content.encode('latin-1')
    resp = make_response(response_data); resp.headers['Content-Type'] = 'application/pdf'; resp.headers['Content-Disposition'] = f'attachment; filename={filename}'
    return resp

# --- 1. PURCHASE CATALOG PAGE ---
@purchase_bp.route("/purchase", methods=["GET"])
@login_required
def purchase_page():
    if current_user.role not in ['purchase', 'admin']:
        flash("Access Denied: Purchase Area is restricted.", "danger")
        return redirect(url_for('inventory.dashboard'))
    auto_supplier = request.args.get('selected_supplier')
    auto_product = request.args.get('selected_product')
    edit_po_id = request.args.get('edit_po')
    edit_cart_json = None

    if edit_po_id:
        purchase = db.session.get(Purchase, edit_po_id)
        if purchase:
            cart = []
            if purchase.received_details:
                details = json.loads(purchase.received_details)
                for item in details:
                    prod = Product.query.filter((Product.name == item.get('name')) | (Product.purchase_name == item.get('name'))).first()
                    if prod:
                        cart.append({'id': prod.id, 'name': prod.name,
                            'purchase_price': prod.purchase_price or 0,
                            'sales_price': prod.mrp or 0,
                            'qty': item.get('ordered_qty', 0),
                            'unit': prod.unit or '', 'pack_size': prod.pack_size or 1})
            elif purchase.product_name:
                raw_items = purchase.product_name.split(' || ') if ' || ' in purchase.product_name else purchase.product_name.split(', ')
                for r in raw_items:
                    if ' (x' in r:
                        name_part, qty_part = r.rsplit(' (x', 1)
                        qty = int(qty_part.rstrip(')'))
                        prod = Product.query.filter((Product.name == name_part) | (Product.purchase_name == name_part)).first()
                        if prod:
                            cart.append({'id': prod.id, 'name': prod.name,
                                'purchase_price': prod.purchase_price or 0,
                                'sales_price': prod.mrp or 0,
                                'qty': qty, 'unit': prod.unit or '',
                                'pack_size': prod.pack_size or 1})
            if cart:
                edit_cart = cart   # pass raw list, Jinja tojson will handle serialization
                edit_supplier_id = purchase.supplier_id
            else:
                edit_cart = None
                edit_supplier_id = None
        else:
            edit_cart = None
            edit_supplier_id = None
    else:
        edit_cart = None
        edit_supplier_id = None

    return render_template('purchase.html',
                           auto_supplier=auto_supplier,
                           auto_product=auto_product,
                           edit_cart_json=edit_cart,
                           edit_po_id=edit_po_id,
                           edit_supplier_id=edit_supplier_id)

# --- 2. PROCESS PURCHASE (CREATE PO) ---
@purchase_bp.route("/process_purchase", methods=["POST"])
@login_required
def process_purchase():
    try:
        supplier_id_form = request.form.get('supplier_id')
        cart_json = request.form.get('purchase_cart')
        
        if not cart_json: return redirect(url_for('purchase.purchase_page'))
        
        items = json.loads(cart_json)
        supplier_groups = defaultdict(list)
        
        for item in items:
            qty = int(item['qty'])
            if qty <= 0: continue
            
            product = None
            if 'id' in item and item['id']: 
                product = db.session.get(Product, item['id'])
            
            if not product: continue 
            
            # Update Product Prices directly from PO
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
            
        edit_po_id = request.form.get('edit_po_id')
        
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
            
            existing_p = None
            if edit_po_id:
                existing_p = db.session.get(Purchase, edit_po_id)
            
            if existing_p and existing_p.supplier_id == sup_id:
                # Update existing PO
                existing_p.category = cat
                existing_p.product_name = product_names_str
                existing_p.qty_purchased = total_qty
                existing_p.unit_price = total_cost
                # Optional: if it was already received, does editing revert it? Let's leave status as is
                # or revert to Pending if they changed it? The user only asked to update the existing PO.
            else:
                new_p = Purchase(
                    supplier_name=sup_name, 
                    supplier_id=sup_id, 
                    category=cat, 
                    product_name=product_names_str,
                    product_id=None,
                    qty_purchased=total_qty, 
                    unit_price=total_cost, 
                    status='Waiting'   # New POs wait for admin approval
                )
                db.session.add(new_p)
            
        db.session.commit()
        if edit_po_id:
            flash(f"Purchase Order #{edit_po_id} updated successfully.", "success")
        else:
            flash("Purchase Order submitted — awaiting admin approval.", "success")
        
    except Exception as e: 
        db.session.rollback()
        flash(f"Error: {e}", "danger")
        
    return redirect(url_for('purchase.purchase_log'))

# --- 3. PURCHASE LOGS ---
@purchase_bp.route("/purchase_log")
@login_required
def purchase_log():
    if current_user.role not in ['purchase', 'admin']:
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
        """Returns (pending_items, received_items, total_damaged)."""
        if p.status == 'Partial Received' and p.received_details:
            details = json.loads(p.received_details)
            pending_items, received_items, total_damaged = [], [], 0
            for item in details:
                prod = Product.query.filter((Product.name == item.get('name')) | (Product.purchase_name == item.get('name'))).first()
                cat = prod.category if prod else "-"
                unit = prod.unit if prod else "-"
                price = prod.purchase_price if prod else 0
                ord_qty = item.get('ordered_qty', 0)
                good = item.get('good_qty', 0)
                dmg = item.get('damaged_qty', 0)
                pending_qty = ord_qty - (good + dmg)
                if pending_qty > 0:
                    pending_items.append({'Product Name': item.get('name'), 'Qty': str(pending_qty), 'Category': cat, 'Unit': unit, 'Price': price})
                if good > 0 or dmg > 0:
                    qty_str = f"{good + dmg}/{ord_qty}"
                    if dmg > 0:
                        qty_str += f" ({dmg} Dmg)"
                        total_damaged += dmg
                    received_items.append({'Product Name': item.get('name'), 'Qty': qty_str, 'Category': cat, 'Unit': unit, 'Price': price})
            return pending_items, received_items, total_damaged

        items_parsed = []
        total_damaged = 0
        if p.received_details:
            details = json.loads(p.received_details)
            for item in details:
                prod = Product.query.filter((Product.name == item.get('name')) | (Product.purchase_name == item.get('name'))).first()
                cat = prod.category if prod else "-"
                unit = prod.unit if prod else "-"
                price = prod.purchase_price if prod else 0
                qty_str = str(item.get('ordered_qty', 0))
                if p.status == 'Received':
                    good = item.get('good_qty', 0)
                    dmg = item.get('damaged_qty', 0)
                    ord_qty = item.get('ordered_qty', 0)
                    qty_str = f"{good + dmg}/{ord_qty}"
                    if dmg > 0:
                        qty_str += f" ({dmg} Dmg)"
                        total_damaged += dmg
                items_parsed.append({'Product Name': item.get('name'), 'Qty': qty_str, 'Category': cat, 'Unit': unit, 'Price': price})
        elif p.product_name:
            raw_items = p.product_name.split(' || ') if ' || ' in p.product_name else p.product_name.split(', ')
            for r in raw_items:
                if ' (x' in r:
                    name_part, qty_part = r.rsplit(' (x', 1)
                    qty = qty_part.rstrip(')')
                    prod = Product.query.filter((Product.name == name_part) | (Product.purchase_name == name_part)).first()
                    cat = prod.category if prod else "-"
                    unit = prod.unit if prod else "-"
                    price = prod.purchase_price if prod else 0
                    items_parsed.append({'Product Name': name_part, 'Qty': qty, 'Category': cat, 'Unit': unit, 'Price': price})
                else:
                    items_parsed.append({'Product Name': r, 'Qty': '?', 'Category': '-', 'Unit': '-', 'Price': 0})
        return items_parsed, [], total_damaged

    all_pos = []
    stats = {'total': 0, 'waiting': 0, 'approved': 0, 'partial': 0, 'received': 0, 'total_damaged': 0}

    for p in purchases:
        date_key = p.date.strftime('%d %b %Y')
        rcv_date_str = p.received_date.strftime('%d %b %Y, %I:%M %p') if p.received_date else None
        supplier_obj = db.session.get(Supplier, p.supplier_id) if p.supplier_id else None
        ci = getattr(supplier_obj, 'contact_info', '') if supplier_obj else ''
        contact_info = ci if isinstance(ci, str) else ''

        pending_items, received_items, total_damaged = parse_items_from_po(p)
        stats['total_damaged'] += total_damaged
        stats['total'] += 1

        if p.status == 'Partial Received':
            if pending_items:
                all_pos.append({'id': p.id, 'date': date_key, 'supplier': p.supplier_name,
                    'status': 'Partial Pending', 'received_date': None,
                    'line_items': pending_items, 'contact_info': contact_info,
                    'total_damaged': 0, 'item_count': len(pending_items)})
                stats['partial'] += 1
            if received_items:
                all_pos.append({'id': p.id, 'date': date_key, 'supplier': p.supplier_name,
                    'status': 'Partial Received', 'received_date': rcv_date_str,
                    'line_items': received_items, 'contact_info': contact_info,
                    'total_damaged': total_damaged, 'item_count': len(received_items)})
                stats['partial'] += 1
        else:
            all_pos.append({'id': p.id, 'date': date_key, 'supplier': p.supplier_name,
                'status': p.status, 'received_date': rcv_date_str,
                'line_items': pending_items if pending_items else received_items,
                'contact_info': contact_info, 'total_damaged': total_damaged,
                'item_count': len(pending_items) if pending_items else len(received_items)})
            if p.status == 'Received':
                stats['received'] += 1
            elif p.status == 'Approved':
                stats['approved'] += 1
            elif p.status == 'Waiting':
                stats['waiting'] += 1

    status_map = {
        'waiting':  ['Waiting'],
        'approved': ['Approved'],
        'pending':  ['Pending', 'Partial Pending'],
        'partial':  ['Partial Received', 'Partial Pending'],
        'received': ['Received', 'Partial Received'],
    }
    if status_filter in status_map:
        all_pos = [po for po in all_pos if po['status'] in status_map[status_filter]]

    return render_template('purchase_log.html',
                           all_pos=all_pos, stats=stats,
                           supplier_names=supplier_names,
                           active_date_range=date_range,
                           active_supplier=supplier_filter,
                           active_status=status_filter,
                           current_role=current_user.role,
                           warehouses=Warehouse.query.order_by(Warehouse.name).all())

# --- 4a. APPROVE PO (Admin only) ---
@purchase_bp.route("/approve_purchase", methods=["POST"])
@login_required
def approve_purchase():
    if current_user.role != 'admin':
        flash("Only admins can approve purchase orders.", "danger")
        return redirect(url_for('purchase.purchase_log'))
    try:
        pid = request.form.get('purchase_id')
        purchase = db.session.get(Purchase, pid)
        if purchase and purchase.status == 'Waiting':
            purchase.status = 'Approved'
            db.session.commit()
            flash(f"PO #{pid} approved — ready to order.", "success")
        else:
            flash("Purchase not found or already processed.", "warning")
    except Exception as e:
        db.session.rollback()
        flash(f"Error: {e}", "danger")
    return redirect(url_for('purchase.purchase_log'))

# --- 4b. DELETE PO (Admin only) ---
@purchase_bp.route("/delete_purchase", methods=["POST"])
@login_required
def delete_purchase():
    if current_user.role != 'admin':
        flash("Only admins can delete purchase orders.", "danger")
        return redirect(url_for('purchase.purchase_log'))
    try:
        pid = request.form.get('purchase_id')
        purchase = db.session.get(Purchase, pid)
        if not purchase:
            flash("Purchase order not found.", "warning")
            return redirect(url_for('purchase.purchase_log'))

        # If already received, revert stock before deleting
        if purchase.status in ['Received', 'Partial Received'] and purchase.received_details:
            details = json.loads(purchase.received_details)
            for item in details:
                qty_added = item.get('good_qty', 0)
                if qty_added > 0:
                    product = Product.query.filter(
                        (Product.name == item.get('name')) |
                        (Product.purchase_name == item.get('name'))
                    ).first()
                    if product:
                        product.quantity -= qty_added
                        db.session.add(product)

        db.session.delete(purchase)
        db.session.commit()
        flash(f"Purchase Order #{pid} deleted successfully.", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"Error deleting PO: {e}", "danger")
    return redirect(url_for('purchase.purchase_log'))

# --- 4. UPDATE STATUS (Received/Pending) ---
import json

@purchase_bp.route("/update_purchase_status", methods=["POST"])
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
            return redirect(url_for('purchase.purchase_log'))
            
        if purchase.status == new_status_req and new_status_req != 'Received':
            return redirect(url_for('purchase.purchase_log'))

        # Simple status reversal — Approved → Waiting (no stock changes needed)
        if new_status_req == 'Waiting' and purchase.status == 'Approved':
            purchase.status = 'Waiting'
            db.session.commit()
            flash(f"PO #{pid} reverted to Waiting for approval.", "info")
            return redirect(url_for('purchase.purchase_log'))

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
            db.session.commit()
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
                                
            db.session.commit()
            flash(f"Purchase updated to {purchase.status}.", "success")
            
    except Exception as e:
        db.session.rollback()
        print(f"Error: {e}")
        flash(f"Error: {e}", "danger")
        
    return redirect(url_for('purchase.purchase_log'))

# --- 5. SUPPLIER & PRODUCT APIs ---
@purchase_bp.route("/add_supplier", methods=["POST"])
@login_required
def add_supplier():
    name = request.form.get('supplier_name')
    if name and not Supplier.query.filter_by(name=name).first(): 
        db.session.add(Supplier(name=name))
        db.session.commit()
        flash(f"Supplier '{name}' added.", "success")
    return redirect(url_for('purchase.purchase_page'))

@purchase_bp.route("/add_product_to_supplier", methods=["POST"])
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
            db.session.commit()
            flash(f"Product '{sales_name}' added.", "success")
            return redirect(url_for('purchase.purchase_page', selected_supplier=supplier_id))
            
    except Exception as e: 
        flash(f"Error: {e}", "danger")
    return redirect(url_for('purchase.purchase_page'))

@purchase_bp.route("/update_product_inline", methods=["POST"])
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
            db.session.commit()
            return jsonify({'success': True})
        except ValueError:
            return jsonify({'success': False, 'error': 'Invalid number'})
    return jsonify({'success': False, 'error': 'Product not found'})

@purchase_bp.route("/get_products_by_category/<path:category>")
@login_required
def get_products_by_category(category):
    products = Product.query.filter_by(category=category).order_by(Product.name).all()
    return jsonify([product_to_dict_full(p) for p in products])

@purchase_bp.route("/download_supplier_products/<int:supplier_id>")
@login_required
def download_supplier_products(supplier_id):
    supplier = db.session.get(Supplier, supplier_id)
    if not supplier: return redirect(url_for('purchase.purchase_page'))
    products = Product.query.filter_by(supplier_id=supplier_id).all()
    return generate_product_pdf(products, f"Product List: {supplier.name}", f"{supplier.name}_Products.pdf")

@purchase_bp.route("/download_purchase_order/<int:purchase_id>")
@login_required
def download_purchase_order(purchase_id):
    purchase = db.session.get(Purchase, purchase_id)
    if not purchase: return redirect(url_for('purchase.purchase_log'))
    deliver_to = request.args.get('deliver_to', 'Safe Environment International\nJaipur, Rajasthan')
    supplier = db.session.get(Supplier, purchase.supplier_id)
    supplier_details = f"{purchase.supplier_name}\n"; 
    if supplier and supplier.contact_info: supplier_details += f"{supplier.contact_info}"
    items_parsed = []; raw_str = purchase.product_name if purchase.product_name else ""
    if ' || ' in raw_str: segments = raw_str.split(' || ')
    else: segments = raw_str.split(', ')
    grand_total = 0.0
    for seg in segments:
        if not seg.strip(): continue
        item_name = seg; item_qty = 0 
        if ' (x' in seg:
            try: name_part, qty_part = seg.rsplit(' (x', 1); item_name = name_part; item_qty = int(qty_part.rstrip(')'))
            except: pass
        if item_qty == 0 and len(segments) == 1: item_qty = purchase.qty_purchased
        prod = Product.query.filter_by(name=item_name).first()
        hsn = prod.hsn_code if prod else "-"; rate = prod.purchase_price if prod else 0.0
        gst = prod.gst_rate if prod else 0.0
        amount = (rate * item_qty) * (1 + gst/100)
        grand_total += amount
        unit = prod.unit if prod else "-"
        items_parsed.append({'desc': item_name, 'hsn': hsn, 'gst': gst, 'qty': item_qty, 'rate': rate, 'amount': amount, 'unit': unit})
    
    pdf = PO_PDF(); pdf.add_page(); pdf.set_margins(10, 10, 10)
    
    pdf.set_font("Helvetica", 'B', 20); pdf.cell(0, 10, "PURCHASE ORDER", 0, 1, 'C'); pdf.ln(5)
    pdf.set_font("Helvetica", 'B', 14); pdf.cell(0, 8, "Safe Environment International", 0, 1, 'L')
    pdf.set_font("Helvetica", '', 10); pdf.multi_cell(0, 5, "E-760, NAKUL PATH, LAL KOTHI SCHEME\nJAIPUR, RAJASTHAN-302015\nPhone: 9587017600", 0, 'L'); pdf.ln(5)
    pdf.line(10, pdf.get_y(), 200, pdf.get_y()); pdf.ln(5)
    
    start_y = pdf.get_y()
    pdf.set_font("Helvetica", 'B', 10); pdf.cell(90, 5, "Vendor:", 0, 1, 'L')
    pdf.set_font("Helvetica", '', 10); pdf.multi_cell(90, 5, supplier_details, 0, 'L')
    vendor_end_y = pdf.get_y()
    
    pdf.set_xy(110, start_y); pdf.set_font("Helvetica", 'B', 10); pdf.cell(30, 5, "PO No:", 0, 0); pdf.set_font("Helvetica", '', 10); pdf.cell(50, 5, str(purchase.id), 0, 1)
    pdf.set_xy(110, pdf.get_y()); pdf.cell(30, 5, "Date:", 0, 0); pdf.cell(50, 5, purchase.date.strftime('%Y-%m-%d'), 0, 1)
    pdf.set_xy(110, pdf.get_y() + 2); pdf.set_font("Helvetica", 'B', 10); pdf.cell(90, 5, "Deliver To:", 0, 1)
    pdf.set_font("Helvetica", '', 10); pdf.set_x(110); pdf.multi_cell(80, 5, deliver_to, 0, 'L')
    deliver_end_y = pdf.get_y()
    
    final_y = max(vendor_end_y, deliver_end_y); pdf.line(105, start_y, 105, final_y); pdf.set_y(final_y + 10)
    
    def print_header():
        pdf.set_font("Helvetica", 'B', 10); pdf.set_fill_color(240, 240, 240)
        pdf.cell(10, 8, "S.No", 1, 0, 'C', True); pdf.cell(65, 8, "Description", 1, 0, 'L', True); pdf.cell(20, 8, "HSN", 1, 0, 'C', True); pdf.cell(15, 8, "GST%", 1, 0, 'C', True); pdf.cell(20, 8, "Qty", 1, 0, 'C', True); pdf.cell(25, 8, "Rate", 1, 0, 'R', True); pdf.cell(35, 8, "Amount", 1, 1, 'R', True)
        pdf.set_font("Helvetica", '', 10)
    
    print_header()

    for i, item in enumerate(items_parsed, 1):
        if pdf.get_y() > 250:
            pdf.add_page()
            print_header()

        start_y = pdf.get_y()
        start_x = pdf.get_x()

        pdf.set_xy(start_x + 10, start_y)
        pdf.multi_cell(65, 8, item['desc'], 1, 'L')
        
        end_y = pdf.get_y()
        row_height = end_y - start_y

        pdf.set_xy(start_x, start_y)
        pdf.cell(10, row_height, str(i), 1, 0, 'C')

        pdf.set_xy(start_x + 75, start_y)
        pdf.cell(20, row_height, str(item['hsn']), 1, 0, 'C')

        pdf.set_xy(start_x + 95, start_y)
        pdf.cell(15, row_height, f"{item['gst']:.0f}%", 1, 0, 'C')

        pdf.set_xy(start_x + 110, start_y)
        pdf.cell(20, row_height, f"{item['qty']} {item['unit']}", 1, 0, 'C')

        pdf.set_xy(start_x + 130, start_y)
        pdf.cell(25, row_height, f"{item['rate']:.2f}", 1, 0, 'R')

        pdf.set_xy(start_x + 155, start_y)
        pdf.cell(35, row_height, f"{item['amount']:.2f}", 1, 1, 'R')
        
        pdf.set_y(end_y)
        
    pdf.set_font("Helvetica", 'B', 10); pdf.cell(155, 8, "Grand Total", 1, 0, 'R'); pdf.cell(35, 8, f"{grand_total:.2f}", 1, 1, 'R')
    
    pdf_content = pdf.output(dest='S'); response_data = bytes(pdf_content) if isinstance(pdf_content, (bytes, bytearray)) else pdf_content.encode('latin-1')
    resp = make_response(response_data); resp.headers['Content-Type'] = 'application/pdf'; resp.headers['Content-Disposition'] = f'attachment; filename=PO_{purchase_id}.pdf'
    return resp 

@purchase_bp.route("/get_products_by_supplier/<int:supplier_id>")
@login_required
def get_products_by_supplier(supplier_id):
    products = Product.query.filter_by(supplier_id=supplier_id).order_by(Product.name).all()
    # Use the helper function to ensure ALL fields (HSN, Barcode, Min/Max) are returned
    return jsonify([product_to_dict_full(p) for p in products])