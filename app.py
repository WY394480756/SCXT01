import sqlite3
import json
from datetime import datetime, date
from flask import Flask, render_template_string, request, jsonify, session

app = Flask(__name__)
app.secret_key = 'your-secret-key-change-in-production'

# ---------- 数据库初始化 ----------
def init_db():
    conn = sqlite3.connect('supermarket.db')
    c = conn.cursor()
    
    # 商品表（增加成本价字段用于利润核算）
    c.execute('''CREATE TABLE IF NOT EXISTS products (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        price REAL NOT NULL CHECK(price >= 0),
        cost REAL DEFAULT 0 CHECK(cost >= 0),
        stock INTEGER NOT NULL CHECK(stock >= 0)
    )''')
    
    # 销售主表（增加会员ID和支付方式）
    c.execute('''CREATE TABLE IF NOT EXISTS sales (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sale_time TEXT NOT NULL,
        total_amount REAL NOT NULL,
        member_id INTEGER DEFAULT NULL,
        payment_method TEXT DEFAULT 'cash',
        FOREIGN KEY (member_id) REFERENCES members (id)
    )''')
    
    # 销售明细表
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
    
    # 会员表
    c.execute('''CREATE TABLE IF NOT EXISTS members (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        phone TEXT UNIQUE,
        card_type TEXT NOT NULL, -- 'stored_value', 'count_limited', 'time_limited'
        balance REAL DEFAULT 0,  -- 储值卡余额
        remaining_counts INTEGER DEFAULT 0, -- 次卡剩余次数
        valid_from TEXT,         -- 期限卡生效日期
        valid_to TEXT,           -- 期限卡失效日期
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
    
    # 系统设置表
    c.execute('''CREATE TABLE IF NOT EXISTS system_settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP
    )''')
    
    # 插入示例商品
    c.execute("SELECT COUNT(*) FROM products")
    if c.fetchone()[0] == 0:
        sample_products = [
            ('纯牛奶', 3.5, 2.5, 100),
            ('面包', 5.0, 3.0, 50),
            ('鸡蛋', 1.2, 0.8, 200),
            ('矿泉水', 1.0, 0.6, 150),
            ('薯片', 7.5, 4.5, 80)
        ]
        c.executemany("INSERT INTO products (name, price, cost, stock) VALUES (?, ?, ?, ?)", sample_products)
    
    # 插入示例会员
    c.execute("SELECT COUNT(*) FROM members")
    if c.fetchone()[0] == 0:
        today = date.today().isoformat()
        next_month = date.today().replace(month=date.today().month+1).isoformat()
        c.execute("INSERT INTO members (name, phone, card_type, balance, remaining_counts, valid_from, valid_to) VALUES (?, ?, ?, ?, ?, ?, ?)",
                  ('张三', '13800001111', 'stored_value', 200.0, 0, None, None))
        c.execute("INSERT INTO members (name, phone, card_type, balance, remaining_counts, valid_from, valid_to) VALUES (?, ?, ?, ?, ?, ?, ?)",
                  ('李四', '13800002222', 'count_limited', 0, 10, None, None))
        c.execute("INSERT INTO members (name, phone, card_type, balance, remaining_counts, valid_from, valid_to) VALUES (?, ?, ?, ?, ?, ?, ?)",
                  ('王五', '13800003333', 'time_limited', 0, 0, today, next_month))
    
    # 初始化系统设置
    c.execute("INSERT OR IGNORE INTO system_settings (key, value) VALUES ('initial_cash', '0')")
    c.execute("INSERT OR IGNORE INTO system_settings (key, value) VALUES ('initial_bank', '0')")
    c.execute("INSERT OR IGNORE INTO system_settings (key, value) VALUES ('initial_inventory_value', '0')")
    c.execute("INSERT OR IGNORE INTO system_settings (key, value) VALUES ('member_liability', '0')")
    
    conn.commit()
    conn.close()

init_db()
# ---------- 辅助函数 ----------
def get_db():
    conn = sqlite3.connect('supermarket.db')
    conn.row_factory = sqlite3.Row
    return conn, conn.cursor()

def get_setting(key, default='0'):
    conn, c = get_db()
    c.execute("SELECT value FROM system_settings WHERE key = ?", (key,))
    row = c.fetchone()
    conn.close()
    return float(row['value']) if row else float(default)

def update_setting(key, value):
    conn, c = get_db()
    c.execute("REPLACE INTO system_settings (key, value, updated_at) VALUES (?, ?, ?)",
              (key, str(value), datetime.now().isoformat()))
    conn.commit()
    conn.close()

# ---------- 页面路由 ----------
@app.route('/')
def index():
    return render_template_string(INDEX_HTML)

@app.route('/inventory')
def inventory():
    return render_template_string(INVENTORY_HTML)

@app.route('/sales')
def sales_history():
    return render_template_string(SALES_HTML)

@app.route('/members')
def members():
    return render_template_string(MEMBERS_HTML)

@app.route('/finance')
def finance():
    return render_template_string(FINANCE_HTML)

# ---------- API：商品管理 ----------
@app.route('/api/products', methods=['GET'])
def get_products():
    conn, c = get_db()
    c.execute("SELECT id, name, price, cost, stock FROM products ORDER BY id")
    products = [dict(row) for row in c.fetchall()]
    conn.close()
    return jsonify(products)

@app.route('/api/products', methods=['POST'])
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
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

@app.route('/api/products/<int:product_id>', methods=['PUT'])
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
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

@app.route('/api/products/<int:product_id>', methods=['DELETE'])
def delete_product(product_id):
    conn, c = get_db()
    try:
        c.execute("SELECT COUNT(*) FROM sale_items WHERE product_id=?", (product_id,))
        if c.fetchone()[0] > 0:
            return jsonify({'error': '已有销售记录，无法删除'}), 400
        c.execute("DELETE FROM products WHERE id=?", (product_id,))
        conn.commit()
        return jsonify({'message': '删除成功'})
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

# ---------- API：购物车 ----------
@app.route('/api/cart', methods=['GET'])
def get_cart():
    cart = session.get('cart', {})
    if not cart:
        return jsonify({'items': [], 'total': 0})
    conn, c = get_db()
    items = []
    total = 0
    for product_id, quantity in cart.items():
        c.execute("SELECT id, name, price, stock FROM products WHERE id=?", (product_id,))
        product = c.fetchone()
        if product:
            p = dict(product)
            subtotal = p['price'] * quantity
            total += subtotal
            items.append({
                'product_id': p['id'],
                'name': p['name'],
                'price': p['price'],
                'quantity': quantity,
                'stock': p['stock'],
                'subtotal': round(subtotal, 2)
            })
    conn.close()
    return jsonify({'items': items, 'total': round(total, 2)})

@app.route('/api/cart/add', methods=['POST'])
def add_to_cart():
    data = request.get_json()
    product_id = str(data.get('product_id'))
    quantity = data.get('quantity', 1)
    if quantity <= 0:
        return jsonify({'error': '数量必须大于0'}), 400
    conn, c = get_db()
    c.execute("SELECT stock FROM products WHERE id=?", (product_id,))
    row = c.fetchone()
    conn.close()
    if not row:
        return jsonify({'error': '商品不存在'}), 404
    if row['stock'] < quantity:
        return jsonify({'error': f'库存不足，当前库存: {row["stock"]}'}), 400
    cart = session.get('cart', {})
    cart[product_id] = cart.get(product_id, 0) + quantity
    session['cart'] = cart
    return jsonify({'message': '添加成功'})

@app.route('/api/cart/update', methods=['PUT'])
def update_cart_item():
    data = request.get_json()
    product_id = str(data.get('product_id'))
    quantity = data.get('quantity', 0)
    cart = session.get('cart', {})
    if product_id not in cart:
        return jsonify({'error': '购物车中没有该商品'}), 404
    if quantity <= 0:
        del cart[product_id]
    else:
        conn, c = get_db()
        c.execute("SELECT stock FROM products WHERE id=?", (product_id,))
        row = c.fetchone()
        conn.close()
        if row and row['stock'] < quantity:
            return jsonify({'error': f'库存不足，当前库存: {row["stock"]}'}), 400
        cart[product_id] = quantity
    session['cart'] = cart
    return jsonify({'message': '更新成功'})

@app.route('/api/cart/remove', methods=['DELETE'])
def remove_from_cart():
    data = request.get_json()
    product_id = str(data.get('product_id'))
    cart = session.get('cart', {})
    if product_id in cart:
        del cart[product_id]
        session['cart'] = cart
    return jsonify({'message': '删除成功'})

@app.route('/api/cart/clear', methods=['DELETE'])
def clear_cart():
    session['cart'] = {}
    return jsonify({'message': '购物车已清空'})

# ---------- API：结算（支持会员）----------
@app.route('/api/checkout', methods=['POST'])
def checkout():
    data = request.get_json() or {}
    member_id = data.get('member_id')
    payment_method = data.get('payment_method', 'cash')
    cart = session.get('cart', {})
    if not cart:
        return jsonify({'error': '购物车为空'}), 400
    
    conn, c = get_db()
    try:
        c.execute("BEGIN")
        # 验证库存并收集商品信息
        items_to_buy = []
        total_amount = 0
        for product_id_str, quantity in cart.items():
            product_id = int(product_id_str)
            c.execute("SELECT name, price, cost, stock FROM products WHERE id=? FOR UPDATE", (product_id,))
            row = c.fetchone()
            if not row:
                raise Exception(f"商品ID {product_id} 不存在")
            if row['stock'] < quantity:
                raise Exception(f"{row['name']} 库存不足，当前库存: {row['stock']}")
            subtotal = row['price'] * quantity
            total_amount += subtotal
            items_to_buy.append({
                'id': product_id,
                'name': row['name'],
                'quantity': quantity,
                'price': row['price'],
                'cost': row['cost']
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
                counts_needed = 1
                if member['remaining_counts'] < counts_needed:
                    raise Exception(f"次卡剩余次数不足，当前剩余: {member['remaining_counts']}")
                new_counts = member['remaining_counts'] - counts_needed
                c.execute("UPDATE members SET remaining_counts = ? WHERE id = ?", (new_counts, member_id))
                c.execute("INSERT INTO member_transactions (member_id, transaction_type, counts_change, description) VALUES (?, ?, ?, ?)",
                          (member_id, 'consume', -counts_needed, f"消费 {counts_needed} 次"))
            elif member['card_type'] == 'time_limited':
                today_str = date.today().isoformat()
                if member['valid_from'] and today_str < member['valid_from']:
                    raise Exception("会员卡尚未生效")
                if member['valid_to'] and today_str > member['valid_to']:
                    raise Exception("会员卡已过期")
                c.execute("INSERT INTO member_transactions (member_id, transaction_type, description) VALUES (?, ?, ?)",
                          (member_id, 'consume', f"期限卡消费 {total_amount} 元"))
            else:
                raise Exception("未知会员卡类型")
        
        # 插入销售主表
        sale_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        c.execute("INSERT INTO sales (sale_time, total_amount, member_id, payment_method) VALUES (?, ?, ?, ?)",
                  (sale_time, total_amount, member_id, payment_method))
        sale_id = c.lastrowid
        
        for item in items_to_buy:
            c.execute("INSERT INTO sale_items (sale_id, product_id, quantity, price_at_sale, cost_at_sale) VALUES (?, ?, ?, ?, ?)",
                      (sale_id, item['id'], item['quantity'], item['price'], item['cost']))
            c.execute("UPDATE products SET stock = stock - ? WHERE id = ?",
                      (item['quantity'], item['id']))
        
        conn.commit()
        session['cart'] = {}
        return jsonify({'message': '结算成功', 'sale_id': sale_id, 'total_amount': total_amount})
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 400
    finally:
        conn.close()
# ---------- API：会员管理 ----------
@app.route('/api/members', methods=['GET'])
def get_members():
    conn, c = get_db()
    c.execute("SELECT id, name, phone, card_type, balance, remaining_counts, valid_from, valid_to, created_at FROM members ORDER BY id")
    members = [dict(row) for row in c.fetchall()]
    conn.close()
    return jsonify(members)

@app.route('/api/members', methods=['POST'])
def add_member():
    data = request.get_json()
    name = data.get('name', '').strip()
    phone = data.get('phone', '').strip()
    card_type = data.get('card_type')
    initial_balance = data.get('balance', 0)
    initial_counts = data.get('remaining_counts', 0)
    valid_from = data.get('valid_from')
    valid_to = data.get('valid_to')
    
    if not name or not phone or card_type not in ['stored_value', 'count_limited', 'time_limited']:
        return jsonify({'error': '参数错误'}), 400
    conn, c = get_db()
    try:
        c.execute("INSERT INTO members (name, phone, card_type, balance, remaining_counts, valid_from, valid_to) VALUES (?, ?, ?, ?, ?, ?, ?)",
                  (name, phone, card_type, initial_balance, initial_counts, valid_from, valid_to))
        member_id = c.lastrowid
        if initial_balance > 0:
            c.execute("INSERT INTO member_transactions (member_id, transaction_type, amount, description) VALUES (?, ?, ?, ?)",
                      (member_id, 'recharge', initial_balance, '开户充值'))
        if initial_counts > 0:
            c.execute("INSERT INTO member_transactions (member_id, transaction_type, counts_change, description) VALUES (?, ?, ?, ?)",
                      (member_id, 'recharge', initial_counts, '开户购次'))
        conn.commit()
        return jsonify({'id': member_id, 'message': '会员添加成功'}), 201
    except sqlite3.IntegrityError:
        return jsonify({'error': '手机号已存在'}), 400
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

@app.route('/api/members/<int:member_id>/recharge', methods=['POST'])
def recharge_member(member_id):
    data = request.get_json()
    amount = data.get('amount', 0)
    counts = data.get('counts', 0)
    if amount <= 0 and counts <= 0:
        return jsonify({'error': '充值金额或次数必须大于0'}), 400
    conn, c = get_db()
    try:
        c.execute("SELECT card_type, balance, remaining_counts FROM members WHERE id=? FOR UPDATE", (member_id,))
        member = c.fetchone()
        if not member:
            return jsonify({'error': '会员不存在'}), 404
        member = dict(member)
        if amount > 0:
            if member['card_type'] != 'stored_value':
                return jsonify({'error': '该会员不是储值卡，不能充值金额'}), 400
            new_balance = member['balance'] + amount
            c.execute("UPDATE members SET balance = ? WHERE id = ?", (new_balance, member_id))
            c.execute("INSERT INTO member_transactions (member_id, transaction_type, amount, description) VALUES (?, ?, ?, ?)",
                      (member_id, 'recharge', amount, f"充值 {amount} 元"))
        if counts > 0:
            if member['card_type'] != 'count_limited':
                return jsonify({'error': '该会员不是次卡，不能充值次数'}), 400
            new_counts = member['remaining_counts'] + counts
            c.execute("UPDATE members SET remaining_counts = ? WHERE id = ?", (new_counts, member_id))
            c.execute("INSERT INTO member_transactions (member_id, transaction_type, counts_change, description) VALUES (?, ?, ?, ?)",
                      (member_id, 'recharge', counts, f"充值 {counts} 次"))
        conn.commit()
        return jsonify({'message': '充值成功'})
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

@app.route('/api/members/<int:member_id>/transactions')
def member_transactions(member_id):
    conn, c = get_db()
    c.execute("SELECT * FROM member_transactions WHERE member_id=? ORDER BY transaction_time DESC", (member_id,))
    rows = [dict(row) for row in c.fetchall()]
    conn.close()
    return jsonify(rows)

# ---------- API：期初余额 ----------
@app.route('/api/initial_balances', methods=['GET'])
def get_initial_balances():
    cash = get_setting('initial_cash')
    bank = get_setting('initial_bank')
    inventory_value = get_setting('initial_inventory_value')
    member_liability = get_setting('member_liability')
    return jsonify({
        'cash': cash,
        'bank': bank,
        'inventory_value': inventory_value,
        'member_liability': member_liability
    })

@app.route('/api/initial_balances', methods=['POST'])
def set_initial_balances():
    data = request.get_json()
    cash = float(data.get('cash', 0))
    bank = float(data.get('bank', 0))
    inventory_value = float(data.get('inventory_value', 0))
    member_liability = float(data.get('member_liability', 0))
    if cash < 0 or bank < 0 or inventory_value < 0 or member_liability < 0:
        return jsonify({'error': '金额不能为负数'}), 400
    update_setting('initial_cash', cash)
    update_setting('initial_bank', bank)
    update_setting('initial_inventory_value', inventory_value)
    update_setting('member_liability', member_liability)
    return jsonify({'message': '期初余额保存成功'})

# ---------- API：财务报表 ----------
@app.route('/api/financial_report')
def financial_report():
    conn, c = get_db()
    c.execute("SELECT SUM(quantity * price_at_sale) as total_sales, SUM(quantity * cost_at_sale) as total_cost FROM sale_items")
    sales_data = c.fetchone()
    total_sales = sales_data['total_sales'] or 0
    total_cost = sales_data['total_cost'] or 0
    
    c.execute("SELECT SUM(stock * cost) as current_inventory_value FROM products")
    current_inv = c.fetchone()[0] or 0
    
    c.execute("SELECT SUM(balance) as member_balance FROM members WHERE card_type='stored_value'")
    member_balance = c.fetchone()[0] or 0
    
    initial_cash = get_setting('initial_cash')
    initial_bank = get_setting('initial_bank')
    initial_inv_value = get_setting('initial_inventory_value')
    initial_member_liability = get_setting('member_liability')
    
    gross_profit = total_sales - total_cost
    current_cash = initial_cash
    current_bank = initial_bank
    current_inventory = current_inv
    total_assets = current_cash + current_bank + current_inventory
    current_liability = member_balance
    equity = total_assets - current_liability
    
    conn.close()
    return jsonify({
        'income_statement': {
            'total_sales': round(total_sales, 2),
            'total_cost': round(total_cost, 2),
            'gross_profit': round(gross_profit, 2)
        },
        'balance_sheet': {
            'cash': round(current_cash, 2),
            'bank': round(current_bank, 2),
            'inventory': round(current_inventory, 2),
            'total_assets': round(total_assets, 2),
            'member_liability': round(current_liability, 2),
            'equity': round(equity, 2)
        }
    })

# ---------- API：销售历史 ----------
@app.route('/api/sales', methods=['GET'])
def get_sales():
    conn, c = get_db()
    c.execute("SELECT id, sale_time, total_amount, member_id, payment_method FROM sales ORDER BY sale_time DESC")
    sales = [dict(row) for row in c.fetchall()]
    for s in sales:
        if s['member_id']:
            c.execute("SELECT name FROM members WHERE id=?", (s['member_id'],))
            member = c.fetchone()
            s['member_name'] = member['name'] if member else None
    conn.close()
    return jsonify(sales)

@app.route('/api/sales/<int:sale_id>', methods=['GET'])
def get_sale_detail(sale_id):
    conn, c = get_db()
    c.execute("SELECT id, sale_time, total_amount, member_id, payment_method FROM sales WHERE id=?", (sale_id,))
    sale = c.fetchone()
    if not sale:
        return jsonify({'error': '销售记录不存在'}), 404
    sale = dict(sale)
    if sale['member_id']:
        c.execute("SELECT name FROM members WHERE id=?", (sale['member_id'],))
        member = c.fetchone()
        sale['member_name'] = member['name'] if member else None
    c.execute('''
        SELECT p.name, si.quantity, si.price_at_sale, (si.quantity * si.price_at_sale) as subtotal
        FROM sale_items si
        JOIN products p ON si.product_id = p.id
        WHERE si.sale_id = ?
    ''', (sale_id,))
    items = [dict(row) for row in c.fetchall()]
    conn.close()
    sale['items'] = items
    return jsonify(sale)
# ========== HTML 模板 ==========
INDEX_HTML = '''
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>收银台 - 商超收银系统</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.1.3/dist/css/bootstrap.min.css" rel="stylesheet">
    <style>
        .cart-item { border-bottom: 1px solid #eee; padding: 8px 0; }
        .total-price { font-size: 1.5rem; font-weight: bold; color: #d9534f; }
        .low-stock { color: #d9534f; font-size: 0.8rem; }
    </style>
</head>
<body>
    <nav class="navbar navbar-expand-lg navbar-dark bg-dark">
        <div class="container">
            <a class="navbar-brand" href="/">商超收银系统</a>
            <div class="navbar-nav">
                <a class="nav-link active" href="/">收银台</a>
                <a class="nav-link" href="/inventory">库存管理</a>
                <a class="nav-link" href="/sales">销售记录</a>
                <a class="nav-link" href="/members">会员管理</a>
                <a class="nav-link" href="/finance">财务期初</a>
            </div>
        </div>
    </nav>
    <div class="container mt-4">
        <div class="row">
            <div class="col-md-7">
                <h3>商品列表</h3>
                <input type="text" id="searchInput" class="form-control mb-3" placeholder="搜索商品...">
                <div id="productList" class="row"></div>
            </div>
            <div class="col-md-5">
                <h3>购物车 <button class="btn btn-sm btn-danger" id="clearCartBtn">清空</button></h3>
                <div id="cartItems"></div>
                <div class="mt-3">
                    <div class="mb-2">
                        <label>支付方式：</label>
                        <select id="paymentMethod" class="form-select">
                            <option value="cash">现金</option>
                            <option value="member_card">会员卡</option>
                        </select>
                    </div>
                    <div id="memberSelectDiv" style="display:none;" class="mb-2">
                        <label>会员ID：</label>
                        <input type="number" id="memberId" class="form-control" placeholder="输入会员ID">
                        <small class="text-muted">可前往会员管理查看ID</small>
                    </div>
                    <h4>总计: <span id="cartTotal">0.00</span> 元</h4>
                    <button class="btn btn-success w-100" id="checkoutBtn">结算</button>
                </div>
            </div>
        </div>
    </div>
    <script>
        async function loadProducts() {
            const resp = await fetch('/api/products');
            const products = await resp.json();
            const searchTerm = document.getElementById('searchInput').value.toLowerCase();
            const filtered = products.filter(p => p.name.toLowerCase().includes(searchTerm));
            const container = document.getElementById('productList');
            container.innerHTML = filtered.map(p => `
                <div class="col-sm-6 col-md-4 mb-3">
                    <div class="card">
                        <div class="card-body">
                            <h5 class="card-title">${escapeHtml(p.name)}</h5>
                            <p class="card-text">价格: ¥${p.price.toFixed(2)}<br>库存: ${p.stock} ${p.stock < 10 ? '<span class="low-stock">(低库存)</span>' : ''}</p>
                            <div class="input-group input-group-sm">
                                <input type="number" id="qty_${p.id}" class="form-control" value="1" min="1" max="${p.stock}">
                                <button class="btn btn-primary" onclick="addToCart(${p.id})">加入购物车</button>
                            </div>
                        </div>
                    </div>
                </div>
            `).join('');
        }
        
        function escapeHtml(str) { return str.replace(/[&<>]/g, function(m) { if(m==='&') return '&amp;'; if(m==='<') return '&lt;'; if(m==='>') return '&gt;'; return m;});}
        
        async function addToCart(productId) {
            const qtyInput = document.getElementById(`qty_${productId}`);
            let quantity = parseInt(qtyInput.value);
            if (isNaN(quantity) || quantity < 1) quantity = 1;
            const resp = await fetch('/api/cart/add', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({product_id: productId, quantity: quantity})
            });
            if (resp.ok) {
                loadCart();
                showToast('添加成功', 'success');
            } else {
                const err = await resp.json();
                showToast(err.error || '添加失败', 'danger');
            }
        }
        
        async function loadCart() {
            const resp = await fetch('/api/cart');
            const data = await resp.json();
            const cartDiv = document.getElementById('cartItems');
            if (data.items.length === 0) {
                cartDiv.innerHTML = '<p class="text-muted">购物车为空</p>';
                document.getElementById('cartTotal').innerText = '0.00';
                return;
            }
            cartDiv.innerHTML = data.items.map(item => `
                <div class="cart-item d-flex justify-content-between align-items-center">
                    <div><strong>${escapeHtml(item.name)}</strong><br>¥${item.price.toFixed(2)} × ${item.quantity} = ¥${item.subtotal.toFixed(2)}</div>
                    <div>
                        <button class="btn btn-sm btn-outline-secondary" onclick="updateCart(${item.product_id}, ${item.quantity-1})">-</button>
                        <span class="mx-1">${item.quantity}</span>
                        <button class="btn btn-sm btn-outline-secondary" onclick="updateCart(${item.product_id}, ${item.quantity+1})">+</button>
                        <button class="btn btn-sm btn-danger ms-2" onclick="removeFromCart(${item.product_id})">删除</button>
                    </div>
                </div>
            `).join('');
            document.getElementById('cartTotal').innerText = data.total.toFixed(2);
        }
        
        async function updateCart(productId, newQty) {
            if (newQty <= 0) { await removeFromCart(productId); return; }
            const resp = await fetch('/api/cart/update', {
                method: 'PUT',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({product_id: productId, quantity: newQty})
            });
            if (resp.ok) loadCart();
            else { const err = await resp.json(); showToast(err.error || '更新失败', 'danger'); }
        }
        
        async function removeFromCart(productId) {
            await fetch('/api/cart/remove', {
                method: 'DELETE',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({product_id: productId})
            });
            loadCart();
        }
        
        document.getElementById('clearCartBtn').addEventListener('click', async () => {
            await fetch('/api/cart/clear', {method: 'DELETE'});
            loadCart();
        });
        
        document.getElementById('paymentMethod').addEventListener('change', function() {
            document.getElementById('memberSelectDiv').style.display = this.value === 'member_card' ? 'block' : 'none';
        });
        
        document.getElementById('checkoutBtn').addEventListener('click', async () => {
            const paymentMethod = document.getElementById('paymentMethod').value;
            let member_id = null;
            if (paymentMethod === 'member_card') {
                member_id = parseInt(document.getElementById('memberId').value);
                if (isNaN(member_id)) { showToast('请输入会员ID', 'danger'); return; }
            }
            const resp = await fetch('/api/checkout', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({payment_method: paymentMethod, member_id: member_id})
            });
            const result = await resp.json();
            if (resp.ok) {
                showToast(`结算成功！销售单号: ${result.sale_id}，金额: ¥${result.total_amount.toFixed(2)}`, 'success');
                loadCart();
                loadProducts();
            } else {
                showToast(result.error || '结算失败', 'danger');
            }
        });
        
        document.getElementById('searchInput').addEventListener('input', loadProducts);
        function showToast(msg, type) {
            const toastDiv = document.createElement('div');
            toastDiv.className = `alert alert-${type} alert-dismissible fade show position-fixed top-0 end-0 m-3`;
            toastDiv.style.zIndex = 1050;
            toastDiv.innerHTML = `${msg}<button type="button" class="btn-close" data-bs-dismiss="alert"></button>`;
            document.body.appendChild(toastDiv);
            setTimeout(() => toastDiv.remove(), 2000);
        }
        loadProducts(); loadCart();
    </script>
    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.1.3/dist/js/bootstrap.bundle.min.js"></script>
</body>
</html>
'''

INVENTORY_HTML = '''
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>库存管理 - 商超系统</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.1.3/dist/css/bootstrap.min.css" rel="stylesheet">
</head>
<body>
    <nav class="navbar navbar-expand-lg navbar-dark bg-dark">
        <div class="container">
            <a class="navbar-brand" href="/">商超收银系统</a>
            <div class="navbar-nav">
                <a class="nav-link" href="/">收银台</a>
                <a class="nav-link active" href="/inventory">库存管理</a>
                <a class="nav-link" href="/sales">销售记录</a>
                <a class="nav-link" href="/members">会员管理</a>
                <a class="nav-link" href="/finance">财务期初</a>
            </div>
        </div>
    </nav>
    <div class="container mt-4">
        <div class="d-flex justify-content-between mb-3">
            <h3>商品库存管理</h3>
            <button class="btn btn-primary" data-bs-toggle="modal" data-bs-target="#productModal" onclick="openAddModal()">+ 新增商品</button>
        </div>
        <table class="table table-striped table-bordered">
            <thead><tr><th>ID</th><th>名称</th><th>售价(元)</th><th>成本(元)</th><th>库存</th><th>状态</th><th>操作</th></tr></thead>
            <tbody id="productTableBody"></tbody>
        </table>
    </div>
    <div class="modal fade" id="productModal" tabindex="-1">
        <div class="modal-dialog">
            <div class="modal-content">
                <div class="modal-header"><h5 class="modal-title" id="modalTitle">新增商品</h5><button type="button" class="btn-close" data-bs-dismiss="modal"></button></div>
                <div class="modal-body">
                    <input type="hidden" id="editId">
                    <div class="mb-3"><label>商品名称</label><input type="text" id="prodName" class="form-control"></div>
                    <div class="mb-3"><label>售价</label><input type="number" id="prodPrice" class="form-control" step="0.01" min="0"></div>
                    <div class="mb-3"><label>成本价</label><input type="number" id="prodCost" class="form-control" step="0.01" min="0"></div>
                    <div class="mb-3"><label>库存数量</label><input type="number" id="prodStock" class="form-control" min="0"></div>
                </div>
                <div class="modal-footer"><button type="button" class="btn btn-secondary" data-bs-dismiss="modal">取消</button><button type="button" class="btn btn-primary" id="saveProductBtn">保存</button></div>
            </div>
        </div>
    </div>
    <script>
        async function loadProducts() {
            const resp = await fetch('/api/products');
            const products = await resp.json();
            const tbody = document.getElementById('productTableBody');
            tbody.innerHTML = products.map(p => `
                <tr><td>${p.id}</td><td>${escapeHtml(p.name)}</td><td>${p.price.toFixed(2)}</td><td>${p.cost.toFixed(2)}</td><td>${p.stock}</td>
                <td>${p.stock < 10 ? '<span class="badge bg-warning">低库存</span>' : '<span class="badge bg-success">充足</span>'}</td>
                <td><button class="btn btn-sm btn-warning" onclick="openEditModal(${p.id}, '${escapeHtml(p.name)}', ${p.price}, ${p.cost}, ${p.stock})">编辑</button>
                <button class="btn btn-sm btn-danger" onclick="deleteProduct(${p.id})">删除</button></td></tr>
            `).join('');
        }
        function escapeHtml(str) { return str.replace(/[&<>]/g, m => m==='&'?'&amp;':m==='<'?'&lt;':'&gt;');}
        function openAddModal() {
            document.getElementById('modalTitle').innerText = '新增商品';
            document.getElementById('editId').value = '';
            document.getElementById('prodName').value = ''; document.getElementById('prodPrice').value = '';
            document.getElementById('prodCost').value = ''; document.getElementById('prodStock').value = '';
            new bootstrap.Modal(document.getElementById('productModal')).show();
        }
        function openEditModal(id,name,price,cost,stock) {
            document.getElementById('modalTitle').innerText = '编辑商品';
            document.getElementById('editId').value = id;
            document.getElementById('prodName').value = name; document.getElementById('prodPrice').value = price;
            document.getElementById('prodCost').value = cost; document.getElementById('prodStock').value = stock;
            new bootstrap.Modal(document.getElementById('productModal')).show();
        }
        document.getElementById('saveProductBtn').onclick = async () => {
            const id = document.getElementById('editId').value;
            const name = document.getElementById('prodName').value.trim();
            const price = parseFloat(document.getElementById('prodPrice').value);
            const cost = parseFloat(document.getElementById('prodCost').value);
            const stock = parseInt(document.getElementById('prodStock').value);
            if (!name || isNaN(price) || price<0 || isNaN(cost) || cost<0 || isNaN(stock) || stock<0) { alert('请正确填写所有字段'); return; }
            let url, method;
            if (id) { url = `/api/products/${id}`; method='PUT'; } else { url='/api/products'; method='POST'; }
            const resp = await fetch(url, {method, headers:{'Content-Type':'application/json'}, body:JSON.stringify({name,price,cost,stock})});
            if (resp.ok) { bootstrap.Modal.getInstance(document.getElementById('productModal')).hide(); loadProducts(); showToast('保存成功','success'); }
            else { const err = await resp.json(); showToast(err.error||'保存失败','danger'); }
        };
        async function deleteProduct(id) {
            if (!confirm('确定删除？')) return;
            const resp = await fetch(`/api/products/${id}`, {method:'DELETE'});
            const result = await resp.json();
            if (resp.ok) { loadProducts(); showToast('删除成功','success'); } else { showToast(result.error||'删除失败','danger'); }
        }
        function showToast(msg,type) { /* 同前 */ const d=document.createElement('div'); d.className=`alert alert-${type} alert-dismissible fade show position-fixed top-0 end-0 m-3`; d.style.zIndex=1050; d.innerHTML=`${msg}<button type="button" class="btn-close" data-bs-dismiss="alert"></button>`; document.body.appendChild(d); setTimeout(()=>d.remove(),2000); }
        loadProducts();
    </script>
    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.1.3/dist/js/bootstrap.bundle.min.js"></script>
</body>
</html>
'''

SALES_HTML = '''
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>销售记录 - 商超系统</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.1.3/dist/css/bootstrap.min.css" rel="stylesheet">
</head>
<body>
    <nav class="navbar navbar-expand-lg navbar-dark bg-dark">
        <div class="container">
            <a class="navbar-brand" href="/">商超收银系统</a>
            <div class="navbar-nav">
                <a class="nav-link" href="/">收银台</a>
                <a class="nav-link" href="/inventory">库存管理</a>
                <a class="nav-link active" href="/sales">销售记录</a>
                <a class="nav-link" href="/members">会员管理</a>
                <a class="nav-link" href="/finance">财务期初</a>
            </div>
        </div>
    </nav>
    <div class="container mt-4">
        <h3>销售历史记录</h3>
        <table class="table table-hover">
            <thead><tr><th>销售单号</th><th>销售时间</th><th>总金额(元)</th><th>会员</th><th>支付方式</th><th>操作</th></tr></thead>
            <tbody id="salesTableBody"></tbody>
        </table>
    </div>
    <div class="modal fade" id="detailModal" tabindex="-1">
        <div class="modal-dialog"><div class="modal-content"><div class="modal-header"><h5 class="modal-title">销售详情</h5><button type="button" class="btn-close" data-bs-dismiss="modal"></button></div><div class="modal-body" id="detailContent"></div></div></div>
    </div>
    <script>
        async function loadSales() {
            const resp = await fetch('/api/sales');
            const sales = await resp.json();
            const tbody = document.getElementById('salesTableBody');
            tbody.innerHTML = sales.map(s => `<tr><td>${s.id}</td><td>${s.sale_time}</td><td>${s.total_amount.toFixed(2)}</td><td>${s.member_name || '散客'}</td><td>${s.payment_method === 'cash' ? '现金' : '会员卡'}</td><td><button class="btn btn-sm btn-info" onclick="viewDetail(${s.id})">查看详情</button></td></tr>`).join('');
        }
        async function viewDetail(saleId) {
            const resp = await fetch(`/api/sales/${saleId}`);
            const detail = await resp.json();
            if (resp.ok) {
                const itemsHtml = detail.items.map(item => `<tr><td>${escapeHtml(item.name)}</td><td>${item.quantity}</td><td>¥${item.price_at_sale.toFixed(2)}</td><td>¥${item.subtotal.toFixed(2)}</td></tr>`).join('');
                document.getElementById('detailContent').innerHTML = `<p>销售单号: ${detail.id}</p><p>时间: ${detail.sale_time}</p><p>总金额: ¥${detail.total_amount.toFixed(2)}</p><p>会员: ${detail.member_name || '无'}</p><table class="table table-sm"><thead><tr><th>商品</th><th>数量</th><th>单价</th><th>小计</th></tr></thead><tbody>${itemsHtml}</tbody></table>`;
                new bootstrap.Modal(document.getElementById('detailModal')).show();
            } else alert('加载详情失败');
        }
        function escapeHtml(str) { return str.replace(/[&<>]/g, m => m==='&'?'&amp;':m==='<'?'&lt;':'&gt;'); }
        loadSales();
    </script>
    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.1.3/dist/js/bootstrap.bundle.min.js"></script>
</body>
</html>
'''
MEMBERS_HTML = '''
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>会员管理 - 商超系统</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.1.3/dist/css/bootstrap.min.css" rel="stylesheet">
</head>
<body>
    <nav class="navbar navbar-expand-lg navbar-dark bg-dark">
        <div class="container">
            <a class="navbar-brand" href="/">商超收银系统</a>
            <div class="navbar-nav">
                <a class="nav-link" href="/">收银台</a>
                <a class="nav-link" href="/inventory">库存管理</a>
                <a class="nav-link" href="/sales">销售记录</a>
                <a class="nav-link active" href="/members">会员管理</a>
                <a class="nav-link" href="/finance">财务期初</a>
            </div>
        </div>
    </nav>
    <div class="container mt-4">
        <div class="d-flex justify-content-between mb-3"><h3>会员列表</h3><button class="btn btn-primary" data-bs-toggle="modal" data-bs-target="#memberModal" onclick="openAddModal()">+ 新增会员</button></div>
        <table class="table table-bordered">
            <thead><tr><th>ID</th><th>姓名</th><th>手机号</th><th>卡类型</th><th>储值余额</th><th>剩余次数</th><th>有效期</th><th>操作</th></tr></thead>
            <tbody id="memberTableBody"></tbody>
        </table>
    </div>
    <!-- 会员模态框 -->
    <div class="modal fade" id="memberModal" tabindex="-1"><div class="modal-dialog"><div class="modal-content"><div class="modal-header"><h5 id="modalTitle">新增会员</h5><button type="button" class="btn-close" data-bs-dismiss="modal"></button></div><div class="modal-body"><input type="hidden" id="editMemberId"><div class="mb-2"><label>姓名</label><input id="memberName" class="form-control"></div><div class="mb-2"><label>手机号</label><input id="memberPhone" class="form-control"></div><div class="mb-2"><label>卡类型</label><select id="cardType" class="form-select"><option value="stored_value">储值卡</option><option value="count_limited">次卡</option><option value="time_limited">期限卡</option></select></div><div id="balanceDiv" class="mb-2"><label>初始余额(元)</label><input id="initBalance" class="form-control" type="number" step="0.01" value="0"></div><div id="countsDiv" class="mb-2" style="display:none;"><label>初始次数</label><input id="initCounts" class="form-control" type="number" value="0"></div><div id="validDiv" class="mb-2" style="display:none;"><label>有效期(起始日期)</label><input id="validFrom" class="form-control" type="date"><label>截止日期</label><input id="validTo" class="form-control" type="date"></div></div><div class="modal-footer"><button type="button" class="btn btn-secondary" data-bs-dismiss="modal">取消</button><button type="button" id="saveMemberBtn" class="btn btn-primary">保存</button></div></div></div></div>
    <!-- 充值模态框 -->
    <div class="modal fade" id="rechargeModal" tabindex="-1"><div class="modal-dialog"><div class="modal-content"><div class="modal-header"><h5>会员充值</h5><button type="button" class="btn-close" data-bs-dismiss="modal"></button></div><div class="modal-body"><input type="hidden" id="rechargeMemberId"><p id="rechargeMemberInfo"></p><div id="rechargeAmountDiv"><label>充值金额(元)</label><input id="rechargeAmount" class="form-control" type="number" step="0.01" value="0"></div><div id="rechargeCountsDiv" style="display:none;"><label>充值次数</label><input id="rechargeCounts" class="form-control" type="number" value="0"></div></div><div class="modal-footer"><button class="btn btn-primary" id="doRechargeBtn">确认充值</button></div></div></div></div>
    <script>
        async function loadMembers() {
            const resp = await fetch('/api/members');
            const members = await resp.json();
            const tbody = document.getElementById('memberTableBody');
            tbody.innerHTML = members.map(m => `<tr><td>${m.id}</td><td>${escapeHtml(m.name)}</td><td>${m.phone}</td><td>${m.card_type==='stored_value'?'储值卡':m.card_type==='count_limited'?'次卡':'期限卡'}</td><td>¥${m.balance.toFixed(2)}</td><td>${m.remaining_counts}</td><td>${m.valid_from||'无'} ~ ${m.valid_to||'无'}</td><td><button class="btn btn-sm btn-info" onclick="showRecharge(${m.id},'${escapeHtml(m.name)}','${m.card_type}')">充值</button> <button class="btn btn-sm btn-secondary" onclick="viewTransactions(${m.id})">记录</button></td></tr>`).join('');
        }
        function escapeHtml(str) { return str.replace(/[&<>]/g, m=>m==='&'?'&amp;':m==='<'?'&lt;':'&gt;');}
        function openAddModal() { document.getElementById('editMemberId').value=''; document.getElementById('memberName').value=''; document.getElementById('memberPhone').value=''; document.getElementById('cardType').value='stored_value'; toggleCardFields(); new bootstrap.Modal(document.getElementById('memberModal')).show(); }
        function toggleCardFields() {
            const ct = document.getElementById('cardType').value;
            document.getElementById('balanceDiv').style.display = ct==='stored_value'?'block':'none';
            document.getElementById('countsDiv').style.display = ct==='count_limited'?'block':'none';
            document.getElementById('validDiv').style.display = ct==='time_limited'?'block':'none';
        }
        document.getElementById('cardType').addEventListener('change', toggleCardFields);
        document.getElementById('saveMemberBtn').onclick = async () => {
            const name = document.getElementById('memberName').value.trim();
            const phone = document.getElementById('memberPhone').value.trim();
            const card_type = document.getElementById('cardType').value;
            let balance = 0, remaining_counts=0, valid_from=null, valid_to=null;
            if (card_type === 'stored_value') balance = parseFloat(document.getElementById('initBalance').value) || 0;
            if (card_type === 'count_limited') remaining_counts = parseInt(document.getElementById('initCounts').value) || 0;
            if (card_type === 'time_limited') { valid_from = document.getElementById('validFrom').value; valid_to = document.getElementById('validTo').value; }
            if (!name || !phone) { alert('请填写姓名和手机号'); return; }
            const resp = await fetch('/api/members', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name,phone,card_type,balance,remaining_counts,valid_from,valid_to})});
            if (resp.ok) { bootstrap.Modal.getInstance(document.getElementById('memberModal')).hide(); loadMembers(); showToast('添加成功','success'); } else { const err = await resp.json(); alert(err.error); }
        };
        async function showRecharge(id, name, card_type) {
            document.getElementById('rechargeMemberId').value = id;
            document.getElementById('rechargeMemberInfo').innerText = `会员：${name} (${card_type==='stored_value'?'储值卡':card_type==='count_limited'?'次卡':'期限卡'})`;
            const amountDiv = document.getElementById('rechargeAmountDiv');
            const countsDiv = document.getElementById('rechargeCountsDiv');
            amountDiv.style.display = card_type==='stored_value'?'block':'none';
            countsDiv.style.display = card_type==='count_limited'?'block':'none';
            document.getElementById('rechargeAmount').value = 0;
            document.getElementById('rechargeCounts').value = 0;
            new bootstrap.Modal(document.getElementById('rechargeModal')).show();
        }
        document.getElementById('doRechargeBtn').onclick = async () => {
            const member_id = document.getElementById('rechargeMemberId').value;
            let amount = parseFloat(document.getElementById('rechargeAmount').value) || 0;
            let counts = parseInt(document.getElementById('rechargeCounts').value) || 0;
            if (amount===0 && counts===0) { alert('请输入充值金额或次数'); return; }
            const resp = await fetch(`/api/members/${member_id}/recharge`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({amount, counts})});
            if (resp.ok) { bootstrap.Modal.getInstance(document.getElementById('rechargeModal')).hide(); loadMembers(); showToast('充值成功','success'); } else { const err = await resp.json(); alert(err.error); }
        };
        async function viewTransactions(member_id) {
            const resp = await fetch(`/api/members/${member_id}/transactions`);
            const trans = await resp.json();
            let msg = trans.map(t => `${t.transaction_time} ${t.transaction_type==='recharge'?'充值':t.transaction_type==='consume'?'消费':''} 金额:${t.amount||0} 次数变化:${t.counts_change||0} ${t.description||''}`).join('\\n');
            alert(msg || '无交易记录');
        }
        function showToast(msg,type) { /* 同前 */ const d=document.createElement('div'); d.className=`alert alert-${type} alert-dismissible fade show position-fixed top-0 end-0 m-3`; d.style.zIndex=1050; d.innerHTML=`${msg}<button type="button" class="btn-close" data-bs-dismiss="alert"></button>`; document.body.appendChild(d); setTimeout(()=>d.remove(),2000); }
        loadMembers();
    </script>
    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.1.3/dist/js/bootstrap.bundle.min.js"></script>
</body>
</html>
'''

FINANCE_HTML = '''
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>财务初始化 - 商超系统</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.1.3/dist/css/bootstrap.min.css" rel="stylesheet">
</head>
<body>
    <nav class="navbar navbar-expand-lg navbar-dark bg-dark">
        <div class="container">
            <a class="navbar-brand" href="/">商超收银系统</a>
            <div class="navbar-nav">
                <a class="nav-link" href="/">收银台</a>
                <a class="nav-link" href="/inventory">库存管理</a>
                <a class="nav-link" href="/sales">销售记录</a>
                <a class="nav-link" href="/members">会员管理</a>
                <a class="nav-link active" href="/finance">财务期初</a>
            </div>
        </div>
    </nav>
    <div class="container mt-4">
        <h3>期初余额设置</h3>
        <div class="row">
            <div class="col-md-6">
                <div class="card"><div class="card-body"><h5>初始化科目余额</h5>
                    <div class="mb-2"><label>现金（收银台现金）</label><input id="cash" class="form-control" type="number" step="0.01"></div>
                    <div class="mb-2"><label>银行存款</label><input id="bank" class="form-control" type="number" step="0.01"></div>
                    <div class="mb-2"><label>库存商品成本价值（期初库存总额）</label><input id="inventoryValue" class="form-control" type="number" step="0.01"></div>
                    <div class="mb-2"><label>会员预收款（储值卡已充值未消费总额）</label><input id="memberLiability" class="form-control" type="number" step="0.01"></div>
                    <button id="saveInitBtn" class="btn btn-primary">保存期初余额</button>
                </div></div>
            </div>
            <div class="col-md-6">
                <div class="card"><div class="card-body"><h5>利润表（基于销售记录）</h5>
                    <p>总销售收入: ¥<span id="totalSales">0</span></p>
                    <p>总销售成本: ¥<span id="totalCost">0</span></p>
                    <p>毛利: ¥<span id="grossProfit">0</span></p>
                    <hr>
                    <h5>资产负债表（期初+变动）</h5>
                    <p>现金: ¥<span id="balanceCash">0</span></p>
                    <p>银行存款: ¥<span id="balanceBank">0</span></p>
                    <p>库存商品: ¥<span id="balanceInventory">0</span></p>
                    <p>总资产: ¥<span id="totalAssets">0</span></p>
                    <p>会员预收负债: ¥<span id="liability">0</span></p>
                    <p>所有者权益: ¥<span id="equity">0</span></p>
                </div></div>
            </div>
        </div>
    </div>
    <script>
        async function loadInitial() {
            const resp = await fetch('/api/initial_balances');
            const data = await resp.json();
            document.getElementById('cash').value = data.cash;
            document.getElementById('bank').value = data.bank;
            document.getElementById('inventoryValue').value = data.inventory_value;
            document.getElementById('memberLiability').value = data.member_liability;
        }
        async function loadReport() {
            const resp = await fetch('/api/financial_report');
            const data = await resp.json();
            document.getElementById('totalSales').innerText = data.income_statement.total_sales;
            document.getElementById('totalCost').innerText = data.income_statement.total_cost;
            document.getElementById('grossProfit').innerText = data.income_statement.gross_profit;
            document.getElementById('balanceCash').innerText = data.balance_sheet.cash;
            document.getElementById('balanceBank').innerText = data.balance_sheet.bank;
            document.getElementById('balanceInventory').innerText = data.balance_sheet.inventory;
            document.getElementById('totalAssets').innerText = data.balance_sheet.total_assets;
            document.getElementById('liability').innerText = data.balance_sheet.member_liability;
            document.getElementById('equity').innerText = data.balance_sheet.equity;
        }
        document.getElementById('saveInitBtn').onclick = async () => {
            const cash = parseFloat(document.getElementById('cash').value) || 0;
            const bank = parseFloat(document.getElementById('bank').value) || 0;
            const inventory_value = parseFloat(document.getElementById('inventoryValue').value) || 0;
            const member_liability = parseFloat(document.getElementById('memberLiability').value) || 0;
            const resp = await fetch('/api/initial_balances', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({cash, bank, inventory_value, member_liability})});
            if (resp.ok) { alert('保存成功'); loadReport(); } else alert('保存失败');
        };
        loadInitial(); loadReport();
    </script>
    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.1.3/dist/js/bootstrap.bundle.min.js"></script>
</body>
</html>
'''

# ---------- 程序入口 ----------
if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)