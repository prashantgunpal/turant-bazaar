import sqlite3
from flask import Flask, jsonify, request, render_template

app = Flask(__name__)
DB = "preview.db"

def get_connection():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_connection()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS vendors (id INTEGER PRIMARY KEY, name TEXT, area TEXT);
    CREATE TABLE IF NOT EXISTS products (id INTEGER PRIMARY KEY, name TEXT, price REAL, category TEXT, vendor_id INTEGER, image_url TEXT);
    CREATE TABLE IF NOT EXISTS orders (id INTEGER PRIMARY KEY, customer_name TEXT, customer_phone TEXT, total_amount REAL, status TEXT DEFAULT 'placed');
    CREATE TABLE IF NOT EXISTS order_items (id INTEGER PRIMARY KEY, order_id INTEGER, product_id INTEGER, quantity INTEGER);
    """)
    if conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 0:
        conn.executescript("""
        INSERT INTO vendors (name, area) VALUES ('Sharma Kirana','Silampuriya'),('Mayank Sweets','Sardarshahar Locality'),('Jodhpur Road Mandi','Jodhpur Road');
        INSERT INTO products (name, price, category, vendor_id, image_url) VALUES
        ('Amul Doodh (500ml)', 32.00, 'Daily', 1, ''),
        ('Besan Laddu (250g)', 90.00, 'Mithai', 2, ''),
        ('Taazi Tamatar (1kg)', 28.00, 'Sabzi', 3, ''),
        ('Bikaneri Papad', 55.00, 'Anaj', 1, '');
        """)
    conn.commit()
    conn.close()

@app.route("/")
def home():
    return render_template("index.html")

@app.route("/api/products")
def get_products():
    conn = get_connection()
    rows = conn.execute("""
        SELECT p.id, p.name, p.price, p.category, p.image_url, v.name AS vendor_name
        FROM products p LEFT JOIN vendors v ON p.vendor_id = v.id
    """).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/orders", methods=["POST"])
def place_order():
    data = request.get_json()
    items = data.get("items", [])
    if not items:
        return jsonify({"error": "Cart is empty"}), 400
    conn = get_connection()
    total = 0
    for item in items:
        row = conn.execute("SELECT price FROM products WHERE id=?", (item["product_id"],)).fetchone()
        total += row["price"] * item["quantity"]
    cur = conn.execute("INSERT INTO orders (customer_name, customer_phone, total_amount) VALUES (?,?,?)",
                        (data.get("customer_name","Guest"), data.get("customer_phone",""), total))
    order_id = cur.lastrowid
    for item in items:
        conn.execute("INSERT INTO order_items (order_id, product_id, quantity) VALUES (?,?,?)",
                     (order_id, item["product_id"], item["quantity"]))
    conn.commit()
    conn.close()
    return jsonify({"order_id": order_id, "total": total, "status": "placed"}), 201

@app.route("/health")
def health():
    return jsonify({"status": "ok"})

if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=8080)
