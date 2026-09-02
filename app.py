import os
import json
import math
import urllib.parse
import urllib.request

import boto3
import mysql.connector
from flask import Flask, jsonify, request, render_template, render_template_string, session

app = Flask(__name__)
app.secret_key = os.getenv('FLASK_SECRET_KEY', 'turant-bazaar-dev-secret-change-in-production')


# ============================================================
# DATABASE CONFIGURATION
# ============================================================

def get_db_credentials():
    """Read DB credentials from AWS Secrets Manager when configured."""
    secret_name = os.getenv("DB_SECRET_NAME")
    region = os.getenv("AWS_REGION", "ap-south-1")

    if not secret_name:
        return {
            "host": os.getenv("DB_HOST", "localhost"),
            "user": os.getenv("DB_USER", "root"),
            "password": os.getenv("DB_PASSWORD", ""),
            "database": os.getenv("DB_NAME", "turant_bazaar"),
            "port": int(os.getenv("DB_PORT", "3306")),
        }

    client = boto3.client("secretsmanager", region_name=region)
    response = client.get_secret_value(SecretId=secret_name)
    secret = response.get("SecretString", "{}")
    return json.loads(secret)


def get_connection():
    """Create a MySQL connection."""
    creds = get_db_credentials()
    return mysql.connector.connect(
        host=creds["host"],
        user=creds["user"],
        password=creds["password"],
        database=creds["database"],
        port=int(creds.get("port", 3306)),
    )


# ============================================================
# CUSTOMER WEBSITE
# ============================================================

@app.route("/")
def home():
    return render_template("index.html")


@app.route("/api/products", methods=["GET"])
def get_products():
    conn = None
    cur = None

    try:
        conn = get_connection()
        cur = conn.cursor(dictionary=True)
        cur.execute(
            """
            SELECT
                p.id,
                p.name,
                p.price,
                p.category,
                p.image_url,
                p.vendor_id,
                v.name AS vendor_name,
                v.area AS vendor_area
            FROM products p
            LEFT JOIN vendors v ON p.vendor_id = v.id
            ORDER BY p.id DESC
            """
        )
        products = cur.fetchall()
        return jsonify(products), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500

    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


@app.route("/api/delivery-areas", methods=["GET"])
def get_delivery_areas():
    conn = None
    cur = None
    try:
        conn = get_connection()
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT id, name, delivery_fee, eta_min, eta_max FROM delivery_areas WHERE is_active = 1 ORDER BY name ASC")
        return jsonify(cur.fetchall()), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


@app.route("/api/orders", methods=["POST"])
def place_order():
    conn = None
    cur = None

    try:
        data = request.get_json() or {}

        customer_name = str(data.get("customer_name", "")).strip()
        customer_phone = str(data.get("customer_phone", "")).strip()
        delivery_address = str(data.get("delivery_address", "")).strip()
        delivery_area = str(data.get("delivery_area", "")).strip()
        landmark = str(data.get("landmark", "")).strip()
        payment_method = str(data.get("payment_method", "cod")).strip().lower()
        items = data.get("items", [])

        if not customer_name:
            return jsonify({"error": "Customer name is required"}), 400

        if not customer_phone:
            return jsonify({"error": "Customer phone is required"}), 400

        if not delivery_address:
            return jsonify({"error": "Delivery address is required"}), 400

        latitude = data.get("latitude")
        longitude = data.get("longitude")
        location_verified = bool(data.get("location_verified"))

        if not delivery_area:
            return jsonify({"error": "Delivery area is required"}), 400

        if payment_method not in ("cod", "upi"):
            return jsonify({"error": "Invalid payment method"}), 400

        if not isinstance(items, list) or not items:
            return jsonify({"error": "Order must contain at least one item"}), 400

        conn = get_connection()
        cur = conn.cursor(dictionary=True)

        # Location-based serviceability: verify coordinates server-side when supplied.
        area_row = None
        if location_verified and latitude is not None and longitude is not None:
            try:
                lat = float(latitude)
                lng = float(longitude)
                distance = haversine_km(lat, lng, SARDARSHAHAR_LAT, SARDARSHAHAR_LNG)
            except (TypeError, ValueError):
                return jsonify({"error": "Invalid location coordinates."}), 400
            if distance > SERVICE_RADIUS_KM:
                return jsonify({"error": "This location is outside our Sardarshahar delivery area."}), 400
            delivery_fee = 20.0
            area_row = {"name": delivery_area or "Sardarshahar", "eta_min": 15, "eta_max": 30}
        else:
            cur.execute(
                "SELECT name, delivery_fee, eta_min, eta_max FROM delivery_areas WHERE name = %s AND is_active = 1 LIMIT 1",
                (delivery_area,),
            )
            area_row = cur.fetchone()
            if not area_row:
                return jsonify({"error": "Please select a valid delivery location."}), 400
            delivery_fee = float(area_row["delivery_fee"])

        # Calculate the subtotal using database prices. Never trust browser totals.
        subtotal = 0.0
        clean_items = []

        for item in items:
            try:
                product_id = int(item.get("product_id"))
                quantity = int(item.get("quantity"))
            except (TypeError, ValueError):
                return jsonify({"error": "Invalid product or quantity"}), 400

            if quantity <= 0:
                return jsonify({"error": "Quantity must be greater than 0"}), 400

            cur.execute(
                "SELECT id, price FROM products WHERE id = %s",
                (product_id,),
            )
            product = cur.fetchone()

            if not product:
                return jsonify({"error": f"Invalid product {product_id}"}), 400

            subtotal += float(product["price"]) * quantity
            clean_items.append((product_id, quantity))

        total = subtotal + delivery_fee

        cur.execute(
            """
            INSERT INTO orders (
                customer_name, customer_phone, delivery_address,
                delivery_area, landmark, payment_method, subtotal_amount,
                delivery_fee, total_amount, status, customer_user_id,
                latitude, longitude
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                customer_name, customer_phone, delivery_address,
                delivery_area, landmark or None, payment_method,
                subtotal, delivery_fee, total, "placed", session.get("customer_id"),
                float(latitude) if latitude is not None else None,
                float(longitude) if longitude is not None else None,
            ),
        )
        order_id = cur.lastrowid

        for product_id, quantity in clean_items:
            cur.execute(
                """
                INSERT INTO order_items (order_id, product_id, quantity)
                VALUES (%s, %s, %s)
                """,
                (order_id, product_id, quantity),
            )

        conn.commit()

        return jsonify(
            {
                "order_id": order_id,
                "subtotal": round(subtotal, 2),
                "delivery_fee": round(delivery_fee, 2),
                "total": round(total, 2),
                "status": "placed",
                "delivery_area": area_row["name"],
                "eta_min": area_row["eta_min"],
                "eta_max": area_row["eta_max"],
            }
        ), 201

    except Exception as e:
        if conn:
            conn.rollback()
        return jsonify({"error": str(e)}), 500

    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


# ============================================================
# CUSTOMER AUTH + LOCATION
# ============================================================

@app.route("/api/auth/request-otp", methods=["POST"])
def request_otp():
    data = request.get_json() or {}
    phone = str(data.get("phone", "")).strip()
    if not phone.isdigit() or len(phone) != 10:
        return jsonify({"error": "Enter a valid 10 digit mobile number."}), 400

    # Development OTP only. Replace with a real SMS/OTP provider before production.
    session["otp_phone"] = phone
    session["otp_code"] = "123456"
    return jsonify({
        "message": "OTP sent successfully.",
        "dev_otp": "123456",
        "development_only": True,
    }), 200


@app.route("/api/auth/verify-otp", methods=["POST"])
def verify_otp():
    data = request.get_json() or {}
    phone = str(data.get("phone", "")).strip()
    otp = str(data.get("otp", "")).strip()

    if phone != session.get("otp_phone") or otp != session.get("otp_code"):
        return jsonify({"error": "Invalid or expired OTP."}), 400

    conn = None
    cur = None
    try:
        conn = get_connection()
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT id, phone, name FROM customer_users WHERE phone = %s LIMIT 1", (phone,))
        user = cur.fetchone()
        if not user:
            cur.execute("INSERT INTO customer_users (phone, name) VALUES (%s, %s)", (phone, None))
            user_id = cur.lastrowid
            user = {"id": user_id, "phone": phone, "name": None}
            conn.commit()

        session["customer_id"] = user["id"]
        session["customer_phone"] = user["phone"]
        session.pop("otp_phone", None)
        session.pop("otp_code", None)
        return jsonify({"logged_in": True, "user": user}), 200
    except Exception as e:
        if conn:
            conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


@app.route("/api/auth/me", methods=["GET"])
def auth_me():
    user_id = session.get("customer_id")
    if not user_id:
        return jsonify({"logged_in": False}), 200
    conn = None
    cur = None
    try:
        conn = get_connection()
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT id, phone, name FROM customer_users WHERE id = %s LIMIT 1", (user_id,))
        user = cur.fetchone()
        if not user:
            session.clear()
            return jsonify({"logged_in": False}), 200
        return jsonify({"logged_in": True, "user": user}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


@app.route("/api/auth/profile", methods=["PUT"])
def update_profile():
    user_id = session.get("customer_id")
    if not user_id:
        return jsonify({"error": "Login required."}), 401
    data = request.get_json() or {}
    name = str(data.get("name", "")).strip()
    if not name:
        return jsonify({"error": "Name is required."}), 400
    conn = None
    cur = None
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("UPDATE customer_users SET name = %s WHERE id = %s", (name, user_id))
        conn.commit()
        return jsonify({"message": "Profile updated", "name": name}), 200
    except Exception as e:
        if conn:
            conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


@app.route("/api/auth/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"logged_in": False}), 200


SARDARSHAHAR_LAT = 28.440554
SARDARSHAHAR_LNG = 74.493011
SERVICE_RADIUS_KM = 12.0


def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


@app.route("/api/location/check", methods=["POST"])
def check_location():
    data = request.get_json() or {}
    try:
        lat = float(data.get("lat"))
        lng = float(data.get("lng"))
    except (TypeError, ValueError):
        return jsonify({"error": "Valid latitude and longitude are required."}), 400

    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return jsonify({"error": "Invalid coordinates."}), 400

    distance = haversine_km(lat, lng, SARDARSHAHAR_LAT, SARDARSHAHAR_LNG)
    serviceable = distance <= SERVICE_RADIUS_KM
    return jsonify({
        "serviceable": serviceable,
        "city": "Sardarshahar",
        "distance_km": round(distance, 2),
        "delivery_fee": 20 if serviceable else None,
        "eta_min": 15 if serviceable else None,
        "eta_max": 30 if serviceable else None,
        "message": "Delivery available" if serviceable else "Sorry, Turant Bazaar abhi aapke location par available nahi hai."
    }), 200


@app.route("/api/location/reverse", methods=["POST"])
def reverse_location():
    data = request.get_json() or {}
    try:
        lat = float(data.get("lat"))
        lng = float(data.get("lng"))
    except (TypeError, ValueError):
        return jsonify({"error": "Valid coordinates are required."}), 400

    # OpenStreetMap Nominatim is used only for development/testing.
    try:
        query = urllib.parse.urlencode({"lat": lat, "lon": lng, "format": "jsonv2", "zoom": 18, "addressdetails": 1})
        url = "https://nominatim.openstreetmap.org/reverse?" + query
        req = urllib.request.Request(url, headers={"User-Agent": "TurantBazaar/1.0 local-development"})
        with urllib.request.urlopen(req, timeout=8) as response:
            result = json.loads(response.read().decode("utf-8"))
        address = result.get("display_name", "")
        return jsonify({"address": address, "raw": result.get("address", {})}), 200
    except Exception:
        return jsonify({"address": "Current location selected", "raw": {}}), 200


@app.route("/api/location/search", methods=["GET"])
def search_location():
    q = str(request.args.get("q", "")).strip()
    if len(q) < 3:
        return jsonify([]), 200

    # First try OpenStreetMap Nominatim. This is only for local development.
    try:
        q_lower = q.lower()
        search_text = q if "sardarshahar" in q_lower else f"{q}, Sardarshahar, Rajasthan, India"
        query = urllib.parse.urlencode({
            "q": search_text,
            "format": "jsonv2",
            "limit": 5,
            "addressdetails": 1,
            "countrycodes": "in",
        })
        url = "https://nominatim.openstreetmap.org/search?" + query
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "TurantBazaar/1.0 local-development"},
        )
        with urllib.request.urlopen(req, timeout=8) as response:
            results = json.loads(response.read().decode("utf-8"))

        formatted = [{
            "display_name": x.get("display_name", ""),
            "lat": float(x["lat"]),
            "lng": float(x["lon"]),
        } for x in results]

        if formatted:
            return jsonify(formatted), 200
    except Exception:
        pass

    # Reliable local-development fallback. If Nominatim is blocked/unavailable,
    # any Sardarshahar search still gives a serviceable point in the town centre.
    q_lower = q.lower()
    sardar_terms = ["sardarshahar", "sardarshahr", "station road", "railway station", "clock tower", "gandhi chowk", "nai sadak", "main market"]
    if any(term in q_lower for term in sardar_terms):
        return jsonify([{
            "display_name": f"{q}, Sardarshahar, Churu, Rajasthan 331403",
            "lat": SARDARSHAHAR_LAT,
            "lng": SARDARSHAHAR_LNG,
            "fallback": True,
        }]), 200

    return jsonify([]), 200


@app.route("/api/my-orders", methods=["GET"])
def my_orders():
    user_id = session.get("customer_id")
    if not user_id:
        return jsonify({"error": "Login required."}), 401
    conn = None
    cur = None
    item_cur = None
    try:
        conn = get_connection()
        cur = conn.cursor(dictionary=True)
        item_cur = conn.cursor(dictionary=True)
        cur.execute("SELECT id, customer_name, customer_phone, delivery_address, delivery_area, landmark, payment_method, subtotal_amount, delivery_fee, total_amount, status, created_at FROM orders WHERE customer_user_id = %s ORDER BY id DESC", (user_id,))
        orders = cur.fetchall()
        for order in orders:
            item_cur.execute("SELECT oi.quantity, p.name AS product_name, p.price FROM order_items oi LEFT JOIN products p ON oi.product_id = p.id WHERE oi.order_id = %s ORDER BY oi.id", (order["id"],))
            order["items"] = item_cur.fetchall()
        return jsonify(orders), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if item_cur: item_cur.close()
        if cur: cur.close()
        if conn: conn.close()


# ============================================================
# ADMIN PANEL
# ============================================================

ADMIN_STATUSES = [
    "placed",
    "confirmed",
    "preparing",
    "out_for_delivery",
    "delivered",
    "cancelled",
]


@app.route("/admin", methods=["GET"])
def admin_page():
    """Single-file admin panel. No admin.html file is required."""
    return render_template_string(ADMIN_HTML)


@app.route("/api/admin/products", methods=["GET"])
def admin_get_products():
    conn = None
    cur = None

    try:
        conn = get_connection()
        cur = conn.cursor(dictionary=True)
        cur.execute(
            """
            SELECT
                p.id,
                p.name,
                p.price,
                p.category,
                p.image_url,
                p.vendor_id,
                v.name AS vendor_name,
                v.area AS vendor_area
            FROM products p
            LEFT JOIN vendors v ON p.vendor_id = v.id
            ORDER BY p.id DESC
            """
        )
        return jsonify(cur.fetchall()), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500

    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


@app.route("/api/admin/products", methods=["POST"])
def admin_create_product():
    conn = None
    cur = None

    try:
        data = request.get_json() or {}

        name = str(data.get("name", "")).strip()
        category = str(data.get("category", "")).strip()
        image_url = str(data.get("image_url", "")).strip()
        vendor_name = str(data.get("vendor_name", "")).strip()
        vendor_area = str(data.get("vendor_area", "")).strip()

        try:
            price = float(data.get("price"))
        except (TypeError, ValueError):
            return jsonify({"error": "Price must be a valid number"}), 400

        if not name:
            return jsonify({"error": "Product name is required"}), 400

        if price < 0:
            return jsonify({"error": "Price cannot be negative"}), 400

        conn = get_connection()
        cur = conn.cursor(dictionary=True)

        vendor_id = None

        if vendor_name:
            cur.execute(
                "SELECT id FROM vendors WHERE name = %s LIMIT 1",
                (vendor_name,),
            )
            vendor = cur.fetchone()

            if vendor:
                vendor_id = vendor["id"]
            else:
                cur.execute(
                    "INSERT INTO vendors (name, area) VALUES (%s, %s)",
                    (vendor_name, vendor_area),
                )
                vendor_id = cur.lastrowid

        cur.execute(
            """
            INSERT INTO products (name, price, category, vendor_id, image_url)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (name, price, category or None, vendor_id, image_url),
        )
        product_id = cur.lastrowid
        conn.commit()

        return jsonify(
            {
                "message": "Product added successfully",
                "product_id": product_id,
            }
        ), 201

    except Exception as e:
        if conn:
            conn.rollback()
        return jsonify({"error": str(e)}), 500

    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


@app.route("/api/admin/products/<int:product_id>", methods=["PUT"])
def admin_update_product(product_id):
    conn = None
    cur = None

    try:
        data = request.get_json() or {}

        name = str(data.get("name", "")).strip()
        category = str(data.get("category", "")).strip()
        image_url = str(data.get("image_url", "")).strip()
        vendor_name = str(data.get("vendor_name", "")).strip()
        vendor_area = str(data.get("vendor_area", "")).strip()

        try:
            price = float(data.get("price"))
        except (TypeError, ValueError):
            return jsonify({"error": "Price must be a valid number"}), 400

        if not name:
            return jsonify({"error": "Product name is required"}), 400

        if price < 0:
            return jsonify({"error": "Price cannot be negative"}), 400

        conn = get_connection()
        cur = conn.cursor(dictionary=True)

        cur.execute("SELECT id FROM products WHERE id = %s", (product_id,))
        if not cur.fetchone():
            return jsonify({"error": "Product not found"}), 404

        vendor_id = None

        if vendor_name:
            cur.execute(
                "SELECT id FROM vendors WHERE name = %s LIMIT 1",
                (vendor_name,),
            )
            vendor = cur.fetchone()

            if vendor:
                vendor_id = vendor["id"]
                if vendor_area:
                    cur.execute(
                        "UPDATE vendors SET area = %s WHERE id = %s",
                        (vendor_area, vendor_id),
                    )
            else:
                cur.execute(
                    "INSERT INTO vendors (name, area) VALUES (%s, %s)",
                    (vendor_name, vendor_area),
                )
                vendor_id = cur.lastrowid

        cur.execute(
            """
            UPDATE products
            SET name = %s,
                price = %s,
                category = %s,
                vendor_id = %s,
                image_url = %s
            WHERE id = %s
            """,
            (
                name,
                price,
                category or None,
                vendor_id,
                image_url,
                product_id,
            ),
        )

        conn.commit()
        return jsonify({"message": "Product updated successfully"}), 200

    except Exception as e:
        if conn:
            conn.rollback()
        return jsonify({"error": str(e)}), 500

    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


@app.route("/api/admin/products/<int:product_id>", methods=["DELETE"])
def admin_delete_product(product_id):
    conn = None
    cur = None

    try:
        conn = get_connection()
        cur = conn.cursor()

        cur.execute("SELECT id FROM products WHERE id = %s", (product_id,))
        if not cur.fetchone():
            return jsonify({"error": "Product not found"}), 404

        cur.execute("DELETE FROM products WHERE id = %s", (product_id,))
        conn.commit()

        return jsonify({"message": "Product deleted successfully"}), 200

    except mysql.connector.Error as e:
        if conn:
            conn.rollback()
        # Product may already be referenced by order_items.
        return jsonify(
            {
                "error": "This product cannot be deleted because it is already used in an order.",
                "details": str(e),
            }
        ), 409

    except Exception as e:
        if conn:
            conn.rollback()
        return jsonify({"error": str(e)}), 500

    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


@app.route("/api/admin/orders", methods=["GET"])
def admin_get_orders():
    conn = None
    cur = None
    item_cur = None

    try:
        conn = get_connection()
        cur = conn.cursor(dictionary=True)
        item_cur = conn.cursor(dictionary=True)

        cur.execute(
            """
            SELECT
                id,
                customer_name,
                customer_phone,
                delivery_address,
                delivery_area,
                landmark,
                payment_method,
                subtotal_amount,
                delivery_fee,
                total_amount,
                status,
                created_at
            FROM orders
            ORDER BY id DESC
            """
        )
        orders = cur.fetchall()

        for order in orders:
            item_cur.execute(
                """
                SELECT
                    oi.product_id,
                    oi.quantity,
                    p.name AS product_name,
                    p.price
                FROM order_items oi
                LEFT JOIN products p ON oi.product_id = p.id
                WHERE oi.order_id = %s
                ORDER BY oi.id ASC
                """,
                (order["id"],),
            )
            order["items"] = item_cur.fetchall()

        return jsonify(orders), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500

    finally:
        if item_cur:
            item_cur.close()
        if cur:
            cur.close()
        if conn:
            conn.close()


@app.route("/api/admin/orders/<int:order_id>/status", methods=["PUT"])
def admin_update_order_status(order_id):
    conn = None
    cur = None

    try:
        data = request.get_json() or {}
        status = str(data.get("status", "")).strip()

        if status not in ADMIN_STATUSES:
            return jsonify(
                {
                    "error": "Invalid status",
                    "allowed": ADMIN_STATUSES,
                }
            ), 400

        conn = get_connection()
        cur = conn.cursor(dictionary=True)

        cur.execute("SELECT id FROM orders WHERE id = %s", (order_id,))
        if not cur.fetchone():
            return jsonify({"error": "Order not found"}), 404

        cur.execute(
            "UPDATE orders SET status = %s WHERE id = %s",
            (status, order_id),
        )
        conn.commit()

        return jsonify(
            {
                "message": "Order status updated successfully",
                "order_id": order_id,
                "status": status,
            }
        ), 200

    except Exception as e:
        if conn:
            conn.rollback()
        return jsonify({"error": str(e)}), 500

    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


# ============================================================
# HEALTH CHECK
# ============================================================

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200


# ============================================================
# ADMIN HTML
# ============================================================

ADMIN_HTML = r"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Turant Bazaar - Admin</title>
    <style>
        * { box-sizing: border-box; }
        body {
            margin: 0;
            font-family: Arial, sans-serif;
            background: #f5f6f8;
            color: #222;
        }
        header {
            background: #111827;
            color: white;
            padding: 18px 24px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        header h1 { margin: 0; font-size: 22px; }
        header a { color: white; text-decoration: none; }
        .container { max-width: 1200px; margin: 24px auto; padding: 0 16px; }
        .tabs { display: flex; gap: 10px; margin-bottom: 18px; }
        .tabs button, button {
            border: 0;
            border-radius: 8px;
            padding: 10px 14px;
            cursor: pointer;
            background: #2563eb;
            color: white;
            font-weight: 600;
        }
        .tabs button.active { background: #111827; }
        .panel {
            background: white;
            border-radius: 12px;
            padding: 18px;
            margin-bottom: 18px;
            box-shadow: 0 2px 10px rgba(0,0,0,.06);
        }
        .form-grid {
            display: grid;
            grid-template-columns: repeat(2, minmax(0, 1fr));
            gap: 12px;
        }
        input, select {
            width: 100%;
            padding: 11px;
            border: 1px solid #d1d5db;
            border-radius: 8px;
            font-size: 14px;
        }
        .full { grid-column: 1 / -1; }
        .table-wrap { overflow-x: auto; }
        table { width: 100%; border-collapse: collapse; min-width: 760px; }
        th, td { padding: 11px; border-bottom: 1px solid #e5e7eb; text-align: left; vertical-align: top; }
        th { background: #f9fafb; }
        .danger { background: #dc2626; }
        .secondary { background: #6b7280; }
        .success { background: #16a34a; }
        .message { margin: 12px 0; padding: 10px; border-radius: 8px; display: none; }
        .message.show { display: block; background: #ecfdf5; color: #166534; }
        .error { background: #fef2f2 !important; color: #991b1b !important; }
        .items { font-size: 13px; line-height: 1.6; }
        .status-select { min-width: 160px; }
        .hidden { display: none; }
        @media (max-width: 700px) {
            .form-grid { grid-template-columns: 1fr; }
            .full { grid-column: auto; }
        }
    </style>
</head>
<body>
<header>
    <h1>Turant Bazaar Admin</h1>
    <a href="/">← Customer Site</a>
</header>

<div class="container">
    <div class="tabs">
        <button id="ordersTab" class="active" onclick="showTab('orders')">Orders</button>
        <button id="productsTab" onclick="showTab('products')">Products</button>
    </div>

    <div id="message" class="message"></div>

    <section id="ordersPanel" class="panel">
        <div style="display:flex;justify-content:space-between;align-items:center;gap:10px;">
            <h2>Orders</h2>
            <button class="secondary" onclick="loadOrders()">Refresh</button>
        </div>
        <div class="table-wrap">
            <table>
                <thead>
                    <tr>
                        <th>ID</th>
                        <th>Customer</th>
                        <th>Phone</th>
                        <th>Delivery</th>
                        <th>Items</th>
                        <th>Subtotal</th>
                        <th>Delivery Fee</th>
                        <th>Total</th>
                        <th>Payment</th>
                        <th>Status</th>
                        <th>Created</th>
                    </tr>
                </thead>
                <tbody id="ordersBody"></tbody>
            </table>
        </div>
    </section>

    <section id="productsPanel" class="panel hidden">
        <h2 id="productFormTitle">Add Product</h2>
        <input type="hidden" id="productId">
        <div class="form-grid">
            <input id="productName" placeholder="Product name">
            <input id="productPrice" type="number" step="0.01" min="0" placeholder="Price">
            <input id="productCategory" placeholder="Category (e.g. Daily, Sabzi)">
            <input id="vendorName" placeholder="Vendor name">
            <input id="vendorArea" placeholder="Vendor area">
            <input id="imageUrl" placeholder="Image URL">
        </div>
        <div style="margin-top:12px;display:flex;gap:10px;">
            <button onclick="saveProduct()">Save Product</button>
            <button class="secondary" onclick="resetProductForm()">Clear</button>
        </div>

        <hr style="margin:24px 0;border:0;border-top:1px solid #e5e7eb;">

        <div style="display:flex;justify-content:space-between;align-items:center;gap:10px;">
            <h2>Products</h2>
            <button class="secondary" onclick="loadProducts()">Refresh</button>
        </div>
        <div class="table-wrap">
            <table>
                <thead>
                    <tr>
                        <th>ID</th>
                        <th>Name</th>
                        <th>Price</th>
                        <th>Category</th>
                        <th>Vendor</th>
                        <th>Actions</th>
                    </tr>
                </thead>
                <tbody id="productsBody"></tbody>
            </table>
        </div>
    </section>
</div>

<script>
const statuses = [
    'placed',
    'confirmed',
    'preparing',
    'out_for_delivery',
    'delivered',
    'cancelled'
];

function showMessage(text, isError = false) {
    const box = document.getElementById('message');
    box.textContent = text;
    box.className = 'message show' + (isError ? ' error' : '');
    setTimeout(() => box.className = 'message', 3500);
}

function showTab(tab) {
    const ordersPanel = document.getElementById('ordersPanel');
    const productsPanel = document.getElementById('productsPanel');
    const ordersTab = document.getElementById('ordersTab');
    const productsTab = document.getElementById('productsTab');

    if (tab === 'orders') {
        ordersPanel.classList.remove('hidden');
        productsPanel.classList.add('hidden');
        ordersTab.classList.add('active');
        productsTab.classList.remove('active');
        loadOrders();
    } else {
        ordersPanel.classList.add('hidden');
        productsPanel.classList.remove('hidden');
        ordersTab.classList.remove('active');
        productsTab.classList.add('active');
        loadProducts();
    }
}

async function loadOrders() {
    try {
        const response = await fetch('/api/admin/orders');
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || 'Failed to load orders');

        const body = document.getElementById('ordersBody');
        body.innerHTML = '';

        if (!data.length) {
            body.innerHTML = '<tr><td colspan="11">No orders yet.</td></tr>';
            return;
        }

        data.forEach(order => {
            const itemHtml = (order.items || []).map(item =>
                `${escapeHtml(item.product_name || 'Deleted product')} × ${item.quantity}`
            ).join('<br>');

            const options = statuses.map(status =>
                `<option value="${status}" ${status === order.status ? 'selected' : ''}>${status}</option>`
            ).join('');

            const tr = document.createElement('tr');
            tr.innerHTML = `
                <td>#${order.id}</td>
                <td>${escapeHtml(order.customer_name || '')}</td>
                <td>${escapeHtml(order.customer_phone || '')}</td>
                <td class="items">
                    <strong>${escapeHtml(order.delivery_address || '-')}</strong><br>
                    ${escapeHtml(order.delivery_area || '')}
                    ${order.landmark ? '<br>Landmark: ' + escapeHtml(order.landmark) : ''}
                </td>
                <td class="items">${itemHtml || '-'}</td>
                <td>₹${Number(order.subtotal_amount || 0).toFixed(2)}</td>
                <td>₹${Number(order.delivery_fee || 0).toFixed(2)}</td>
                <td><strong>₹${Number(order.total_amount || 0).toFixed(2)}</strong></td>
                <td>${escapeHtml((order.payment_method || 'cod').toUpperCase())}</td>
                <td>
                    <select class="status-select" onchange="updateStatus(${order.id}, this.value)">
                        ${options}
                    </select>
                </td>
                <td>${escapeHtml(String(order.created_at || ''))}</td>
            `;
            body.appendChild(tr);
        });
    } catch (error) {
        showMessage(error.message, true);
    }
}

async function updateStatus(orderId, status) {
    try {
        const response = await fetch(`/api/admin/orders/${orderId}/status`, {
            method: 'PUT',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({status})
        });
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || 'Failed to update status');
        showMessage(`Order #${orderId} status changed to ${status}`);
    } catch (error) {
        showMessage(error.message, true);
        loadOrders();
    }
}

async function loadProducts() {
    try {
        const response = await fetch('/api/admin/products');
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || 'Failed to load products');

        const body = document.getElementById('productsBody');
        body.innerHTML = '';

        if (!data.length) {
            body.innerHTML = '<tr><td colspan="6">No products yet.</td></tr>';
            return;
        }

        data.forEach(product => {
            const tr = document.createElement('tr');
            tr.innerHTML = `
                <td>${product.id}</td>
                <td>${escapeHtml(product.name || '')}</td>
                <td>₹${Number(product.price || 0).toFixed(2)}</td>
                <td>${escapeHtml(product.category || '-')}</td>
                <td>${escapeHtml(product.vendor_name || '-')}</td>
                <td>
                    <button class="secondary" onclick='editProduct(${JSON.stringify(product)})'>Edit</button>
                    <button class="danger" onclick="deleteProduct(${product.id})">Delete</button>
                </td>
            `;
            body.appendChild(tr);
        });
    } catch (error) {
        showMessage(error.message, true);
    }
}

function editProduct(product) {
    document.getElementById('productFormTitle').textContent = 'Edit Product';
    document.getElementById('productId').value = product.id || '';
    document.getElementById('productName').value = product.name || '';
    document.getElementById('productPrice').value = product.price || '';
    document.getElementById('productCategory').value = product.category || '';
    document.getElementById('vendorName').value = product.vendor_name || '';
    document.getElementById('vendorArea').value = product.vendor_area || '';
    document.getElementById('imageUrl').value = product.image_url || '';
    window.scrollTo({top: 0, behavior: 'smooth'});
}

function resetProductForm() {
    document.getElementById('productFormTitle').textContent = 'Add Product';
    document.getElementById('productId').value = '';
    document.getElementById('productName').value = '';
    document.getElementById('productPrice').value = '';
    document.getElementById('productCategory').value = '';
    document.getElementById('vendorName').value = '';
    document.getElementById('vendorArea').value = '';
    document.getElementById('imageUrl').value = '';
}

async function saveProduct() {
    const id = document.getElementById('productId').value;
    const payload = {
        name: document.getElementById('productName').value.trim(),
        price: document.getElementById('productPrice').value,
        category: document.getElementById('productCategory').value.trim(),
        vendor_name: document.getElementById('vendorName').value.trim(),
        vendor_area: document.getElementById('vendorArea').value.trim(),
        image_url: document.getElementById('imageUrl').value.trim()
    };

    if (!payload.name || payload.price === '') {
        showMessage('Product name and price are required.', true);
        return;
    }

    try {
        const url = id ? `/api/admin/products/${id}` : '/api/admin/products';
        const method = id ? 'PUT' : 'POST';
        const response = await fetch(url, {
            method,
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(payload)
        });
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || 'Failed to save product');

        showMessage(id ? 'Product updated successfully.' : 'Product added successfully.');
        resetProductForm();
        loadProducts();
    } catch (error) {
        showMessage(error.message, true);
    }
}

async function deleteProduct(id) {
    if (!confirm(`Delete product #${id}?`)) return;

    try {
        const response = await fetch(`/api/admin/products/${id}`, {method: 'DELETE'});
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || 'Failed to delete product');
        showMessage('Product deleted successfully.');
        loadProducts();
    } catch (error) {
        showMessage(error.message, true);
    }
}

function escapeHtml(value) {
    return String(value)
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;')
        .replaceAll("'", '&#039;');
}

loadOrders();
</script>
</body>
</html>
"""


# ============================================================
# START APPLICATION
# ============================================================

if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=8080,
        debug=True,
    )
