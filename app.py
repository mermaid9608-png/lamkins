"""
รายรับ-รายจ่าย (Income/Expense Tracker)
Flask backend. Multi-user (open registration), each user's data is private.

Local/LAN mode (default): SQLite file + local uploads/ folder + local config files.
Cloud mode (set DATABASE_URL): Postgres (e.g. Supabase) + Supabase Storage for slips +
config stored in the database itself - the local filesystem on most free hosts is wiped
on every restart/redeploy, so nothing that needs to survive can live on disk there.

Run locally:
    pip install -r requirements.txt
    python app.py
Then open http://127.0.0.1:5000

Cloud deployment env vars:
    DATABASE_URL         - Postgres connection string (e.g. from Supabase)
    SUPABASE_URL          - e.g. https://xxxx.supabase.co
    SUPABASE_SERVICE_KEY  - Supabase service_role key (Storage uploads)
    FLASK_SECRET_KEY      - random string; MUST be set on a cloud host or sessions
                            reset on every restart
"""
import os
import secrets
import socket
import time
from datetime import datetime, timedelta

from flask import Flask, g, jsonify, request, send_from_directory, render_template, session, redirect, url_for
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from werkzeug.security import generate_password_hash, check_password_hash

import db as db_module
import gsheet_sync
import storage

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SECRET_KEY_PATH = os.path.join(BASE_DIR, "flask_secret.key")
MAX_CONTENT_LENGTH = 10 * 1024 * 1024  # 10 MB

MIN_PASSWORD_LENGTH = 4
MIN_USERNAME_LENGTH = 2
LOGIN_MAX_ATTEMPTS = 5
LOGIN_LOCKOUT_SECONDS = 60

# Endpoints reachable without being logged in
PUBLIC_ENDPOINTS = {"login_page", "register_page", "auth_login", "auth_register", "static"}


def load_or_create_secret_key():
    env_key = os.environ.get("FLASK_SECRET_KEY")
    if env_key:
        return env_key
    if os.path.exists(SECRET_KEY_PATH):
        with open(SECRET_KEY_PATH, "r", encoding="utf-8") as f:
            return f.read().strip()
    key = secrets.token_hex(32)
    with open(SECRET_KEY_PATH, "w", encoding="utf-8") as f:
        f.write(key)
    return key


def get_lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH
app.secret_key = load_or_create_secret_key()
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

DEFAULT_CATEGORIES = [
    ("เงินเดือน", "income"),
    ("โบนัส", "income"),
    ("ขายของ", "income"),
    ("รายได้อื่นๆ", "income"),
    ("อาหาร", "expense"),
    ("เดินทาง", "expense"),
    ("ที่พัก/ค่าเช่า", "expense"),
    ("ช้อปปิ้ง", "expense"),
    ("บิล/สาธารณูปโภค", "expense"),
    ("สุขภาพ", "expense"),
    ("บันเทิง", "expense"),
    ("การศึกษา", "expense"),
    ("อื่นๆ", "expense"),
]


def get_db():
    if "db" not in g:
        g.db = db_module.engine.connect()
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def seed_default_categories(db, user_id):
    for name, cat_type in DEFAULT_CATEGORIES:
        db.execute(
            text("INSERT INTO categories (user_id, name, type) VALUES (:user_id, :name, :type)"),
            {"user_id": user_id, "name": name, "type": cat_type},
        )


def current_user_id():
    return session["user_id"]


def transaction_to_dict(row):
    d = db_module.row_to_dict(row)
    if d is not None:
        d["slip_url"] = storage.slip_url(d["slip_filename"]) if d.get("slip_filename") else None
    return d


# ---------- Auth ----------

# In-memory attempt tracker (login + register, same abuse pattern): ip -> (fail_count, window_start_time).
# Resets on server restart, and isn't shared across worker processes - fine for a personal
# app, it just slows down brute force rather than fully preventing it.
_login_attempts = {}


def rate_limited(addr):
    count, window_start = _login_attempts.get(addr, (0, 0))
    if time.time() - window_start > LOGIN_LOCKOUT_SECONDS:
        return False
    return count >= LOGIN_MAX_ATTEMPTS


def record_failed_login(addr):
    count, window_start = _login_attempts.get(addr, (0, 0))
    now = time.time()
    if now - window_start > LOGIN_LOCKOUT_SECONDS:
        _login_attempts[addr] = (1, now)
    else:
        _login_attempts[addr] = (count + 1, window_start)


def clear_failed_logins(addr):
    _login_attempts.pop(addr, None)


@app.before_request
def require_login():
    if request.endpoint in PUBLIC_ENDPOINTS or request.endpoint is None:
        return None

    if not session.get("user_id"):
        if request.path.startswith("/api/"):
            return jsonify({"error": "กรุณาเข้าสู่ระบบก่อน (please log in)"}), 401
        return redirect(url_for("login_page"))
    return None


@app.route("/register")
def register_page():
    if session.get("user_id"):
        return redirect(url_for("index"))
    return render_template("register.html")


@app.route("/api/auth/register", methods=["POST"])
def auth_register():
    addr = request.remote_addr
    if rate_limited(addr):
        return jsonify({"error": "ลองผิดหลายครั้งเกินไป กรุณารอสักครู่แล้วลองใหม่"}), 429
    # Registration is open (no invite code) - throttle attempts per IP regardless of
    # outcome so a script can't mass-create accounts.
    record_failed_login(addr)

    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""

    if len(username) < MIN_USERNAME_LENGTH:
        return jsonify({"error": f"ชื่อผู้ใช้ต้องมีอย่างน้อย {MIN_USERNAME_LENGTH} ตัวอักษร"}), 400
    if len(password) < MIN_PASSWORD_LENGTH:
        return jsonify({"error": f"รหัสผ่านต้องมีอย่างน้อย {MIN_PASSWORD_LENGTH} ตัวอักษร"}), 400

    db = get_db()
    try:
        result = db.execute(
            text(
                "INSERT INTO users (username, password_hash, created_at) "
                "VALUES (:username, :password_hash, :created_at) RETURNING id"
            ),
            {"username": username, "password_hash": generate_password_hash(password),
             "created_at": datetime.utcnow().isoformat()},
        )
        new_user_id = result.scalar()
        db.commit()
    except IntegrityError:
        db.rollback()
        return jsonify({"error": "มีชื่อผู้ใช้นี้อยู่แล้ว"}), 409

    # Inherit pre-existing (pre-multi-user) unclaimed data if any exists - only true for a
    # local install upgraded from before multi-user support. A fresh database (e.g. a new
    # cloud deployment) has none, so every registrant there gets fresh default categories.
    unclaimed_categories = db.execute(text("SELECT COUNT(*) FROM categories WHERE user_id IS NULL")).scalar()
    if unclaimed_categories > 0:
        db.execute(text("UPDATE categories SET user_id = :uid WHERE user_id IS NULL"), {"uid": new_user_id})
        db.execute(text("UPDATE transactions SET user_id = :uid WHERE user_id IS NULL"), {"uid": new_user_id})
    else:
        seed_default_categories(db, new_user_id)
    db.commit()

    clear_failed_logins(addr)
    session.permanent = True
    session["user_id"] = new_user_id
    session["username"] = username
    return jsonify({"ok": True})


@app.route("/login")
def login_page():
    if session.get("user_id"):
        return redirect(url_for("index"))
    return render_template("login.html")


@app.route("/api/auth/login", methods=["POST"])
def auth_login():
    addr = request.remote_addr
    if rate_limited(addr):
        return jsonify({"error": "ลองผิดหลายครั้งเกินไป กรุณารอสักครู่แล้วลองใหม่"}), 429

    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""

    db = get_db()
    row = db.execute(
        text("SELECT id, password_hash FROM users WHERE username = :username"), {"username": username}
    ).fetchone()
    if not row or not check_password_hash(row.password_hash, password):
        record_failed_login(addr)
        return jsonify({"error": "ชื่อผู้ใช้หรือรหัสผ่านไม่ถูกต้อง"}), 401

    clear_failed_logins(addr)
    session.permanent = True
    session["user_id"] = row.id
    session["username"] = username
    return jsonify({"ok": True})


@app.route("/api/auth/logout", methods=["POST"])
def auth_logout():
    session.clear()
    return jsonify({"ok": True})


# ---------- Pages ----------

@app.route("/")
def index():
    return render_template("index.html", username=session.get("username"))


@app.route("/uploads/<path:filename>")
def uploaded_file(filename):
    # Only reached in local mode - cloud mode returns direct Supabase public URLs instead.
    return send_from_directory(storage.UPLOAD_DIR, filename)


# ---------- Categories ----------

@app.route("/api/categories", methods=["GET"])
def list_categories():
    db = get_db()
    user_id = current_user_id()

    # Self-heal: an account that somehow ended up with zero categories (e.g. accounts
    # created by the first-registrant-on-a-fresh-database edge case, now fixed) gets
    # seeded here instead of staying stuck with an empty category dropdown forever.
    total = db.execute(text("SELECT COUNT(*) FROM categories WHERE user_id = :uid"), {"uid": user_id}).scalar()
    if total == 0:
        seed_default_categories(db, user_id)
        db.commit()

    cat_type = request.args.get("type")
    if cat_type in ("income", "expense"):
        rows = db.execute(
            text("SELECT * FROM categories WHERE user_id = :uid AND type = :type ORDER BY name"),
            {"uid": user_id, "type": cat_type},
        ).fetchall()
    else:
        rows = db.execute(
            text("SELECT * FROM categories WHERE user_id = :uid ORDER BY type, name"), {"uid": user_id}
        ).fetchall()
    return jsonify(db_module.rows_to_dicts(rows))


@app.route("/api/categories", methods=["POST"])
def create_category():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    cat_type = data.get("type")
    if not name:
        return jsonify({"error": "กรุณาระบุชื่อหมวดหมู่ (name is required)"}), 400
    if cat_type not in ("income", "expense"):
        return jsonify({"error": "type ต้องเป็น income หรือ expense"}), 400
    db = get_db()
    user_id = current_user_id()
    try:
        result = db.execute(
            text("INSERT INTO categories (user_id, name, type) VALUES (:uid, :name, :type) RETURNING id"),
            {"uid": user_id, "name": name, "type": cat_type},
        )
        new_id = result.scalar()
        db.commit()
    except IntegrityError:
        db.rollback()
        return jsonify({"error": "มีหมวดหมู่นี้อยู่แล้ว (category already exists)"}), 409
    new_row = db.execute(text("SELECT * FROM categories WHERE id = :id"), {"id": new_id}).fetchone()
    return jsonify(db_module.row_to_dict(new_row)), 201


@app.route("/api/categories/<int:cat_id>", methods=["DELETE"])
def delete_category(cat_id):
    db = get_db()
    user_id = current_user_id()
    owned = db.execute(
        text("SELECT id FROM categories WHERE id = :id AND user_id = :uid"), {"id": cat_id, "uid": user_id}
    ).fetchone()
    if not owned:
        return jsonify({"error": "ไม่พบหมวดหมู่ (category not found)"}), 404
    in_use = db.execute(
        text("SELECT COUNT(*) FROM transactions WHERE category_id = :id AND user_id = :uid"),
        {"id": cat_id, "uid": user_id},
    ).scalar()
    if in_use > 0:
        return (
            jsonify(
                {
                    "error": f"ลบไม่ได้ มีรายการใช้หมวดหมู่นี้อยู่ {in_use} รายการ "
                    "(category is in use by existing transactions)"
                }
            ),
            409,
        )
    db.execute(text("DELETE FROM categories WHERE id = :id AND user_id = :uid"), {"id": cat_id, "uid": user_id})
    db.commit()
    return jsonify({"ok": True})


# ---------- Transactions ----------

def parse_transaction_form(form):
    errors = []
    tx_date = (form.get("date") or "").strip()
    tx_type = form.get("type")
    category_id = form.get("category_id")
    amount = form.get("amount")
    note = (form.get("note") or "").strip()

    if not tx_date:
        errors.append("กรุณาระบุวันที่ (date is required)")
    if tx_type not in ("income", "expense"):
        errors.append("type ต้องเป็น income หรือ expense")
    try:
        category_id = int(category_id)
    except (TypeError, ValueError):
        errors.append("กรุณาเลือกหมวดหมู่ (category_id is required)")
        category_id = None
    try:
        amount = float(amount)
        if amount <= 0:
            errors.append("จำนวนเงินต้องมากกว่า 0 (amount must be positive)")
    except (TypeError, ValueError):
        errors.append("จำนวนเงินไม่ถูกต้อง (invalid amount)")
        amount = None

    return {
        "date": tx_date,
        "type": tx_type,
        "category_id": category_id,
        "amount": amount,
        "note": note,
    }, errors


@app.route("/api/transactions", methods=["GET"])
def list_transactions():
    db = get_db()
    user_id = current_user_id()
    month = request.args.get("month")  # YYYY-MM
    tx_type = request.args.get("type")
    category_id = request.args.get("category_id")

    query = """
        SELECT t.*, c.name AS category_name
        FROM transactions t
        JOIN categories c ON c.id = t.category_id
        WHERE t.user_id = :uid
    """
    params = {"uid": user_id}
    if month:
        query += " AND substr(t.date, 1, 7) = :month"
        params["month"] = month
    if tx_type in ("income", "expense"):
        query += " AND t.type = :type"
        params["type"] = tx_type
    if category_id:
        query += " AND t.category_id = :cat_id"
        params["cat_id"] = category_id
    query += " ORDER BY t.date DESC, t.id DESC"

    rows = db.execute(text(query), params).fetchall()
    return jsonify([transaction_to_dict(r) for r in rows])


@app.route("/api/transactions/<int:tx_id>", methods=["GET"])
def get_transaction(tx_id):
    db = get_db()
    row = db.execute(
        text(
            """
            SELECT t.*, c.name AS category_name
            FROM transactions t JOIN categories c ON c.id = t.category_id
            WHERE t.id = :id AND t.user_id = :uid
            """
        ),
        {"id": tx_id, "uid": current_user_id()},
    ).fetchone()
    if not row:
        return jsonify({"error": "ไม่พบรายการ (transaction not found)"}), 404
    return jsonify(transaction_to_dict(row))


@app.route("/api/transactions", methods=["POST"])
def create_transaction():
    fields, errors = parse_transaction_form(request.form)
    if errors:
        return jsonify({"error": " / ".join(errors)}), 400

    db = get_db()
    user_id = current_user_id()
    cat_row = db.execute(
        text("SELECT id FROM categories WHERE id = :id AND type = :type AND user_id = :uid"),
        {"id": fields["category_id"], "type": fields["type"], "uid": user_id},
    ).fetchone()
    if not cat_row:
        return jsonify({"error": "หมวดหมู่ไม่ตรงกับประเภทรายการ (category/type mismatch)"}), 400

    slip_filename = None
    if "slip" in request.files:
        result = storage.save_slip(request.files["slip"])
        if result == "INVALID_TYPE":
            return jsonify({"error": "ไฟล์สลิปต้องเป็นรูปภาพหรือ PDF เท่านั้น"}), 400
        slip_filename = result

    result = db.execute(
        text(
            """
            INSERT INTO transactions (user_id, date, type, category_id, amount, note, slip_filename, created_at)
            VALUES (:uid, :date, :type, :category_id, :amount, :note, :slip_filename, :created_at)
            RETURNING id
            """
        ),
        {
            "uid": user_id,
            "date": fields["date"],
            "type": fields["type"],
            "category_id": fields["category_id"],
            "amount": fields["amount"],
            "note": fields["note"],
            "slip_filename": slip_filename,
            "created_at": datetime.utcnow().isoformat(),
        },
    )
    new_id = result.scalar()
    db.commit()
    new_row = db.execute(
        text(
            """
            SELECT t.*, c.name AS category_name
            FROM transactions t JOIN categories c ON c.id = t.category_id
            WHERE t.id = :id
            """
        ),
        {"id": new_id},
    ).fetchone()
    return jsonify(transaction_to_dict(new_row)), 201


@app.route("/api/transactions/<int:tx_id>", methods=["PUT"])
def update_transaction(tx_id):
    db = get_db()
    user_id = current_user_id()
    existing = db.execute(
        text("SELECT * FROM transactions WHERE id = :id AND user_id = :uid"), {"id": tx_id, "uid": user_id}
    ).fetchone()
    if not existing:
        return jsonify({"error": "ไม่พบรายการ (transaction not found)"}), 404

    fields, errors = parse_transaction_form(request.form)
    if errors:
        return jsonify({"error": " / ".join(errors)}), 400

    cat_row = db.execute(
        text("SELECT id FROM categories WHERE id = :id AND type = :type AND user_id = :uid"),
        {"id": fields["category_id"], "type": fields["type"], "uid": user_id},
    ).fetchone()
    if not cat_row:
        return jsonify({"error": "หมวดหมู่ไม่ตรงกับประเภทรายการ (category/type mismatch)"}), 400

    slip_filename = existing.slip_filename
    remove_slip = request.form.get("remove_slip") == "1"
    new_file = request.files.get("slip")

    if remove_slip and slip_filename:
        storage.delete_slip(slip_filename)
        slip_filename = None

    if new_file and new_file.filename:
        result = storage.save_slip(new_file)
        if result == "INVALID_TYPE":
            return jsonify({"error": "ไฟล์สลิปต้องเป็นรูปภาพหรือ PDF เท่านั้น"}), 400
        if result:
            if slip_filename:
                storage.delete_slip(slip_filename)
            slip_filename = result

    db.execute(
        text(
            """
            UPDATE transactions
            SET date = :date, type = :type, category_id = :category_id, amount = :amount,
                note = :note, slip_filename = :slip_filename
            WHERE id = :id AND user_id = :uid
            """
        ),
        {
            "date": fields["date"],
            "type": fields["type"],
            "category_id": fields["category_id"],
            "amount": fields["amount"],
            "note": fields["note"],
            "slip_filename": slip_filename,
            "id": tx_id,
            "uid": user_id,
        },
    )
    db.commit()
    row = db.execute(
        text(
            """
            SELECT t.*, c.name AS category_name
            FROM transactions t JOIN categories c ON c.id = t.category_id
            WHERE t.id = :id
            """
        ),
        {"id": tx_id},
    ).fetchone()
    return jsonify(transaction_to_dict(row))


@app.route("/api/transactions/<int:tx_id>", methods=["DELETE"])
def delete_transaction(tx_id):
    db = get_db()
    user_id = current_user_id()
    row = db.execute(
        text("SELECT slip_filename FROM transactions WHERE id = :id AND user_id = :uid"),
        {"id": tx_id, "uid": user_id},
    ).fetchone()
    if not row:
        return jsonify({"error": "ไม่พบรายการ (transaction not found)"}), 404
    if row.slip_filename:
        storage.delete_slip(row.slip_filename)
    db.execute(text("DELETE FROM transactions WHERE id = :id AND user_id = :uid"), {"id": tx_id, "uid": user_id})
    db.commit()
    return jsonify({"ok": True})


# ---------- Summary ----------

@app.route("/api/summary", methods=["GET"])
def summary():
    db = get_db()
    user_id = current_user_id()
    month = request.args.get("month")

    base = "SELECT type, COALESCE(SUM(amount), 0) AS total FROM transactions WHERE user_id = :uid"
    params = {"uid": user_id}
    if month:
        base += " AND substr(date, 1, 7) = :month"
        params["month"] = month
    base += " GROUP BY type"

    totals = {"income": 0.0, "expense": 0.0}
    for r in db.execute(text(base), params).fetchall():
        totals[r.type] = r.total

    by_cat_query = """
        SELECT c.id AS category_id, c.name AS category_name, t.type AS type,
               COALESCE(SUM(t.amount), 0) AS total
        FROM transactions t
        JOIN categories c ON c.id = t.category_id
        WHERE t.user_id = :uid
    """
    cat_params = {"uid": user_id}
    if month:
        by_cat_query += " AND substr(t.date, 1, 7) = :month"
        cat_params["month"] = month
    by_cat_query += " GROUP BY c.id, c.name, t.type ORDER BY total DESC"
    by_category = db_module.rows_to_dicts(db.execute(text(by_cat_query), cat_params).fetchall())

    return jsonify(
        {
            "income_total": totals["income"],
            "expense_total": totals["expense"],
            "balance": totals["income"] - totals["expense"],
            "by_category": by_category,
        }
    )


# ---------- Google Sheet sync ----------
# NOTE: sync config (spreadsheet + credentials) is shared app-wide, not per-user.
# Push/pull are scoped to the CURRENT user's own data only, but if two different users
# both push to the same configured Sheet, whoever pushes last overwrites the other's copy
# in the Sheet (local data stays safe either way - only the cloud copy can be clobbered).

def sync_status_payload():
    return {
        "spreadsheet_id": db_module.get_config("sync_spreadsheet_id"),
        "service_account_email": db_module.get_config("sync_service_account_email"),
        "has_credentials": bool(db_module.get_config("sync_credentials_json")),
        "last_synced_at": db_module.get_config("sync_last_synced_at"),
        "last_sync_direction": db_module.get_config("sync_last_sync_direction"),
    }


@app.route("/api/sync/config", methods=["GET"])
def get_sync_config():
    return jsonify(sync_status_payload())


@app.route("/api/sync/config", methods=["POST"])
def update_sync_config():
    spreadsheet_input = (request.form.get("spreadsheet_id") or "").strip()
    if spreadsheet_input:
        db_module.set_config("sync_spreadsheet_id", gsheet_sync.extract_spreadsheet_id(spreadsheet_input))

    creds_file = request.files.get("credentials")
    if creds_file and creds_file.filename:
        if not creds_file.filename.lower().endswith(".json"):
            return jsonify({"error": "ไฟล์ credentials ต้องเป็นไฟล์ .json"}), 400
        import json as _json

        try:
            content = _json.load(creds_file.stream)
        except _json.JSONDecodeError:
            return jsonify({"error": "ไฟล์ credentials ไม่ใช่ JSON ที่ถูกต้อง"}), 400
        if "client_email" not in content:
            return (
                jsonify({"error": "ไฟล์นี้ไม่ใช่ไฟล์ credentials ของ Service Account (ไม่มี client_email)"}),
                400,
            )
        db_module.set_config("sync_credentials_json", _json.dumps(content))
        db_module.set_config("sync_service_account_email", content["client_email"])

    return jsonify(sync_status_payload())


@app.route("/api/sync/push", methods=["POST"])
def sync_push():
    spreadsheet_id = db_module.get_config("sync_spreadsheet_id")
    credentials_json = db_module.get_config("sync_credentials_json")
    if not spreadsheet_id:
        return jsonify({"error": "กรุณาระบุลิงก์ Google Sheet ก่อน"}), 400
    if not credentials_json:
        return jsonify({"error": "กรุณาอัปโหลดไฟล์ credentials ก่อน"}), 400
    try:
        result = gsheet_sync.push(credentials_json, spreadsheet_id, current_user_id())
    except gsheet_sync.SyncError as e:
        return jsonify({"error": str(e)}), 400
    db_module.set_config("sync_last_synced_at", datetime.utcnow().isoformat())
    db_module.set_config("sync_last_sync_direction", "push")
    return jsonify({**sync_status_payload(), **result})


@app.route("/api/sync/pull", methods=["POST"])
def sync_pull():
    spreadsheet_id = db_module.get_config("sync_spreadsheet_id")
    credentials_json = db_module.get_config("sync_credentials_json")
    if not spreadsheet_id:
        return jsonify({"error": "กรุณาระบุลิงก์ Google Sheet ก่อน"}), 400
    if not credentials_json:
        return jsonify({"error": "กรุณาอัปโหลดไฟล์ credentials ก่อน"}), 400
    try:
        result = gsheet_sync.pull(credentials_json, spreadsheet_id, current_user_id())
    except gsheet_sync.SyncError as e:
        return jsonify({"error": str(e)}), 400
    db_module.set_config("sync_last_synced_at", datetime.utcnow().isoformat())
    db_module.set_config("sync_last_sync_direction", "pull")
    return jsonify({**sync_status_payload(), **result})


db_module.init_db()

if __name__ == "__main__":
    lan_ip = get_lan_ip()
    mode = "cloud (Postgres)" if db_module.IS_POSTGRES else "local (SQLite)"
    print("=" * 60)
    print(f" บันทึกรายรับรายจ่าย SB - {mode}")
    print(f" เครื่องนี้      : http://127.0.0.1:5000")
    print(f" เครื่องอื่นในวงเดียวกัน : http://{lan_ip}:5000")
    if db_module.IS_POSTGRES and not os.environ.get("FLASK_SECRET_KEY"):
        print(" คำเตือน: ไม่มี FLASK_SECRET_KEY - ผู้ใช้จะถูกล็อกเอาต์ทุกครั้งที่ redeploy")
    print("=" * 60)
    # debug=False: the app is reachable from other devices/the internet - Werkzeug's
    # interactive debugger is a real risk once untrusted clients can reach it.
    app.run(host="0.0.0.0", port=5000, debug=False)
