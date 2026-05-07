from app.extensions import db
from flask_login import UserMixin
# from datetime import datetime
import datetime
from werkzeug.security import generate_password_hash, check_password_hash

# --- MODELS ---
class Supplier(db.Model):
    __tablename__ = 'suppliers'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False, unique=True)
    contact_info = db.Column(db.String(200))
    products = db.relationship('Product', backref='supplier', lazy=True)

class Product(db.Model):
    __tablename__ = 'products'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False) # Sales Name
    
    # --- NEW COLUMN FOR PO NAME ---
    purchase_name = db.Column(db.String(500), nullable=True) 
    
    category = db.Column(db.String(100))
    unit = db.Column(db.String(50))
    purchase_price = db.Column(db.Float, default=0.0) 
    mrp = db.Column(db.Float, default=0.0)            
    quantity = db.Column(db.Integer, default=0) 
    pack_size = db.Column(db.Integer, default=1)
    min_stock = db.Column(db.Integer, default=10)   
    max_stock = db.Column(db.Integer, default=100)  
    hsn_code = db.Column(db.String(50), nullable=True)
    gst_rate = db.Column(db.Float, default=0.0)
    barcode = db.Column(db.String(100), unique=True, nullable=True)
    has_subcategory = db.Column(db.Boolean, default=False)
    subcategory_type = db.Column(db.String(100), nullable=True) 
    subcategory_options = db.Column(db.String(500), nullable=True) 
    supplier_id = db.Column(db.Integer, db.ForeignKey('suppliers.id'), nullable=True)
    last_purchased_date = db.Column(db.DateTime, nullable=True) #New Date Column
# In app.py

class Customer(db.Model):
    __tablename__ = 'customers'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False, unique=True)
    phone = db.Column(db.String(20), nullable=True)
    # --- ADD THIS LINE BELOW ---
    wallet_balance = db.Column(db.Float, default=0.0)
class Sale(db.Model):
    __tablename__ = 'sales'
    id = db.Column(db.Integer, primary_key=True)
    client_name = db.Column(db.String(200))
    date = db.Column(db.DateTime, default=datetime.datetime.utcnow)
    order_status = db.Column(db.String(50), default='Order Pending')
    payment_status = db.Column(db.String(50), default='Payment Not Received')
    payment_mode = db.Column(db.String(50), nullable=True)
    grand_total = db.Column(db.Float, default=0.0)
    
    paid_cash = db.Column(db.Float, default=0.0)
    paid_online = db.Column(db.Float, default=0.0)
    
    # --- NEW COLUMN TO TRACK WALLET CREDIT ---
    wallet_credit = db.Column(db.Float, default=0.0)
    
    # --- WAREHOUSE ID ---
    warehouse_id = db.Column(db.Integer, db.ForeignKey('warehouses.id'), nullable=True)

    items = db.relationship('SaleItem', backref='sale', lazy=True, cascade="all, delete-orphan")    
    

class SaleItem(db.Model):
    __tablename__ = 'sale_items'
    id = db.Column(db.Integer, primary_key=True)
    sale_id = db.Column(db.Integer, db.ForeignKey('sales.id'), nullable=False)
    category = db.Column(db.String(100))
    product_name = db.Column(db.String(200))
    
    # --- DESCRIPTION FIELD ---
    description = db.Column(db.String(500), nullable=True)
    
    qty_sold = db.Column(db.Integer)
    unit = db.Column(db.String(50))
    total_price = db.Column(db.Float)
    gst_rate = db.Column(db.Float, default=0.0) 

class Purchase(db.Model):
    __tablename__ = 'purchases'
    id = db.Column(db.Integer, primary_key=True)
    supplier_name = db.Column(db.String(200)) 
    supplier_id = db.Column(db.Integer, db.ForeignKey('suppliers.id'), nullable=True)
    category = db.Column(db.String(100))
    
    # CHANGED TO TEXT TO SUPPORT UNLIMITED ITEMS
    product_name = db.Column(db.Text) 
    
    qty_purchased = db.Column(db.Integer)
    unit_price = db.Column(db.Float, default=0.0) 
    date = db.Column(db.DateTime, default=datetime.datetime.utcnow)
    received_date = db.Column(db.DateTime, nullable=True)
    status = db.Column(db.String(50), default='Pending')
    received_details = db.Column(db.Text, nullable=True) # JSON of partial receives
    product_id = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=True)

class TallyBill(db.Model):
    __tablename__ = 'tally_bills'
    id = db.Column(db.Integer, primary_key=True)
    client_name = db.Column(db.String(200), nullable=True)
    invoice_number = db.Column(db.String(100))
    date = db.Column(db.DateTime, default=datetime.datetime.utcnow)
    payment_status = db.Column(db.String(50), default='Payment Not Received')
    order_status = db.Column(db.String(50), default='Order Pending')
    credit_period = db.Column(db.Integer, nullable=True)
    payment_mode = db.Column(db.String(50), nullable=True)
    paid_cash = db.Column(db.Float, default=0.0)
    paid_online = db.Column(db.Float, default=0.0)
    grand_total = db.Column(db.Float, default=0.0)
    customer_email = db.Column(db.String(150), nullable=True)
    customer_phone = db.Column(db.String(20), nullable=True)
    warehouse_id = db.Column(db.Integer, db.ForeignKey('warehouses.id'), nullable=True)
    items = db.relationship('TallyBillItem', backref='bill', lazy=True, cascade="all, delete-orphan")

class TallyBillItem(db.Model):
    __tablename__ = 'tally_bill_items'
    id = db.Column(db.Integer, primary_key=True)
    tally_bill_id = db.Column(db.Integer, db.ForeignKey('tally_bills.id'), nullable=False)
    product_name = db.Column(db.String(200))
    qty = db.Column(db.Integer)
    product_id = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=True)

class User(UserMixin, db.Model):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    role = db.Column(db.String(20), nullable=False, default='sales')

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class Warehouse(db.Model):
    __tablename__ = 'warehouses'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False, unique=True)
    location = db.Column(db.String(500), nullable=True)
    is_default = db.Column(db.Boolean, default=False)
    
    # Relationships
    stocks = db.relationship('WarehouseStock', backref='warehouse', lazy=True, cascade="all, delete-orphan")


class WarehouseStock(db.Model):
    __tablename__ = 'warehouse_stock'
    id = db.Column(db.Integer, primary_key=True)
    warehouse_id = db.Column(db.Integer, db.ForeignKey('warehouses.id'), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=False)
    quantity = db.Column(db.Integer, default=0)

    # Relationship to product
    product = db.relationship('Product', backref=db.backref('warehouse_stocks', lazy=True, cascade="all, delete-orphan"))


class StockTransfer(db.Model):
    __tablename__ = 'stock_transfers'
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=False)
    from_warehouse_id = db.Column(db.Integer, db.ForeignKey('warehouses.id'), nullable=True)
    to_warehouse_id = db.Column(db.Integer, db.ForeignKey('warehouses.id'), nullable=True)
    quantity = db.Column(db.Integer, nullable=False)
    date = db.Column(db.DateTime, default=datetime.datetime.utcnow)
    delivery_method = db.Column(db.String(100), nullable=True)
    reference = db.Column(db.String(200), nullable=True)
    
    product = db.relationship('Product', backref='transfers')
    from_warehouse = db.relationship('Warehouse', foreign_keys=[from_warehouse_id], backref='transfers_out')
    to_warehouse = db.relationship('Warehouse', foreign_keys=[to_warehouse_id], backref='transfers_in')


class CashBookEntry(db.Model):
    """
    Manual debit/expense entries for the daily Cash Book.
    Retail & Tally cash/online sales are derived dynamically; only
    manual items (e.g. tea, freight, petty cash) live in this table.
    """
    __tablename__ = 'cashbook_entries'
    id           = db.Column(db.Integer, primary_key=True)
    date         = db.Column(db.Date,    nullable=False, index=True)
    description  = db.Column(db.String(300), nullable=False)
    amount       = db.Column(db.Float,   default=0.0, nullable=False)
    payment_mode = db.Column(db.String(20), default='Cash')  # 'Cash' | 'Online'
    created_by   = db.Column(db.String(50), nullable=True)
    created_at   = db.Column(db.DateTime, default=datetime.datetime.utcnow)

