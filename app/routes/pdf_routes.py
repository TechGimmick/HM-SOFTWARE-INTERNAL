from flask import Blueprint, make_response, request, redirect, url_for
from flask_login import login_required
from fpdf import FPDF
from app.models import Product, Sale, Purchase, Supplier
from app.extensions import db

pdf_bp = Blueprint('pdf', __name__)

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

@pdf_bp.route("/download_inventory_report")
@login_required
def download_inventory_report():
    filter_by = request.args.get('filter_by'); filter_val = request.args.get('filter_val')
    title = "Full Inventory Report"
    query = Product.query
    if filter_by == 'category' and filter_val:
        query = query.filter_by(category=filter_val)
        title = f"Inventory Report: Category - {filter_val}"
    elif filter_by == 'supplier' and filter_val:
        query = query.filter_by(supplier_id=filter_val)
        sup = db.session.get(Supplier, filter_val)
        sup_name = sup.name if sup else "Unknown"
        title = f"Inventory Report: Supplier - {sup_name}"
    products = query.all()
    
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", 'B', 16)
    pdf.cell(0, 10, title, align='C', ln=1)
    pdf.ln(5)
    
    pdf.set_font("Helvetica", 'B', 10)
    pdf.cell(10, 10, "S.No", 1, 0, 'C')
    pdf.cell(90, 10, "Product Name", 1, 0, 'L')
    pdf.cell(30, 10, "HSN Code", 1, 0, 'C')
    pdf.cell(30, 10, "List Price", 1, 0, 'R')
    pdf.cell(30, 10, "Current Qty", 1, 1, 'C')
    
    pdf.set_font("Helvetica", size=10)
    for i, p in enumerate(products, 1):
        x_start = pdf.get_x()
        y_start = pdf.get_y()
        if y_start > 270:
            pdf.add_page()
            y_start = pdf.get_y()
            x_start = pdf.get_x()
        
        product_name = str(p.name)
        if p.quantity <= p.min_stock:
            product_name = "[X] " + product_name
            
        pdf.set_xy(x_start + 10, y_start)
        pdf.multi_cell(90, 8, product_name, 1, 'L')
        y_end = pdf.get_y()
        row_height = y_end - y_start
        
        pdf.set_xy(x_start, y_start)
        pdf.cell(10, row_height, str(i), 1, 0, 'C')
        
        pdf.set_xy(x_start + 10 + 90, y_start)
        pdf.cell(30, row_height, str(p.hsn_code or '-'), 1, 0, 'C')
        
        pdf.set_xy(x_start + 10 + 90 + 30, y_start)
        pdf.cell(30, row_height, f"{p.mrp:.2f}", 1, 0, 'R')
        
        pdf.set_xy(x_start + 10 + 90 + 30 + 30, y_start)
        pdf.cell(30, row_height, str(p.quantity), 1, 1, 'C')
        
        pdf.set_y(y_end)
        
    pdf_content = pdf.output(dest='S')
    response_data = bytes(pdf_content) if isinstance(pdf_content, (bytes, bytearray)) else pdf_content.encode('latin-1')
    resp = make_response(response_data)
    resp.headers['Content-Type'] = 'application/pdf'
    resp.headers['Content-Disposition'] = f'attachment; filename=Inventory_Report.pdf'
    return resp

@pdf_bp.route("/download_bill/<int:sales_id>")
@login_required
def download_bill(sales_id):
    sale = db.session.get(Sale, sales_id)
    if not sale: return redirect(url_for('sales.sales_log'))
    bill_format = request.args.get('format', 'regular')
    formatted_date = sale.date.strftime('%Y-%m-%d')
    
    grand_total = sale.grand_total if sale.grand_total else sum(i.total_price for i in sale.items)
    total_paid = (sale.paid_cash or 0) + (sale.paid_online or 0)
    balance_due = grand_total - total_paid
    has_gst = any((i.gst_rate or 0) > 0 for i in sale.items)

    def get_clean_name_and_hsn(raw_name):
        variation_part = ""; base_name_part = raw_name
        if ' - ' in raw_name: parts = raw_name.split(' - '); base_name_part = parts[0]; variation_part = " - " + parts[1]
        if base_name_part.endswith(')'):
            last_open = base_name_part.rfind(' (')
            if last_open != -1: base_name_part = base_name_part[:last_open]
        final_display_name = base_name_part + variation_part
        product_obj = Product.query.filter(Product.name.ilike(base_name_part)).first()
        hsn_val = str(product_obj.hsn_code) if product_obj and product_obj.hsn_code else "-"
        return final_display_name, hsn_val

    if bill_format == 'barcode':
        pdf = FPDF(orientation='P', unit='mm', format=(72, 297))
        pdf.set_margins(1, 2, 1)
        pdf.add_page()
        
        pdf.set_font("Helvetica", 'B', 10)
        pdf.cell(0, 5, "ESTIMATE", align='C', ln=1)
        pdf.ln(2)
        pdf.set_font("Helvetica", size=9)
        pdf.cell(30, 5, f"Bill No: {sale.id}", align='L')
        pdf.cell(0, 5, f"Date: {formatted_date}", align='R', ln=1)
        pdf.multi_cell(0, 5, f"Client: {sale.client_name}", align='L')
        pdf.cell(0, 4, "-"*38, align='C', ln=1)
        
        pdf.set_font("Helvetica", 'B', 7)
        pdf.cell(4, 5, "Sn", align='C')
        pdf.cell(28, 5, "Item", align='L')
        pdf.cell(6, 5, "Qty", align='C')
        pdf.cell(12, 5, "Rate", align='R')
        pdf.cell(15, 5, "Total", align='R', ln=1)
        
        pdf.set_font("Helvetica", size=7)
        
        for i, item in enumerate(sale.items):
            display_name, _ = get_clean_name_and_hsn(str(item.product_name))
            effective_rate = item.total_price / item.qty_sold if item.qty_sold > 0 else 0
            
            start_x = pdf.get_x()
            start_y = pdf.get_y()
            
            pdf.set_xy(start_x + 4, start_y)
            pdf.multi_cell(28, 4, display_name, align='L') 
            
            end_y = pdf.get_y()
            text_height = end_y - start_y
            row_height = max(text_height + 3, 6)
            
            pdf.set_xy(start_x, start_y)
            pdf.cell(4, 4, str(i+1), 0, 0, 'C')
            
            pdf.set_xy(start_x + 4 + 28, start_y)
            pdf.cell(6, 4, str(item.qty_sold), 0, 0, 'C')
            
            pdf.set_xy(start_x + 4 + 28 + 6, start_y)
            pdf.cell(12, 4, f"{effective_rate:.0f}", 0, 0, 'R')
            
            pdf.set_xy(start_x + 4 + 28 + 6 + 12, start_y)
            pdf.cell(15, 4, f"{item.total_price:.2f}", 0, 0, 'R')

            pdf.set_y(start_y + row_height)

            # Small GST note below the row if item has GST
            if (item.gst_rate or 0) > 0:
                pdf.set_font("Helvetica", 'I', 6)
                pdf.cell(0, 3, f"  ({item.gst_rate:.0f}% GST incl.)", align='L', ln=1)
                pdf.set_font("Helvetica", size=7)
            
        pdf.cell(0, 4, "-"*38, align='C', ln=1)
        pdf.set_font("Helvetica", 'B', 9)
        pdf.cell(0, 6, f"Total: Rs. {grand_total:.2f}", align='R', ln=1)
        
        if total_paid > 0:
            pdf.set_font("Helvetica", '', 7)
            if sale.paid_cash > 0: pdf.cell(0, 4, f"Paid (Cash): {sale.paid_cash:.2f}", align='R', ln=1)
            if sale.paid_online > 0: pdf.cell(0, 4, f"Paid (Online): {sale.paid_online:.2f}", align='R', ln=1)
            
        if balance_due > 0:
            pdf.set_font("Helvetica", 'B', 8)
            pdf.cell(0, 5, f"Balance Due: {balance_due:.2f}", align='R', ln=1)
            
        pdf.set_font("Helvetica", '', 7)
        if has_gst:
            pdf.cell(0, 4, "(GST Extra)", align='R', ln=1)
        pdf.ln(5)

    else:
        pdf = FPDF()
        pdf.add_page()
        pdf.set_font("Helvetica", 'B', 14)
        pdf.cell(0, 10, "ESTIMATE", align='C', ln=1)
        pdf.ln(5)
        pdf.set_font("Helvetica", size=12)
        pdf.cell(95, 8, f"Bill No: {sale.id}", align='L')
        pdf.cell(95, 8, f"Date: {formatted_date}", align='R', ln=1)
        pdf.cell(0, 8, f"Billed To: {sale.client_name}", align='L', ln=1)
        pdf.ln(5)
        
        # Regular A4 bill header — with or without GST column
        pdf.set_font("Helvetica", 'B', 10)
        if has_gst:
            pdf.cell(10, 10, "Sn", 1, 0, 'C')
            pdf.cell(55, 10, "Item Description", 1, 0, 'L')
            pdf.cell(20, 10, "HSN", 1, 0, 'C')
            pdf.cell(15, 10, "Qty", 1, 0, 'C')
            pdf.cell(20, 10, "GST%", 1, 0, 'C')
            pdf.cell(25, 10, "Rate", 1, 0, 'R')
            pdf.cell(45, 10, "Total (Rs.)", 1, 1, 'R')
        else:
            pdf.cell(10, 10, "Sn", 1, 0, 'C')
            pdf.cell(60, 10, "Item Description", 1, 0, 'L')
            pdf.cell(20, 10, "HSN", 1, 0, 'C')
            pdf.cell(20, 10, "Qty", 1, 0, 'C')
            pdf.cell(30, 10, "Rate", 1, 0, 'R')
            pdf.cell(50, 10, "Total (Rs.)", 1, 1, 'R')
        
        pdf.set_font("Helvetica", size=10)
        for i, item in enumerate(sale.items):
            display_name, hsn_val = get_clean_name_and_hsn(str(item.product_name)) 
            final_text = display_name
            if item.description: final_text += f"\n{item.description}"
            effective_rate = item.total_price / item.qty_sold if item.qty_sold > 0 else 0
            gst_str = f"{item.gst_rate:.0f}%" if (item.gst_rate or 0) > 0 else "-"
            
            start_x = pdf.get_x(); start_y = pdf.get_y()
            if has_gst:
                pdf.set_xy(start_x + 10, start_y)
                pdf.multi_cell(55, 5, final_text, 1, 'L')
                end_y = pdf.get_y(); row_height = end_y - start_y
                pdf.set_xy(start_x, start_y); pdf.cell(10, row_height, str(i+1), 1, 0, 'C')
                pdf.set_xy(start_x + 10 + 55, start_y); pdf.cell(20, row_height, hsn_val, 1, 0, 'C')
                pdf.set_xy(start_x + 10 + 55 + 20, start_y); pdf.cell(15, row_height, str(item.qty_sold), 1, 0, 'C')
                pdf.set_xy(start_x + 10 + 55 + 20 + 15, start_y); pdf.cell(20, row_height, gst_str, 1, 0, 'C')
                pdf.set_xy(start_x + 10 + 55 + 20 + 15 + 20, start_y); pdf.cell(25, row_height, f"{effective_rate:.2f}", 1, 0, 'R')
                pdf.set_xy(start_x + 10 + 55 + 20 + 15 + 20 + 25, start_y); pdf.cell(45, row_height, f"{item.total_price:.2f}", 1, 1, 'R')
            else:
                pdf.set_xy(start_x + 10, start_y)
                pdf.multi_cell(60, 5, final_text, 1, 'L')
                end_y = pdf.get_y(); row_height = end_y - start_y
                pdf.set_xy(start_x, start_y); pdf.cell(10, row_height, str(i+1), 1, 0, 'C')
                pdf.set_xy(start_x + 10 + 60, start_y); pdf.cell(20, row_height, hsn_val, 1, 0, 'C')
                pdf.set_xy(start_x + 10 + 60 + 20, start_y); pdf.cell(20, row_height, str(item.qty_sold), 1, 0, 'C')
                pdf.set_xy(start_x + 10 + 60 + 20 + 20, start_y); pdf.cell(30, row_height, f"{effective_rate:.2f}", 1, 0, 'R')
                pdf.set_xy(start_x + 10 + 60 + 20 + 20 + 30, start_y); pdf.cell(50, row_height, f"{item.total_price:.2f}", 1, 1, 'R')
            pdf.set_y(end_y)
            
        pdf.cell(0, 4, "-"*38, align='C', ln=1)
        pdf.set_font("Helvetica", 'B', 12); pdf.cell(140, 10, "Grand Total", 1, 0, 'R'); pdf.cell(50, 10, f"Rs. {grand_total:.2f}", 1, 1, 'R')
        
        if total_paid > 0:
            pdf.set_font("Helvetica", '', 10)
            if sale.paid_cash > 0: pdf.cell(140, 6, "Paid (Cash)", 1, 0, 'R'); pdf.cell(50, 6, f"{sale.paid_cash:.2f}", 1, 1, 'R')
            if sale.paid_online > 0: pdf.cell(140, 6, "Paid (Online)", 1, 0, 'R'); pdf.cell(50, 6, f"{sale.paid_online:.2f}", 1, 1, 'R')
            
        if balance_due > 0:
            pdf.set_font("Helvetica", 'B', 10)
            pdf.cell(140, 8, "Balance Due", 1, 0, 'R'); pdf.cell(50, 8, f"{balance_due:.2f}", 1, 1, 'R')

        pdf.set_font("Helvetica", '', 8)
        if has_gst:
            pdf.cell(0, 6, "(GST Included)", align='R', ln=1)
        pdf.ln(10); pdf.set_font("Helvetica", 'I', 8); pdf.cell(0, 5, "This is a computer generated invoice.", align='C', ln=1)
    
    pdf_content = pdf.output(dest='S')
    response_data = bytes(pdf_content) if isinstance(pdf_content, (bytes, bytearray)) else pdf_content.encode('latin-1')
    resp = make_response(response_data)
    resp.headers['Content-Type'] = 'application/pdf'
    resp.headers['Content-Disposition'] = f'attachment; filename=Bill_{sales_id}_{bill_format}.pdf'
    return resp