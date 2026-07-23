"""
Database layer - works against local SQLite (default, unchanged local/LAN behavior)
or a Postgres database (when DATABASE_URL is set, e.g. Supabase, for cloud deployment).

Uses SQLAlchemy Core (not the ORM) so the same hand-written SQL works on both dialects -
it translates bound-parameter style and gives consistent `INSERT ... RETURNING id` support,
which is the part that's genuinely error-prone to hand-roll across sqlite3 vs psycopg2.

Also hosts app_config: a small key/value table that replaces the old local JSON config
files (auth_config.json, sync_config.json, the uploaded Google credentials file). Config
now always lives in the database instead of on disk, because on a cloud host the local
filesystem is wiped on every restart/redeploy - the database is the only thing that persists.
"""
import os

from sqlalchemy import create_engine, text

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_SQLITE_PATH = os.path.join(BASE_DIR, "data.db")

_database_url = os.environ.get("DATABASE_URL")
if _database_url:
    # Some providers (Supabase, Render, Heroku-style) hand out "postgres://" but
    # SQLAlchemy's psycopg2 dialect requires "postgresql://".
    if _database_url.startswith("postgres://"):
        _database_url = _database_url.replace("postgres://", "postgresql://", 1)
    engine = create_engine(_database_url, pool_pre_ping=True)
else:
    engine = create_engine(f"sqlite:///{DEFAULT_SQLITE_PATH}")

IS_SQLITE = engine.dialect.name == "sqlite"
IS_POSTGRES = engine.dialect.name == "postgresql"


def column_exists(conn, table, column):
    if IS_SQLITE:
        rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
        return column in [r[1] for r in rows]
    row = conn.execute(
        text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = :table AND column_name = :column"
        ),
        {"table": table, "column": column},
    ).fetchone()
    return row is not None


def row_to_dict(row):
    return None if row is None else dict(row._mapping)


def rows_to_dicts(rows):
    return [dict(r._mapping) for r in rows]


def init_db():
    """Create schema (both dialects), and migrate a pre-multi-user local data.db if found."""
    pk = "INTEGER PRIMARY KEY AUTOINCREMENT" if IS_SQLITE else "SERIAL PRIMARY KEY"

    with engine.begin() as conn:
        if IS_SQLITE:
            # Off for the whole migration below: rebuilding categories while transactions
            # still references it trips SQLite's FK enforcement even though the data stays
            # fully consistent throughout. Harmless to leave off for this one-shot setup.
            conn.execute(text("PRAGMA foreign_keys = OFF"))

        conn.execute(text(
            f"""
            CREATE TABLE IF NOT EXISTS app_config (
                key TEXT PRIMARY KEY,
                value TEXT
            )
            """
        ))

        conn.execute(text(
            f"""
            CREATE TABLE IF NOT EXISTS users (
                id {pk},
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        ))

        conn.execute(text(
            f"""
            CREATE TABLE IF NOT EXISTS categories (
                id {pk},
                user_id INTEGER REFERENCES users(id),
                name TEXT NOT NULL,
                type TEXT NOT NULL CHECK(type IN ('income', 'expense')),
                UNIQUE(user_id, name, type)
            )
            """
        ))

        conn.execute(text(
            f"""
            CREATE TABLE IF NOT EXISTS transactions (
                id {pk},
                user_id INTEGER REFERENCES users(id),
                date TEXT NOT NULL,
                type TEXT NOT NULL CHECK(type IN ('income', 'expense')),
                category_id INTEGER NOT NULL REFERENCES categories(id),
                amount REAL NOT NULL,
                note TEXT,
                slip_filename TEXT,
                created_at TEXT NOT NULL
            )
            """
        ))

        # Migrate a pre-multi-user local data.db (categories had UNIQUE(name, type), no
        # user_id). Only relevant for SQLite - a fresh Postgres database never has this.
        if IS_SQLITE and not column_exists(conn, "categories", "user_id"):
            conn.execute(text(
                """
                ALTER TABLE categories RENAME TO categories_old
                """
            ))
            conn.execute(text(
                """
                CREATE TABLE categories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER REFERENCES users(id),
                    name TEXT NOT NULL,
                    type TEXT NOT NULL CHECK(type IN ('income', 'expense')),
                    UNIQUE(user_id, name, type)
                )
                """
            ))
            conn.execute(text(
                """
                INSERT INTO categories (id, user_id, name, type)
                    SELECT id, NULL, name, type FROM categories_old
                """
            ))
            conn.execute(text("DROP TABLE categories_old"))

        if IS_SQLITE and not column_exists(conn, "transactions", "user_id"):
            conn.execute(text("ALTER TABLE transactions ADD COLUMN user_id INTEGER REFERENCES users(id)"))

    _migrate_legacy_json_config()


def _migrate_legacy_json_config():
    """One-time import of the old local-file config (sync_config.json, google-credentials.json)
    into app_config, for installs upgraded from before config moved into the database. No-op
    once already migrated, and a no-op on a fresh install (cloud or local) where these files
    never existed."""
    import json

    sync_path = os.path.join(BASE_DIR, "sync_config.json")
    if os.path.exists(sync_path) and get_config("sync_spreadsheet_id") is None:
        with open(sync_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        if cfg.get("spreadsheet_id"):
            set_config("sync_spreadsheet_id", cfg["spreadsheet_id"])
        if cfg.get("service_account_email"):
            set_config("sync_service_account_email", cfg["service_account_email"])
        if cfg.get("last_synced_at"):
            set_config("sync_last_synced_at", cfg["last_synced_at"])
        if cfg.get("last_sync_direction"):
            set_config("sync_last_sync_direction", cfg["last_sync_direction"])

    creds_path = os.path.join(BASE_DIR, "google-credentials.json")
    if os.path.exists(creds_path) and get_config("sync_credentials_json") is None:
        with open(creds_path, "r", encoding="utf-8") as f:
            set_config("sync_credentials_json", f.read())


def get_config(key, default=None):
    with engine.connect() as conn:
        row = conn.execute(text("SELECT value FROM app_config WHERE key = :key"), {"key": key}).fetchone()
    return row[0] if row else default


def set_config(key, value):
    with engine.begin() as conn:
        if IS_SQLITE:
            conn.execute(
                text("INSERT OR REPLACE INTO app_config (key, value) VALUES (:key, :value)"),
                {"key": key, "value": value},
            )
        else:
            conn.execute(
                text(
                    "INSERT INTO app_config (key, value) VALUES (:key, :value) "
                    "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"
                ),
                {"key": key, "value": value},
            )


def delete_config(key):
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM app_config WHERE key = :key"), {"key": key})
