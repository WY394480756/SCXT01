import sqlite3
import json
import os
import shutil
import csv
from io import StringIO, BytesIO
from datetime import datetime, date
from flask import Flask, render_template_string, request, jsonify, session, redirect, url_for, flash, Response, send_file
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

app = Flask(__name__)
app.secret_key = 'your-secret-key-change-in-production'
app.config['SESSION_COOKIE_HTTPONLY'] = True

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'supermarket.db')
BACKUP_DIR = os.path.join(BASE_DIR, 'backups')
if not os.path.exists(BACKUP_DIR):
    os.makedirs(BACKUP_DIR)

# ---------- 数据库初始化（含动态迁移）---------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    # 商品表
    c.execute('''CREATE TABLE IF NOT EXISTS products (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        price REAL NOT NULL CHECK(price >= 0),
        cost REAL DEFAULT 0 CHECK(cost >= 0),
        stock INTEGER NOT NULL CHECK(stock >= 0)
    )''')
    
    # 用户表
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        role TEXT NOT NULL CHECK(role IN ('admin', 'operator')),
        fullname TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )''')
    
    # 会员表
    c.execute('''CREATE TABLE IF NOT EXISTS members (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        phone TEXT UNIQUE,
        card_type TEXT NOT NULL,
        balance REAL DEFAULT 0,
        remaining_counts INTEGER DEFAULT 0,
        valid_from TEXT,
        valid_to TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )''')
    
    # 会员交易记录
    c.execute('''CREATE TABLE IF NOT EXISTS member_transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        member_id INTEGER NOT NULL,
        transaction_type TEXT NOT NULL,
        amount REAL,
        counts_change INTEGER,
        description TEXT,
        transaction_time TEXT DEFAULT CURRENT_TIMESTAMP,
        sale_id INTEGER DEFAULT NULL,
        FOREIGN KEY (member_id) REFERENCES members (id)
    )''')
    
    # 往来单位表（支持层级）
    c.execute('''CREATE TABLE IF NOT EXISTS partners (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        type TEXT NOT NULL CHECK(type IN ('supplier', 'customer')),
        parent_id INTEGER DEFAULT NULL,
        level INTEGER DEFAULT 1,
        contact_person TEXT,
        phone TEXT,
        address TEXT,
        opening_balance REAL DEFAULT 0,
        current_balance REAL DEFAULT 0,
        notes TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (parent_id) REFERENCES partners (id) ON DELETE SET NULL
    )''')
    
    # 销售主表（增加 partner_id 和 payment_method 字符串）
    c.execute('''CREATE TABLE IF NOT EXISTS sales (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sale_time TEXT NOT NULL,
        total_amount REAL NOT NULL,
        member_id INTEGER DEFAULT NULL,
        partner_id INTEGER DEFAULT NULL,
        payment_method TEXT DEFAULT 'cash',
        created_by INTEGER,
        FOREIGN KEY (member_id) REFERENCES members (id),
        FOREIGN KEY (partner_id) REFERENCES partners (id),
        FOREIGN KEY (created_by) REFERENCES users (id)
    )''')
    
    # 销售明细
    c.execute('''CREATE TABLE IF NOT EXISTS sale_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sale_id INTEGER NOT NULL,
        product_id INTEGER NOT NULL,
        quantity INTEGER NOT NULL,
        price_at_sale REAL NOT NULL,
        cost_at_sale REAL NOT NULL,
        FOREIGN KEY (sale_id) REFERENCES sales (id) ON DELETE CASCADE,
        FOREIGN KEY (product_id) REFERENCES products (id)
    )''')
    
    # 采购主表（增加 partner_id）
    c.execute('''CREATE TABLE IF NOT EXISTS purchases (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        purchase_time TEXT NOT NULL,
        supplier TEXT,
        partner_id INTEGER DEFAULT NULL,
        total_amount REAL NOT NULL,
        payment_method TEXT DEFAULT 'cash',
        payment_status TEXT DEFAULT 'paid',
        created_by INTEGER,
        FOREIGN KEY (partner_id) REFERENCES partners (id),
        FOREIGN KEY (created_by) REFERENCES users (id)
    )''')
    
    # 采购明细
    c.execute('''CREATE TABLE IF NOT EXISTS purchase_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        purchase_id INTEGER NOT NULL,
        product_id INTEGER NOT NULL,
        quantity INTEGER NOT NULL,
        unit_cost REAL NOT NULL,
        FOREIGN KEY (purchase_id) REFERENCES purchases (id) ON DELETE CASCADE,
        FOREIGN KEY (product_id) REFERENCES products (id)
    )''')
    
    # 资金流水
    c.execute('''CREATE TABLE IF NOT EXISTS cash_flows (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        flow_time TEXT NOT NULL,
        amount REAL NOT NULL,
        flow_type TEXT NOT NULL,
        account_type TEXT NOT NULL,
        related_id INTEGER,
        note TEXT,
        created_by INTEGER,
        FOREIGN KEY (created_by) REFERENCES users (id)
    )''')
    
    # 系统设置
    c.execute('''CREATE TABLE IF NOT EXISTS system_settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP
    )''')
    
    # ---------- 动态迁移：为现有表增加缺失的列 ----------
    # 为 partners 表增加 parent_id 和 level（如果不存在）
    try:
        c.execute("ALTER TABLE partners ADD COLUMN parent_id INTEGER")
    except sqlite3.OperationalError:
        pass  # 列已存在
    try:
        c.execute("ALTER TABLE partners ADD COLUMN level INTEGER DEFAULT 1")
    except sqlite3.OperationalError:
        pass
    # 为 sales 表增加 partner_id（如果不存在）
    try:
        c.execute("ALTER TABLE sales ADD COLUMN partner_id INTEGER DEFAULT NULL")
    except sqlite3.OperationalError:
        pass
    # 为 purchases 表增加 partner_id（如果不存在）
    try:
        c.execute("ALTER TABLE purchases ADD COLUMN partner_id INTEGER DEFAULT NULL")
    except sqlite3.OperationalError:
        pass
    
    # ---------- 默认数据 ----------
    # 默认管理员
    c.execute("SELECT COUNT(*) FROM users WHERE role='admin'")
    if c.fetchone()[0] == 0:
        admin_pw = generate_password_hash('admin123')
        c.execute("INSERT INTO users (username, password_hash, role, fullname) VALUES (?, ?, ?, ?)",
                  ('admin', admin_pw, 'admin', '系统管理员'))
    
    # 示例商品
    c.execute("SELECT COUNT(*) FROM products")
    if c.fetchone()[0] == 0:
        sample = [('纯牛奶',3.5,2.5,100),('面包',5.0,3.0,50),('鸡蛋',1.2,0.8,200),('矿泉水',1.0,0.6,150),('薯片',7.5,4.5,80)]
        c.executemany("INSERT INTO products (name,price,cost,stock) VALUES (?,?,?,?)", sample)
    
    # 示例会员
    c.execute("SELECT COUNT(*) FROM members")
    if c.fetchone()[0] == 0:
        today = date.today().isoformat()
        next_month = date.today().replace(month=date.today().month+1).isoformat()
        c.execute("INSERT INTO members (name,phone,card_type,balance,remaining_counts,valid_from,valid_to) VALUES (?,?,?,?,?,?,?)",
                  ('张三','13800001111','stored_value',200,0,None,None))
        c.execute("INSERT INTO members (name,phone,card_type,balance,remaining_counts,valid_from,valid_to) VALUES (?,?,?,?,?,?,?)",
                  ('李四','13800002222','count_limited',0,10,None,None))
        c.execute("INSERT INTO members (name,phone,card_type,balance,remaining_counts,valid_from,valid_to) VALUES (?,?,?,?,?,?,?)",
                  ('王五','13800003333','time_limited',0,0,today,next_month))
    
    # 示例往来单位
    c.execute("SELECT COUNT(*) FROM partners")
    if c.fetchone()[0] == 0:
        c.execute("INSERT INTO partners (name, type, contact_person, phone) VALUES (?,?,?,?)",
                  ('大华供应商','supplier','张经理','13811112222'))
        c.execute("INSERT INTO partners (name, type, contact_person, phone) VALUES (?,?,?,?)",
                  ('天天客户','customer','李小姐','13833334444'))
    
    # 支付方式默认配置
    c.execute("INSERT OR IGNORE INTO system_settings (key, value) VALUES ('payment_methods', '[\"现金\",\"银行转账\",\"微信\",\"支付宝\"]')")
    c.execute("INSERT OR IGNORE INTO system_settings (key, value) VALUES ('current_cash', '0')")
    c.execute("INSERT OR IGNORE INTO system_settings (key, value) VALUES ('current_bank', '0')")
    
    conn.commit()
    conn.close()

init_db()
# ---------- Flask-Login ----------
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

class User(UserMixin):
    def __init__(self, id, username, role):
        self.id = id
        self.username = username
        self.role = role

@login_manager.user_loader
def load_user(user_id):
    conn, c = get_db()
    c.execute("SELECT id, username, role FROM users WHERE id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    if row:
        return User(row['id'], row['username'], row['role'])
    return None

def role_required(allowed_roles):
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if not current_user.is_authenticated:
                return redirect(url_for('login'))
            if current_user.role not in allowed_roles:
                return "权限不足", 403
            return f(*args, **kwargs)
        return decorated_function
    return decorator

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn, conn.cursor()

def get_setting(key, default='0'):
    conn, c = get_db()
    c.execute("SELECT value FROM system_settings WHERE key = ?", (key,))
    row = c.fetchone()
    conn.close()
    return row['value'] if row else default

def update_setting(key, value):
    conn, c = get_db()
    c.execute("REPLACE INTO system_settings (key, value, updated_at) VALUES (?, ?, ?)",
              (key, str(value), datetime.now().isoformat()))
    conn.commit()
    conn.close()

def add_cash_flow(amount, flow_type, account_type, related_id, note, created_by):
    conn, c = get_db()
    c.execute("INSERT INTO cash_flows (flow_time, amount, flow_type, account_type, related_id, note, created_by) VALUES (?, ?, ?, ?, ?, ?, ?)",
              (datetime.now().isoformat(), amount, flow_type, account_type, related_id, note, created_by))
    current_key = f'current_{account_type}'
    current = float(get_setting(current_key, '0'))
    new_balance = current + amount
    update_setting(current_key, new_balance)
    conn.commit()
    conn.close()

# ---------- 支付方式辅助 ----------
def get_payment_methods():
    methods_str = get_setting('payment_methods', '["现金","银行转账"]')
    try:
        return json.loads(methods_str)
    except:
        return ["现金", "银行转账"]
# ---------- 页面路由 ----------
@app.route('/')
def index():
    return redirect(url_for('login'))

@app.route('/pos')
@login_required
def pos():
    return render_template_string(POS_HTML)

@app.route('/inventory')
@login_required
@role_required(['admin'])
def inventory():
    return render_template_string(INVENTORY_HTML)

@app.route('/sales')
@login_required
def sales_history():
    return render_template_string(SALES_HTML)

@app.route('/members')
@login_required
@role_required(['admin'])
def members():
    return render_template_string(MEMBERS_HTML)

@app.route('/purchase')
@login_required
@role_required(['admin'])
def purchase():
    return render_template_string(PURCHASE_HTML)

@app.route('/partners')
@login_required
@role_required(['admin'])
def partners():
    return render_template_string(PARTNERS_HTML)

@app.route('/finance')
@login_required
@role_required(['admin'])
def finance():
    return render_template_string(FINANCE_HTML)

@app.route('/backup')
@login_required
@role_required(['admin'])
def backup_page():
    return render_template_string(BACKUP_HTML)

@app.route('/useradmin')
@login_required
@role_required(['admin'])
def useradmin():
    return render_template_string(USERADMIN_HTML)

# ---------- 商品API ----------
@app.route('/api/products', methods=['GET'])
@login_required
def get_products():
    conn, c = get_db()
    c.execute("SELECT id, name, price, cost, stock FROM products ORDER BY id")
    products = [dict(row) for row in c.fetchall()]
    conn.close()
    return jsonify(products)

@app.route('/api/products', methods=['POST'])
@login_required
@role_required(['admin'])
def add_product():
    data = request.get_json()
    name = data.get('name', '').strip()
    price = data.get('price', 0)
    cost = data.get('cost', 0)
    stock = data.get('stock', 0)
    if not name or price < 0 or cost < 0 or stock < 0:
        return jsonify({'error': '参数错误'}), 400
    conn, c = get_db()
    try:
        c.execute("INSERT INTO products (name, price, cost, stock) VALUES (?, ?, ?, ?)",
                  (name, price, cost, stock))
        conn.commit()
        return jsonify({'id': c.lastrowid, 'message': '成功'}), 201
    except:
        conn.rollback()
        return jsonify({'error': '添加失败'}), 500
    finally:
        conn.close()

@app.route('/api/products/<int:product_id>', methods=['PUT'])
@login_required
@role_required(['admin'])
def update_product(product_id):
    data = request.get_json()
    name = data.get('name', '').strip()
    price = data.get('price', 0)
    cost = data.get('cost', 0)
    stock = data.get('stock', 0)
    if not name or price < 0 or cost < 0 or stock < 0:
        return jsonify({'error': '参数错误'}), 400
    conn, c = get_db()
    try:
        c.execute("UPDATE products SET name=?, price=?, cost=?, stock=? WHERE id=?",
                  (name, price, cost, stock, product_id))
        if c.rowcount == 0:
            return jsonify({'error': '商品不存在'}), 404
        conn.commit()
        return jsonify({'message': '成功'})
    except:
        conn.rollback()
        return jsonify({'error': '更新失败'}), 500
    finally:
        conn.close()

@app.route('/api/products/<int:product_id>', methods=['DELETE'])
@login_required
@role_required(['admin'])
def delete_product(product_id):
    conn, c = get_db()
    try:
        c.execute("SELECT COUNT(*) FROM sale_items WHERE product_id=?", (product_id,))
        if c.fetchone()[0] > 0:
            return jsonify({'error': '已有销售记录，无法删除'}), 400
        c.execute("DELETE FROM products WHERE id=?", (product_id,))
        conn.commit()
        return jsonify({'message': '删除成功'})
    except:
        conn.rollback()
        return jsonify({'error': '删除失败'}), 500
    finally:
        conn.close()

# ---------- 购物车API ----------
@app.route('/api/cart', methods=['GET'])
@login_required
def get_cart():
    cart = session.get('cart', {})
    if not cart:
        return jsonify({'items': [], 'total': 0})
    conn, c = get_db()
    items = []
    total = 0
    for pid, qty in cart.items():
        c.execute("SELECT id, name, price, stock FROM products WHERE id=?", (pid,))
        p = c.fetchone()
        if p:
            subtotal = p['price'] * qty
            total += subtotal
            items.append({'product_id': p['id'], 'name': p['name'], 'price': p['price'],
                          'quantity': qty, 'stock': p['stock'], 'subtotal': round(subtotal,2)})
    conn.close()
    return jsonify({'items': items, 'total': round(total,2)})

@app.route('/api/cart/add', methods=['POST'])
@login_required
def add_to_cart():
    data = request.get_json()
    pid = str(data.get('product_id'))
    qty = data.get('quantity', 1)
    if qty <= 0:
        return jsonify({'error': '数量必须大于0'}), 400
    conn, c = get_db()
    c.execute("SELECT stock FROM products WHERE id=?", (pid,))
    row = c.fetchone()
    conn.close()
    if not row:
        return jsonify({'error': '商品不存在'}), 404
    if row['stock'] < qty:
        return jsonify({'error': f'库存不足，当前库存: {row["stock"]}'}), 400
    cart = session.get('cart', {})
    cart[pid] = cart.get(pid, 0) + qty
    session['cart'] = cart
    return jsonify({'message': '添加成功'})

@app.route('/api/cart/update', methods=['PUT'])
@login_required
def update_cart_item():
    data = request.get_json()
    pid = str(data.get('product_id'))
    qty = data.get('quantity', 0)
    cart = session.get('cart', {})
    if pid not in cart:
        return jsonify({'error': '购物车中没有该商品'}), 404
    if qty <= 0:
        del cart[pid]
    else:
        conn, c = get_db()
        c.execute("SELECT stock FROM products WHERE id=?", (pid,))
        row = c.fetchone()
        conn.close()
        if row and row['stock'] < qty:
            return jsonify({'error': f'库存不足，当前库存: {row["stock"]}'}), 400
        cart[pid] = qty
    session['cart'] = cart
    return jsonify({'message': '更新成功'})

@app.route('/api/cart/remove', methods=['DELETE'])
@login_required
def remove_from_cart():
    data = request.get_json()
    pid = str(data.get('product_id'))
    cart = session.get('cart', {})
    if pid in cart:
        del cart[pid]
        session['cart'] = cart
    return jsonify({'message': '删除成功'})

@app.route('/api/cart/clear', methods=['DELETE'])
@login_required
def clear_cart():
    session['cart'] = {}
    return jsonify({'message': '清空成功'})
# ---------- 结算API ----------
@app.route('/api/checkout', methods=['POST'])
@login_required
def checkout():
    data = request.get_json() or {}
    member_id = data.get('member_id')
    customer_id = data.get('customer_id')
    payment_method = data.get('payment_method', 'cash')
    cart = session.get('cart', {})
    if not cart:
        return jsonify({'error': '购物车为空'}), 400
    
    conn, c = get_db()
    try:
        c.execute("BEGIN")
        items_to_buy = []
        total_amount = 0
        for pid_str, qty in cart.items():
            pid = int(pid_str)
            c.execute("SELECT name, price, cost, stock FROM products WHERE id=? FOR UPDATE", (pid,))
            row = c.fetchone()
            if not row:
                raise Exception(f"商品ID {pid} 不存在")
            if row['stock'] < qty:
                raise Exception(f"{row['name']} 库存不足")
            subtotal = row['price'] * qty
            total_amount += subtotal
            items_to_buy.append({
                'id': pid, 'name': row['name'], 'quantity': qty,
                'price': row['price'], 'cost': row['cost']
            })
        
        # 会员支付处理
        if payment_method == 'member_card' and member_id:
            c.execute("SELECT * FROM members WHERE id=? FOR UPDATE", (member_id,))
            member = c.fetchone()
            if not member:
                raise Exception("会员不存在")
            member = dict(member)
            if member['card_type'] == 'stored_value':
                if member['balance'] < total_amount:
                    raise Exception(f"储值卡余额不足，当前余额: {member['balance']}")
                new_balance = member['balance'] - total_amount
                c.execute("UPDATE members SET balance = ? WHERE id = ?", (new_balance, member_id))
                c.execute("INSERT INTO member_transactions (member_id, transaction_type, amount, description) VALUES (?, ?, ?, ?)",
                          (member_id, 'consume', -total_amount, f"消费 {total_amount} 元"))
            elif member['card_type'] == 'count_limited':
                if member['remaining_counts'] <= 0:
                    raise Exception("次卡剩余次数不足")
                new_counts = member['remaining_counts'] - 1
                c.execute("UPDATE members SET remaining_counts = ? WHERE id = ?", (new_counts, member_id))
                c.execute("INSERT INTO member_transactions (member_id, transaction_type, counts_change, description) VALUES (?, ?, ?, ?)",
                          (member_id, 'consume', -1, "消费1次"))
            elif member['card_type'] == 'time_limited':
                today_str = date.today().isoformat()
                if member['valid_from'] and today_str < member['valid_from']:
                    raise Exception("会员卡尚未生效")
                if member['valid_to'] and today_str > member['valid_to']:
                    raise Exception("会员卡已过期")
                c.execute("INSERT INTO member_transactions (member_id, transaction_type, description) VALUES (?, ?, ?)",
                          (member_id, 'consume', f"期限卡消费 {total_amount} 元"))
        
        # 更新客户（往来单位）的应收余额
        if customer_id:
            c.execute("SELECT type, current_balance FROM partners WHERE id=? FOR UPDATE", (customer_id,))
            partner = c.fetchone()
            if partner and partner['type'] == 'customer':
                new_bal = partner['current_balance'] + total_amount
                c.execute("UPDATE partners SET current_balance=? WHERE id=?", (new_bal, customer_id))
        
        # 销售主表
        sale_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        c.execute("INSERT INTO sales (sale_time, total_amount, member_id, partner_id, payment_method, created_by) VALUES (?, ?, ?, ?, ?, ?)",
                  (sale_time, total_amount, member_id, customer_id, payment_method, current_user.id))
        sale_id = c.lastrowid
        
        for item in items_to_buy:
            c.execute("INSERT INTO sale_items (sale_id, product_id, quantity, price_at_sale, cost_at_sale) VALUES (?, ?, ?, ?, ?)",
                      (sale_id, item['id'], item['quantity'], item['price'], item['cost']))
            c.execute("UPDATE products SET stock = stock - ? WHERE id = ?", (item['quantity'], item['id']))
        
        # 现金流水
        if payment_method not in ['member_card']:  # 会员卡支付不产生现金流水
            add_cash_flow(total_amount, 'sales_income', 'cash', sale_id, f"销售单{sale_id}", current_user.id)
        
        conn.commit()
        session['cart'] = {}
        
        # 小票数据
        ticket_data = {
            'sale_id': sale_id,
            'sale_time': sale_time,
            'operator': current_user.username,
            'member': None,
            'customer': None,
            'items': items_to_buy,
            'total': total_amount,
            'payment_method': payment_method
        }
        if member_id:
            ticket_data['member'] = member['name'] if 'member' in locals() else ''
        if customer_id:
            c.execute("SELECT name FROM partners WHERE id=?", (customer_id,))
            cust = c.fetchone()
            ticket_data['customer'] = cust['name'] if cust else ''
        return jsonify({'message': '结算成功', 'sale_id': sale_id, 'total_amount': total_amount, 'ticket': ticket_data})
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 400
    finally:
        conn.close()
# ---------- 会员API ----------
@app.route('/api/members', methods=['GET'])
@login_required
@role_required(['admin'])
def get_members():
    conn, c = get_db()
    c.execute("SELECT id, name, phone, card_type, balance, remaining_counts, valid_from, valid_to, created_at FROM members")
    members = [dict(row) for row in c.fetchall()]
    conn.close()
    return jsonify(members)

@app.route('/api/members', methods=['POST'])
@login_required
@role_required(['admin'])
def add_member():
    data = request.get_json()
    name = data.get('name', '').strip()
    phone = data.get('phone', '').strip()
    card_type = data.get('card_type')
    balance = data.get('balance', 0)
    counts = data.get('remaining_counts', 0)
    valid_from = data.get('valid_from')
    valid_to = data.get('valid_to')
    if not name or not phone or card_type not in ['stored_value','count_limited','time_limited']:
        return jsonify({'error':'参数错误'}),400
    conn,c = get_db()
    try:
        c.execute("INSERT INTO members (name,phone,card_type,balance,remaining_counts,valid_from,valid_to) VALUES (?,?,?,?,?,?,?)",
                  (name,phone,card_type,balance,counts,valid_from,valid_to))
        mid = c.lastrowid
        if balance>0:
            add_cash_flow(balance,'member_recharge','cash',mid,f"会员{name}开户充值",current_user.id)
        conn.commit()
        return jsonify({'id':mid,'message':'成功'})
    except sqlite3.IntegrityError:
        return jsonify({'error':'手机号已存在'}),400
    except:
        conn.rollback()
        return jsonify({'error':'添加失败'}),500
    finally:
        conn.close()

@app.route('/api/members/<int:mid>/recharge', methods=['POST'])
@login_required
@role_required(['admin'])
def recharge_member(mid):
    data = request.get_json()
    amount = data.get('amount',0)
    counts = data.get('counts',0)
    if amount<=0 and counts<=0:
        return jsonify({'error':'充值金额或次数必须大于0'}),400
    conn,c = get_db()
    try:
        c.execute("SELECT card_type,balance,remaining_counts FROM members WHERE id=? FOR UPDATE",(mid,))
        m = c.fetchone()
        if not m:
            return jsonify({'error':'会员不存在'}),404
        m = dict(m)
        if amount>0:
            if m['card_type']!='stored_value':
                return jsonify({'error':'该会员不是储值卡'}),400
            new_bal = m['balance']+amount
            c.execute("UPDATE members SET balance=? WHERE id=?",(new_bal,mid))
            add_cash_flow(amount,'member_recharge','cash',mid,f"会员充值{amount}元",current_user.id)
            c.execute("INSERT INTO member_transactions (member_id,transaction_type,amount,description) VALUES (?,?,?,?)",
                      (mid,'recharge',amount,f"充值{amount}元"))
        if counts>0:
            if m['card_type']!='count_limited':
                return jsonify({'error':'该会员不是次卡'}),400
            new_cnt = m['remaining_counts']+counts
            c.execute("UPDATE members SET remaining_counts=? WHERE id=?",(new_cnt,mid))
            c.execute("INSERT INTO member_transactions (member_id,transaction_type,counts_change,description) VALUES (?,?,?,?)",
                      (mid,'recharge',counts,f"充值{counts}次"))
        conn.commit()
        return jsonify({'message':'充值成功'})
    except Exception as e:
        conn.rollback()
        return jsonify({'error':str(e)}),500
    finally:
        conn.close()

@app.route('/api/members/<int:mid>/transactions')
@login_required
def member_transactions(mid):
    conn,c = get_db()
    c.execute("SELECT * FROM member_transactions WHERE member_id=? ORDER BY transaction_time DESC",(mid,))
    rows = [dict(row) for row in c.fetchall()]
    conn.close()
    return jsonify(rows)

# ---------- 用户管理API ----------
@app.route('/api/users', methods=['GET'])
@login_required
@role_required(['admin'])
def get_users():
    conn,c = get_db()
    c.execute("SELECT id, username, role, fullname, created_at FROM users")
    users = [dict(row) for row in c.fetchall()]
    conn.close()
    return jsonify(users)

@app.route('/api/users', methods=['POST'])
@login_required
@role_required(['admin'])
def add_user():
    data = request.get_json()
    username = data.get('username','').strip()
    password = data.get('password','')
    role = data.get('role','operator')
    fullname = data.get('fullname','')
    if not username or not password or role not in ['admin','operator']:
        return jsonify({'error':'参数错误'}),400
    conn,c = get_db()
    try:
        c.execute("SELECT id FROM users WHERE username=?",(username,))
        if c.fetchone():
            return jsonify({'error':'用户名已存在'}),400
        pw_hash = generate_password_hash(password)
        c.execute("INSERT INTO users (username, password_hash, role, fullname) VALUES (?,?,?,?)",
                  (username, pw_hash, role, fullname))
        conn.commit()
        return jsonify({'message':'添加成功'})
    except:
        conn.rollback()
        return jsonify({'error':'添加失败'}),500
    finally:
        conn.close()

@app.route('/api/users/<int:uid>', methods=['DELETE'])
@login_required
@role_required(['admin'])
def delete_user(uid):
    if uid == current_user.id:
        return jsonify({'error':'不能删除当前登录的管理员'}),400
    conn,c = get_db()
    try:
        c.execute("DELETE FROM users WHERE id=?",(uid,))
        if c.rowcount==0:
            return jsonify({'error':'用户不存在'}),404
        conn.commit()
        return jsonify({'message':'删除成功'})
    except:
        conn.rollback()
        return jsonify({'error':'删除失败'}),500
    finally:
        conn.close()

@app.route('/api/users/<int:uid>/reset_password', methods=['POST'])
@login_required
@role_required(['admin'])
def reset_user_password(uid):
    data = request.get_json()
    new_pw = data.get('password','')
    if not new_pw:
        return jsonify({'error':'密码不能为空'}),400
    conn,c = get_db()
    try:
        pw_hash = generate_password_hash(new_pw)
        c.execute("UPDATE users SET password_hash=? WHERE id=?",(pw_hash,uid))
        conn.commit()
        return jsonify({'message':'密码重置成功'})
    except:
        conn.rollback()
        return jsonify({'error':'重置失败'}),500
    finally:
        conn.close()

# ---------- 往来单位API（支持层级）----------
@app.route('/api/partners', methods=['GET'])
@login_required
def get_partners():
    conn, c = get_db()
    c.execute("SELECT * FROM partners ORDER BY level, name")
    partners = [dict(row) for row in c.fetchall()]
    conn.close()
    return jsonify(partners)

@app.route('/api/partners', methods=['POST'])
@login_required
@role_required(['admin'])
def add_partner():
    data = request.get_json()
    name = data.get('name', '').strip()
    ptype = data.get('type')
    parent_id = data.get('parent_id')
    contact = data.get('contact_person', '')
    phone = data.get('phone', '')
    address = data.get('address', '')
    opening = data.get('opening_balance', 0)
    notes = data.get('notes', '')
    if not name or ptype not in ('supplier', 'customer'):
        return jsonify({'error': '参数错误'}), 400
    conn, c = get_db()
    try:
        # 计算层级
        level = 1
        if parent_id:
            c.execute("SELECT level FROM partners WHERE id=?", (parent_id,))
            p = c.fetchone()
            if p:
                level = p['level'] + 1
        c.execute("""INSERT INTO partners (name, type, parent_id, level, contact_person, phone, address, opening_balance, current_balance, notes)
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                  (name, ptype, parent_id, level, contact, phone, address, opening, opening, notes))
        conn.commit()
        return jsonify({'id': c.lastrowid, 'message': '成功'})
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

@app.route('/api/partners/<int:pid>', methods=['PUT'])
@login_required
@role_required(['admin'])
def update_partner(pid):
    data = request.get_json()
    name = data.get('name', '').strip()
    ptype = data.get('type')
    parent_id = data.get('parent_id')
    contact = data.get('contact_person', '')
    phone = data.get('phone', '')
    address = data.get('address', '')
    opening = data.get('opening_balance', 0)
    notes = data.get('notes', '')
    if not name or ptype not in ('supplier', 'customer'):
        return jsonify({'error': '参数错误'}), 400
    conn, c = get_db()
    try:
        # 更新层级（简化，不递归更新子节点）
        level = 1
        if parent_id:
            c.execute("SELECT level FROM partners WHERE id=?", (parent_id,))
            p = c.fetchone()
            if p:
                level = p['level'] + 1
        c.execute("""UPDATE partners SET name=?, type=?, parent_id=?, level=?, contact_person=?, phone=?, address=?, opening_balance=?, notes=?
                     WHERE id=?""",
                  (name, ptype, parent_id, level, contact, phone, address, opening, notes, pid))
        if c.rowcount == 0:
            return jsonify({'error': '记录不存在'}), 404
        conn.commit()
        return jsonify({'message': '更新成功'})
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

@app.route('/api/partners/<int:pid>', methods=['DELETE'])
@login_required
@role_required(['admin'])
def delete_partner(pid):
    conn, c = get_db()
    try:
        c.execute("DELETE FROM partners WHERE id=?", (pid,))
        if c.rowcount == 0:
            return jsonify({'error': '记录不存在'}), 404
        conn.commit()
        return jsonify({'message': '删除成功'})
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()
# ---------- 采购API（支持合作伙伴）----------
@app.route('/api/purchases', methods=['POST'])
@login_required
@role_required(['admin'])
def create_purchase():
    data = request.get_json()
    supplier = data.get('supplier', '')
    partner_id = data.get('partner_id')
    items = data.get('items', [])
    payment_method = data.get('payment_method', 'cash')
    if not items:
        return jsonify({'error': '采购明细不能为空'}), 400
    total = sum(i['quantity']*i['unit_cost'] for i in items)
    conn,c = get_db()
    try:
        c.execute("BEGIN")
        pt = datetime.now().isoformat()
        c.execute("""INSERT INTO purchases (purchase_time, supplier, partner_id, total_amount, payment_method, payment_status, created_by)
                     VALUES (?, ?, ?, ?, ?, ?, ?)""",
                  (pt, supplier, partner_id, total, payment_method, 'paid', current_user.id))
        pid = c.lastrowid
        
        # 更新供应商应付余额
        if partner_id:
            c.execute("SELECT type, current_balance FROM partners WHERE id=? FOR UPDATE", (partner_id,))
            partner = c.fetchone()
            if partner and partner['type'] == 'supplier':
                new_bal = partner['current_balance'] - total
                c.execute("UPDATE partners SET current_balance=? WHERE id=?", (new_bal, partner_id))
        
        for it in items:
            product_id = it['product_id']
            qty = it['quantity']
            unit_cost = it['unit_cost']
            c.execute("INSERT INTO purchase_items (purchase_id, product_id, quantity, unit_cost) VALUES (?,?,?,?)",
                      (pid, product_id, qty, unit_cost))
            c.execute("SELECT stock, cost FROM products WHERE id=? FOR UPDATE", (product_id,))
            p = c.fetchone()
            new_stock = p['stock'] + qty
            new_cost = ((p['stock']*p['cost']) + (qty*unit_cost)) / new_stock if new_stock>0 else 0
            c.execute("UPDATE products SET stock=?, cost=? WHERE id=?", (new_stock, new_cost, product_id))
        
        add_cash_flow(-total, 'purchase_payment', payment_method, pid, f"采购单{pid}", current_user.id)
        conn.commit()
        return jsonify({'id': pid, 'message': '采购入库成功'})
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

@app.route('/api/purchases', methods=['GET'])
@login_required
@role_required(['admin'])
def get_purchases():
    conn,c = get_db()
    c.execute("SELECT id,purchase_time,supplier,partner_id,total_amount,payment_method FROM purchases ORDER BY purchase_time DESC")
    rows = [dict(row) for row in c.fetchall()]
    conn.close()
    return jsonify(rows)

# ---------- 销售历史 ----------
@app.route('/api/sales', methods=['GET'])
@login_required
def get_sales():
    conn,c = get_db()
    c.execute('''SELECT s.id,s.sale_time,s.total_amount,s.member_id,s.partner_id,s.payment_method,u.username as created_by_name
                 FROM sales s LEFT JOIN users u ON s.created_by=u.id ORDER BY s.sale_time DESC''')
    sales = [dict(row) for row in c.fetchall()]
    for s in sales:
        if s['member_id']:
            c.execute("SELECT name FROM members WHERE id=?", (s['member_id'],))
            m = c.fetchone()
            s['member_name'] = m['name'] if m else None
        if s['partner_id']:
            c.execute("SELECT name FROM partners WHERE id=?", (s['partner_id'],))
            p = c.fetchone()
            s['customer_name'] = p['name'] if p else None
    conn.close()
    return jsonify(sales)

@app.route('/api/sales/<int:sid>', methods=['GET'])
@login_required
def get_sale_detail(sid):
    conn,c = get_db()
    c.execute("SELECT id,sale_time,total_amount,member_id,partner_id,payment_method FROM sales WHERE id=?", (sid,))
    sale = c.fetchone()
    if not sale:
        return jsonify({'error':'记录不存在'}),404
    sale = dict(sale)
    if sale['member_id']:
        c.execute("SELECT name FROM members WHERE id=?", (sale['member_id'],))
        m = c.fetchone()
        sale['member_name'] = m['name'] if m else None
    if sale['partner_id']:
        c.execute("SELECT name FROM partners WHERE id=?", (sale['partner_id'],))
        p = c.fetchone()
        sale['customer_name'] = p['name'] if p else None
    c.execute('''SELECT p.name, si.quantity, si.price_at_sale, (si.quantity*si.price_at_sale) as subtotal
                 FROM sale_items si JOIN products p ON si.product_id=p.id WHERE si.sale_id=?''', (sid,))
    sale['items'] = [dict(row) for row in c.fetchall()]
    conn.close()
    return jsonify(sale)

# ---------- 财务报表 ----------
@app.route('/api/financial_report')
@login_required
@role_required(['admin'])
def financial_report():
    conn,c = get_db()
    c.execute("SELECT SUM(quantity*price_at_sale) as sales, SUM(quantity*cost_at_sale) as cost FROM sale_items")
    sc = c.fetchone()
    total_sales = sc['sales'] or 0
    total_cost = sc['cost'] or 0
    c.execute("SELECT SUM(total_amount) as purchases FROM purchases")
    total_purchases = c.fetchone()[0] or 0
    c.execute("SELECT SUM(stock*cost) as inv_value FROM products")
    inv_val = c.fetchone()[0] or 0
    c.execute("SELECT SUM(balance) as member_bal FROM members WHERE card_type='stored_value'")
    member_bal = c.fetchone()[0] or 0
    current_cash = float(get_setting('current_cash', '0'))
    current_bank = float(get_setting('current_bank', '0'))
    total_assets = current_cash + current_bank + inv_val
    equity = total_assets - member_bal
    conn.close()
    return jsonify({
        'income_statement': {'total_sales':round(total_sales,2), 'total_cost':round(total_cost,2), 'gross_profit':round(total_sales-total_cost,2)},
        'balance_sheet': {'cash':round(current_cash,2), 'bank':round(current_bank,2), 'inventory':round(inv_val,2),
                          'total_assets':round(total_assets,2), 'member_liability':round(member_bal,2), 'equity':round(equity,2)},
        'purchases_total': round(total_purchases,2)
    })

# ---------- 备份恢复 ----------
@app.route('/api/backup', methods=['POST'])
@login_required
@role_required(['admin'])
def backup_db():
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    backup_file = os.path.join(BACKUP_DIR, f'supermarket_{ts}.db')
    try:
        shutil.copy2(DB_PATH, backup_file)
        return jsonify({'message':'备份成功', 'file':os.path.basename(backup_file)})
    except Exception as e:
        return jsonify({'error':str(e)}),500

@app.route('/api/backups', methods=['GET'])
@login_required
@role_required(['admin'])
def list_backups():
    files = []
    for f in os.listdir(BACKUP_DIR):
        if f.endswith('.db'):
            stat = os.stat(os.path.join(BACKUP_DIR, f))
            files.append({'name':f, 'size':stat.st_size, 'modified':datetime.fromtimestamp(stat.st_mtime).isoformat()})
    files.sort(key=lambda x:x['modified'], reverse=True)
    return jsonify(files)

@app.route('/api/restore', methods=['POST'])
@login_required
@role_required(['admin'])
def restore_db():
    data = request.get_json()
    filename = data.get('filename')
    src = os.path.join(BACKUP_DIR, filename)
    if not os.path.exists(src):
        return jsonify({'error':'备份文件不存在'}),404
    try:
        temp_backup = os.path.join(BACKUP_DIR, f'before_restore_{datetime.now().strftime("%Y%m%d_%H%M%S")}.db')
        shutil.copy2(DB_PATH, temp_backup)
        shutil.copy2(src, DB_PATH)
        return jsonify({'message':f'恢复成功，原数据库备份为{os.path.basename(temp_backup)}'})
    except Exception as e:
        return jsonify({'error':str(e)}),500

@app.route('/api/migrate', methods=['POST'])
@login_required
@role_required(['admin'])
def export_migrate():
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    export_file = os.path.join(BACKUP_DIR, f'export_{ts}.sql')
    try:
        conn = sqlite3.connect(DB_PATH)
        with open(export_file, 'w', encoding='utf-8') as f:
            for line in conn.iterdump():
                f.write(line + '\n')
        conn.close()
        return jsonify({'message':'导出成功', 'file':os.path.basename(export_file)})
    except Exception as e:
        return jsonify({'error':str(e)}),500

# ---------- PDF导出 ----------
@app.route('/api/sales/<int:sid>/pdf')
@login_required
def export_sale_pdf(sid):
    conn, c = get_db()
    c.execute("SELECT s.*, u.username as operator, m.name as member_name, p.name as customer_name FROM sales s "
              "LEFT JOIN users u ON s.created_by=u.id "
              "LEFT JOIN members m ON s.member_id=m.id "
              "LEFT JOIN partners p ON s.partner_id=p.id "
              "WHERE s.id=?", (sid,))
    sale = c.fetchone()
    if not sale:
        return jsonify({'error': '记录不存在'}), 404
    sale = dict(sale)
    c.execute("""SELECT p.name, si.quantity, si.price_at_sale, (si.quantity*si.price_at_sale) as subtotal
                 FROM sale_items si JOIN products p ON si.product_id=p.id
                 WHERE si.sale_id=?""", (sid,))
    items = [dict(row) for row in c.fetchall()]
    conn.close()
    
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4)
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(name='Title', parent=styles['Heading1'], alignment=1, fontSize=16)
    normal_style = styles['Normal']
    
    story = []
    story.append(Paragraph("商超收银系统 - 销售单", title_style))
    story.append(Spacer(1, 10))
    story.append(Paragraph(f"单号：{sale['id']}", normal_style))
    story.append(Paragraph(f"时间：{sale['sale_time']}", normal_style))
    story.append(Paragraph(f"操作员：{sale['operator'] or ''}", normal_style))
    story.append(Paragraph(f"会员：{sale['member_name'] or '散客'}", normal_style))
    story.append(Paragraph(f"客户：{sale['customer_name'] or '无'}", normal_style))
    story.append(Paragraph(f"支付方式：{sale['payment_method']}", normal_style))
    story.append(Spacer(1, 10))
    
    data = [['商品名称', '数量', '单价', '小计']]
    for item in items:
        data.append([item['name'], str(item['quantity']), f"{item['price_at_sale']:.2f}", f"{item['subtotal']:.2f}"])
    data.append(['', '', '合计', f"{sale['total_amount']:.2f}"])
    
    table = Table(data)
    table.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), colors.grey),
        ('TEXTCOLOR', (0,0), (-1,0), colors.whitesmoke),
        ('ALIGN', (0,0), (-1,-1), 'CENTER'),
        ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
        ('BOTTOMPADDING', (0,0), (-1,0), 12),
        ('GRID', (0,0), (-1,-2), 1, colors.black),
        ('SPAN', (0, -1), (2, -1)),
    ]))
    story.append(table)
    story.append(Spacer(1, 20))
    story.append(Paragraph("感谢您的光临！", normal_style))
    
    doc.build(story)
    buffer.seek(0)
    return send_file(buffer, as_attachment=True, download_name=f'sale_{sid}.pdf', mimetype='application/pdf')

# ---------- 支付方式API ----------
@app.route('/api/payment_methods', methods=['GET'])
@login_required
def get_payment_methods():
    methods = get_payment_methods()
    return jsonify(methods)

@app.route('/api/payment_methods', methods=['POST'])
@login_required
@role_required(['admin'])
def update_payment_methods():
    data = request.get_json()
    methods = data.get('methods', [])
    if not isinstance(methods, list) or len(methods) == 0:
        return jsonify({'error': '支付方式列表不能为空'}), 400
    update_setting('payment_methods', json.dumps(methods))
    return jsonify({'message': '保存成功'})

# ---------- 当前用户 ----------
@app.route('/api/current_user')
@login_required
def current_user_info():
    return jsonify({'id':current_user.id, 'username':current_user.username, 'role':current_user.role})
# ---------- 登录登出 ----------
@app.route('/login', methods=['GET','POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        conn,c = get_db()
        c.execute("SELECT id,username,password_hash,role FROM users WHERE username=?", (username,))
        user = c.fetchone()
        conn.close()
        if user and check_password_hash(user['password_hash'], password):
            login_user(User(user['id'], user['username'], user['role']))
            return redirect(request.args.get('next') or url_for('pos'))
        flash('用户名或密码错误')
    return render_template_string(LOGIN_HTML)

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

# ========== HTML模板 ==========
LOGIN_HTML = '''
<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>登录</title><link href="https://cdn.jsdelivr.net/npm/bootstrap@5.1.3/dist/css/bootstrap.min.css" rel="stylesheet"></head>
<body style="background:#f8f9fa"><div class="container"><div class="login-form" style="max-width:400px;margin:100px auto"><div class="card"><div class="card-header bg-primary text-white">商超收银系统 - 登录</div><div class="card-body">
{% with messages = get_flashed_messages() %}{% if messages %}<div class="alert alert-danger">{{ messages[0] }}</div>{% endif %}{% endwith %}
<form method="post"><div class="mb-3"><label>用户名</label><input type="text" name="username" class="form-control" required></div><div class="mb-3"><label>密码</label><input type="password" name="password" class="form-control" required></div><button type="submit" class="btn btn-primary w-100">登录</button></form>
</div></div></div></div></body></html>
'''

USERADMIN_HTML = '''
<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>用户管理</title><link href="https://cdn.jsdelivr.net/npm/bootstrap@5.1.3/dist/css/bootstrap.min.css" rel="stylesheet"></head>
<body>
<nav class="navbar navbar-expand-lg navbar-dark bg-dark"><div class="container"><a class="navbar-brand" href="/pos">商超系统</a><div class="navbar-nav"><a class="nav-link" href="/pos">收银台</a><a class="nav-link" href="/inventory">库存管理</a><a class="nav-link" href="/sales">销售记录</a><a class="nav-link" href="/members">会员管理</a><a class="nav-link" href="/purchase">采购入库</a><a class="nav-link" href="/partners">往来单位</a><a class="nav-link" href="/finance">财务期初</a><a class="nav-link" href="/backup">数据备份</a><a class="nav-link active" href="/useradmin">用户管理</a><a class="nav-link" href="/logout">退出</a></div><div class="navbar-nav ms-auto"><button class="btn btn-outline-light btn-sm me-2" onclick="window.print()">🖨️ 打印本页</button></div></div></nav>
<div class="container mt-4"><div class="d-flex justify-content-between"><h3>操作员管理</h3><button class="btn btn-primary" data-bs-toggle="modal" data-bs-target="#addUserModal">+ 新增操作员</button></div>
<div class="table-responsive"><table class="table table-bordered mt-3"><thead><tr><th>ID</th><th>用户名</th><th>姓名</th><th>角色</th><th>创建时间</th><th>操作</th></tr></thead><tbody id="userTableBody"></tbody></table></div></div>
<div class="modal fade" id="addUserModal" tabindex="-1"><div class="modal-dialog"><div class="modal-content"><div class="modal-header"><h5>新增操作员</h5><button type="button" class="btn-close" data-bs-dismiss="modal"></button></div><div class="modal-body"><div><label>用户名</label><input id="newUsername" class="form-control"></div><div><label>姓名</label><input id="newFullname" class="form-control"></div><div><label>密码</label><input id="newPassword" type="password" class="form-control"></div><div><label>角色</label><select id="newRole" class="form-select"><option value="operator">操作员</option><option value="admin">管理员</option></select></div></div><div class="modal-footer"><button class="btn btn-secondary" data-bs-dismiss="modal">取消</button><button class="btn btn-primary" id="submitAddUser">保存</button></div></div></div></div>
<div class="modal fade" id="resetPwModal" tabindex="-1"><div class="modal-dialog"><div class="modal-content"><div class="modal-header"><h5>重置密码</h5><button type="button" class="btn-close" data-bs-dismiss="modal"></button></div><div class="modal-body"><input type="hidden" id="resetUserId"><div><label>新密码</label><input id="resetPassword" type="password" class="form-control"></div></div><div class="modal-footer"><button class="btn btn-secondary" data-bs-dismiss="modal">取消</button><button class="btn btn-warning" id="submitResetPw">确认重置</button></div></div></div></div>
<script>
async function loadUsers(){ const resp=await fetch('/api/users'); const users=await resp.json(); const tbody=document.getElementById('userTableBody'); tbody.innerHTML=users.map(u=>`<tr><td>${u.id}</td><td>${escapeHtml(u.username)}</td><td>${escapeHtml(u.fullname||'')}</td><td>${u.role==='admin'?'管理员':'操作员'}</td><td>${u.created_at}</td><td><button class="btn btn-sm btn-warning" onclick="resetPw(${u.id},'${escapeHtml(u.username)}')">重置密码</button> <button class="btn btn-sm btn-danger" onclick="deleteUser(${u.id})">删除</button></td></tr>`).join(''); }
function escapeHtml(s){ return s.replace(/[&<>]/g,m=>m==='&'?'&amp;':m==='<'?'&lt;':'&gt;'); }
document.getElementById('submitAddUser').onclick=async()=>{ const username=document.getElementById('newUsername').value.trim(); const fullname=document.getElementById('newFullname').value.trim(); const password=document.getElementById('newPassword').value; const role=document.getElementById('newRole').value; if(!username||!password){ alert('用户名和密码不能为空'); return; } const resp=await fetch('/api/users',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username,password,role,fullname})}); if(resp.ok){ bootstrap.Modal.getInstance(document.getElementById('addUserModal')).hide(); loadUsers(); alert('添加成功'); }else{ const err=await resp.json(); alert(err.error); } };
async function deleteUser(id){ if(!confirm('确定删除该用户？')) return; const resp=await fetch(`/api/users/${id}`,{method:'DELETE'}); if(resp.ok){ loadUsers(); alert('删除成功'); }else{ const err=await resp.json(); alert(err.error); } }
function resetPw(id,name){ document.getElementById('resetUserId').value=id; document.getElementById('resetPassword').value=''; new bootstrap.Modal(document.getElementById('resetPwModal')).show(); }
document.getElementById('submitResetPw').onclick=async()=>{ const id=document.getElementById('resetUserId').value; const newPw=document.getElementById('resetPassword').value; if(!newPw){ alert('请输入新密码'); return; } const resp=await fetch(`/api/users/${id}/reset_password`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:newPw})}); if(resp.ok){ bootstrap.Modal.getInstance(document.getElementById('resetPwModal')).hide(); alert('密码重置成功'); }else{ const err=await resp.json(); alert(err.error); } };
loadUsers();
</script>
<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.1.3/dist/js/bootstrap.bundle.min.js"></script>
</body></html>
'''

POS_HTML = '''
<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>收银台</title><link href="https://cdn.jsdelivr.net/npm/bootstrap@5.1.3/dist/css/bootstrap.min.css" rel="stylesheet">
<style>.cart-item{border-bottom:1px solid #eee;padding:8px 0}.low-stock{color:#d9534f}</style></head>
<body>
<nav class="navbar navbar-expand-lg navbar-dark bg-dark"><div class="container"><a class="navbar-brand" href="/pos">商超系统</a><div class="navbar-nav"><a class="nav-link active" href="/pos">收银台</a><a class="nav-link" href="/inventory">库存管理</a><a class="nav-link" href="/sales">销售记录</a><a class="nav-link" href="/members">会员管理</a><a class="nav-link" href="/purchase">采购入库</a><a class="nav-link" href="/partners">往来单位</a><a class="nav-link" href="/finance">财务期初</a><a class="nav-link" href="/backup">数据备份</a><a class="nav-link" href="/useradmin">用户管理</a><a class="nav-link" href="/logout">退出</a></div><div class="navbar-nav ms-auto"><button class="btn btn-outline-light btn-sm me-2" onclick="window.print()">🖨️ 打印本页</button></div></div></nav>
<div class="container mt-4"><div class="row"><div class="col-md-7"><h3>商品列表</h3><input type="text" id="searchInput" class="form-control mb-3" placeholder="搜索..."><div id="productList" class="row"></div></div><div class="col-md-5"><h3>购物车 <button class="btn btn-sm btn-danger" id="clearCartBtn">清空</button></h3><div id="cartItems"></div><div class="mt-3"><div><label>支付方式</label><select id="paymentMethod" class="form-select"></select></div><div id="memberSelectDiv" style="display:none"><label>会员ID</label><input type="number" id="memberId" class="form-control" placeholder="输入会员ID"></div><div class="mt-2"><label>客户（往来单位）</label><select id="customerSelect" class="form-select"><option value="">散客（不记录）</option></select></div><h4 class="mt-2">总计: ¥<span id="cartTotal">0.00</span></h4><button class="btn btn-success w-100" id="checkoutBtn">结算</button></div></div></div></div>
<script>
let customers=[];
async function loadProducts(){ const resp=await fetch('/api/products'); const ps=await resp.json(); const search=document.getElementById('searchInput').value.toLowerCase(); const filtered=ps.filter(p=>p.name.toLowerCase().includes(search)); document.getElementById('productList').innerHTML=filtered.map(p=>`<div class="col-6 col-md-4 mb-3"><div class="card"><div class="card-body"><h6>${escapeHtml(p.name)}</h6><p>¥${p.price.toFixed(2)}<br>库存:${p.stock} ${p.stock<10?'<span class="low-stock">(低)</span>':''}</p><div class="input-group"><input type="number" id="qty_${p.id}" class="form-control" value="1" min="1" max="${p.stock}"><button class="btn btn-primary btn-sm" onclick="addToCart(${p.id})">加入</button></div></div></div></div>`).join(''); }
async function loadCustomers(){ const resp=await fetch('/api/partners'); const all=await resp.json(); customers=all.filter(p=>p.type==='customer'); const select=document.getElementById('customerSelect'); select.innerHTML='<option value="">散客（不记录）</option>'+customers.map(c=>`<option value="${c.id}">${escapeHtml(c.name)}</option>`).join(''); }
async function loadPaymentMethods(){ const resp=await fetch('/api/payment_methods'); const methods=await resp.json(); const select=document.getElementById('paymentMethod'); select.innerHTML=methods.map(m=>`<option value="${m}">${m}</option>`).join(''); }
function escapeHtml(s){ return s.replace(/[&<>]/g,m=>m==='&'?'&amp;':m==='<'?'&lt;':'&gt;'); }
async function addToCart(pid){ let qty=parseInt(document.getElementById(`qty_${pid}`).value)||1; const resp=await fetch('/api/cart/add',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({product_id:pid,quantity:qty})}); if(resp.ok){ loadCart(); showToast('添加成功','success'); }else{ const err=await resp.json(); showToast(err.error,'danger'); } }
async function loadCart(){ const resp=await fetch('/api/cart'); const data=await resp.json(); const cartDiv=document.getElementById('cartItems'); if(data.items.length===0){ cartDiv.innerHTML='<p class="text-muted">购物车为空</p>'; document.getElementById('cartTotal').innerText='0.00'; return; } cartDiv.innerHTML=data.items.map(it=>`<div class="cart-item d-flex justify-content-between"><div><strong>${escapeHtml(it.name)}</strong><br>¥${it.price} × ${it.quantity} = ¥${it.subtotal}</div><div><button class="btn btn-sm btn-outline-secondary" onclick="updateCart(${it.product_id},${it.quantity-1})">-</button><span class="mx-1">${it.quantity}</span><button class="btn btn-sm btn-outline-secondary" onclick="updateCart(${it.product_id},${it.quantity+1})">+</button><button class="btn btn-sm btn-danger ms-2" onclick="removeFromCart(${it.product_id})">删</button></div></div>`).join(''); document.getElementById('cartTotal').innerText=data.total.toFixed(2); }
async function updateCart(pid,qty){ if(qty<=0){ removeFromCart(pid); return; } const resp=await fetch('/api/cart/update',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({product_id:pid,quantity:qty})}); if(resp.ok) loadCart(); else{ const err=await resp.json(); showToast(err.error,'danger'); } }
async function removeFromCart(pid){ await fetch('/api/cart/remove',{method:'DELETE',headers:{'Content-Type':'application/json'},body:JSON.stringify({product_id:pid})}); loadCart(); }
document.getElementById('clearCartBtn').onclick=async()=>{ await fetch('/api/cart/clear',{method:'DELETE'}); loadCart(); };
document.getElementById('paymentMethod').onchange=function(){ document.getElementById('memberSelectDiv').style.display=this.value==='member_card'?'block':'none'; };
document.getElementById('checkoutBtn').onclick=async()=>{ let member_id=null, pm=document.getElementById('paymentMethod').value; if(pm==='member_card'){ member_id=parseInt(document.getElementById('memberId').value); if(isNaN(member_id)){ showToast('请输入会员ID','danger'); return; } } const customer_id=document.getElementById('customerSelect').value||null; const resp=await fetch('/api/checkout',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({payment_method:pm,member_id:member_id,customer_id:customer_id})}); const result=await resp.json(); if(resp.ok){ showToast(`结算成功！金额:¥${result.total_amount}`,'success'); loadCart(); loadProducts(); if(result.ticket) printTicket(result.ticket); }else{ showToast(result.error,'danger'); } };
function printTicket(ticket){ let html=`<html><head><title>销售小票</title><style>body{font-family:monospace;padding:20px}table{width:100%}</style></head><body><h3>商超收银系统</h3><p>单号:${ticket.sale_id}<br>时间:${ticket.sale_time}<br>操作员:${ticket.operator}<br>会员:${ticket.member||'散客'}<br>客户:${ticket.customer||''}</p><div class="table-responsive"><table border="1" cellpadding="4"><tr><th>商品</th><th>单价</th><th>数量</th><th>小计</th></tr>${ticket.items.map(i=>`<tr><td>${escapeHtml(i.name)}</td><td>¥${i.price.toFixed(2)}</td><td>${i.quantity}</td><td>¥${(i.price*i.quantity).toFixed(2)}</td></tr>`).join('')}<tr><td colspan="3" align="right"><strong>总计</strong></td><td>¥${ticket.total.toFixed(2)}</td></tr></table></div><p>支付方式:${ticket.payment_method}</p><button onclick="window.print()">打印</button><script>setTimeout(function(){window.print();window.close()},500)<\/script></body></html>`; let w=window.open('','_blank'); w.document.write(html); w.document.close(); }
document.getElementById('searchInput').addEventListener('input',loadProducts);
function showToast(msg,type){ const d=document.createElement('div'); d.className=`alert alert-${type} alert-dismissible fade show position-fixed top-0 end-0 m-3`; d.style.zIndex=1050; d.innerHTML=`${msg}<button type="button" class="btn-close" data-bs-dismiss="alert"></button>`; document.body.appendChild(d); setTimeout(()=>d.remove(),2000); }
loadProducts(); loadCart(); loadCustomers(); loadPaymentMethods();
</script>
<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.1.3/dist/js/bootstrap.bundle.min.js"></script>
</body></html>
'''
INVENTORY_HTML = '''
<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>库存管理</title><link href="https://cdn.jsdelivr.net/npm/bootstrap@5.1.3/dist/css/bootstrap.min.css" rel="stylesheet"></head>
<body>
<nav class="navbar navbar-expand-lg navbar-dark bg-dark"><div class="container"><a class="navbar-brand" href="/pos">商超系统</a><div class="navbar-nav"><a class="nav-link" href="/pos">收银台</a><a class="nav-link active" href="/inventory">库存管理</a><a class="nav-link" href="/sales">销售记录</a><a class="nav-link" href="/members">会员管理</a><a class="nav-link" href="/purchase">采购入库</a><a class="nav-link" href="/partners">往来单位</a><a class="nav-link" href="/finance">财务期初</a><a class="nav-link" href="/backup">数据备份</a><a class="nav-link" href="/useradmin">用户管理</a><a class="nav-link" href="/logout">退出</a></div><div class="navbar-nav ms-auto"><button class="btn btn-outline-light btn-sm me-2" onclick="window.print()">🖨️ 打印本页</button><button class="btn btn-outline-light btn-sm" onclick="exportInventory()">📎 导出 CSV</button></div></div></nav>
<div class="container mt-4"><div class="d-flex justify-content-between"><h3>商品库存</h3><button class="btn btn-primary" data-bs-toggle="modal" data-bs-target="#productModal" onclick="openAddModal()">+ 新增商品</button></div>
<div class="table-responsive"><table class="table table-bordered"><thead><tr><th>ID</th><th>名称</th><th>售价</th><th>成本</th><th>库存</th><th>状态</th><th>操作</th></tr></thead><tbody id="productTableBody"></tbody></td></div></div>
<div class="modal fade" id="productModal" tabindex="-1"><div class="modal-dialog"><div class="modal-content"><div class="modal-header"><h5 id="modalTitle">商品</h5><button type="button" class="btn-close" data-bs-dismiss="modal"></button></div><div class="modal-body"><input type="hidden" id="editId"><div><label>名称</label><input id="prodName" class="form-control"></div><div><label>售价</label><input id="prodPrice" type="number" step="0.01" class="form-control"></div><div><label>成本</label><input id="prodCost" type="number" step="0.01" class="form-control"></div><div><label>库存</label><input id="prodStock" type="number" class="form-control"></div></div><div class="modal-footer"><button class="btn btn-secondary" data-bs-dismiss="modal">取消</button><button class="btn btn-primary" id="saveProductBtn">保存</button></div></div></div></div>
<script>
async function loadProducts(){ const resp=await fetch('/api/products'); const ps=await resp.json(); const tbody=document.getElementById('productTableBody'); tbody.innerHTML=ps.map(p=>`<tr><td>${p.id}</td><td>${escapeHtml(p.name)}</td><td>${p.price.toFixed(2)}</td><td>${p.cost.toFixed(2)}</td><td>${p.stock}</td><td>${p.stock<10?'<span class="badge bg-warning">低库存</span>':'<span class="badge bg-success">充足</span>'}</td><td><button class="btn btn-sm btn-warning" onclick="openEditModal(${p.id},'${escapeHtml(p.name)}',${p.price},${p.cost},${p.stock})">编辑</button> <button class="btn btn-sm btn-danger" onclick="deleteProduct(${p.id})">删除</button></td></tr>`).join(''); }
function escapeHtml(s){ return s.replace(/[&<>]/g,m=>m==='&'?'&amp;':m==='<'?'&lt;':'&gt;'); }
function openAddModal(){ document.getElementById('editId').value=''; document.getElementById('prodName').value=''; document.getElementById('prodPrice').value=''; document.getElementById('prodCost').value=''; document.getElementById('prodStock').value=''; new bootstrap.Modal(document.getElementById('productModal')).show(); }
function openEditModal(id,name,price,cost,stock){ document.getElementById('editId').value=id; document.getElementById('prodName').value=name; document.getElementById('prodPrice').value=price; document.getElementById('prodCost').value=cost; document.getElementById('prodStock').value=stock; new bootstrap.Modal(document.getElementById('productModal')).show(); }
document.getElementById('saveProductBtn').onclick=async()=>{ const id=document.getElementById('editId').value; const name=document.getElementById('prodName').value.trim(); const price=parseFloat(document.getElementById('prodPrice').value); const cost=parseFloat(document.getElementById('prodCost').value); const stock=parseInt(document.getElementById('prodStock').value); if(!name||isNaN(price)||isNaN(cost)||isNaN(stock)){ alert('请正确填写'); return; } const url=id?`/api/products/${id}`:'/api/products'; const method=id?'PUT':'POST'; const resp=await fetch(url,{method,headers:{'Content-Type':'application/json'},body:JSON.stringify({name,price,cost,stock})}); if(resp.ok){ bootstrap.Modal.getInstance(document.getElementById('productModal')).hide(); loadProducts(); showToast('成功','success'); }else{ const err=await resp.json(); showToast(err.error,'danger'); } };
async function deleteProduct(id){ if(!confirm('确定删除？')) return; const resp=await fetch(`/api/products/${id}`,{method:'DELETE'}); if(resp.ok){ loadProducts(); showToast('删除成功','success'); }else{ const err=await resp.json(); showToast(err.error,'danger'); } }
function showToast(msg,type){ const d=document.createElement('div'); d.className=`alert alert-${type} alert-dismissible fade show position-fixed top-0 end-0 m-3`; d.style.zIndex=1050; d.innerHTML=`${msg}<button type="button" class="btn-close" data-bs-dismiss="alert"></button>`; document.body.appendChild(d); setTimeout(()=>d.remove(),2000); }
function exportInventory(){ window.location.href='/api/products/export'; }
loadProducts();
</script>
<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.1.3/dist/js/bootstrap.bundle.min.js"></script>
</body></html>
'''

SALES_HTML = '''
<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>销售记录</title><link href="https://cdn.jsdelivr.net/npm/bootstrap@5.1.3/dist/css/bootstrap.min.css" rel="stylesheet"></head>
<body>
<nav class="navbar navbar-expand-lg navbar-dark bg-dark"><div class="container"><a class="navbar-brand" href="/pos">商超系统</a><div class="navbar-nav"><a class="nav-link" href="/pos">收银台</a><a class="nav-link" href="/inventory">库存管理</a><a class="nav-link active" href="/sales">销售记录</a><a class="nav-link" href="/members">会员管理</a><a class="nav-link" href="/purchase">采购入库</a><a class="nav-link" href="/partners">往来单位</a><a class="nav-link" href="/finance">财务期初</a><a class="nav-link" href="/backup">数据备份</a><a class="nav-link" href="/useradmin">用户管理</a><a class="nav-link" href="/logout">退出</a></div><div class="navbar-nav ms-auto"><button class="btn btn-outline-light btn-sm me-2" onclick="window.print()">🖨️ 打印本页</button><button class="btn btn-outline-light btn-sm" onclick="exportSales()">📎 导出 CSV</button></div></div></nav>
<div class="container mt-4"><h3>销售历史记录</h3><div class="table-responsive"><table class="table table-hover"><thead><tr><th>单号</th><th>时间</th><th>金额</th><th>会员</th><th>客户</th><th>支付方式</th><th>操作员</th><th>操作</th></tr></thead><tbody id="salesTableBody"></tbody></table></div></div>
<div class="modal fade" id="detailModal" tabindex="-1"><div class="modal-dialog"><div class="modal-content"><div class="modal-header"><h5>销售详情</h5><button type="button" class="btn-close" data-bs-dismiss="modal"></button></div><div class="modal-body" id="detailContent"></div></div></div></div>
<script>
async function loadSales(){ const resp=await fetch('/api/sales'); const sales=await resp.json(); const tbody=document.getElementById('salesTableBody'); tbody.innerHTML=sales.map(s=>`<tr><td>${s.id}</td><td>${s.sale_time}</td><td>${s.total_amount.toFixed(2)}</td><td>${s.member_name||'散客'}</td><td>${s.customer_name||''}</td><td>${s.payment_method}</td><td>${s.created_by_name||''}</td><td><button class="btn btn-sm btn-info" onclick="viewDetail(${s.id})">详情</button> <button class="btn btn-sm btn-secondary" onclick="exportPDF(${s.id})">PDF</button></td></tr>`).join(''); }
async function viewDetail(id){ const resp=await fetch(`/api/sales/${id}`); const d=await resp.json(); if(resp.ok){ let itemsHtml=d.items.map(i=>`<tr><td>${escapeHtml(i.name)}</td><td>${i.quantity}</td><td>${i.price_at_sale.toFixed(2)}</td><td>${i.subtotal.toFixed(2)}</td></tr>`).join(''); document.getElementById('detailContent').innerHTML=`<p>单号:${d.id}</p><p>时间:${d.sale_time}</p><p>总金额:${d.total_amount}</p><p>会员:${d.member_name||'无'}</p><p>客户:${d.customer_name||'无'}</p><div class="table-responsive"><table class="table table-sm"><thead><tr><th>商品</th><th>数量</th><th>单价</th><th>小计</th></tr></thead><tbody>${itemsHtml}</tbody></table></div>`; new bootstrap.Modal(document.getElementById('detailModal')).show(); } else alert('加载失败'); }
function escapeHtml(s){ return s.replace(/[&<>]/g,m=>m==='&'?'&amp;':m==='<'?'&lt;':'&gt;'); }
function exportSales(){ window.location.href='/api/sales/export'; }
function exportPDF(id){ window.location.href=`/api/sales/${id}/pdf`; }
loadSales();
</script>
<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.1.3/dist/js/bootstrap.bundle.min.js"></script>
</body></html>
'''

MEMBERS_HTML = '''
<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>会员管理</title><link href="https://cdn.jsdelivr.net/npm/bootstrap@5.1.3/dist/css/bootstrap.min.css" rel="stylesheet"></head>
<body>
<nav class="navbar navbar-expand-lg navbar-dark bg-dark"><div class="container"><a class="navbar-brand" href="/pos">商超系统</a><div class="navbar-nav"><a class="nav-link" href="/pos">收银台</a><a class="nav-link" href="/inventory">库存管理</a><a class="nav-link" href="/sales">销售记录</a><a class="nav-link active" href="/members">会员管理</a><a class="nav-link" href="/purchase">采购入库</a><a class="nav-link" href="/partners">往来单位</a><a class="nav-link" href="/finance">财务期初</a><a class="nav-link" href="/backup">数据备份</a><a class="nav-link" href="/useradmin">用户管理</a><a class="nav-link" href="/logout">退出</a></div><div class="navbar-nav ms-auto"><button class="btn btn-outline-light btn-sm me-2" onclick="window.print()">🖨️ 打印本页</button><button class="btn btn-outline-light btn-sm" onclick="exportMembers()">📎 导出 CSV</button></div></div></nav>
<div class="container mt-4"><div class="d-flex justify-content-between"><h3>会员列表</h3><button class="btn btn-primary" data-bs-toggle="modal" data-bs-target="#memberModal" onclick="openAddModal()">+ 新增会员</button></div>
<div class="table-responsive"><table class="table table-bordered"><thead><tr><th>ID</th><th>姓名</th><th>手机</th><th>卡类型</th><th>余额/次数</th><th>有效期</th><th>操作</th></tr></thead><tbody id="memberTableBody"></tbody></table></div></div>
<div class="modal fade" id="memberModal" tabindex="-1"><div class="modal-dialog"><div class="modal-content"><div class="modal-header"><h5>会员</h5><button type="button" class="btn-close" data-bs-dismiss="modal"></button></div><div class="modal-body"><input type="hidden" id="editMemberId"><div><label>姓名</label><input id="memberName" class="form-control"></div><div><label>手机</label><input id="memberPhone" class="form-control"></div><div><label>卡类型</label><select id="cardType" class="form-select"><option value="stored_value">储值卡</option><option value="count_limited">次卡</option><option value="time_limited">期限卡</option></select></div><div id="balanceDiv"><label>初始余额</label><input id="initBalance" type="number" step="0.01" class="form-control" value="0"></div><div id="countsDiv" style="display:none"><label>初始次数</label><input id="initCounts" type="number" class="form-control" value="0"></div><div id="validDiv" style="display:none"><label>生效日期</label><input id="validFrom" type="date" class="form-control"><label>失效日期</label><input id="validTo" type="date" class="form-control"></div></div><div class="modal-footer"><button class="btn btn-secondary" data-bs-dismiss="modal">取消</button><button class="btn btn-primary" id="saveMemberBtn">保存</button></div></div></div></div>
<div class="modal fade" id="rechargeModal" tabindex="-1"><div class="modal-dialog"><div class="modal-content"><div class="modal-header"><h5>会员充值</h5><button type="button" class="btn-close" data-bs-dismiss="modal"></button></div><div class="modal-body"><input type="hidden" id="rechargeMemberId"><p id="rechargeMemberInfo"></p><div id="rechargeAmountDiv"><label>充值金额</label><input id="rechargeAmount" type="number" step="0.01" class="form-control" value="0"></div><div id="rechargeCountsDiv" style="display:none"><label>充值次数</label><input id="rechargeCounts" type="number" class="form-control" value="0"></div></div><div class="modal-footer"><button class="btn btn-primary" id="doRechargeBtn">确认充值</button></div></div></div></div>
<script>
async function loadMembers(){ const resp=await fetch('/api/members'); const ms=await resp.json(); const tbody=document.getElementById('memberTableBody'); tbody.innerHTML=ms.map(m=>`<tr><td>${m.id}</td><td>${escapeHtml(m.name)}</td><td>${m.phone}</td><td>${m.card_type==='stored_value'?'储值卡':m.card_type==='count_limited'?'次卡':'期限卡'}</td><td>余额:${m.balance} 次数:${m.remaining_counts}</td><td>${m.valid_from||'无'}~${m.valid_to||'无'}</td><td><button class="btn btn-sm btn-info" onclick="showRecharge(${m.id},'${escapeHtml(m.name)}','${m.card_type}')">充值</button> <button class="btn btn-sm btn-secondary" onclick="viewTransactions(${m.id})">记录</button></td></tr>`).join(''); }
function escapeHtml(s){ return s.replace(/[&<>]/g,m=>m==='&'?'&amp;':m==='<'?'&lt;':'&gt;'); }
function openAddModal(){ document.getElementById('editMemberId').value=''; document.getElementById('memberName').value=''; document.getElementById('memberPhone').value=''; document.getElementById('cardType').value='stored_value'; toggleFields(); new bootstrap.Modal(document.getElementById('memberModal')).show(); }
function toggleFields(){ const ct=document.getElementById('cardType').value; document.getElementById('balanceDiv').style.display=ct==='stored_value'?'block':'none'; document.getElementById('countsDiv').style.display=ct==='count_limited'?'block':'none'; document.getElementById('validDiv').style.display=ct==='time_limited'?'block':'none'; }
document.getElementById('cardType').addEventListener('change',toggleFields);
document.getElementById('saveMemberBtn').onclick=async()=>{ const name=document.getElementById('memberName').value.trim(); const phone=document.getElementById('memberPhone').value.trim(); const card_type=document.getElementById('cardType').value; let balance=0,remaining_counts=0,valid_from=null,valid_to=null; if(card_type==='stored_value') balance=parseFloat(document.getElementById('initBalance').value)||0; if(card_type==='count_limited') remaining_counts=parseInt(document.getElementById('initCounts').value)||0; if(card_type==='time_limited'){ valid_from=document.getElementById('validFrom').value; valid_to=document.getElementById('validTo').value; } if(!name||!phone){ alert('请填写完整'); return; } const resp=await fetch('/api/members',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name,phone,card_type,balance,remaining_counts,valid_from,valid_to})}); if(resp.ok){ bootstrap.Modal.getInstance(document.getElementById('memberModal')).hide(); loadMembers(); showToast('成功','success'); }else{ const err=await resp.json(); alert(err.error); } };
async function showRecharge(id,name,card_type){ document.getElementById('rechargeMemberId').value=id; document.getElementById('rechargeMemberInfo').innerText=`会员：${name} (${card_type==='stored_value'?'储值卡':card_type==='count_limited'?'次卡':'期限卡'})`; document.getElementById('rechargeAmountDiv').style.display=card_type==='stored_value'?'block':'none'; document.getElementById('rechargeCountsDiv').style.display=card_type==='count_limited'?'block':'none'; document.getElementById('rechargeAmount').value=0; document.getElementById('rechargeCounts').value=0; new bootstrap.Modal(document.getElementById('rechargeModal')).show(); }
document.getElementById('doRechargeBtn').onclick=async()=>{ const id=document.getElementById('rechargeMemberId').value; let amount=parseFloat(document.getElementById('rechargeAmount').value)||0; let counts=parseInt(document.getElementById('rechargeCounts').value)||0; if(amount===0 && counts===0){ alert('请输入充值金额或次数'); return; } const resp=await fetch(`/api/members/${id}/recharge`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({amount,counts})}); if(resp.ok){ bootstrap.Modal.getInstance(document.getElementById('rechargeModal')).hide(); loadMembers(); showToast('充值成功','success'); }else{ const err=await resp.json(); alert(err.error); } };
async function viewTransactions(id){ const resp=await fetch(`/api/members/${id}/transactions`); const ts=await resp.json(); let msg=ts.map(t=>`${t.transaction_time} ${t.transaction_type==='recharge'?'充值':'消费'} 金额:${t.amount||0} 次数:${t.counts_change||0} ${t.description||''}`).join('\\n'); alert(msg||'无记录'); }
function showToast(msg,type){ const d=document.createElement('div'); d.className=`alert alert-${type} alert-dismissible fade show position-fixed top-0 end-0 m-3`; d.style.zIndex=1050; d.innerHTML=`${msg}<button type="button" class="btn-close" data-bs-dismiss="alert"></button>`; document.body.appendChild(d); setTimeout(()=>d.remove(),2000); }
function exportMembers(){ window.location.href='/api/members/export'; }
loadMembers();
</script>
<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.1.3/dist/js/bootstrap.bundle.min.js"></script>
</body></html>
'''
PURCHASE_HTML = '''
<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>采购入库</title><link href="https://cdn.jsdelivr.net/npm/bootstrap@5.1.3/dist/css/bootstrap.min.css" rel="stylesheet"></head>
<body>
<nav class="navbar navbar-expand-lg navbar-dark bg-dark"><div class="container"><a class="navbar-brand" href="/pos">商超系统</a><div class="navbar-nav"><a class="nav-link" href="/pos">收银台</a><a class="nav-link" href="/inventory">库存管理</a><a class="nav-link" href="/sales">销售记录</a><a class="nav-link" href="/members">会员管理</a><a class="nav-link active" href="/purchase">采购入库</a><a class="nav-link" href="/partners">往来单位</a><a class="nav-link" href="/finance">财务期初</a><a class="nav-link" href="/backup">数据备份</a><a class="nav-link" href="/useradmin">用户管理</a><a class="nav-link" href="/logout">退出</a></div><div class="navbar-nav ms-auto"><button class="btn btn-outline-light btn-sm me-2" onclick="window.print()">🖨️ 打印本页</button></div></div></nav>
<div class="container mt-4"><h3>采购入库单</h3><div class="row"><div class="col-md-6"><div><label>供应商</label><input id="supplier" class="form-control" placeholder="手动输入供应商名称"></div><div class="mt-2"><label>选择已有供应商</label><select id="partnerSelect" class="form-select"><option value="">-- 选择（将自动填充名称） --</option></select></div><div class="mt-2"><label>支付方式</label><select id="payMethod" class="form-select"></select></div></div><div class="col-md-6"><h5>采购明细</h5><div class="table-responsive"><table class="table table-sm"><thead><tr><th>商品</th><th>数量</th><th>进价</th><th>小计</th><th></th></tr></thead><tbody id="purchaseItemsBody"></tbody></table></div><button class="btn btn-sm btn-primary" onclick="addPurchaseRow()">添加商品</button><button class="btn btn-success mt-3" id="submitPurchase">提交入库</button></div></div></div>
<script>
let itemIndex=0; let products=[], partners=[], paymentMethods=[];
async function loadData(){ const [pResp, partResp, payResp]=await Promise.all([fetch('/api/products'), fetch('/api/partners'), fetch('/api/payment_methods')]); products=await pResp.json(); partners=await partResp.json(); paymentMethods=await payResp.json(); const select=document.getElementById('partnerSelect'); select.innerHTML='<option value="">-- 选择（将自动填充名称） --</option>'+partners.filter(p=>p.type==='supplier').map(p=>`<option value="${p.id}" data-name="${escapeHtml(p.name)}">${escapeHtml(p.name)}</option>`).join(''); const paySelect=document.getElementById('payMethod'); paySelect.innerHTML=paymentMethods.map(m=>`<option value="${m}">${m}</option>`).join(''); }
function addPurchaseRow(){ const idx=itemIndex++; const tbody=document.getElementById('purchaseItemsBody'); const row=document.createElement('tr'); row.id=`row_${idx}`; row.innerHTML=`<td><select class="form-select product-select" data-idx="${idx}"><option value="">选择商品</option>${products.map(p=>`<option value="${p.id}" data-price="${p.cost}">${escapeHtml(p.name)}</option>`).join('')}</select></td><td><input type="number" class="form-control qty" data-idx="${idx}" value="1" min="1"></td><td><input type="number" step="0.01" class="form-control price" data-idx="${idx}" value="0"></td><td class="subtotal">0.00</td><td><button class="btn btn-sm btn-danger" onclick="document.getElementById('row_${idx}').remove()">删除</button></td>`; tbody.appendChild(row); attachEvents(idx); }
function attachEvents(idx){ const sel=document.querySelector(`#row_${idx} .product-select`); const qty=document.querySelector(`#row_${idx} .qty`); const price=document.querySelector(`#row_${idx} .price`); const update=()=>{ const selected=sel.options[sel.selectedIndex]; if(selected.value){ const cost=parseFloat(selected.dataset.price)||0; price.value=cost; } const q=parseFloat(qty.value)||0; const p=parseFloat(price.value)||0; document.querySelector(`#row_${idx} .subtotal`).innerText=(q*p).toFixed(2); }; sel.addEventListener('change',update); qty.addEventListener('input',update); price.addEventListener('input',update); update(); }
function escapeHtml(s){ return s.replace(/[&<>]/g,m=>m==='&'?'&amp;':m==='<'?'&lt;':'&gt;'); }
document.getElementById('partnerSelect').addEventListener('change',function(){ const selected=this.options[this.selectedIndex]; if(selected.value){ document.getElementById('supplier').value=selected.getAttribute('data-name'); } });
document.getElementById('submitPurchase').onclick=async()=>{ const items=[]; const rows=document.querySelectorAll('#purchaseItemsBody tr'); for(let row of rows){ const sel=row.querySelector('.product-select'); const pid=sel.value; if(!pid) continue; const qty=parseFloat(row.querySelector('.qty').value); const unitCost=parseFloat(row.querySelector('.price').value); if(isNaN(qty)||isNaN(unitCost)||qty<=0||unitCost<0){ alert('请正确填写数量与进价'); return; } items.push({product_id:parseInt(pid),quantity:qty,unit_cost:unitCost}); } if(items.length===0){ alert('请添加至少一种商品'); return; } let partner_id=document.getElementById('partnerSelect').value; if(partner_id) partner_id=parseInt(partner_id); const supplier=document.getElementById('supplier').value; const payment_method=document.getElementById('payMethod').value; const resp=await fetch('/api/purchases',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({supplier,partner_id,items,payment_method})}); const result=await resp.json(); if(resp.ok){ alert('采购入库成功！'); location.reload(); }else{ alert(result.error); } };
loadData().then(()=>addPurchaseRow());
</script>
<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.1.3/dist/js/bootstrap.bundle.min.js"></script>
</body></html>
'''

PARTNERS_HTML = '''
<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>往来单位管理</title><link href="https://cdn.jsdelivr.net/npm/bootstrap@5.1.3/dist/css/bootstrap.min.css" rel="stylesheet"></head>
<body>
<nav class="navbar navbar-expand-lg navbar-dark bg-dark"><div class="container"><a class="navbar-brand" href="/pos">商超系统</a><div class="navbar-nav"><a class="nav-link" href="/pos">收银台</a><a class="nav-link" href="/inventory">库存管理</a><a class="nav-link" href="/sales">销售记录</a><a class="nav-link" href="/members">会员管理</a><a class="nav-link" href="/purchase">采购入库</a><a class="nav-link active" href="/partners">往来单位</a><a class="nav-link" href="/finance">财务期初</a><a class="nav-link" href="/backup">数据备份</a><a class="nav-link" href="/useradmin">用户管理</a><a class="nav-link" href="/logout">退出</a></div><div class="navbar-nav ms-auto"><button class="btn btn-outline-light btn-sm me-2" onclick="window.print()">🖨️ 打印本页</button></div></div></nav>
<div class="container mt-4"><div class="d-flex justify-content-between"><h3>往来单位（供应商/客户）</h3><button class="btn btn-primary" data-bs-toggle="modal" data-bs-target="#partnerModal" onclick="openAddModal()">+ 新增</button></div>
<div class="table-responsive"><table class="table table-bordered"><thead><tr><th>ID</th><th>名称</th><th>类型</th><th>上级</th><th>联系人</th><th>电话</th><th>期初余额</th><th>当前余额</th><th>操作</th></tr></thead><tbody id="partnerTableBody"></tbody></table></div></div>
<div class="modal fade" id="partnerModal" tabindex="-1"><div class="modal-dialog"><div class="modal-content"><div class="modal-header"><h5 id="modalTitle">往来单位</h5><button type="button" class="btn-close" data-bs-dismiss="modal"></button></div><div class="modal-body"><input type="hidden" id="editId"><div><label>名称</label><input id="name" class="form-control"></div><div><label>类型</label><select id="type" class="form-select"><option value="customer">客户</option><option value="supplier">供应商</option></select></div><div><label>上级单位</label><select id="parentId" class="form-select"><option value="">无（顶级）</option></select></div><div><label>联系人</label><input id="contact" class="form-control"></div><div><label>电话</label><input id="phone" class="form-control"></div><div><label>地址</label><input id="address" class="form-control"></div><div><label>期初余额（元，客户为正欠款，供应商为负欠款）</label><input id="opening" type="number" step="0.01" class="form-control" value="0"></div><div><label>备注</label><input id="notes" class="form-control"></div></div><div class="modal-footer"><button class="btn btn-secondary" data-bs-dismiss="modal">取消</button><button class="btn btn-primary" id="saveBtn">保存</button></div></div></div></div>
<script>
let allPartners=[];
async function loadPartners(){ const resp=await fetch('/api/partners'); allPartners=await resp.json(); const tbody=document.getElementById('partnerTableBody'); tbody.innerHTML=allPartners.map(p=>`<tr><td>${p.id}</td><td>${escapeHtml(p.name)}</td><td>${p.type==='customer'?'客户':'供应商'}</td><td>${getParentName(p.parent_id)}</td><td>${escapeHtml(p.contact_person||'')}</td><td>${escapeHtml(p.phone||'')}</td><td>${p.opening_balance.toFixed(2)}</td><td>${p.current_balance.toFixed(2)}</td><td><button class="btn btn-sm btn-warning" onclick="openEditModal(${p.id})">编辑</button> <button class="btn btn-sm btn-danger" onclick="deletePartner(${p.id})">删除</button></td></tr>`).join(''); rebuildParentSelect(); }
function getParentName(pid){ const p=allPartners.find(x=>x.id==pid); return p?escapeHtml(p.name):''; }
function rebuildParentSelect(){ const select=document.getElementById('parentId'); const currentId=document.getElementById('editId').value; select.innerHTML='<option value="">无（顶级）</option>'; allPartners.forEach(p=>{ if(p.id!=currentId) select.innerHTML+=`<option value="${p.id}">${escapeHtml(p.name)}</option>`; }); }
function escapeHtml(s){ return s.replace(/[&<>]/g,m=>m==='&'?'&amp;':m==='<'?'&lt;':'&gt;'); }
function openAddModal(){ document.getElementById('editId').value=''; document.getElementById('name').value=''; document.getElementById('type').value='customer'; document.getElementById('contact').value=''; document.getElementById('phone').value=''; document.getElementById('address').value=''; document.getElementById('opening').value='0'; document.getElementById('notes').value=''; rebuildParentSelect(); new bootstrap.Modal(document.getElementById('partnerModal')).show(); }
async function openEditModal(id){ const p=allPartners.find(x=>x.id==id); if(p){ document.getElementById('editId').value=p.id; document.getElementById('name').value=p.name; document.getElementById('type').value=p.type; document.getElementById('contact').value=p.contact_person||''; document.getElementById('phone').value=p.phone||''; document.getElementById('address').value=p.address||''; document.getElementById('opening').value=p.opening_balance; document.getElementById('notes').value=p.notes||''; rebuildParentSelect(); if(p.parent_id) document.getElementById('parentId').value=p.parent_id; new bootstrap.Modal(document.getElementById('partnerModal')).show(); } }
document.getElementById('saveBtn').onclick=async()=>{ const id=document.getElementById('editId').value; const name=document.getElementById('name').value.trim(); const type=document.getElementById('type').value; const parent_id=document.getElementById('parentId').value||null; const contact=document.getElementById('contact').value; const phone=document.getElementById('phone').value; const address=document.getElementById('address').value; const opening=parseFloat(document.getElementById('opening').value)||0; const notes=document.getElementById('notes').value; if(!name){ alert('请输入名称'); return; } const url=id?`/api/partners/${id}`:'/api/partners'; const method=id?'PUT':'POST'; const resp=await fetch(url,{method,headers:{'Content-Type':'application/json'},body:JSON.stringify({name,type,parent_id,contact_person:contact,phone,address,opening_balance:opening,notes})}); if(resp.ok){ bootstrap.Modal.getInstance(document.getElementById('partnerModal')).hide(); loadPartners(); showToast('成功','success'); }else{ const err=await resp.json(); showToast(err.error,'danger'); } };
async function deletePartner(id){ if(!confirm('确定删除？')) return; const resp=await fetch(`/api/partners/${id}`,{method:'DELETE'}); if(resp.ok){ loadPartners(); showToast('删除成功','success'); }else{ const err=await resp.json(); showToast(err.error,'danger'); } }
function showToast(msg,type){ const d=document.createElement('div'); d.className=`alert alert-${type} alert-dismissible fade show position-fixed top-0 end-0 m-3`; d.style.zIndex=1050; d.innerHTML=`${msg}<button type="button" class="btn-close" data-bs-dismiss="alert"></button>`; document.body.appendChild(d); setTimeout(()=>d.remove(),2000); }
loadPartners();
</script>
<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.1.3/dist/js/bootstrap.bundle.min.js"></script>
</body></html>
'''

FINANCE_HTML = '''
<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>财务期初</title><link href="https://cdn.jsdelivr.net/npm/bootstrap@5.1.3/dist/css/bootstrap.min.css" rel="stylesheet"></head>
<body>
<nav class="navbar navbar-expand-lg navbar-dark bg-dark"><div class="container"><a class="navbar-brand" href="/pos">商超系统</a><div class="navbar-nav"><a class="nav-link" href="/pos">收银台</a><a class="nav-link" href="/inventory">库存管理</a><a class="nav-link" href="/sales">销售记录</a><a class="nav-link" href="/members">会员管理</a><a class="nav-link" href="/purchase">采购入库</a><a class="nav-link" href="/partners">往来单位</a><a class="nav-link active" href="/finance">财务期初</a><a class="nav-link" href="/backup">数据备份</a><a class="nav-link" href="/useradmin">用户管理</a><a class="nav-link" href="/logout">退出</a></div><div class="navbar-nav ms-auto"><button class="btn btn-outline-light btn-sm me-2" onclick="window.print()">🖨️ 打印本页</button></div></div></nav>
<div class="container mt-4"><div class="row"><div class="col-md-6"><div class="card"><div class="card-body"><h5>期初余额设置</h5><div><label>现金</label><input id="cash" type="number" step="0.01" class="form-control"></div><div><label>银行存款</label><input id="bank" type="number" step="0.01" class="form-control"></div><button id="saveInitBtn" class="btn btn-primary mt-2">保存期初</button></div></div><div class="card mt-3"><div class="card-body"><h5>支付方式配置</h5><div><label>支付方式（英文逗号分隔）</label><input id="payMethodsInput" class="form-control" placeholder="现金,银行转账,微信,支付宝"></div><button id="savePayMethodsBtn" class="btn btn-primary mt-2">保存支付方式</button></div></div></div><div class="col-md-6"><div class="card"><div class="card-body"><h5>财务报表</h5><p>销售收入: ¥<span id="totalSales">0</span></p><p>销售成本: ¥<span id="totalCost">0</span></p><p>毛利: ¥<span id="grossProfit">0</span></p><p>采购总额: ¥<span id="purchasesTotal">0</span></p><hr><p>现金余额: ¥<span id="balanceCash">0</span></p><p>银行余额: ¥<span id="balanceBank">0</span></p><p>库存价值: ¥<span id="balanceInventory">0</span></p><p>总资产: ¥<span id="totalAssets">0</span></p><p>会员预收款: ¥<span id="liability">0</span></p><p>所有者权益: ¥<span id="equity">0</span></p></div></div></div></div></div>
<script>
async function loadInitial(){ const resp=await fetch('/api/initial_balances'); const data=await resp.json(); document.getElementById('cash').value=data.cash; document.getElementById('bank').value=data.bank; }
async function loadReport(){ const resp=await fetch('/api/financial_report'); const data=await resp.json(); document.getElementById('totalSales').innerText=data.income_statement.total_sales; document.getElementById('totalCost').innerText=data.income_statement.total_cost; document.getElementById('grossProfit').innerText=data.income_statement.gross_profit; document.getElementById('purchasesTotal').innerText=data.purchases_total; document.getElementById('balanceCash').innerText=data.balance_sheet.cash; document.getElementById('balanceBank').innerText=data.balance_sheet.bank; document.getElementById('balanceInventory').innerText=data.balance_sheet.inventory; document.getElementById('totalAssets').innerText=data.balance_sheet.total_assets; document.getElementById('liability').innerText=data.balance_sheet.member_liability; document.getElementById('equity').innerText=data.balance_sheet.equity; }
document.getElementById('saveInitBtn').onclick=async()=>{ const cash=parseFloat(document.getElementById('cash').value)||0; const bank=parseFloat(document.getElementById('bank').value)||0; const resp=await fetch('/api/initial_balances',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({cash,bank})}); if(resp.ok){ alert('保存成功'); loadReport(); }else alert('保存失败'); };
async function loadPayMethods(){ const resp=await fetch('/api/payment_methods'); const methods=await resp.json(); document.getElementById('payMethodsInput').value=methods.join(','); }
document.getElementById('savePayMethodsBtn').onclick=async()=>{ const val=document.getElementById('payMethodsInput').value; const methods=val.split(',').map(s=>s.trim()).filter(s=>s); const resp=await fetch('/api/payment_methods',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({methods})}); const result=await resp.json(); alert(result.message||result.error); if(resp.ok) loadPayMethods(); };
loadInitial(); loadReport(); loadPayMethods();
</script>
<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.1.3/dist/js/bootstrap.bundle.min.js"></script>
</body></html>
'''

BACKUP_HTML = '''
<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>数据备份与恢复</title><link href="https://cdn.jsdelivr.net/npm/bootstrap@5.1.3/dist/css/bootstrap.min.css" rel="stylesheet"></head>
<body>
<nav class="navbar navbar-expand-lg navbar-dark bg-dark"><div class="container"><a class="navbar-brand" href="/pos">商超系统</a><div class="navbar-nav"><a class="nav-link" href="/pos">收银台</a><a class="nav-link" href="/inventory">库存管理</a><a class="nav-link" href="/sales">销售记录</a><a class="nav-link" href="/members">会员管理</a><a class="nav-link" href="/purchase">采购入库</a><a class="nav-link" href="/partners">往来单位</a><a class="nav-link" href="/finance">财务期初</a><a class="nav-link active" href="/backup">数据备份</a><a class="nav-link" href="/useradmin">用户管理</a><a class="nav-link" href="/logout">退出</a></div><div class="navbar-nav ms-auto"><button class="btn btn-outline-light btn-sm me-2" onclick="window.print()">🖨️ 打印本页</button></div></div></nav>
<div class="container mt-4"><div class="row"><div class="col-md-6"><div class="card"><div class="card-body"><h5>备份数据库</h5><button id="backupBtn" class="btn btn-primary">立即备份</button></div></div><div class="card mt-3"><div class="card-body"><h5>恢复数据库</h5><select id="restoreSelect" class="form-select"></select><button id="restoreBtn" class="btn btn-warning mt-2">恢复选中备份</button></div></div><div class="card mt-3"><div class="card-body"><h5>导出SQL迁移文件</h5><button id="exportBtn" class="btn btn-info">导出完整SQL</button></div></div></div><div class="col-md-6"><div class="card"><div class="card-body"><h5>备份文件列表</h5><ul id="backupList" class="list-group"></ul></div></div></div></div></div>
<script>
async function listBackups(){ const resp=await fetch('/api/backups'); const files=await resp.json(); const select=document.getElementById('restoreSelect'); const list=document.getElementById('backupList'); select.innerHTML=''; list.innerHTML=''; files.forEach(f=>{ const opt=document.createElement('option'); opt.value=f.name; opt.innerText=`${f.name} (${(f.size/1024).toFixed(1)}KB) ${new Date(f.modified).toLocaleString()}`; select.appendChild(opt); const li=document.createElement('li'); li.className='list-group-item'; li.innerText=opt.innerText; list.appendChild(li); }); }
document.getElementById('backupBtn').onclick=async()=>{ const resp=await fetch('/api/backup',{method:'POST'}); const data=await resp.json(); alert(data.message||data.error); listBackups(); };
document.getElementById('restoreBtn').onclick=async()=>{ const filename=document.getElementById('restoreSelect').value; if(!filename){ alert('请选择备份文件'); return; } if(confirm('恢复将覆盖当前数据，确认吗？')){ const resp=await fetch('/api/restore',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({filename})}); const data=await resp.json(); alert(data.message||data.error); } };
document.getElementById('exportBtn').onclick=async()=>{ const resp=await fetch('/api/migrate',{method:'POST'}); const data=await resp.json(); alert(data.message||data.error); };
listBackups();
</script>
<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.1.3/dist/js/bootstrap.bundle.min.js"></script>
</body></html>
'''
# ---------- 导出API（CSV）----------
@app.route('/api/sales/export')
@login_required
def export_sales_csv():
    conn,c = get_db()
    c.execute('''SELECT s.id,s.sale_time,s.total_amount,u.username as operator,m.name as member_name,p.name as customer_name,s.payment_method
                 FROM sales s LEFT JOIN users u ON s.created_by=u.id 
                 LEFT JOIN members m ON s.member_id=m.id 
                 LEFT JOIN partners p ON s.partner_id=p.id
                 ORDER BY s.sale_time DESC''')
    rows = c.fetchall()
    conn.close()
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(['销售单号','销售时间','总金额(元)','操作员','会员','客户','支付方式'])
    for r in rows:
        writer.writerow([r['id'], r['sale_time'], r['total_amount'], r['operator'] or '', r['member_name'] or '散客', r['customer_name'] or '', r['payment_method']])
    return Response(output.getvalue(), mimetype='text/csv', headers={"Content-Disposition":"attachment;filename=sales_export.csv"})

@app.route('/api/products/export')
@login_required
def export_products_csv():
    conn,c = get_db()
    c.execute("SELECT id,name,price,cost,stock FROM products")
    rows = c.fetchall()
    conn.close()
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(['ID','商品名称','售价(元)','成本价(元)','库存数量'])
    for r in rows:
        writer.writerow([r['id'], r['name'], r['price'], r['cost'], r['stock']])
    return Response(output.getvalue(), mimetype='text/csv', headers={"Content-Disposition":"attachment;filename=inventory_export.csv"})

@app.route('/api/members/export')
@login_required
@role_required(['admin'])
def export_members_csv():
    conn,c = get_db()
    c.execute("SELECT id,name,phone,card_type,balance,remaining_counts,valid_from,valid_to FROM members")
    rows = c.fetchall()
    conn.close()
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(['ID','姓名','手机号','卡类型','储值余额','剩余次数','生效日期','失效日期'])
    for r in rows:
        writer.writerow([r['id'], r['name'], r['phone'], r['card_type'], r['balance'], r['remaining_counts'], r['valid_from'] or '', r['valid_to'] or ''])
    return Response(output.getvalue(), mimetype='text/csv', headers={"Content-Disposition":"attachment;filename=members_export.csv"})

@app.route('/api/initial_balances', methods=['GET'])
@login_required
def get_initial_balances():
    cash = float(get_setting('current_cash', '0'))
    bank = float(get_setting('current_bank', '0'))
    return jsonify({'cash': cash, 'bank': bank})

@app.route('/api/initial_balances', methods=['POST'])
@login_required
@role_required(['admin'])
def set_initial_balances():
    data = request.get_json()
    cash = data.get('cash', 0)
    bank = data.get('bank', 0)
    update_setting('current_cash', cash)
    update_setting('current_bank', bank)
    return jsonify({'message': '保存成功'})

# ---------- 程序入口 ----------
if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)