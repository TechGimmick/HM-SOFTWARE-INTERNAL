from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, session, make_response
from flask_login import login_required, current_user
from app.extensions import db
from app.models import Sale, SaleItem, Product, Customer
from sqlalchemy import func
import json
from datetime import datetime, timedelta
from collections import defaultdict
import re

sales_bp = Blueprint('sales', __name__)

def get_all_products_catalog():
    products = Product.query.all()
    catalog = {}
    for p in products:
        cat = p.category if p.category else "Uncategorized"
        if cat not in catalog: catalog[cat] = []
        
        catalog[cat].append({
            'id': p.id, 
            'name': p.name, 
            'raw_name': p.name, 
            'category': cat,
            'unit': p.unit, 
            'mrp': p.mrp, 
            'quantity': p.quantity, 
            'barcode': p.barcode,
            'has_sub': p.has_subcategory,
            'sub_type': p.subcategory_type,
            'sub_opts': p.subcategory_options.split(',') if p.subcategory_options else []
        })
    return catalog, sorted(catalog.keys())

def add_new_client_db(name, phone=None):
    if not name: return False
    existing = Customer.query.filter(func.lower(Customer.name) == func.lower(name)).first()
    if not existing: 
        db.session.add(Customer(name=name, phone=phone))
        db.session.commit()
        return True
    return False

@sales_bp.route("/sales")
@login_required
def sales_page():
    if current_user.role not in ["admin", "sales"]:
        flash("Access Denied: Sales Area Only", "danger")
        return redirect(url_for('inventory.dashboard'))
    
    selected_client = request.args.get('client_name')
    catalog, categories = get_all_products_catalog()
    product_catalog_json = json.dumps(catalog)

    # Reuse already-loaded products from catalog to avoid second DB query
    all_products_list = []
    for cat_products in catalog.values():
        for p in cat_products:
            all_products_list.append(p)
    all_products_json = json.dumps(all_products_list)

    # --- Calculate Outstanding Dues per Client ---
    dues_map = defaultdict(float)
    unpaid_sales = Sale.query.filter(Sale.payment_status != 'Payment Received').all()
    for s in unpaid_sales:
        total = s.grand_total if s.grand_total is not None else sum(i.total_price for i in s.items)
        paid = (s.paid_cash or 0.0) + (s.paid_online or 0.0)
        due = total - paid
        if due > 0.1:
            dues_map[s.client_name] += due

    # --- Build Client List & Net Balance Map ---
    customers = Customer.query.order_by(Customer.name).all()
    client_list = []
    client_balances = {}
    for c in customers:
        if c.phone:
            c_key = f"{c.name} - {c.phone}"
        else:
            c_key = c.name
        
        client_list.append(c_key)
        
        wallet = c.wallet_balance or 0.0
        debt = dues_map.get(c_key, 0.0)
        net_balance = wallet - debt
        
        if abs(net_balance) > 0.1:
            client_balances[c_key] = net_balance
    
    return render_template('sales.html', client_list=client_list, client_balances=client_balances, selected_client=selected_client, categories=categories, all_products_json=all_products_json, product_catalog_json=product_catalog_json)

@sales_bp.route("/add_client", methods=["POST"])
@login_required
def add_client():
    new_name = request.form.get('new_client_name')
    new_phone = request.form.get('new_client_phone')
    
    if new_name:
        if add_new_client_db(new_name, new_phone): 
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
        if not client_name or not cart_json: return redirect(url_for('sales.sales_page'))
        
        cart_items = json.loads(cart_json)
        new_sale = Sale(client_name=client_name)
        db.session.add(new_sale)
        db.session.commit()
        
        running_total = 0.0
        for item in cart_items:
            qty = int(item['qty'])
            if qty <= 0: continue
            product = None
            if 'id' in item and item['id']: product = db.session.get(Product, item['id'])
            
            final_name = item['name']
            if 'variation' in item and item['variation']: final_name = f"{item['name']} - {item['variation']}"
            
            if not product:
                product = Product.query.filter(Product.name.ilike(item['name'])).first()
            
            if product: 
                product.quantity -= qty
                db.session.add(product)
            
            line_total = float(item['total'])
            running_total += line_total
            desc = item.get('description', '')
            sale_item = SaleItem(sale_id=new_sale.id, category=item['category'], product_name=final_name, description=desc, qty_sold=qty, unit=item['unit'], total_price=line_total, gst_rate=float(item.get('gst') or 0.0))
            db.session.add(sale_item)
        
        new_sale.grand_total = running_total
        db.session.commit()
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
    
    query = Sale.query
    
    # Logic: 
    # 1. If Date selected -> Show that Date (apply status filter if exists)
    # 2. If Status selected (and not 'all') -> Show ALL matched status (ignore date limit)
    # 3. If None -> Show Default (Last 2 Days)
    
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
    
    elif not has_date_filter:
        # DEFAULT: If no date AND no status filter, just show recent
        two_days_ago = now_ist.date() - timedelta(days=2)
        query = query.filter(func.date(Sale.date) >= two_days_ago)
    
    sales = query.order_by(Sale.date.desc(), Sale.id.desc()).all()
    
    # --- 2. STATS CALCULATION (AGGREGATE SQL) ---
    # Calculated independently of view filter, using IST date
    today_date = now_ist.date()
    start_of_month = today_date.replace(day=1)
    
    # Helper to get sums safely
    def get_sums(filters):
        # Cash & Online actually received
        cash   = db.session.query(func.sum(Sale.paid_cash)).filter(*filters).scalar() or 0.0
        online = db.session.query(func.sum(Sale.paid_online)).filter(*filters).scalar() or 0.0
        # Total = money actually collected (not billing total which inflates with unpaid dues)
        total  = cash + online
        # Credit = outstanding dues only from bills that are NOT fully paid
        unpaid_filters = list(filters) + [Sale.payment_status != 'Payment Received']
        gt_sum  = db.session.query(func.sum(Sale.grand_total)).filter(*unpaid_filters).scalar() or 0.0
        pc_sum  = db.session.query(func.sum(Sale.paid_cash)).filter(*unpaid_filters).scalar() or 0.0
        po_sum  = db.session.query(func.sum(Sale.paid_online)).filter(*unpaid_filters).scalar() or 0.0
        credit  = max(0.0, gt_sum - pc_sum - po_sum)
        return {'total': total, 'cash': cash, 'online': online, 'credit': credit}

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
            'items': items_list
        })
        
    customers = Customer.query.all()
    wallet_map = {}
    for c in customers:
        if c.wallet_balance and abs(c.wallet_balance) > 0.01: 
            wallet_map[c.name] = c.wallet_balance

    now_month = now_ist.strftime('%Y-%m')
    return render_template('sales_log.html', sales_by_date=sales_by_date, daily_stats=daily_stats, monthly_stats=monthly_stats, wallet_map=wallet_map, current_filter_date=filter_date_str, current_filter_status=filter_status, now_month=now_month)

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
    
    catalog, categories = get_all_products_catalog()
    product_catalog_json = json.dumps(catalog)
    client_list = [c.name for c in Customer.query.order_by(Customer.name).all()]
    
    bill_data = {'sales_id': sale.id, 'client_name': sale.client_name, 'items': []}
    for item in sale.items:
        gst_percent = item.gst_rate or 0.0
        base_unit_price = (item.total_price / (1 + (gst_percent/100))) / item.qty_sold if item.qty_sold > 0 else 0
        bill_data['items'].append({'category': item.category, 'name': item.product_name, 'description': item.description, 'qty': item.qty_sold, 'unit': item.unit, 'mrp': base_unit_price, 'total': item.total_price, 'gst': gst_percent})
    
    return render_template('edit_bill.html', bill=bill_data, client_list=client_list, categories=categories, product_catalog_json=product_catalog_json)

@sales_bp.route("/get_last_sale_price")
@login_required
def get_last_sale_price():
    client_name = request.args.get('client_name')
    product_name = request.args.get('product_name')
    
    if not client_name or not product_name:
        return jsonify({'found': False})

    last_item = db.session.query(SaleItem).join(Sale).filter(
        Sale.client_name == client_name,
        SaleItem.product_name.ilike(f"{product_name}%") 
    ).order_by(Sale.date.desc()).first()

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
        client_name = request.form.get('client_name')
        cart_json = request.form.get('sales_cart')
        
        sale = db.session.get(Sale, sales_id)
        if not sale or not cart_json: return redirect(url_for('sales.sales_log'))
        
        cart_items = json.loads(cart_json)
        
        for old_item in sale.items: 
            product = Product.query.filter_by(name=old_item.product_name).first()
            if not product and ' (' in old_item.product_name: 
                product = Product.query.filter_by(name=old_item.product_name.split(' (')[0]).first()
            if product: 
                product.quantity += old_item.qty_sold
                db.session.add(product)
        
        if not cart_items: 
            if sale.wallet_credit and sale.wallet_credit > 0:
                search_name = sale.client_name.split(' - ')[0].strip()
                customer = Customer.query.filter(func.lower(Customer.name) == func.lower(search_name)).first()
                if customer:
                    customer.wallet_balance -= sale.wallet_credit
                    db.session.add(customer)
                    flash(f"Reverted ₹{sale.wallet_credit} from {customer.name}'s wallet.", "info")

            db.session.delete(sale)
            db.session.commit()
            flash(f"Bill #{sales_id} deleted.", "warning")
            return redirect(url_for('sales.sales_log'))

        for old_item in list(sale.items): db.session.delete(old_item)
        
        sale.client_name = client_name
        running_total = 0.0
        
        for item in cart_items:
            qty = int(item['qty'])
            gst_rate = float(item.get('gst') or 0.0)
            if qty <= 0: continue
            
            final_name = item['name']
            if 'variation' in item and item['variation']: final_name = f"{item['name']} - {item['variation']}"
            
            product = None
            if 'id' in item and item['id']: product = db.session.get(Product, item['id'])
            if not product: 
                # Try splitting by ' (' for legacy or ' - ' for variations
                base_name_search = item['name'].split(' (')[0].split(' - ')[0]
                product = Product.query.filter(Product.name.ilike(base_name_search)).first()
            
            if product: 
                product.quantity -= qty
                db.session.add(product)
            
            line_total = float(item['total'])
            running_total += line_total
            desc = item.get('description', '')
            
            sale_item = SaleItem(sale_id=sale.id, category=item['category'], product_name=final_name, description=desc, qty_sold=qty, unit=item['unit'], total_price=line_total, gst_rate=gst_rate)
            db.session.add(sale_item)
            
        sale.grand_total = running_total
        db.session.commit()
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
                
                total_received = raw_cash + raw_online
                grand_total = sale.grand_total if sale.grand_total is not None else sum(i.total_price for i in sale.items)
                
                if sale.wallet_credit > 0:
                    search_name = sale.client_name.split(' - ')[0].strip()
                    cust = Customer.query.filter(func.lower(Customer.name) == func.lower(search_name)).first()
                    if cust:
                        cust.wallet_balance -= sale.wallet_credit
                    sale.wallet_credit = 0.0

                if total_received > grand_total:
                    extra_amount = total_received - grand_total
                    
                    if overpayment_action == 'credit':
                        if raw_cash >= grand_total:
                            sale.paid_cash = grand_total
                            sale.paid_online = 0
                        else:
                            sale.paid_cash = raw_cash
                            sale.paid_online = grand_total - raw_cash
                            
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
                        if raw_cash >= grand_total:
                            sale.paid_cash = grand_total
                            sale.paid_online = 0
                        else:
                            sale.paid_cash = raw_cash
                            sale.paid_online = grand_total - raw_cash
                        flash(f"Bill settled. Change returned: ₹{extra_amount:.2f}", "info")

                elif total_received < grand_total:
                    if underpayment_action == 'settle':
                        diff = grand_total - total_received
                        sale.grand_total = total_received
                        sale.paid_cash = raw_cash
                        sale.paid_online = raw_online
                        flash(f"Bill settled fully. Discount given: ₹{diff:.2f}", "success")
                    else:
                        sale.paid_cash = raw_cash
                        sale.paid_online = raw_online
                        flash("Partial payment recorded.", "warning")

                else:
                    sale.paid_cash = raw_cash
                    sale.paid_online = raw_online
                    flash("Payment updated.", "success")

                final_total = sale.grand_total
                final_paid = sale.paid_cash + sale.paid_online
                
                if final_paid >= final_total - 0.1: sale.payment_status = "Payment Received"
                elif final_paid > 0: sale.payment_status = "Partial Payment"
                else: sale.payment_status = "Payment Not Received"
                
                modes = []
                if sale.paid_cash > 0: modes.append("Cash")
                if sale.paid_online > 0: modes.append("Online")
                sale.payment_mode = " + ".join(modes) if modes else None
                
            db.session.commit()
    except Exception as e:
        db.session.rollback()
        print(f"ERROR: {e}")
        flash(f"Error: {e}", "danger")
        
    return redirect(url_for('sales.sales_log'))