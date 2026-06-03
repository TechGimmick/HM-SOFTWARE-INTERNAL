from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, session, make_response
from flask_login import login_required, current_user
from app.extensions import db
from app.models import Sale, SaleItem, Product, Customer, Warehouse, WarehouseStock, SalePayment
from app.activity_service import log_activity
from sqlalchemy import func, case
import json
from datetime import datetime, timedelta
from collections import defaultdict
import re

sales_bp = Blueprint('sales', __name__)

def get_product_names_catalog():
    """Lightweight: only id, name, category — no prices, stock, or extras."""
    products = db.session.query(
        Product.id, Product.name, Product.category, Product.unit,
        Product.has_subcategory, Product.subcategory_type, Product.subcategory_options
    ).all()
    catalog = {}
    all_list = []
    for p in products:
        cat = p.category if p.category else "Uncategorized"
        if cat not in catalog:
            catalog[cat] = []
        entry = {
            'id': p.id,
            'name': p.name,
            'raw_name': p.name,
            'category': cat,
            'unit': p.unit,
            'has_sub': p.has_subcategory,
            'sub_type': p.subcategory_type,
            'sub_opts': p.subcategory_options.split(',') if p.subcategory_options else []
        }
        catalog[cat].append(entry)
        all_list.append(entry)
    return catalog, sorted(catalog.keys()), all_list

@sales_bp.route("/get_product_details/<int:product_id>")
@login_required
def get_product_details(product_id):
    """Returns full product info for a single product. Called when adding to cart."""
    p = db.session.get(Product, product_id)
    if not p:
        return jsonify({'error': 'Not found'}), 404
    return jsonify({
        'id': p.id,
        'name': p.name,
        'category': p.category if p.category else 'Uncategorized',
        'unit': p.unit or '',
        'mrp': float(p.mrp) if p.mrp is not None else 0.0,
        'quantity': int(p.quantity) if p.quantity is not None else 0,
        'barcode': p.barcode,
        'gst_rate': float(p.gst_rate) if p.gst_rate is not None else 0.0,
        'has_sub': bool(p.has_subcategory),
        'sub_type': p.subcategory_type or '',
        'sub_opts': p.subcategory_options.split(',') if p.subcategory_options else [],
        'pack_size': p.pack_size or 1
    })

def add_new_client_db(name, phone=None):
    if not name: return False
    existing = Customer.query.filter(func.lower(Customer.name) == func.lower(name)).first()
    if not existing:
        customer = Customer(name=name, phone=phone)
        db.session.add(customer)
        db.session.flush()                          # get ID without a separate commit
        log_activity('CREATE', 'Clients',
                     f'New client "{name}" added' + (f' ({phone})' if phone else ''),
                     ref_id=customer.id, ref_type='Customer')
        return True
    return False

@sales_bp.route("/sales")
@login_required
def sales_page():
    if current_user.role not in ["admin", "sales"]:
        flash("Access Denied: Sales Area Only", "danger")
        return redirect(url_for('inventory.dashboard'))
    
    selected_client = request.args.get('client_name')
    
    # Only customer names for the dropdown — very lightweight
    customers = Customer.query.with_entities(Customer.name, Customer.phone).order_by(Customer.name).all()
    client_list = []
    for c in customers:
        if c.phone:
            client_list.append(f"{c.name} - {c.phone}")
        else:
            client_list.append(c.name)
            
    if not selected_client:
        return render_template('sales.html', client_list=client_list, client_balances={}, selected_client=None, categories=[], all_products_json="[]", product_catalog_json="{}")

    # Client selected: load ONLY product names/IDs (not full product data)
    catalog, categories, all_list = get_product_names_catalog()
    product_catalog_json = json.dumps(catalog)
    all_products_json = json.dumps(all_list)

    # Outstanding balance for this client
    debt = 0.0
    unpaid_sales = Sale.query.filter(
        Sale.client_name == selected_client,
        Sale.payment_status != 'Payment Received'
    ).all()
    for s in unpaid_sales:
        total = s.grand_total if s.grand_total is not None else sum(i.total_price for i in s.items)
        paid = (s.paid_cash or 0.0) + (s.paid_online or 0.0)
        due = total - paid
        if due > 0.1:
            debt += due

    base_name = selected_client.split(' - ')[0].strip()
    customer = Customer.query.filter(func.lower(Customer.name) == func.lower(base_name)).first()
    wallet = customer.wallet_balance if customer else 0.0
    
    net_balance = wallet - debt
    client_balances = {}
    if abs(net_balance) > 0.1:
        client_balances[selected_client] = net_balance
    
    return render_template('sales.html', client_list=client_list, client_balances=client_balances, selected_client=selected_client, categories=categories, all_products_json=all_products_json, product_catalog_json=product_catalog_json, warehouses=Warehouse.query.order_by(Warehouse.name).all())

@sales_bp.route("/add_client", methods=["POST"])
@login_required
def add_client():
    new_name  = request.form.get('new_client_name')
    new_phone = request.form.get('new_client_phone')

    if new_name:
        if add_new_client_db(new_name, new_phone):
            db.session.commit()   # single atomic commit: customer row + activity log
            flash("Client added.", "success")
            redirect_val = f"{new_name} - {new_phone}" if new_phone else new_name
            return redirect(url_for('sales.sales_page', client_name=redirect_val))
        else:
            flash("Client exists or invalid.", "warning")
    return redirect(url_for('sales.sales_page'))



@sales_bp.route("/process_sale", methods=["POST"])
@login_required
def process_sale():
    try:
        client_name = request.form.get('client_name')
        cart_json = request.form.get('sales_cart')
        warehouse_id = request.form.get('warehouse_id')
        if not client_name or not cart_json: return redirect(url_for('sales.sales_page'))

        cart_items = json.loads(cart_json)
        bill_type = request.form.get('bill_type', 'simple')

        if bill_type == 'tally':
            from app.models import TallyBill, TallyBillItem
            invoice_number = request.form.get('invoice_number')
            tally_payment_status = request.form.get('tally_payment_status')
            credit_period = request.form.get('credit_period')
            try:
                credit_period = int(credit_period)
            except (ValueError, TypeError):
                credit_period = None
                
            payment_mode = None
            paid_cash = 0.0
            paid_online = 0.0
            if tally_payment_status == 'Payment Received':
                raw_cash = request.form.get('tally_cash_amt') or '0'
                raw_online = request.form.get('tally_online_amt') or '0'
                paid_cash = float(raw_cash)
                paid_online = float(raw_online)
                
                modes = []
                if paid_cash > 0: modes.append("Cash")
                if paid_online > 0: modes.append("Online")
                payment_mode = " + ".join(modes) if modes else None
            
            new_tally = TallyBill(
                client_name=client_name,
                invoice_number=invoice_number,
                payment_status=tally_payment_status,
                payment_mode=payment_mode,
                paid_cash=paid_cash,
                paid_online=paid_online,
                credit_period=credit_period,
                order_status='Order Pending',
                warehouse_id=warehouse_id
            )
            db.session.add(new_tally)
            db.session.flush()
            for item in cart_items:
                qty = int(item.get('qty', 0))
                if qty <= 0: continue
                db.session.add(TallyBillItem(
                    tally_bill_id=new_tally.id,
                    product_name=item['name'],
                    qty=qty
                ))
            log_activity('CREATE', 'Tally',
                         f'New Tally Bill #{invoice_number} for {client_name}',
                         ref_id=new_tally.id, ref_type='TallyBill')
            db.session.commit()   # single atomic commit: tally bill + log
            flash(f"Tally Bill #{invoice_number} recorded for {client_name}.", "success")
            return redirect(url_for('sales.tally_sales_page'))

        # --- NORMAL SIMPLE BILL SAVING ---
        new_sale = Sale(client_name=client_name, warehouse_id=warehouse_id)
        db.session.add(new_sale)
        db.session.flush()

        # Batch-load all products
        id_items   = [item for item in cart_items if item.get('id')  and int(item.get('qty', 0)) > 0]
        name_items = [item for item in cart_items if not item.get('id') and int(item.get('qty', 0)) > 0]

        id_map, name_map = {}, {}
        if id_items:
            pids  = [item['id'] for item in id_items]
            prods = Product.query.filter(Product.id.in_(pids)).all()
            id_map = {p.id: p for p in prods}
        if name_items:
            names = [item['name'] for item in name_items]
            prods = Product.query.filter(func.lower(Product.name).in_([n.lower() for n in names])).all()
            name_map = {p.name.lower(): p for p in prods}

        running_total = 0.0
        for item in cart_items:
            qty = int(item.get('qty', 0))
            if qty <= 0: continue

            final_name = item['name']
            if item.get('variation'): final_name = f"{item['name']} - {item['variation']}"

            product = id_map.get(item['id']) if item.get('id') else name_map.get(item['name'].lower())
            if product:
                product.quantity -= qty
                if warehouse_id:
                    ws = WarehouseStock.query.filter_by(warehouse_id=warehouse_id, product_id=product.id).first()
                    if ws: ws.quantity -= qty

            line_total = float(item['total'])
            running_total += line_total
            db.session.add(SaleItem(
                sale_id=new_sale.id, category=item['category'], product_name=final_name,
                description=item.get('description', ''), qty_sold=qty, unit=item['unit'],
                total_price=line_total, gst_rate=float(item.get('gst') or 0.0)
            ))

        new_sale.grand_total = running_total
        log_activity('CREATE', 'Sales', f'New sale #{new_sale.id} for {client_name} — ₹{running_total:.0f}', ref_id=new_sale.id, ref_type='Sale')
        db.session.commit()   # single atomic commit: sale + log together
        flash(f"Bill No. {new_sale.id} recorded.", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"Error: {e}", "danger")

    return redirect(url_for('sales.sales_log'))

@sales_bp.route("/sales_log")
@login_required
def sales_log():
    if current_user.role not in ['sales', 'admin']:
        flash("Access Denied: Sales History is for Sales Staff only.", "danger")
        return redirect(url_for('inventory.dashboard'))
    
    # --- 1. FILTER LOGIC ---
    # Use IST (UTC+5:30) so date boundaries are correct for India
    now_ist = datetime.utcnow() + timedelta(hours=5, minutes=30)
    # Get filters
    filter_date_str = request.args.get('filter_date')
    filter_status = request.args.get('filter_status')
    filter_client_str = request.args.get('filter_client')
    
    query = Sale.query
    
    is_global_search = False
    
    if filter_client_str:
        query = query.filter(Sale.client_name.ilike(f"%{filter_client_str}%"))
        is_global_search = True

    has_date_filter = False
    if filter_date_str:
        try:
            filter_date = datetime.strptime(filter_date_str, '%Y-%m-%d').date()
            query = query.filter(func.date(Sale.date) == filter_date)
            has_date_filter = True
            flash(f"Showing bills for {filter_date.strftime('%d %b %Y')}", "info")
        except ValueError:
            flash("Invalid date format.", "danger")

    if filter_status and filter_status != 'all':
        is_global_search = True
        if filter_status == 'partial':
            query = query.filter(Sale.payment_status.in_(['Partial Payment', 'Payment Not Received']))
        elif filter_status == 'received':
            query = query.filter(Sale.payment_status == 'Payment Received')
        elif filter_status == 'full_cash':
            query = query.filter(Sale.payment_status == 'Payment Received', Sale.paid_online == 0)
        elif filter_status == 'full_online':
            query = query.filter(Sale.payment_status == 'Payment Received', Sale.paid_cash == 0)
        
        # If no specific date was picked, we do NOT limit by date here.
        # This allows seeing all unpaid bills from history, for example.
        if not has_date_filter:
             flash(f"Showing all '{filter_status}' records found in system.", "info")
    
    elif not has_date_filter and not is_global_search:
        # DEFAULT: If no date AND no status filter and no client search, just show recent
        two_days_ago = now_ist.date() - timedelta(days=2)
        query = query.filter(func.date(Sale.date) >= two_days_ago)
    
    sales = query.order_by(Sale.date.desc(), Sale.id.desc()).all()
    
    # --- 2. STATS CALCULATION (AGGREGATE SQL) ---
    # Calculated independently of view filter, using IST date
    today_date = now_ist.date()
    start_of_month = today_date.replace(day=1)
    
    # Helper to get sums safely
    def get_sums(filters):
        # Single query using SQL CASE — replaces 6 separate queries per call
        is_unpaid = Sale.payment_status != 'Payment Received'
        row = db.session.query(
            func.coalesce(func.sum(Sale.paid_cash), 0.0),
            func.coalesce(func.sum(Sale.paid_online), 0.0),
            func.coalesce(func.sum(case((is_unpaid, Sale.grand_total), else_=0)), 0.0),
            func.coalesce(func.sum(case((is_unpaid, Sale.paid_cash),    else_=0)), 0.0),
            func.coalesce(func.sum(case((is_unpaid, Sale.paid_online),  else_=0)), 0.0),
        ).filter(*filters).one()
        cash   = float(row[0])
        online = float(row[1])
        credit = max(0.0, float(row[2]) - float(row[3]) - float(row[4]))
        return {'total': cash + online, 'cash': cash, 'online': online, 'credit': credit}

    daily_stats = get_sums([func.date(Sale.date) == today_date])
    monthly_stats = get_sums([func.date(Sale.date) >= start_of_month])

    # --- 3. PREPARE VIEW DATA ---
    sales_by_date = defaultdict(list)
    for sale in sales:
        date_key = sale.date.strftime('%d %b %Y')
        
        # Calculate Logic for display
        total = sale.grand_total if sale.grand_total is not None else sum(i.total_price for i in sale.items)
        p_cash = sale.paid_cash or 0.0
        p_online = sale.paid_online or 0.0
        real_paid_amt = p_cash + p_online
        
        items_list = [{'Category': i.category, 'Product Name': i.product_name, 'Qty Sold': i.qty_sold, 'Unit': i.unit, 'Total': i.total_price} for i in sale.items]
        
        payment_date_ist_str = None
        raw_payment_date_str = None
        if sale.payment_date:
            payment_date_ist_str = (sale.payment_date + timedelta(hours=5, minutes=30)).strftime('%d %b %Y %I:%M %p')
            raw_payment_date_str = (sale.payment_date + timedelta(hours=5, minutes=30)).strftime('%Y-%m-%dT%H:%M')

        sales_by_date[date_key].append({
            'id': sale.id,
            'client_name': sale.client_name, 
            'order_status': sale.order_status, 
            'payment_status': sale.payment_status, 
            'payment_mode': sale.payment_mode, 
            'grand_total': total, 
            'paid_amount': real_paid_amt, 
            'paid_cash': p_cash,
            'paid_online': p_online,
            'payment_date': payment_date_ist_str,
            'raw_payment_date': raw_payment_date_str,
            'items': items_list
        })

    # Only fetch customers who appear in the current visible sales — not the full table
    visible_names = {s.client_name.split(' - ')[0].strip() for s in sales}
    wallet_map = {}
    if visible_names:
        custs = Customer.query\
            .filter(Customer.name.in_(visible_names))\
            .with_entities(Customer.name, Customer.wallet_balance)\
            .all()
        wallet_map = {
            c.name: c.wallet_balance
            for c in custs
            if c.wallet_balance and abs(c.wallet_balance) > 0.01
        }

    now_month = now_ist.strftime('%Y-%m')
    return render_template('sales_log.html', sales_by_date=sales_by_date, daily_stats=daily_stats, monthly_stats=monthly_stats, wallet_map=wallet_map, current_filter_date=filter_date_str, current_filter_status=filter_status, current_filter_client=filter_client_str, now_month=now_month)

@sales_bp.route("/sales_monthly_stats")
@login_required
def sales_monthly_stats():
    """Returns aggregate sales stats for a given month/year as JSON."""
    try:
        month = int(request.args.get('month', 0))
        year  = int(request.args.get('year', 0))
        if not (1 <= month <= 12) or year < 2000:
            return jsonify({'error': 'Invalid month/year'}), 400

        from calendar import monthrange
        last_day = monthrange(year, month)[1]
        from datetime import date as date_type
        start = date_type(year, month, 1)
        end   = date_type(year, month, last_day)

        filters = [func.date(Sale.date) >= start, func.date(Sale.date) <= end]
        cash   = db.session.query(func.sum(Sale.paid_cash)).filter(*filters).scalar() or 0.0
        online = db.session.query(func.sum(Sale.paid_online)).filter(*filters).scalar() or 0.0
        total  = cash + online

        unpaid_filters = filters + [Sale.payment_status != 'Payment Received']
        gt_sum = db.session.query(func.sum(Sale.grand_total)).filter(*unpaid_filters).scalar() or 0.0
        pc_sum = db.session.query(func.sum(Sale.paid_cash)).filter(*unpaid_filters).scalar() or 0.0
        po_sum = db.session.query(func.sum(Sale.paid_online)).filter(*unpaid_filters).scalar() or 0.0
        credit = max(0.0, gt_sum - pc_sum - po_sum)

        return jsonify({'total': round(total,2), 'cash': round(cash,2), 'online': round(online,2), 'credit': round(credit,2)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@sales_bp.route("/edit_bill/<int:sales_id>")
@login_required
def edit_bill(sales_id):
    sale = db.session.get(Sale, sales_id)
    if not sale: return redirect(url_for('sales.sales_log'))
    
    catalog, categories, _ = get_product_names_catalog()
    product_catalog_json = json.dumps(catalog)
    client_list = [c.name for c in Customer.query.with_entities(Customer.name).order_by(Customer.name).all()]
    
    bill_data = {'sales_id': sale.id, 'client_name': sale.client_name, 'items': []}
    for item in sale.items:
        gst_percent = item.gst_rate or 0.0
        base_unit_price = (item.total_price / (1 + (gst_percent/100))) / item.qty_sold if item.qty_sold > 0 else 0
        bill_data['items'].append({'category': item.category, 'name': item.product_name, 'description': item.description, 'qty': item.qty_sold, 'unit': item.unit, 'mrp': base_unit_price, 'total': item.total_price, 'gst': gst_percent})
    
    return render_template('edit_bill.html', bill=bill_data, client_list=client_list, categories=categories, product_catalog_json=product_catalog_json, warehouses=Warehouse.query.order_by(Warehouse.name).all())

@sales_bp.route("/get_last_sale_price")
@login_required
def get_last_sale_price():
    client_name = request.args.get('client_name')
    product_name = request.args.get('product_name')
    
    if not client_name or not product_name:
        return jsonify({'found': False})

    base_client_name = client_name.split(' - ')[0].strip()

    last_item = db.session.query(SaleItem).join(Sale).filter(
        Sale.client_name.ilike(f"{base_client_name}%"),
        SaleItem.product_name.ilike(f"{product_name}%") 
    ).order_by(Sale.date.desc(), Sale.id.desc()).first()

    if last_item:
        unit_price = last_item.total_price / last_item.qty_sold if last_item.qty_sold > 0 else 0
        sale_date = last_item.sale.date.strftime('%d %b %Y')
        return jsonify({
            'found': True, 
            'price': unit_price, 
            'date': sale_date,
            'sold_name': last_item.product_name
        })
    
    return jsonify({'found': False})

@sales_bp.route("/process_edit_sale", methods=["POST"])
@login_required
def process_edit_sale():
    try:
        sales_id = request.form.get('sales_id')
        bill_type = request.form.get('bill_type', 'simple')
        client_name = request.form.get('client_name')
        cart_json  = request.form.get('sales_cart')
        warehouse_id = request.form.get('warehouse_id')

        sale = db.session.get(Sale, sales_id)
        if not sale or not cart_json: return redirect(url_for('sales.sales_log'))

        cart_items = json.loads(cart_json)
        old_items  = list(sale.items)   # snapshot before any deletes

        # --- 1. BATCH restore old stock (1 query instead of N) ---
        if old_items:
            old_names = set()
            for oi in old_items:
                old_names.add(oi.product_name)
                if ' (' in oi.product_name: old_names.add(oi.product_name.split(' (')[0].strip())
                if ' - ' in oi.product_name: old_names.add(oi.product_name.split(' - ')[0].strip())
            old_prod_map = {p.name: p for p in Product.query.filter(Product.name.in_(list(old_names))).all()}

            for oi in old_items:
                prod = (old_prod_map.get(oi.product_name)
                        or old_prod_map.get(oi.product_name.split(' (')[0].strip() if ' (' in oi.product_name else None)
                        or old_prod_map.get(oi.product_name.split(' - ')[0].strip() if ' - ' in oi.product_name else None))
                if prod:
                    prod.quantity += oi.qty_sold
                    w_id = sale.warehouse_id or warehouse_id
                    if w_id:
                        ws = WarehouseStock.query.filter_by(warehouse_id=w_id, product_id=prod.id).first()
                        if ws: ws.quantity += oi.qty_sold

        # --- 2. Handle delete-bill case ---
        if not cart_items:
            # Snapshot values BEFORE delete — SQLAlchemy expires object after commit
            sale_id_snap    = sale.id
            client_name_snap = sale.client_name
            if sale.wallet_credit and sale.wallet_credit > 0:
                search_name = sale.client_name.split(' - ')[0].strip()
                customer = Customer.query.filter(func.lower(Customer.name) == func.lower(search_name)).first()
                if customer:
                    customer.wallet_balance -= sale.wallet_credit
                    db.session.add(customer)
                    flash(f"₹{sale.wallet_credit} reverted from {customer.name}'s wallet.", "info")
            db.session.delete(sale)
            log_activity('DELETE', 'Sales', f'Deleted sale #{sale_id_snap} ({client_name_snap})', ref_id=sale_id_snap, ref_type='Sale')
            db.session.commit()   # single atomic commit: delete + log together
            flash("Bill deleted.", "success")
            return redirect(url_for('sales.sales_log'))

        # --- 3. Clear old line items ---
        for oi in old_items: db.session.delete(oi)

        sale.client_name = client_name
        sale.warehouse_id = warehouse_id
        running_total = 0.0

        # --- 4. BATCH load new products (1 query by ID + 1 by name instead of N) ---
        new_id_items   = [item for item in cart_items if item.get('id')  and int(item.get('qty', 0)) > 0]
        new_name_items = [item for item in cart_items if not item.get('id') and int(item.get('qty', 0)) > 0]

        new_id_map, new_name_map = {}, {}
        if new_id_items:
            pids  = [item['id'] for item in new_id_items]
            prods = Product.query.filter(Product.id.in_(pids)).all()
            new_id_map = {p.id: p for p in prods}
        if new_name_items:
            base_names = [item['name'].split(' (')[0].split(' - ')[0].strip() for item in new_name_items]
            prods = Product.query.filter(func.lower(Product.name).in_([n.lower() for n in base_names])).all()
            new_name_map = {p.name.lower(): p for p in prods}

        for item in cart_items:
            qty      = int(item.get('qty', 0))
            gst_rate = float(item.get('gst') or 0.0)
            if qty <= 0: continue

            final_name = item['name']
            if item.get('variation'): final_name = f"{item['name']} - {item['variation']}"

            product = new_id_map.get(item['id']) if item.get('id') else None
            if not product:
                base = item['name'].split(' (')[0].split(' - ')[0].strip()
                product = new_name_map.get(base.lower())
            if product:
                product.quantity -= qty
                if warehouse_id:
                    ws = WarehouseStock.query.filter_by(warehouse_id=warehouse_id, product_id=product.id).first()
                    if ws: ws.quantity -= qty

            line_total = float(item['total'])
            running_total += line_total
            db.session.add(SaleItem(
                sale_id=sale.id, category=item['category'], product_name=final_name,
                description=item.get('description', ''), qty_sold=qty, unit=item['unit'],
                total_price=line_total, gst_rate=gst_rate
            ))

        sale.grand_total = running_total
        log_activity('UPDATE', 'Sales', f'Updated sale #{sale.id} for {sale.client_name} — ₹{running_total:.0f}', ref_id=sale.id, ref_type='Sale')
        db.session.commit()   # single atomic commit: edit + log together
        flash(f"Bill #{sale.id} updated.", "success")

    except Exception as e:
        db.session.rollback()
        flash(f"Error: {e}", "danger")
        
    return redirect(url_for('sales.sales_log'))

@sales_bp.route("/update_status", methods=["POST"])
@login_required
def update_status():
    sid = request.form.get('sales_id')
    stype = request.form.get('status_type')
    new_val = request.form.get('new_value')
    overpayment_action = request.form.get('overpayment_action')
    underpayment_action = request.form.get('underpayment_action')

    try:
        sale = db.session.get(Sale, sid)
        if sale:
            if stype == 'order': 
                sale.order_status = new_val
            elif stype == 'payment':
                cash_input = request.form.get('cash_amount')
                online_input = request.form.get('online_amount')
                raw_cash = float(cash_input) if cash_input and cash_input.strip() else 0.0
                raw_online = float(online_input) if online_input and online_input.strip() else 0.0
                
                # If both are 0, fully revert all payments for this sale
                if raw_cash == 0.0 and raw_online == 0.0:
                    # Revert any wallet credit first
                    if sale.wallet_credit and sale.wallet_credit > 0:
                        search_name = sale.client_name.split(' - ')[0].strip()
                        cust = Customer.query.filter(func.lower(Customer.name) == func.lower(search_name)).first()
                        if cust:
                            cust.wallet_balance -= sale.wallet_credit
                        sale.wallet_credit = 0.0
                    
                    # Delete all associated SalePayment records
                    SalePayment.query.filter_by(sale_id=sale.id).delete()
                    
                    sale.paid_cash = 0.0
                    sale.paid_online = 0.0
                    sale.payment_date = None
                    sale.payment_status = "Payment Not Received"
                    sale.payment_mode = None
                    flash("Payment fully reverted / reset.", "info")
                else:
                    # Parse the payment date from user input in IST, converting to UTC
                    payment_date_str = request.form.get('payment_date')
                    payment_date_utc = datetime.utcnow()
                    if payment_date_str:
                        try:
                            if 'T' in payment_date_str:
                                local_dt = datetime.strptime(payment_date_str, '%Y-%m-%dT%H:%M')
                            else:
                                local_dt = datetime.strptime(payment_date_str, '%Y-%m-%d %H:%M:%S')
                            payment_date_utc = local_dt - timedelta(hours=5, minutes=30)
                        except Exception:
                            pass

                    grand_total = sale.grand_total if sale.grand_total is not None else sum(i.total_price for i in sale.items)
                    prev_paid = (sale.paid_cash or 0.0) + (sale.paid_online or 0.0)
                    total_paid = prev_paid + raw_cash + raw_online

                    # If they previously had a wallet credit and are recording a new payment, let's revert the wallet credit first.
                    if sale.wallet_credit and sale.wallet_credit > 0:
                        search_name = sale.client_name.split(' - ')[0].strip()
                        cust = Customer.query.filter(func.lower(Customer.name) == func.lower(search_name)).first()
                        if cust:
                            cust.wallet_balance -= sale.wallet_credit
                        sale.wallet_credit = 0.0

                    if total_paid > grand_total:
                        extra_amount = total_paid - grand_total
                        
                        # Calculate ledger applied amounts (cap total payment at grand_total)
                        excess_to_reduce = extra_amount
                        applied_cash = raw_cash
                        applied_online = raw_online
                        
                        if excess_to_reduce > 0:
                            if applied_online >= excess_to_reduce:
                                applied_online -= excess_to_reduce
                                excess_to_reduce = 0.0
                            else:
                                excess_to_reduce -= applied_online
                                applied_online = 0.0
                                
                        if excess_to_reduce > 0:
                            if applied_cash >= excess_to_reduce:
                                applied_cash -= excess_to_reduce
                                excess_to_reduce = 0.0
                            else:
                                applied_cash = 0.0

                        # Record new payment in ledger
                        new_pay = SalePayment(
                            sale_id=sale.id,
                            amount_cash=applied_cash,
                            amount_online=applied_online,
                            payment_date=payment_date_utc
                        )
                        db.session.add(new_pay)

                        # Update sale
                        sale.paid_cash = (sale.paid_cash or 0.0) + applied_cash
                        sale.paid_online = (sale.paid_online or 0.0) + applied_online
                        sale.payment_date = payment_date_utc

                        if overpayment_action == 'credit':
                            search_name = sale.client_name.split(' - ')[0].strip()
                            customer = Customer.query.filter(func.lower(Customer.name) == func.lower(search_name)).first()
                            if customer:
                                current_bal = customer.wallet_balance or 0.0
                                customer.wallet_balance = current_bal + extra_amount
                                sale.wallet_credit = extra_amount
                                db.session.add(customer)
                                flash(f"Bill settled. ₹{extra_amount:.2f} added to {customer.name}'s wallet.", "success")
                            else:
                                flash(f"Bill settled. Warning: Customer '{search_name}' not found.", "warning")
                        else:
                            flash(f"Bill settled. Change returned: ₹{extra_amount:.2f}", "info")

                    elif total_paid < grand_total:
                        if underpayment_action == 'settle':
                            diff = grand_total - total_paid
                            
                            # For underpayment settle, we reduce grand_total to total_paid
                            sale.grand_total = total_paid
                            
                            # Record new payment in ledger
                            new_pay = SalePayment(
                                sale_id=sale.id,
                                amount_cash=raw_cash,
                                amount_online=raw_online,
                                payment_date=payment_date_utc
                            )
                            db.session.add(new_pay)

                            sale.paid_cash = (sale.paid_cash or 0.0) + raw_cash
                            sale.paid_online = (sale.paid_online or 0.0) + raw_online
                            sale.payment_date = payment_date_utc
                            
                            flash(f"Bill settled fully. Discount given: ₹{diff:.2f}", "success")
                        else:
                            # Record new payment in ledger
                            new_pay = SalePayment(
                                sale_id=sale.id,
                                amount_cash=raw_cash,
                                amount_online=raw_online,
                                payment_date=payment_date_utc
                            )
                            db.session.add(new_pay)

                            sale.paid_cash = (sale.paid_cash or 0.0) + raw_cash
                            sale.paid_online = (sale.paid_online or 0.0) + raw_online
                            sale.payment_date = payment_date_utc
                            
                            flash("Partial payment recorded.", "warning")
                    else:
                        # Exact payment
                        new_pay = SalePayment(
                            sale_id=sale.id,
                            amount_cash=raw_cash,
                            amount_online=raw_online,
                            payment_date=payment_date_utc
                        )
                        db.session.add(new_pay)

                        sale.paid_cash = (sale.paid_cash or 0.0) + raw_cash
                        sale.paid_online = (sale.paid_online or 0.0) + raw_online
                        sale.payment_date = payment_date_utc
                        
                        flash("Payment updated.", "success")

                    # Calculate status and mode
                    final_total = sale.grand_total
                    final_paid = (sale.paid_cash or 0.0) + (sale.paid_online or 0.0)
                    
                    if final_paid >= final_total - 0.1:
                        sale.payment_status = "Payment Received"
                    elif final_paid > 0.01:
                        sale.payment_status = "Partial Payment"
                    else:
                        sale.payment_status = "Payment Not Received"
                    
                    modes = []
                    if (sale.paid_cash or 0.0) > 0:
                        modes.append("Cash")
                    if (sale.paid_online or 0.0) > 0:
                        modes.append("Online")
                    sale.payment_mode = " + ".join(modes) if modes else None
                
            # --- Activity Log (before commit so it's atomic) ---
            if stype == 'payment':
                log_activity('PAYMENT', 'Sales', f'Payment recorded for sale #{sid} ({sale.client_name}) — ₹{(sale.paid_cash or 0) + (sale.paid_online or 0):.0f}', ref_id=sale.id, ref_type='Sale')
            else:
                log_activity('UPDATE', 'Sales', f'Order status updated to "{sale.order_status}" for sale #{sid}', ref_id=sale.id, ref_type='Sale')
            db.session.commit()   # single atomic commit: status change + log
    except Exception as e:
        db.session.rollback()
        print(f"ERROR: {e}")
        flash(f"Error: {e}", "danger")
        
    return redirect(url_for('sales.sales_log'))

    return redirect(url_for('sales.sales_log'))


# ─────────────────────────────────────────────────────────────────────────────
#  QUICK ADD PRODUCT  — create a new product inline from the sales page
#  Field set is identical to purchase.add_product_to_supplier
# ─────────────────────────────────────────────────────────────────────────────
@sales_bp.route('/api/quick_add_product', methods=['POST'])
@login_required
def quick_add_product():
    """
    Creates a new Product record from the sales page modal.
    Accepts JSON; returns the new product's full detail for immediate
    cart use without a page reload.
    Field set mirrors purchase.add_product_to_supplier exactly.
    """
    if current_user.role not in ('admin', 'sales'):
        return jsonify({'error': 'Access denied.'}), 403

    data = request.get_json(silent=True) or {}

    sales_name     = (data.get('product_name') or '').strip()
    purchase_name  = (data.get('purchase_name') or '').strip() or None
    category       = (data.get('category') or '').strip()
    supplier_id    = data.get('supplier_id')
    unit           = (data.get('unit') or 'Nos.').strip()
    barcode        = (data.get('barcode') or '').strip() or None
    hsn_code       = (data.get('hsn_code') or '').strip() or None
    gst_rate       = float(data.get('gst_rate') or 0)
    purchase_price = float(data.get('purchase_price') or 0)
    mrp            = float(data.get('sales_price') or 0)
    min_stock      = int(data.get('min_stock') or 5)
    max_stock      = int(data.get('max_stock') or 100)
    pack_size      = int(data.get('pack_size') or 1)
    has_sub        = bool(data.get('has_subcategory'))
    sub_type       = (data.get('subcategory_type') or '').strip() or None
    sub_opts       = (data.get('subcategory_options') or '').strip() or None

    if not sales_name:
        return jsonify({'error': 'Product name is required.'}), 400
    if not category:
        return jsonify({'error': 'Category is required.'}), 400
    if not supplier_id:
        return jsonify({'error': 'Supplier is required.'}), 400

    # Duplicate name check (case-insensitive)
    exists = Product.query.filter(func.lower(Product.name) == sales_name.lower()).first()
    if exists:
        return jsonify({'error': f'A product named "{exists.name}" already exists.'}), 409

    # Barcode uniqueness check
    if barcode:
        bc_clash = Product.query.filter_by(barcode=barcode).first()
        if bc_clash:
            return jsonify({'error': f'Barcode "{barcode}" is already assigned to "{bc_clash.name}".'}), 409

    product = Product(
        name              = sales_name,
        purchase_name     = purchase_name if purchase_name else sales_name,
        category          = category,
        unit              = unit,
        barcode           = barcode,
        hsn_code          = hsn_code,
        gst_rate          = gst_rate,
        purchase_price    = purchase_price,
        mrp               = mrp,
        min_stock         = min_stock,
        max_stock         = max_stock,
        pack_size         = pack_size,
        has_subcategory   = has_sub,
        subcategory_type  = sub_type if has_sub else None,
        subcategory_options = sub_opts if has_sub else None,
        quantity          = 0,
        supplier_id       = supplier_id,
    )
    db.session.add(product)
    log_activity('CREATE', 'Inventory', f'New product "{sales_name}" added from Sales page', ref_id=product.id, ref_type='Product')
    db.session.commit()   # single atomic commit: product + log together

    return jsonify({
        'success': True,
        'product': {
            'id'        : product.id,
            'name'      : product.name,
            'raw_name'  : product.name,
            'category'  : product.category,
            'unit'      : product.unit,
            'mrp'       : product.mrp,
            'purchase_price': product.purchase_price,
            'gst_rate'  : product.gst_rate,
            'quantity'  : 0,
            'min_stock' : product.min_stock,
            'pack_size' : product.pack_size,
            'has_sub'   : has_sub,
            'sub_type'  : sub_type,
            'sub_opts'  : sub_opts.split(',') if (has_sub and sub_opts) else [],
            'barcode'   : product.barcode,
        }
    }), 201
