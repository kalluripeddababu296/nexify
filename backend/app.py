"""
Nexify backend — Flask + Supabase (Postgres)
=============================================
Public endpoints (used by index.html, the customer storefront):
    GET  /api/products        -> live product list (price/stock/discount)
    GET  /api/settings        -> free-delivery threshold, etc.
    GET  /api/banners         -> homepage banner images (for the slider)
    POST /api/orders          -> customer places an order (Buy Now / Checkout)
    GET  /api/orders/<order_ref> -> look up a single order's status (tracking)
    GET  /api/products/<id>/reviews          -> real customer reviews for a product
    GET  /api/orders/<order_ref>/reviewable  -> which items on that order can still be reviewed
    POST /api/reviews                        -> customer submits a review (only for items they actually ordered)
    POST /api/support                        -> customer care message, emailed straight to the store owner

Admin endpoints (used by admin.html, the admin dashboard):
    POST   /api/admin/login                    -> returns a JWT session token
    GET    /api/admin/orders                   -> list all orders
    PUT    /api/admin/orders/<order_ref>/status -> update order status
    PUT    /api/admin/orders/<order_ref>/payment-status -> mark a UPI payment verified/paid
    POST   /api/admin/products                 -> add a product
    PUT    /api/admin/products/<id>             -> edit a product (incl. stock, image)
    DELETE /api/admin/products/<id>             -> remove a product
    POST   /api/admin/upload-image              -> upload an image (product or banner), returns its URL
    POST   /api/admin/banners                   -> add a homepage banner (pass the uploaded image_url)
    DELETE /api/admin/banners/<id>               -> remove a homepage banner
    PUT    /api/admin/settings                  -> update site settings (free delivery threshold)
    GET    /api/products/<id>/gallery           -> extra "more photos" for a product (public)
    POST   /api/admin/products/<id>/gallery     -> add an extra photo to a product's gallery (stored in MongoDB)
    DELETE /api/admin/products/<id>/gallery     -> remove an extra photo from a product's gallery

Setup:
    1. Create a free project at https://supabase.com
    2. Open SQL Editor -> run schema.sql (creates tables + seed data)
    3. Project Settings -> API -> copy "Project URL" and the "service_role" key
    4. Copy .env.example to .env and fill in the values
    5. pip install -r requirements.txt
    6. python app.py   (runs on http://localhost:5000)
"""

import os
import time
import uuid
from functools import wraps

import jwt
from flask import Flask, request, jsonify
from flask_cors import CORS
from dotenv import load_dotenv
from supabase import create_client, Client
from pymongo import MongoClient

load_dotenv()

SUPABASE_URL = (os.environ.get("SUPABASE_URL") or "").strip()
# Guard against a common misconfiguration: SUPABASE_URL should be just the
# project URL (e.g. https://xxxx.supabase.co) — the supabase-py client adds
# /rest/v1/ itself. If someone pastes the full REST URL into .env, strip it
# back down instead of silently sending every request to a broken, doubled-up path.
if "/rest/v1" in SUPABASE_URL:
    SUPABASE_URL = SUPABASE_URL.split("/rest/v1")[0]
SUPABASE_URL = SUPABASE_URL.rstrip("/")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")
JWT_SECRET = os.environ.get("JWT_SECRET", "change-me-please")
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")
# Where "Customer Care" messages from the storefront get emailed to. Can be
# overridden with a CUSTOMER_CARE_EMAIL env var, but defaults to the address
# you gave us so it works out of the box.
CUSTOMER_CARE_EMAIL = os.environ.get("CUSTOMER_CARE_EMAIL", "kpbabuexams@gmail.com")

if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
    print("WARNING: SUPABASE_URL / SUPABASE_SERVICE_KEY not set. "
          "Set them in a .env file (see .env.example) before running for real.")

supabase: Client = create_client(SUPABASE_URL or "", SUPABASE_SERVICE_KEY or "")

app = Flask(__name__)

CORS(
    app,
    origins=["https://nexifyonline.netlify.app"],
    methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
)
# --------------------------------------------------------------------------
# MongoDB — used ONLY for each product's extra "gallery" photos (the
# Amazon-style row of extra thumbnails on the product page). Everything else
# stays in Supabase; this is a second, narrowly-scoped database on purpose.
# One document per product: { _id: <product_id>, images: [url, url, ...] }
# --------------------------------------------------------------------------
MONGODB_URI = (os.environ.get("MONGODB_URI") or "").strip()
MONGODB_DB_NAME = (os.environ.get("MONGODB_DB") or "nexify").strip() or "nexify"
print("Mongo URI:", MONGODB_URI)
print("Mongo DB:", MONGODB_DB_NAME)

mongo_client = None
gallery_col = None
if MONGODB_URI:
    try:
        mongo_client = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=5000)
        mongo_client.admin.command("ping")  # fail fast if the URI/credentials are wrong
        gallery_col = mongo_client[MONGODB_DB_NAME]["product_gallery"]
        print("Connected to MongoDB — product gallery images enabled.")
    except Exception as e:
        print(f"WARNING: could not connect to MongoDB ({e}). "
              f"Gallery images will be unavailable until MONGODB_URI is fixed.")
        mongo_client = None
        gallery_col = None
else:
    print("NOTE: MONGODB_URI not set — product gallery images (extra photos) are disabled. "
          "Everything else works normally.")


def get_gallery_images(product_id):
    """Extra photos for one product, most-recently-added last."""
    if gallery_col is None:
        return []
    doc = gallery_col.find_one({"_id": product_id})
    return (doc or {}).get("images", [])


def get_all_galleries():
    """{ product_id: [image_url, ...] } for every product in one query, so the
    products list endpoint doesn't need one MongoDB round-trip per product."""
    if gallery_col is None:
        return {}
    return {doc["_id"]: doc.get("images", []) for doc in gallery_col.find({})}

# --------------------------------------------------------------------------
# Product image storage — uses a public Supabase Storage bucket so the admin
# dashboard can upload a photo and get back a URL to save on the product.
# --------------------------------------------------------------------------
PRODUCT_IMAGE_BUCKET = "product-images"
ALLOWED_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
MAX_IMAGE_BYTES = 5 * 1024 * 1024  # 5 MB


def ensure_image_bucket():
    """Create the product-images bucket (public) if it doesn't exist yet.
    Safe to call on every startup — no-ops if the bucket is already there."""
    try:
        buckets = supabase.storage.list_buckets()
        names = {getattr(b, "name", None) or (b.get("name") if isinstance(b, dict) else None) for b in buckets}
        if PRODUCT_IMAGE_BUCKET not in names:
            supabase.storage.create_bucket(PRODUCT_IMAGE_BUCKET, options={"public": True})
            print(f"Created public storage bucket '{PRODUCT_IMAGE_BUCKET}'.")
    except Exception as e:
        print(f"NOTE: could not verify/create the '{PRODUCT_IMAGE_BUCKET}' storage bucket automatically "
              f"({e}). Create a PUBLIC bucket with this exact name in Supabase → Storage.")


if SUPABASE_URL and SUPABASE_SERVICE_KEY:
    ensure_image_bucket()


def get_public_image_url(path):
    result = supabase.storage.from_(PRODUCT_IMAGE_BUCKET).get_public_url(path)
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        return result.get("publicUrl") or result.get("publicURL") or result.get("data", {}).get("publicUrl")
    return str(result)


# --------------------------------------------------------------------------
# Admin auth helpers — single fixed admin account, JWT session token
# --------------------------------------------------------------------------

def make_token():
    payload = {"sub": ADMIN_USERNAME, "exp": int(time.time()) + 8 * 60 * 60}  # 8 hour session
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")


def require_admin(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return jsonify({"error": "missing token"}), 401
        token = auth.split(" ", 1)[1]
        try:
            jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        except jwt.ExpiredSignatureError:
            return jsonify({"error": "session expired, please log in again"}), 401
        except jwt.InvalidTokenError:
            return jsonify({"error": "invalid token"}), 401
        return fn(*args, **kwargs)
    return wrapper


# --------------------------------------------------------------------------
# Root / health check — visiting the bare backend URL in a browser used to
# show a confusing generic "Not Found" page. This makes it obvious the API
# is up and where to look next.
# --------------------------------------------------------------------------

@app.get("/")
def root():
    return jsonify({
        "service": "Nexify backend",
        "status": "ok",
        "try": ["/api/products", "/api/settings"]
    })


@app.get("/api/health")
def health():
    return jsonify({"status": "ok"})


@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": "No such endpoint. See / for available routes."}), 404


@app.errorhandler(500)
def server_error(e):
    return jsonify({"error": "Internal server error. Check SUPABASE_URL / SUPABASE_SERVICE_KEY in .env."}), 500


# --------------------------------------------------------------------------
# Public routes
# --------------------------------------------------------------------------

@app.get("/api/products")
def get_products():
    res = supabase.table("products").select("*").order("created_at").execute()
    products = res.data
    galleries = get_all_galleries()
    for p in products:
        p["gallery_images"] = galleries.get(p["id"], [])
    return jsonify(products)


@app.get("/api/products/<product_id>/gallery")
def get_product_gallery(product_id):
    """Public — the extra 'more photos' shown on the product detail view."""
    return jsonify({"images": get_gallery_images(product_id)})


@app.get("/api/settings")
def get_settings():
    res = supabase.table("settings").select("*").execute()
    return jsonify({row["key"]: row["value"] for row in res.data})


@app.get("/api/banners")
def get_banners():
    """Public — the homepage banner slider in index.html reads this directly,
    no auth needed. Ordered so the admin's arrangement is stable."""
    res = supabase.table("banners").select("*").order("sort_order").order("created_at").execute()
    return jsonify(res.data)


@app.post("/api/orders")
def create_order():
    body = request.get_json(force=True) or {}
    customer = body.get("customer", {})
    items = body.get("items", [])
    total = body.get("total", 0)
    order_type = body.get("type", "cart_checkout")
    payment_method = body.get("payment_method", "cod")
    utr_number = (body.get("utr_number") or "").strip()

    if not customer.get("name") or not customer.get("mobile") or not customer.get("address"):
        return jsonify({"error": "missing customer details"}), 400
    if not items:
        return jsonify({"error": "no items in order"}), 400
    if payment_method not in ("cod", "upi"):
        return jsonify({"error": "invalid payment_method"}), 400
    if payment_method == "upi" and not utr_number:
        return jsonify({"error": "utr_number is required for UPI orders"}), 400

    order_ref = "NEX-" + uuid.uuid4().hex[:8].upper()
    row = {
        "order_ref": order_ref,
        "customer_name": customer["name"],
        "customer_mobile": customer["mobile"],
        "customer_address": customer["address"],
        "items": items,
        "total": total,
        "type": order_type,
        "status": "pending",
        "payment_method": payment_method,
        "payment_status": "pending_verification" if payment_method == "upi" else "not_required",
        "utr_number": utr_number or None,
    }
    supabase.table("orders").insert(row).execute()
    notify_admin_new_order(row)
    return jsonify({"orderId": order_ref}), 201


@app.get("/api/orders/<order_ref>")
def track_order(order_ref):
    """Public, no-auth order lookup used by index.html's "My Orders" modal so
    a customer can see the current status (e.g. once the admin marks it
    Delivered, or their UPI payment as verified) without needing an account
    or the admin's JWT token."""
    res = (
        supabase.table("orders")
        .select("order_ref,status,total,items,placed_at,payment_method,payment_status")
        .eq("order_ref", order_ref)
        .execute()
    )
    if not res.data:
        return jsonify({"error": "order not found"}), 404
    return jsonify(res.data[0])


@app.get("/api/products/<product_id>/reviews")
def get_product_reviews(product_id):
    """Public — real customer reviews for a single product, shown on the
    storefront. These replace the old admin-set 'reviews count' field; they
    can only be created by a customer who actually ordered the item (see
    POST /api/reviews below)."""
    res = (
        supabase.table("reviews")
        .select("customer_name,rating,comment,created_at")
        .eq("product_id", product_id)
        .order("created_at", desc=True)
        .execute()
    )
    return jsonify(res.data)


@app.get("/api/orders/<order_ref>/reviewable")
def get_reviewable_items(order_ref):
    """Public — given an order_ref + the mobile number that placed it, returns
    which product ids on that order can still be reviewed (i.e. haven't been
    reviewed yet). Lets the frontend show a 'Rate this item' button only where
    it's actually allowed."""
    mobile = (request.args.get("mobile") or "").strip()
    order_res = supabase.table("orders").select("*").eq("order_ref", order_ref).execute()
    if not order_res.data:
        return jsonify({"error": "order not found"}), 404
    order = order_res.data[0]
    if not mobile or order["customer_mobile"] != mobile:
        return jsonify({"error": "mobile number does not match this order"}), 403
    if order["status"] == "cancelled":
        return jsonify({"reviewable": [], "status": order["status"]})

    reviewed_res = supabase.table("reviews").select("product_id").eq("order_ref", order_ref).execute()
    reviewed_ids = {r["product_id"] for r in reviewed_res.data}
    reviewable = [i["id"] for i in order["items"] if i.get("id") and i["id"] not in reviewed_ids]
    return jsonify({"reviewable": reviewable, "status": order["status"]})


@app.post("/api/reviews")
def submit_review():
    """Public — a customer leaves a review on a product, but ONLY if it was
    genuinely part of an order placed with the mobile number they provide.
    This is what replaces the admin manually typing in a fake reviews count:
    real reviews now come from real orders, and the product's displayed
    rating/review count is recalculated from them automatically."""
    body = request.get_json(force=True) or {}
    order_ref = (body.get("order_ref") or "").strip()
    mobile = (body.get("mobile") or "").strip()
    product_id = (body.get("product_id") or "").strip()
    comment = (body.get("comment") or "").strip()

    try:
        rating = int(body.get("rating"))
    except (TypeError, ValueError):
        return jsonify({"error": "a rating from 1 to 5 is required"}), 400
    if not (1 <= rating <= 5):
        return jsonify({"error": "rating must be between 1 and 5"}), 400
    if not order_ref or not mobile or not product_id:
        return jsonify({"error": "order_ref, mobile and product_id are required"}), 400

    order_res = supabase.table("orders").select("*").eq("order_ref", order_ref).execute()
    if not order_res.data:
        return jsonify({"error": "order not found"}), 404
    order = order_res.data[0]

    if order["customer_mobile"] != mobile:
        return jsonify({"error": "mobile number does not match this order"}), 403
    if order["status"] == "cancelled":
        return jsonify({"error": "cancelled orders can't be reviewed"}), 400
    if not any(i.get("id") == product_id for i in order["items"]):
        return jsonify({"error": "this item wasn't part of that order"}), 400

    existing = (
        supabase.table("reviews")
        .select("id")
        .eq("order_ref", order_ref)
        .eq("product_id", product_id)
        .execute()
    )
    if existing.data:
        return jsonify({"error": "you've already reviewed this item for this order"}), 409

    row = {
        "product_id": product_id,
        "order_ref": order_ref,
        "customer_name": order["customer_name"],
        "customer_mobile": mobile,
        "rating": rating,
        "comment": comment or None,
    }
    supabase.table("reviews").insert(row).execute()

    # Keep the product card's star rating / review count in sync with real reviews.
    all_reviews = supabase.table("reviews").select("rating").eq("product_id", product_id).execute()
    ratings = [r["rating"] for r in all_reviews.data]
    if ratings:
        avg_rating = round(sum(ratings) / len(ratings), 1)
        supabase.table("products").update({
            "rating": avg_rating,
            "reviews": str(len(ratings)),
        }).eq("id", product_id).execute()

    return jsonify({"ok": True}), 201


def send_email(subject, body_text, to_email):
    """Shared SMTP sender used by both the new-order alert and Customer Care
    messages. Returns (ok, error_message). Skips silently (ok=False, a
    friendly reason) if SMTP_HOST isn't configured yet."""
    smtp_host = os.environ.get("SMTP_HOST")
    if not smtp_host:
        return False, "email isn't configured on the server yet (SMTP_HOST missing in .env)"
    import smtplib
    from email.mime.text import MIMEText

    msg = MIMEText(body_text)
    msg["Subject"] = subject
    msg["From"] = os.environ.get("SMTP_FROM", "noreply@nexify.app")
    msg["To"] = to_email

    try:
        with smtplib.SMTP(smtp_host, int(os.environ.get("SMTP_PORT", 587))) as server:
            server.starttls()
            server.login(os.environ.get("SMTP_USER", ""), os.environ.get("SMTP_PASS", ""))
            server.send_message(msg)
        return True, None
    except Exception as e:
        return False, str(e)


def notify_admin_new_order(row):
    """Optional email alert to the admin. Skips silently if SMTP_HOST isn't set."""
    items_lines = "\n".join(
        f"  - {i.get('name')} x{i.get('qty')} = ₹{i.get('price', 0) * i.get('qty', 1)}"
        for i in row["items"]
    )
    payment_line = (
        f"Payment: UPI — UTR {row.get('utr_number')} (verify before shipping)"
        if row.get("payment_method") == "upi"
        else "Payment: Cash on Delivery"
    )
    body_text = (
        f"New order {row['order_ref']}\n"
        f"Customer: {row['customer_name']} ({row['customer_mobile']})\n"
        f"Address: {row['customer_address']}\n"
        f"Total: ₹{row['total']}\n"
        f"{payment_line}\n"
        f"Items:\n{items_lines}"
    )
    ok, err = send_email(f"New Nexify Order — {row['order_ref']}", body_text, os.environ.get("ADMIN_EMAIL", ""))
    if not ok:
        print("Order email notification failed:", err)


@app.route("/api/support", methods=["POST", "OPTIONS"])
def submit_support_message():
    try:
        body = request.get_json(force=True) or {}
        name = (body.get("name") or "").strip() or "A customer"
        contact = (body.get("contact") or "").strip()
        message = (body.get("message") or "").strip()

        if not message:
            return jsonify({"error": "message is required"}), 400

        body_text = (
            f"New Customer Care message\n\n"
            f"From: {name}\n"
            f"Contact: {contact or 'not provided'}\n\n"
            f"Message:\n{message}"
        )

        ok, err = send_email(
            f"Nexify Customer Care — message from {name}",
            body_text,
            CUSTOMER_CARE_EMAIL
        )

        if not ok:
            print("SMTP ERROR:", err)
            return jsonify({"error": err}), 500

        return jsonify({"ok": True}), 201

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

# --------------------------------------------------------------------------
# Admin: auth
# --------------------------------------------------------------------------

@app.post("/api/admin/login")
def admin_login():
    body = request.get_json(force=True) or {}
    if body.get("username") == ADMIN_USERNAME and body.get("password") == ADMIN_PASSWORD:
        return jsonify({"token": make_token()})
    return jsonify({"error": "invalid username or password"}), 401


# --------------------------------------------------------------------------
# Admin: orders
# --------------------------------------------------------------------------

@app.get("/api/admin/orders")
@require_admin
def admin_list_orders():
    res = supabase.table("orders").select("*").order("placed_at", desc=True).execute()
    return jsonify(res.data)


@app.put("/api/admin/orders/<order_ref>/status")
@require_admin
def admin_update_order_status(order_ref):
    body = request.get_json(force=True) or {}
    status = body.get("status")
    if status not in ("pending", "confirmed", "delivered", "cancelled"):
        return jsonify({"error": "invalid status"}), 400
    supabase.table("orders").update({"status": status}).eq("order_ref", order_ref).execute()
    return jsonify({"ok": True})


@app.put("/api/admin/orders/<order_ref>/payment-status")
@require_admin
def admin_update_payment_status(order_ref):
    """The admin checks the UTR against their bank/UPI app manually, then
    marks it here — there's no automatic verification since this isn't going
    through a payment gateway."""
    body = request.get_json(force=True) or {}
    payment_status = body.get("payment_status")
    if payment_status not in ("not_required", "pending_verification", "paid"):
        return jsonify({"error": "invalid payment_status"}), 400
    supabase.table("orders").update({"payment_status": payment_status}).eq("order_ref", order_ref).execute()
    return jsonify({"ok": True})


# --------------------------------------------------------------------------
# Admin: products (stock, price, discount, add/remove)
# --------------------------------------------------------------------------

@app.post("/api/admin/products")
@require_admin
def admin_create_product():
    body = request.get_json(force=True) or {}

    name = (body.get("name") or "").strip()
    if not name:
        return jsonify({"error": "product name is required"}), 400
    try:
        price = int(body.get("price"))
    except (TypeError, ValueError):
        return jsonify({"error": "a valid price is required"}), 400
    if price < 0:
        return jsonify({"error": "price cannot be negative"}), 400

    stock = body.get("stock", "in")
    if stock not in ("in", "low", "out"):
        return jsonify({"error": "invalid stock value"}), 400

    row = {
        "id": body.get("id") or ("p" + uuid.uuid4().hex[:8]),
        "name": name,
        "emoji": body.get("emoji") or "📦",
        "tint": body.get("tint") or "#FFF8F1",
        "image_url": body.get("image_url") or None,
        "price": price,
        "old_price": int(body["old_price"]) if body.get("old_price") not in (None, "") else None,
        "discount": int(body.get("discount") or 0),
        "rating": float(body.get("rating") or 4.5),
        "reviews": str(body.get("reviews") or "0"),
        "stock": stock,
        "category": body.get("category") or "General",
        "express_delivery": bool(body.get("express_delivery", False)),
    }
    supabase.table("products").insert(row).execute()
    return jsonify({"ok": True, "id": row["id"]}), 201


@app.put("/api/admin/products/<product_id>")
@require_admin
def admin_update_product(product_id):
    body = request.get_json(force=True) or {}
    body.pop("id", None)  # never allow overwriting the primary key

    if "price" in body:
        try:
            body["price"] = int(body["price"])
        except (TypeError, ValueError):
            return jsonify({"error": "invalid price"}), 400
    if "old_price" in body:
        body["old_price"] = int(body["old_price"]) if body["old_price"] not in (None, "") else None
    if "discount" in body:
        body["discount"] = int(body["discount"] or 0)
    if "rating" in body:
        body["rating"] = float(body["rating"] or 0)
    if "stock" in body and body["stock"] not in ("in", "low", "out"):
        return jsonify({"error": "invalid stock value"}), 400
    if "express_delivery" in body:
        body["express_delivery"] = bool(body["express_delivery"])

    supabase.table("products").update(body).eq("id", product_id).execute()
    return jsonify({"ok": True})


@app.delete("/api/admin/products/<product_id>")
@require_admin
def admin_delete_product(product_id):
    supabase.table("products").delete().eq("id", product_id).execute()
    if gallery_col is not None:
        gallery_col.delete_one({"_id": product_id})
    return jsonify({"ok": True})


# --------------------------------------------------------------------------
# Admin: product gallery images (extra photos, stored in MongoDB)
# --------------------------------------------------------------------------
# Workflow mirrors the main product photo: the admin uploads the file via the
# existing /api/admin/upload-image endpoint (which puts it in Supabase
# Storage same as before) and then POSTs the returned URL here to add it to
# that product's gallery array in MongoDB.

@app.post("/api/admin/products/<product_id>/gallery")
@require_admin
def admin_add_gallery_image(product_id):
    if gallery_col is None:
        return jsonify({"error": "MongoDB is not configured. Set MONGODB_URI in .env to enable gallery images."}), 500

    body = request.get_json(force=True) or {}
    image_url = (body.get("image_url") or "").strip()
    if not image_url:
        return jsonify({"error": "image_url is required"}), 400

    # Make sure the product actually exists before attaching photos to it.
    exists = supabase.table("products").select("id").eq("id", product_id).execute()
    if not exists.data:
        return jsonify({"error": "product not found"}), 404

    gallery_col.update_one(
        {"_id": product_id},
        {"$addToSet": {"images": image_url}},
        upsert=True,
    )
    return jsonify({"ok": True, "images": get_gallery_images(product_id)}), 201


@app.delete("/api/admin/products/<product_id>/gallery")
@require_admin
def admin_remove_gallery_image(product_id):
    if gallery_col is None:
        return jsonify({"error": "MongoDB is not configured. Set MONGODB_URI in .env to enable gallery images."}), 500

    body = request.get_json(force=True) or {}
    image_url = (body.get("image_url") or "").strip()
    if not image_url:
        return jsonify({"error": "image_url is required"}), 400

    gallery_col.update_one({"_id": product_id}, {"$pull": {"images": image_url}})
    return jsonify({"ok": True, "images": get_gallery_images(product_id)})


@app.post("/api/admin/upload-image")
@require_admin
def admin_upload_image():
    """Accepts multipart/form-data with a 'file' field, uploads it to the
    public product-images bucket, and returns { url } to save on a product."""
    if "file" not in request.files:
        return jsonify({"error": "no file uploaded (expected form field 'file')"}), 400
    file = request.files["file"]
    if not file.filename:
        return jsonify({"error": "empty filename"}), 400

    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in ALLOWED_IMAGE_EXT:
        return jsonify({"error": f"unsupported image type '{ext}'. Use jpg, png, webp or gif."}), 400

    data = file.read()
    if len(data) > MAX_IMAGE_BYTES:
        return jsonify({"error": "image too large (max 5MB)"}), 400

    path = f"{uuid.uuid4().hex}{ext}"
    content_type = file.mimetype or "image/jpeg"
    try:
        supabase.storage.from_(PRODUCT_IMAGE_BUCKET).upload(
            path, data, {"content-type": content_type}
        )
        url = get_public_image_url(path)
        return jsonify({"ok": True, "url": url}), 201
    except Exception as e:
        return jsonify({"error": f"upload failed: {e}"}), 500


# --------------------------------------------------------------------------
# Admin: homepage banners
# --------------------------------------------------------------------------
# The admin uploads an image via /api/admin/upload-image first (same endpoint
# products use for photos), then POSTs the returned URL here to add it as a
# banner. index.html's slider reads them back via the public GET /api/banners.

@app.post("/api/admin/banners")
@require_admin
def admin_create_banner():
    body = request.get_json(force=True) or {}
    image_url = (body.get("image_url") or "").strip()
    if not image_url:
        return jsonify({"error": "image_url is required"}), 400

    row = {
        "id": "b" + uuid.uuid4().hex[:8],
        "image_url": image_url,
        "sort_order": int(body.get("sort_order") or 0),
    }
    supabase.table("banners").insert(row).execute()
    return jsonify({"ok": True, "id": row["id"]}), 201


@app.delete("/api/admin/banners/<banner_id>")
@require_admin
def admin_delete_banner(banner_id):
    supabase.table("banners").delete().eq("id", banner_id).execute()
    return jsonify({"ok": True})


# --------------------------------------------------------------------------
# Admin: settings (free delivery threshold, etc.)
# --------------------------------------------------------------------------

@app.put("/api/admin/settings")
@require_admin
def admin_update_settings():
    body = request.get_json(force=True) or {}  # { key: value, ... }
    for key, value in body.items():
        supabase.table("settings").upsert({"key": key, "value": str(value)}).execute()
    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(debug=False, port=5000)
