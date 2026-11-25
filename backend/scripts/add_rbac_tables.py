"""One-off: facility groups + per-user facility access + new roles (RBAC step 1). Safe to re-run."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sqlalchemy import text

from app.database import engine

GROUPS = {"Alnoor": [5, 6, 7, 8], "Noor Al Shifa": [17, 19]}

with engine.begin() as c:
    c.execute(text("""CREATE TABLE IF NOT EXISTS facility_groups (
        id SERIAL PRIMARY KEY, name VARCHAR(100) NOT NULL UNIQUE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now())"""))
    c.execute(text("ALTER TABLE facilities ADD COLUMN IF NOT EXISTS group_id INTEGER REFERENCES facility_groups(id) ON DELETE SET NULL"))
    c.execute(text("""CREATE TABLE IF NOT EXISTS user_facility_access (
        id SERIAL PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        facility_id INTEGER REFERENCES facilities(id) ON DELETE CASCADE,
        group_id INTEGER REFERENCES facility_groups(id) ON DELETE CASCADE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT ck_ufa_one CHECK ((facility_id IS NULL) <> (group_id IS NULL)))"""))
    c.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_ufa_fac ON user_facility_access(user_id, facility_id) WHERE facility_id IS NOT NULL"))
    c.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_ufa_grp ON user_facility_access(user_id, group_id) WHERE group_id IS NOT NULL"))
    c.execute(text("ALTER TABLE users DROP CONSTRAINT IF EXISTS ck_user_role"))
    c.execute(text("""ALTER TABLE users ADD CONSTRAINT ck_user_role CHECK (role IN
        ('admin','group_admin','facility_admin','staff','viewer','billing_manager','clinician'))"""))
    c.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS expires_at DATE"))
    c.execute(text("""CREATE TABLE IF NOT EXISTS app_settings (
        key VARCHAR(100) PRIMARY KEY, value JSONB NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_by INTEGER REFERENCES users(id) ON DELETE SET NULL)"""))
    for name, fids in GROUPS.items():
        gid = c.execute(text("INSERT INTO facility_groups(name) VALUES (:n) ON CONFLICT (name) DO UPDATE SET name=EXCLUDED.name RETURNING id"), {"n": name}).scalar()
        c.execute(text("UPDATE facilities SET group_id=:g WHERE id = ANY(:f) AND group_id IS NULL"), {"g": gid, "f": fids})
print("RBAC tables ready")
