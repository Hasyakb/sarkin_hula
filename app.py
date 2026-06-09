# app.py
import os
import uuid
import hashlib
from datetime import datetime, timedelta
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, session, abort
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from werkzeug.utils import secure_filename
from sqlalchemy.orm import joinedload

# ---------- App Configuration ----------
app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'your-secret-key-change-in-production')

# ✅ FIXED: Use PostgreSQL on Render, SQLite locally
database_url = os.environ.get('DATABASE_URL')
if database_url:
    # Fix for Render's PostgreSQL URL (needs to remove postgres:// vs postgresql://)
    if database_url.startswith('postgres://'):
        database_url = database_url.replace('postgres://', 'postgresql://', 1)
    app.config['SQLALCHEMY_DATABASE_URI'] = database_url
else:
    # Local development with SQLite
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///store.db'

app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# Upload settings
UPLOAD_FOLDER = 'static/uploads'
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max

# Ensure upload folder exists
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

db = SQLAlchemy(app)
migrate = Migrate(app, db)

# ---------- Database Models ----------
class Customer(db.Model):
    __tablename__ = 'customers'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    phone = db.Column(db.String(20), unique=True, nullable=False)
    email = db.Column(db.String(100))
    address = db.Column(db.String(200))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    items = db.relationship('Item', backref='customer', lazy='dynamic', cascade='all, delete-orphan')

    def total_unpaid(self):
        return sum(item.remaining_balance() for item in self.items if not item.is_fully_paid)
    
    def total_items_count(self):
        return self.items.count()
    
    def active_items_count(self):
        return self.items.filter_by(status='active').count()

class Item(db.Model):
    __tablename__ = 'items'
    id = db.Column(db.Integer, primary_key=True)
    unique_token = db.Column(db.String(36), unique=True, default=lambda: str(uuid.uuid4()))
    description = db.Column(db.String(200), nullable=False)
    photo_filename = db.Column(db.String(200))
    storage_price = db.Column(db.Float, default=10000.0)  # Price in Naira
    amount_paid = db.Column(db.Float, default=0.0)
    payment_type = db.Column(db.String(20), default='full')  # ONLY 'full' now
    status = db.Column(db.String(20), default='active')  # active, collected, expired
    stored_at = db.Column(db.DateTime, default=datetime.utcnow)
    collected_at = db.Column(db.DateTime, nullable=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('customers.id'), nullable=False)

    def is_fully_paid(self):
        # Always check against full storage price
        return self.amount_paid >= self.storage_price

    def remaining_balance(self):
        return max(0, self.storage_price - self.amount_paid)

    def is_expired(self):
        """Item expires 48 hours after stored_at if not collected"""
        if self.status == 'collected':
            return False
        expiry_time = self.stored_at + timedelta(hours=48)
        return datetime.utcnow() > expiry_time

    def time_remaining(self):
        """Returns human readable time remaining"""
        if self.status == 'collected':
            return "Collected"
        expiry = self.stored_at + timedelta(hours=48)
        remaining = expiry - datetime.utcnow()
        if remaining.total_seconds() <= 0:
            return "Expired"
        hours = int(remaining.total_seconds() // 3600)
        minutes = int((remaining.total_seconds() % 3600) // 60)
        return f"{hours}h {minutes}m"

class ArchivedItem(db.Model):
    __tablename__ = 'archived_items'
    id = db.Column(db.Integer, primary_key=True)
    original_item_id = db.Column(db.Integer, nullable=False)
    unique_token = db.Column(db.String(36), unique=True)
    description = db.Column(db.String(200), nullable=False)
    photo_filename = db.Column(db.String(200))
    storage_price = db.Column(db.Float, default=10000.0)
    amount_paid = db.Column(db.Float, default=0.0)
    payment_type = db.Column(db.String(20), default='full')
    customer_name = db.Column(db.String(100), nullable=False)
    customer_phone = db.Column(db.String(20), nullable=False)
    customer_email = db.Column(db.String(100))
    stored_at = db.Column(db.DateTime)
    collected_at = db.Column(db.DateTime)
    archived_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    def __repr__(self):
        return f'<ArchivedItem {self.description} - {self.customer_name}>'

# ---------- Helper Functions ----------
def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def format_naira(amount):
    """Format amount in Naira currency"""
    return f"₦{amount:,.2f}"

app.jinja_env.filters['naira'] = format_naira

@app.template_filter('timesince')
def timesince(dt, default="just now"):
    """Returns string representing 'time since' e.g. 3 days ago"""
    if dt is None:
        return default
    
    now = datetime.utcnow()
    diff = now - dt
    
    seconds = diff.total_seconds()
    
    if seconds < 60:
        return "just now"
    elif seconds < 3600:
        minutes = int(seconds // 60)
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    elif seconds < 86400:
        hours = int(seconds // 3600)
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    elif seconds < 604800:
        days = int(seconds // 86400)
        return f"{days} day{'s' if days != 1 else ''} ago"
    elif seconds < 2592000:
        weeks = int(seconds // 604800)
        return f"{weeks} week{'s' if weeks != 1 else ''} ago"
    else:
        months = int(seconds // 2592000)
        return f"{months} month{'s' if months != 1 else ''} ago"

def auto_archive_collected_items():
    """Automatically archive items that were collected more than 48 hours ago"""
    try:
        cutoff_time = datetime.utcnow() - timedelta(hours=48)
        
        # Find collected items older than 48 hours that haven't been archived yet
        items_to_archive = Item.query.filter(
            Item.status == 'collected',
            Item.collected_at <= cutoff_time
        ).all()
        
        archived_count = 0
        for item in items_to_archive:
            # Check if already archived (by original_item_id)
            existing = ArchivedItem.query.filter_by(original_item_id=item.id).first()
            if existing:
                continue
                
            # Create archive record
            archived = ArchivedItem(
                original_item_id=item.id,
                unique_token=item.unique_token,
                description=item.description,
                photo_filename=item.photo_filename,
                storage_price=item.storage_price,
                amount_paid=item.amount_paid,
                payment_type=item.payment_type,
                customer_name=item.customer.name,
                customer_phone=item.customer.phone,
                customer_email=item.customer.email,
                stored_at=item.stored_at,
                collected_at=item.collected_at,
                archived_at=datetime.utcnow()
            )
            db.session.add(archived)
            archived_count += 1
        
        if archived_count > 0:
            db.session.commit()
            print(f"[Auto-Archive] Archived {archived_count} collected items")
        
        return archived_count
    except Exception as e:
        print(f"[Auto-Archive Error] {str(e)}")
        db.session.rollback()
        return 0

# ---------- Routes ----------
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/stats')
def api_stats():
    """Return quick statistics for the dashboard"""
    total_items = Item.query.count()
    active_items = Item.query.filter_by(status='active').count()
    total_unpaid = sum(item.remaining_balance() for item in Item.query.filter_by(status='active').all())
    
    return jsonify({
        'total_items': total_items,
        'active_items': active_items,
        'total_unpaid': total_unpaid
    })

# ---------- Customer Routes ----------
@app.route('/customers')
def customer_list():
    """Display all customers with search functionality"""
    search_query = request.args.get('search', '').strip()
    
    query = Customer.query
    
    if search_query:
        query = query.filter(
            db.or_(
                Customer.name.ilike(f'%{search_query}%'),
                Customer.phone.ilike(f'%{search_query}%'),
                Customer.email.ilike(f'%{search_query}%')
            )
        )
    
    customers = query.order_by(Customer.created_at.desc()).all()
    
    return render_template('customer_list.html', customers=customers, search_query=search_query)

@app.route('/customer/<int:customer_id>')
def customer_detail(customer_id):
    """View customer details and their items"""
    customer = Customer.query.get_or_404(customer_id)
    items = Item.query.filter_by(customer_id=customer_id).order_by(Item.stored_at.desc()).all()
    
    # Update expired status
    for item in items:
        if item.is_expired() and item.status == 'active':
            item.status = 'expired'
            db.session.commit()
    
    return render_template('customer_detail.html', customer=customer, items=items)

@app.route('/customer/new', methods=['GET', 'POST'])
def new_customer():
    if request.method == 'POST':
        name = request.form.get('name')
        phone = request.form.get('phone')
        email = request.form.get('email')
        address = request.form.get('address')
        
        if not name or not phone:
            flash('Name and phone are required', 'danger')
            return redirect(url_for('new_customer'))
        
        # Check if customer exists
        existing = Customer.query.filter_by(phone=phone).first()
        if existing:
            flash(f'Customer {existing.name} already exists with this phone number!', 'warning')
            return redirect(url_for('customer_detail', customer_id=existing.id))
        
        customer = Customer(name=name, phone=phone, email=email, address=address)
        db.session.add(customer)
        db.session.commit()
        
        flash(f'Customer {name} created successfully!', 'success')
        return redirect(url_for('store_item', customer_id=customer.id))
    
    return render_template('new_customer.html')

@app.route('/customer/<int:customer_id>/edit', methods=['GET', 'POST'])
def edit_customer(customer_id):
    customer = Customer.query.get_or_404(customer_id)
    
    if request.method == 'POST':
        customer.name = request.form.get('name')
        customer.phone = request.form.get('phone')
        customer.email = request.form.get('email')
        customer.address = request.form.get('address')
        
        db.session.commit()
        flash('Customer information updated!', 'success')
        return redirect(url_for('customer_detail', customer_id=customer.id))
    
    return render_template('edit_customer.html', customer=customer)

@app.route('/customer/<int:customer_id>/delete', methods=['POST'])
def delete_customer(customer_id):
    """Delete a customer and all their items"""
    customer = Customer.query.get_or_404(customer_id)
    
    # Delete photos from filesystem
    for item in customer.items:
        if item.photo_filename:
            photo_path = os.path.join(app.config['UPLOAD_FOLDER'], item.photo_filename)
            if os.path.exists(photo_path):
                os.remove(photo_path)
    
    db.session.delete(customer)
    db.session.commit()
    
    flash(f'Customer {customer.name} and all their items have been deleted', 'success')
    return redirect(url_for('customer_list'))

@app.route('/store/<int:customer_id>', methods=['GET', 'POST'])
def store_item(customer_id):
    customer = Customer.query.get_or_404(customer_id)
    
    if request.method == 'POST':
        description = request.form.get('description')
        storage_price = float(request.form.get('storage_price', 10000))
        amount_paid = float(request.form.get('amount_paid', 0))
        
        # Handle photo upload
        photo_file = request.files.get('photo')
        photo_filename = None
        if photo_file and allowed_file(photo_file.filename):
            ext = photo_file.filename.rsplit('.', 1)[1].lower()
            filename = f"{uuid.uuid4().hex}.{ext}"
            photo_file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
            photo_filename = filename
        
        item = Item(
            description=description,
            photo_filename=photo_filename,
            storage_price=storage_price,
            amount_paid=amount_paid,
            payment_type='full',  # Always full payment
            customer_id=customer.id
        )
        db.session.add(item)
        db.session.commit()
        
        flash(f'Item stored successfully! Token: {item.unique_token[:8]}...', 'success')
        return redirect(url_for('customer_detail', customer_id=customer.id))
    
    return render_template('store_item.html', customer=customer)

@app.route('/item/<token>/pay', methods=['GET', 'POST'])
def make_payment(token):
    item = Item.query.filter_by(unique_token=token).first_or_404()
    customer = item.customer
    
    if request.method == 'POST':
        amount = float(request.form.get('amount', 0))
        if amount <= 0:
            flash('Amount must be positive', 'danger')
        else:
            item.amount_paid += amount
            if item.is_fully_paid():
                flash(f'Payment complete! Item is now fully paid.', 'success')
            else:
                remaining = item.remaining_balance()
                flash(f'Payment of ₦{amount:,.2f} received! Remaining: ₦{remaining:,.2f}', 'success')
            db.session.commit()
        return redirect(url_for('customer_detail', customer_id=customer.id))
    
    return render_template('make_payment.html', item=item, customer=customer)

@app.route('/item/<token>/collect', methods=['POST'])
def collect_item(token):
    item = Item.query.filter_by(unique_token=token).first_or_404()
    
    if not item.is_fully_paid():
        flash('Cannot collect: Full payment required first', 'danger')
        return redirect(url_for('customer_detail', customer_id=item.customer.id))
    
    if item.status == 'collected':
        flash('Item already collected', 'warning')
    else:
        item.status = 'collected'
        item.collected_at = datetime.utcnow()
        db.session.commit()
        flash('Item collected successfully!', 'success')
    
    return redirect(url_for('customer_detail', customer_id=item.customer.id))

# ---------- Dashboard (public, no login required) ----------
@app.route('/dashboard')
def dashboard():
    """Public dashboard to view all items"""
    # Auto-archive collected items older than 48 hours
    auto_archive_collected_items()
    
    # Search and filter parameters
    search_query = request.args.get('search', '').strip()
    filter_status = request.args.get('status', 'all')
    filter_payment = request.args.get('payment', 'all')
    
    # Base query with eager loading
    query = Item.query.options(joinedload(Item.customer))
    
    # Apply search (customer name, phone, item description, token)
    if search_query:
        query = query.join(Customer).filter(
            db.or_(
                Customer.name.ilike(f'%{search_query}%'),
                Customer.phone.ilike(f'%{search_query}%'),
                Item.description.ilike(f'%{search_query}%'),
                Item.unique_token.ilike(f'%{search_query}%')
            )
        )
    
    # Apply status filter
    if filter_status != 'all':
        query = query.filter(Item.status == filter_status)
    
    # Apply payment filter
    if filter_payment == 'paid':
        # Get all items and filter those that are fully paid
        items_list = query.all()
        items = [item for item in items_list if item.is_fully_paid()]
    elif filter_payment == 'unpaid':
        items_list = query.all()
        items = [item for item in items_list if not item.is_fully_paid()]
    else:
        items = query.all()
    
    # If we used query.all() above, sort it
    if filter_payment in ['paid', 'unpaid']:
        items = sorted(items, key=lambda x: x.stored_at, reverse=True)
    else:
        items = query.order_by(Item.stored_at.desc()).all()
    
    # Update expired statuses
    for item in items:
        if item.is_expired() and item.status == 'active':
            item.status = 'expired'
            db.session.commit()
    
    # Stats for dashboard
    total_items = Item.query.count()
    active_items = Item.query.filter_by(status='active').count()
    collected_items = Item.query.filter_by(status='collected').count()
    expired_items = Item.query.filter_by(status='expired').count()
    total_unpaid_balance = sum(item.remaining_balance() for item in Item.query.filter_by(status='active').all())
    
    # Customer stats
    total_customers = Customer.query.count()
    
    return render_template('dashboard.html',
                         items=items,
                         search_query=search_query,
                         filter_status=filter_status,
                         filter_payment=filter_payment,
                         total_items=total_items,
                         active_items=active_items,
                         collected_items=collected_items,
                         expired_items=expired_items,
                         total_unpaid_balance=total_unpaid_balance,
                         total_customers=total_customers)

@app.route('/delete-expired', methods=['POST'])
def delete_expired_items():
    """Delete all expired items permanently"""
    expired_items = Item.query.filter_by(status='expired').all()
    
    if not expired_items:
        flash('No expired items to delete', 'info')
        return redirect(url_for('dashboard'))
    
    # Delete photos from filesystem
    for item in expired_items:
        if item.photo_filename:
            photo_path = os.path.join(app.config['UPLOAD_FOLDER'], item.photo_filename)
            if os.path.exists(photo_path):
                os.remove(photo_path)
    
    # Delete items from database
    count = Item.query.filter_by(status='expired').delete()
    db.session.commit()
    
    flash(f'Successfully deleted {count} expired item(s)', 'success')
    return redirect(url_for('dashboard'))

# ---------- Archive Routes ----------
@app.route('/archive')
def archive_history():
    """View archived items history"""
    try:
        search_query = request.args.get('search', '').strip()
        filter_date = request.args.get('date', 'all')  # today, week, month, all
        
        query = ArchivedItem.query
        
        # Search filter
        if search_query:
            query = query.filter(
                db.or_(
                    ArchivedItem.description.ilike(f'%{search_query}%'),
                    ArchivedItem.customer_name.ilike(f'%{search_query}%'),
                    ArchivedItem.customer_phone.ilike(f'%{search_query}%'),
                    ArchivedItem.unique_token.ilike(f'%{search_query}%')
                )
            )
        
        # Date filter
        now = datetime.utcnow()
        if filter_date == 'today':
            start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            query = query.filter(ArchivedItem.archived_at >= start)
        elif filter_date == 'week':
            start = now - timedelta(days=7)
            query = query.filter(ArchivedItem.archived_at >= start)
        elif filter_date == 'month':
            start = now - timedelta(days=30)
            query = query.filter(ArchivedItem.archived_at >= start)
        
        archived_items = query.order_by(ArchivedItem.archived_at.desc()).all()
        
        # Statistics
        total_archived = ArchivedItem.query.count()
        total_value = sum(item.storage_price for item in archived_items)
        total_paid = sum(item.amount_paid for item in archived_items)
        
        return render_template('archive_history.html',
                             archived_items=archived_items,
                             search_query=search_query,
                             filter_date=filter_date,
                             total_archived=total_archived,
                             total_value=total_value,
                             total_paid=total_paid)
    except Exception as e:
        print(f"Archive error: {str(e)}")
        flash(f'Error loading archive: {str(e)}', 'danger')
        return render_template('archive_history.html', archived_items=[], total_archived=0, total_value=0, total_paid=0)

@app.route('/archive/<int:archive_id>/restore', methods=['POST'])
def restore_from_archive(archive_id):
    """Restore an archived item back to active items"""
    try:
        archived = ArchivedItem.query.get_or_404(archive_id)
        
        # Check if original customer still exists
        customer = Customer.query.filter_by(phone=archived.customer_phone).first()
        if not customer:
            flash(f'Cannot restore: Customer {archived.customer_name} no longer exists', 'danger')
            return redirect(url_for('archive_history'))
        
        # Create new item from archived data
        new_item = Item(
            description=archived.description,
            photo_filename=archived.photo_filename,
            storage_price=archived.storage_price,
            amount_paid=archived.amount_paid,
            payment_type=archived.payment_type,
            status='active',
            stored_at=archived.stored_at,
            customer_id=customer.id
        )
        db.session.add(new_item)
        db.session.commit()
        
        flash(f'Item "{archived.description}" restored successfully!', 'success')
    except Exception as e:
        flash(f'Error restoring item: {str(e)}', 'danger')
    
    return redirect(url_for('archive_history'))

@app.route('/archive/clear', methods=['POST'])
def clear_old_archives():
    """Delete archives older than specified days"""
    try:
        days = int(request.form.get('days', 90))
        cutoff = datetime.utcnow() - timedelta(days=days)
        
        old_archives = ArchivedItem.query.filter(ArchivedItem.archived_at <= cutoff).all()
        count = len(old_archives)
        
        # Delete photos from filesystem
        for archive in old_archives:
            if archive.photo_filename:
                photo_path = os.path.join(app.config['UPLOAD_FOLDER'], archive.photo_filename)
                if os.path.exists(photo_path):
                    os.remove(photo_path)
        
        # Delete from database
        ArchivedItem.query.filter(ArchivedItem.archived_at <= cutoff).delete()
        db.session.commit()
        
        flash(f'Deleted {count} archived items older than {days} days', 'success')
    except Exception as e:
        flash(f'Error clearing archives: {str(e)}', 'danger')
    
    return redirect(url_for('archive_history'))

# ---------- API Endpoints ----------
@app.route('/api/item/<token>')
def api_get_item(token):
    item = Item.query.filter_by(unique_token=token).first_or_404()
    return jsonify({
        'token': item.unique_token,
        'description': item.description,
        'status': item.status,
        'paid': item.amount_paid,
        'required': item.storage_price,
        'remaining': item.remaining_balance(),
        'time_remaining': item.time_remaining()
    })

@app.route('/api/archive/stats')
def api_archive_stats():
    """Return archive statistics"""
    try:
        total_archived = ArchivedItem.query.count()
        total_value = sum(item.storage_price for item in ArchivedItem.query.all())
        total_paid = sum(item.amount_paid for item in ArchivedItem.query.all())
        
        # Monthly breakdown
        from sqlalchemy import func
        monthly = db.session.query(
            func.strftime('%Y-%m', ArchivedItem.archived_at).label('month'),
            func.count(ArchivedItem.id).label('count'),
            func.sum(ArchivedItem.storage_price).label('value')
        ).group_by('month').order_by('month').all()
        
        return jsonify({
            'total_archived': total_archived,
            'total_value': float(total_value) if total_value else 0,
            'total_paid': float(total_paid) if total_paid else 0,
            'monthly': [{'month': m.month, 'count': m.count, 'value': float(m.value) if m.value else 0} for m in monthly]
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    port = int(os.environ.get('PORT', 5000))
    app.run(debug=False, host='0.0.0.0', port=port)
