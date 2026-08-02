"""
Google Sheets sync for the expense tracker.

Full-mirror sync only (no field-level merge), scoped to a single user's data:
- push(): overwrites the Google Sheet with the given user's current local data.
- pull(): overwrites that user's local data with what's in the Google Sheet.

Row ids from the sheet are NOT reused as local primary keys (the categories/transactions
tables are shared across all users, so blindly reinserting a sheet's id could collide with
another user's row). Instead, pull() inserts fresh rows and remaps category_id references
via the id that was in the sheet at push time.
"""
import json
import re

from sqlalchemy import text

import db as db_module

try:
    import gspread
    from google.oauth2.service_account import Credentials

    GSPREAD_AVAILABLE = True
except ImportError:
    GSPREAD_AVAILABLE = False

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
CATEGORIES_HEADER = ["id", "name", "type"]
TRANSACTIONS_HEADER = ["id", "date", "type", "category_id", "amount", "note", "created_at"]


class SyncError(Exception):
    """Raised for any sync failure with a message safe to show the user."""


def extract_spreadsheet_id(text_):
    text_ = (text_ or "").strip()
    match = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", text_)
    if match:
        return match.group(1)
    return text_


def get_client(credentials_json):
    if not GSPREAD_AVAILABLE:
        raise SyncError(
            "ยังไม่ได้ติดตั้งไลบรารีสำหรับ Google Sheets กรุณารัน: pip install -r requirements.txt"
        )
    if not credentials_json:
        raise SyncError("ยังไม่ได้อัปโหลดไฟล์ credentials ของ Service Account")
    try:
        raw = json.loads(credentials_json)
        creds = Credentials.from_service_account_info(raw, scopes=SCOPES)
    except (ValueError, json.JSONDecodeError, KeyError) as e:
        raise SyncError(f"ไฟล์ credentials ไม่ถูกต้อง: {e}")
    return gspread.authorize(creds)


def open_spreadsheet(client, spreadsheet_id):
    try:
        return client.open_by_key(spreadsheet_id)
    except gspread.exceptions.SpreadsheetNotFound:
        raise SyncError(
            "ไม่พบ Google Sheet นี้ - ตรวจสอบว่าใส่ลิงก์/ID ถูกต้อง "
            "และแชร์สิทธิ์ Editor ให้อีเมลของ Service Account แล้ว"
        )
    except gspread.exceptions.APIError as e:
        raise SyncError(f"Google Sheets API ผิดพลาด: {e}")


def _get_or_create_worksheet(spreadsheet, title, header):
    try:
        return spreadsheet.worksheet(title)
    except gspread.exceptions.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=title, rows=1000, cols=max(len(header), 1))
        return ws


def read_local_data(user_id):
    with db_module.engine.connect() as conn:
        categories = conn.execute(
            text("SELECT id, name, type FROM categories WHERE user_id = :uid ORDER BY id"), {"uid": user_id}
        ).fetchall()
        transactions = conn.execute(
            text(
                "SELECT id, date, type, category_id, amount, note, created_at "
                "FROM transactions WHERE user_id = :uid ORDER BY id"
            ),
            {"uid": user_id},
        ).fetchall()
    return db_module.rows_to_dicts(categories), db_module.rows_to_dicts(transactions)


def write_to_sheet(spreadsheet, categories, transactions):
    cat_ws = _get_or_create_worksheet(spreadsheet, "Categories", CATEGORIES_HEADER)
    tx_ws = _get_or_create_worksheet(spreadsheet, "Transactions", TRANSACTIONS_HEADER)

    cat_rows = [CATEGORIES_HEADER] + [[c["id"], c["name"], c["type"]] for c in categories]
    tx_rows = [TRANSACTIONS_HEADER] + [
        [
            t["id"],
            t["date"],
            t["type"],
            t["category_id"],
            t["amount"],
            t["note"] or "",
            t["created_at"],
        ]
        for t in transactions
    ]

    cat_ws.clear()
    cat_ws.update(values=cat_rows, range_name="A1")
    tx_ws.clear()
    tx_ws.update(values=tx_rows, range_name="A1")


def push(credentials_json, spreadsheet_id, user_id):
    client = get_client(credentials_json)
    spreadsheet = open_spreadsheet(client, spreadsheet_id)
    categories, transactions = read_local_data(user_id)
    write_to_sheet(spreadsheet, categories, transactions)
    return {"categories": len(categories), "transactions": len(transactions)}


def _rows_to_dicts(values, header, sheet_label):
    if not values:
        return []
    actual_header = values[0]
    if actual_header != header:
        raise SyncError(
            f"หัวตารางในชีต {sheet_label} ไม่ตรงกับที่แอปคาดไว้ "
            "(อาจถูกแก้ไขโครงสร้างชีตด้วยมือ) ลองซิงค์ขึ้นใหม่จากเครื่องที่มีข้อมูลถูกต้องก่อน"
        )
    return [dict(zip(header, row)) for row in values[1:] if any(cell.strip() for cell in row)]


def fetch_sheet_data(spreadsheet):
    try:
        cat_ws = spreadsheet.worksheet("Categories")
        tx_ws = spreadsheet.worksheet("Transactions")
    except gspread.exceptions.WorksheetNotFound:
        raise SyncError("ไม่พบชีต Categories/Transactions - ยังไม่เคยกด \"ซิงค์ขึ้น\" จากเครื่องไหนเลยหรือเปล่า?")

    categories = _rows_to_dicts(cat_ws.get_all_values(), CATEGORIES_HEADER, "Categories")
    transactions = _rows_to_dicts(tx_ws.get_all_values(), TRANSACTIONS_HEADER, "Transactions")
    return categories, transactions


def apply_to_db(user_id, categories, transactions):
    """Replace this user's categories/transactions with the given rows (sheet-shaped dicts of strings).

    Inserts get fresh local ids (not the sheet's ids) since the categories/transactions
    tables are shared across users - the sheet's id is only used to link a transaction
    to the right category within this same pull.
    """
    with db_module.engine.begin() as conn:
        conn.execute(text("DELETE FROM transactions WHERE user_id = :uid"), {"uid": user_id})
        conn.execute(text("DELETE FROM categories WHERE user_id = :uid"), {"uid": user_id})

        id_map = {}
        for c in categories:
            result = conn.execute(
                text("INSERT INTO categories (user_id, name, type) VALUES (:uid, :name, :type) RETURNING id"),
                {"uid": user_id, "name": c["name"], "type": c["type"]},
            )
            id_map[str(c["id"])] = result.scalar()

        for t in transactions:
            new_cat_id = id_map.get(str(t["category_id"]))
            if new_cat_id is None:
                raise SyncError(
                    f"รายการวันที่ {t.get('date')} อ้างอิงหมวดหมู่ (id={t.get('category_id')}) "
                    "ที่ไม่มีอยู่ในชีต Categories"
                )
            try:
                amount = float(t["amount"])
            except (TypeError, ValueError):
                raise SyncError(f"จำนวนเงินในชีตไม่ถูกต้อง: {t.get('amount')!r}")
            conn.execute(
                text(
                    """
                    INSERT INTO transactions
                        (user_id, date, type, category_id, amount, note, created_at)
                    VALUES (:uid, :date, :type, :category_id, :amount, :note, :created_at)
                    """
                ),
                {
                    "uid": user_id,
                    "date": t["date"],
                    "type": t["type"],
                    "category_id": new_cat_id,
                    "amount": amount,
                    "note": t["note"] or None,
                    "created_at": t["created_at"],
                },
            )


def pull(credentials_json, spreadsheet_id, user_id):
    client = get_client(credentials_json)
    spreadsheet = open_spreadsheet(client, spreadsheet_id)
    categories, transactions = fetch_sheet_data(spreadsheet)
    apply_to_db(user_id, categories, transactions)
    return {"categories": len(categories), "transactions": len(transactions)}
