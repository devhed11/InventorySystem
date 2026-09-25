import os
import re
import io
import json
import uuid
import difflib
import requests
from datetime import datetime, date, timedelta
from functools import wraps
from flask import Flask, render_template, request, jsonify, session, redirect, url_for, flash, send_from_directory
import psycopg2
from psycopg2.extras import RealDictCursor
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret-change-me')
# Keep techs/admins logged in for 30 days and slide the expiry forward on each
# request, so the PWA doesn't drop the session while a tech is mid-request.
app.permanent_session_lifetime = timedelta(days=30)
app.config['SESSION_REFRESH_EACH_REQUEST'] = True

DATABASE_URL = os.environ.get('DATABASE_URL', '')
FR_API_KEY = os.environ.get('FR_API_KEY', '')
FR_AUTH_KEY = os.environ.get('FR_AUTH_KEY', '')
FR_BASE_URL = os.environ.get('FR_BASE_URL', 'https://holperspest.fieldroutes.com')
SAMSARA_API_TOKEN = os.environ.get('SAMSARA_API_TOKEN', '')
SAMSARA_BASE_URL = os.environ.get('SAMSARA_BASE_URL', 'https://api.samsara.com')

# ─── DB ───────────────────────────────────────────────────────────────────────

def get_db():
    url = DATABASE_URL
    if url.startswith('postgres://'):
        url = url.replace('postgres://', 'postgresql://', 1)
    conn = psycopg2.connect(url, cursor_factory=RealDictCursor)
    return conn

def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            username VARCHAR(100) UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role VARCHAR(20) DEFAULT 'admin',
            created_at TIMESTAMP DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS products (
            id SERIAL PRIMARY KEY,
            name VARCHAR(200) NOT NULL UNIQUE,
            fr_product_id VARCHAR(100),
            storage_unit VARCHAR(50) NOT NULL,
            usage_unit VARCHAR(50) NOT NULL,
            conversion_factor NUMERIC(12,6) NOT NULL DEFAULT 1,
            conversion_note TEXT,
            active BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMP DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS technicians (
            id SERIAL PRIMARY KEY,
            name VARCHAR(200) NOT NULL UNIQUE,
            fr_employee_id VARCHAR(100),
            truck_id VARCHAR(100),
            active BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMP DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS warehouse_inventory (
            id SERIAL PRIMARY KEY,
            product_id INTEGER REFERENCES products(id),
            location VARCHAR(50) NOT NULL DEFAULT 'Holpers',
            quantity_storage_units NUMERIC(12,4) NOT NULL DEFAULT 0,
            last_updated TIMESTAMP DEFAULT NOW(),
            UNIQUE(product_id, location)
        );

        CREATE TABLE IF NOT EXISTS tech_inventory (
            id SERIAL PRIMARY KEY,
            technician_id INTEGER REFERENCES technicians(id),
            product_id INTEGER REFERENCES products(id),
            quantity_storage_units NUMERIC(12,4) NOT NULL DEFAULT 0,
            last_updated TIMESTAMP DEFAULT NOW(),
            UNIQUE(technician_id, product_id)
        );

        CREATE TABLE IF NOT EXISTS truck_par_levels (
            id SERIAL PRIMARY KEY,
            technician_id INTEGER REFERENCES technicians(id),
            product_id INTEGER REFERENCES products(id),
            min_qty NUMERIC(12,4) NOT NULL DEFAULT 0,
            updated_at TIMESTAMP DEFAULT NOW(),
            UNIQUE(technician_id, product_id)
        );

        CREATE TABLE IF NOT EXISTS oldham_sku_map (
            id SERIAL PRIMARY KEY,
            oldham_sku VARCHAR(60) UNIQUE NOT NULL,
            product_id INTEGER REFERENCES products(id),
            oldham_name TEXT,
            updated_at TIMESTAMP DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS inventory_transactions (
            id SERIAL PRIMARY KEY,
            transaction_type VARCHAR(50) NOT NULL,
            product_id INTEGER REFERENCES products(id),
            technician_id INTEGER REFERENCES technicians(id),
            quantity_storage_units NUMERIC(12,4) NOT NULL,
            quantity_usage_units NUMERIC(12,4),
            notes TEXT,
            fr_appointment_id VARCHAR(100),
            fr_job_id VARCHAR(100),
            performed_by VARCHAR(200),
            created_at TIMESTAMP DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS audit_log (
            id SERIAL PRIMARY KEY,
            audit_type VARCHAR(50) NOT NULL,
            product_id INTEGER REFERENCES products(id),
            technician_id INTEGER REFERENCES technicians(id),
            old_quantity NUMERIC(12,4),
            new_quantity NUMERIC(12,4),
            reason TEXT,
            performed_by VARCHAR(200),
            created_at TIMESTAMP DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS fr_sync_log (
            id SERIAL PRIMARY KEY,
            sync_date DATE NOT NULL,
            appointments_processed INTEGER DEFAULT 0,
            usage_deducted INTEGER DEFAULT 0,
            errors TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS product_thresholds (
            id SERIAL PRIMARY KEY,
            product_id INTEGER REFERENCES products(id) UNIQUE,
            min_warehouse_qty NUMERIC(12,4) NOT NULL DEFAULT 0,
            reorder_qty NUMERIC(12,4) DEFAULT 0,
            reorder_note TEXT,
            updated_at TIMESTAMP DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS sync_state (
            key VARCHAR(100) PRIMARY KEY,
            value TEXT,
            updated_at TIMESTAMP DEFAULT NOW()
        );

        -- System event log (changelog / health)
        CREATE TABLE IF NOT EXISTS system_log (
            id         SERIAL PRIMARY KEY,
            event_type VARCHAR(50)  NOT NULL,  -- sync, report, admin, auth, error, archive
            description TEXT        NOT NULL,
            performed_by VARCHAR(100),
            details    JSONB,
            created_at TIMESTAMP DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS idx_system_log_created ON system_log(created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_system_log_type    ON system_log(event_type);

        -- Raw FR data storage (the "JSON database" in Postgres)
        CREATE TABLE IF NOT EXISTS fr_appointments_raw (
            appointment_id VARCHAR(50) PRIMARY KEY,
            customer_id VARCHAR(50),
            status VARCHAR(10),
            status_text VARCHAR(50),
            date_completed DATE,
            scheduled_date DATE,
            serviced_by VARCHAR(50),
            completed_by VARCHAR(50),
            assigned_tech VARCHAR(50),
            employee_id VARCHAR(50),
            tech_fr_id VARCHAR(50),  -- resolved tech (servicedBy → completedBy → assignedTech → employeeID)
            appt_date DATE,          -- dateCompleted or date, whichever is available
            raw_json JSONB,
            synced_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW()
        );

        -- Service type lookup table (synced from FR serviceType/get)
        CREATE TABLE IF NOT EXISTS service_types (
            type_id    VARCHAR(20) PRIMARY KEY,
            description VARCHAR(200),
            category   VARCHAR(100),
            synced_at  TIMESTAMP DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS fr_chemical_uses_raw (
            chemical_use_id VARCHAR(50) PRIMARY KEY,
            appointment_id VARCHAR(50),
            customer_id VARCHAR(50),
            chemical_id VARCHAR(50),
            amount NUMERIC(12,6) DEFAULT 0,
            concentrated_amount NUMERIC(12,6) DEFAULT 0,
            unit VARCHAR(50),
            concentrated_unit VARCHAR(50),
            resolved_amount NUMERIC(12,6) DEFAULT 0,  -- concentrated if > 0 else amount
            resolved_unit VARCHAR(50),                -- unit used for deduction
            date_created DATE,
            created_by VARCHAR(50),
            raw_json JSONB,
            inventory_updated BOOLEAN DEFAULT FALSE,  -- has this been deducted from inventory?
            synced_at TIMESTAMP DEFAULT NOW()
        );
    """)
    # Runtime column migrations (safe, idempotent)
    cur.execute("ALTER TABLE fr_appointments_raw ADD COLUMN IF NOT EXISTS service_type_id VARCHAR(20)")
    cur.execute("ALTER TABLE inventory_transactions ADD COLUMN IF NOT EXISTS archived BOOLEAN DEFAULT FALSE")
    cur.execute("ALTER TABLE inventory_transactions ADD COLUMN IF NOT EXISTS transfer_batch_id UUID")
    # Backfill service_type_id from raw_json for existing rows
    cur.execute("""
        UPDATE fr_appointments_raw
        SET service_type_id = raw_json->>'type'
        WHERE service_type_id IS NULL AND raw_json->>'type' IS NOT NULL
    """)

    # Inventory request tables (tech digital paper card)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS inventory_requests (
            id           SERIAL PRIMARY KEY,
            technician_id INTEGER REFERENCES technicians(id),
            status       VARCHAR(20) DEFAULT 'pending',
            notes        TEXT,
            submitted_at TIMESTAMP DEFAULT NOW(),
            reviewed_at  TIMESTAMP,
            reviewed_by  VARCHAR(200)
        );
        CREATE TABLE IF NOT EXISTS inventory_request_items (
            id                  SERIAL PRIMARY KEY,
            request_id          INTEGER REFERENCES inventory_requests(id) ON DELETE CASCADE,
            product_id          INTEGER REFERENCES products(id),
            quantity_requested  NUMERIC(12,4) NOT NULL,
            unit_mode           VARCHAR(10) DEFAULT 'usage',
            status              VARCHAR(20) DEFAULT 'pending'
        );

        -- Rules for the service-compliance report: "a job of this service type
        -- should have used at least one of these products."
        CREATE TABLE IF NOT EXISTS compliance_rules (
            id                   SERIAL PRIMARY KEY,
            name                 VARCHAR(150),
            service_type_id      VARCHAR(20),
            expected_product_ids INTEGER[] NOT NULL DEFAULT '{}',
            skip_zero_invoice    BOOLEAN DEFAULT FALSE,
            active               BOOLEAN DEFAULT TRUE,
            created_at           TIMESTAMP DEFAULT NOW()
        );

        -- Admin-raised prompts asking a tech to recount a specific truck item.
        CREATE TABLE IF NOT EXISTS inventory_flags (
            id            SERIAL PRIMARY KEY,
            technician_id INTEGER REFERENCES technicians(id),
            product_id    INTEGER REFERENCES products(id),
            note          TEXT,
            status        VARCHAR(20) NOT NULL DEFAULT 'open',   -- 'open' | 'resolved' | 'cancelled'
            flagged_qty   NUMERIC(12,4),                          -- truck qty at flag time (reference)
            resolved_qty  NUMERIC(12,4),                          -- what the tech entered
            created_by    VARCHAR(100),
            created_at    TIMESTAMP DEFAULT NOW(),
            resolved_at   TIMESTAMP
        );
    """)

    # Link a user login to a specific technician (admins stay NULL; tech logins
    # point at their technician row so the PWA knows who is signed in).
    try:
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS technician_id INTEGER REFERENCES technicians(id)")
        conn.commit()
    except Exception:
        conn.rollback()
    # Split truck par into "keep on truck" (min_qty) and a separate restock-alert
    # level (alert_qty). When alert_qty is unset, low = below min_qty (old behavior).
    try:
        cur.execute("ALTER TABLE truck_par_levels ADD COLUMN IF NOT EXISTS alert_qty NUMERIC(12,4)")
        conn.commit()
    except Exception:
        conn.rollback()

    # Default admin user
    cur.execute("SELECT id FROM users WHERE username='admin'")
    if not cur.fetchone():
        cur.execute(
            "INSERT INTO users (username, password_hash, role) VALUES (%s, %s, 'admin')",
            ('admin', generate_password_hash(os.environ.get('ADMIN_PASSWORD', 'inventory2024')))
        )
    # Shared tech login — all techs use this to access /r
    # Rename lowercase 'tech' → 'Tech' if it exists from older deploy
    cur.execute("UPDATE users SET username='Tech' WHERE username='tech'")
    cur.execute("SELECT id FROM users WHERE username='Tech'")
    tech_password = os.environ.get('TECH_PASSWORD', 'changeme')
    if not cur.fetchone():
        cur.execute(
            "INSERT INTO users (username, password_hash, role) VALUES (%s, %s, 'tech')",
            ('Tech', generate_password_hash(tech_password))
        )
    else:
        cur.execute(
            "UPDATE users SET password_hash=%s WHERE username='Tech'",
            (generate_password_hash(tech_password),)
        )
    # (Deduplication removed — warehouse supports multiple locations per product)
    # Add appt_date column to inventory_transactions if not exists
    try:
        cur.execute("ALTER TABLE inventory_transactions ADD COLUMN IF NOT EXISTS appt_date DATE")
        conn.commit()
    except Exception:
        conn.rollback()
    # Add cost column to products if not exists
    try:
        cur.execute("ALTER TABLE products ADD COLUMN IF NOT EXISTS cost_per_storage_unit NUMERIC(12,4) DEFAULT 0")
        conn.commit()
    except Exception:
        conn.rollback()
    # Add use_applied_amount flag — for gels/baits/granules that track applied qty not concentrate
    try:
        cur.execute("ALTER TABLE products ADD COLUMN IF NOT EXISTS use_applied_amount BOOLEAN DEFAULT FALSE")
        conn.commit()
    except Exception:
        conn.rollback()
    # Add is_equipment flag — for stations/traps that are tracked but not consumed via FR sync
    try:
        cur.execute("ALTER TABLE products ADD COLUMN IF NOT EXISTS is_equipment BOOLEAN DEFAULT FALSE")
        conn.commit()
    except Exception:
        conn.rollback()
    # Add equipment_type: 'deployable' (sent to customers in bulk) | 'assigned' (1:1 with a tech)
    try:
        cur.execute("ALTER TABLE products ADD COLUMN IF NOT EXISTS equipment_type VARCHAR(20) DEFAULT 'deployable'")
        conn.commit()
    except Exception:
        conn.rollback()
    # Add photo_url for product/equipment photos shown in warehouse card popup
    try:
        cur.execute("ALTER TABLE products ADD COLUMN IF NOT EXISTS photo_url VARCHAR(500)")
        conn.commit()
    except Exception:
        conn.rollback()
    # show_storage_dist: force distribution tab to display in storage units
    try:
        cur.execute("ALTER TABLE products ADD COLUMN IF NOT EXISTS show_storage_dist BOOLEAN DEFAULT FALSE")
        conn.commit()
    except Exception:
        conn.rollback()
    # include_in_alerts on thresholds: lets equipment consumables opt out of low-stock alerts
    try:
        cur.execute("ALTER TABLE product_thresholds ADD COLUMN IF NOT EXISTS include_in_alerts BOOLEAN DEFAULT TRUE")
        conn.commit()
    except Exception:
        conn.rollback()
    # Add location column if not exists
    try:
        cur.execute("ALTER TABLE warehouse_inventory ADD COLUMN IF NOT EXISTS location VARCHAR(50) DEFAULT 'Holpers'")
        conn.commit()
    except Exception:
        conn.rollback()
    # Drop ALL unique constraints on warehouse_inventory — let location badge handle display
    for constraint in ['warehouse_inventory_product_id_unique', 'wh_product_location_unique']:
        try:
            cur.execute(f"ALTER TABLE warehouse_inventory DROP CONSTRAINT IF EXISTS {constraint}")
            conn.commit()
        except Exception:
            conn.rollback()
    # Add unique constraints for other tables
    for stmt in [
        'ALTER TABLE products ADD CONSTRAINT products_name_unique UNIQUE (name)',
        'ALTER TABLE technicians ADD CONSTRAINT technicians_name_unique UNIQUE (name)',
    ]:
        try:
            cur.execute(stmt)
            conn.commit()
        except Exception:
            conn.rollback()
    cur.close()
    conn.close()

# ─── AUTH ─────────────────────────────────────────────────────────────────────

def login_required(f):
    """Admin-only pages — blocks tech role."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        if session.get('role') == 'tech':
            return redirect(url_for('request_page'))
        return f(*args, **kwargs)
    return decorated

def any_login_required(f):
    """Any logged-in user (admin or tech)."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM users WHERE username=%s", (username,))
        user = cur.fetchone()
        cur.close()
        conn.close()
        if user and check_password_hash(user['password_hash'], password):
            session.permanent = True
            session['user_id'] = user['id']
            session['username'] = user['username']
            session['role'] = user['role']
            session['technician_id'] = user.get('technician_id')
            if user['role'] == 'tech':
                return redirect(url_for('request_page'))
            return redirect(url_for('index'))
        flash('Invalid credentials', 'error')
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

# ─── MAIN PAGES ───────────────────────────────────────────────────────────────

@app.route('/ping')
def ping():
    """Public keep-alive endpoint — hit by cron-job.org every 5 min to prevent cold starts."""
    return 'ok', 200

@app.route('/')
@login_required
def index():
    return render_template('index.html')

# ─── PWA ASSETS ───────────────────────────────────────────────────────────────

@app.route('/manifest.json')
def pwa_manifest():
    return send_from_directory('static', 'manifest.json', mimetype='application/manifest+json')

@app.route('/sw.js')
def pwa_sw():
    resp = send_from_directory('static', 'sw.js', mimetype='application/javascript')
    resp.headers['Service-Worker-Allowed'] = '/'
    return resp

# ─── TECH REQUEST PAGE ────────────────────────────────────────────────────────

@app.route('/r')
@any_login_required
def request_page():
    return render_template('request.html')

@app.route('/api/me', methods=['GET'])
@any_login_required
def whoami():
    """Identity of the logged-in user. A tech login linked to a technician
    returns that technician so the PWA can lock to them; the shared 'Tech'
    login returns no technician_id and the app keeps its name picker."""
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        SELECT u.username, u.role, u.technician_id, t.name AS technician_name
        FROM users u LEFT JOIN technicians t ON u.technician_id = t.id
        WHERE u.id = %s
    """, (session.get('user_id'),))
    row = cur.fetchone()
    cur.close(); conn.close()
    if not row:
        return jsonify({'error': 'Unknown user'}), 404
    return jsonify({
        'username': row['username'],
        'role': row['role'],
        'technician_id': row['technician_id'],
        'technician_name': row['technician_name'],
    })

@app.route('/api/requests', methods=['POST'])
@any_login_required
def submit_request():
    """Tech submits a digital inventory request."""
    d = request.json or {}
    tech_id = d.get('technician_id')
    items   = d.get('items', [])
    notes   = d.get('notes', '').strip()
    if not tech_id or not items:
        return jsonify({'error': 'technician_id and items required'}), 400
    conn = get_db(); cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO inventory_requests (technician_id, notes) VALUES (%s, %s) RETURNING id",
            (tech_id, notes or None)
        )
        req_id = cur.fetchone()['id']
        for item in items:
            pid  = item.get('product_id')
            qty  = float(item.get('quantity', 0))
            mode = item.get('unit_mode', 'usage')
            if not pid or qty <= 0:
                continue
            cur.execute(
                "INSERT INTO inventory_request_items (request_id, product_id, quantity_requested, unit_mode) VALUES (%s,%s,%s,%s)",
                (req_id, pid, qty, mode)
            )
        conn.commit()
        cur.close(); conn.close()
        return jsonify({'ok': True, 'request_id': req_id})
    except Exception as e:
        conn.rollback(); cur.close(); conn.close()
        return jsonify({'error': str(e)}), 500

@app.route('/api/requests/pending', methods=['GET'])
@login_required
def get_pending_requests():
    """Admin: list all pending requests with items."""
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        SELECT r.id, r.submitted_at, r.notes,
               t.id AS tech_id, t.name AS tech_name, t.truck_id
        FROM inventory_requests r
        JOIN technicians t ON r.technician_id = t.id
        WHERE r.status = 'pending'
        ORDER BY r.submitted_at ASC
    """)
    requests_list = [dict(r) for r in cur.fetchall()]
    if not requests_list:
        cur.close(); conn.close()
        return jsonify([])
    req_ids = [r['id'] for r in requests_list]
    cur.execute("""
        SELECT ri.id AS item_id, ri.request_id, ri.product_id,
               ri.quantity_requested, ri.unit_mode,
               p.name AS product_name, p.storage_unit, p.usage_unit,
               p.conversion_factor, p.conversion_note,
               COALESCE(p.is_equipment, FALSE) AS is_equipment
        FROM inventory_request_items ri
        JOIN products p ON ri.product_id = p.id
        WHERE ri.request_id = ANY(%s) AND ri.status = 'pending'
        ORDER BY ri.id
    """, (req_ids,))
    items = [dict(r) for r in cur.fetchall()]
    cur.close(); conn.close()
    for it in items:
        it['quantity_requested'] = float(it['quantity_requested'])
        it['conversion_factor']  = float(it['conversion_factor'])
    item_map = {}
    for it in items:
        item_map.setdefault(it['request_id'], []).append(it)
    for r in requests_list:
        r['submitted_at'] = (r['submitted_at'].isoformat() + 'Z') if r['submitted_at'] else None
        r['items'] = item_map.get(r['id'], [])
    # Only return requests that still have pending items
    return jsonify([r for r in requests_list if r['items']])

@app.route('/api/requests/<int:req_id>/approve', methods=['POST'])
@login_required
def approve_request(req_id):
    """
    Approve a pending request.
    Body: { items: [ {item_id, quantity_approved} ] }
    Executes a transfer for each item, shares one batch_id across the whole request.
    """
    d = request.json or {}
    approved_items = d.get('items', [])  # [{item_id, quantity_approved}]
    if not approved_items:
        return jsonify({'error': 'No items to approve'}), 400

    batch_id = str(uuid.uuid4())
    conn = get_db(); cur = conn.cursor()
    try:
        transferred = 0
        for ai in approved_items:
            item_id = ai['item_id']
            qty_approved = float(ai.get('quantity_approved', 0))
            if qty_approved <= 0:
                continue
            # Get item details
            cur.execute("""
                SELECT ri.product_id, ri.unit_mode, ri.request_id,
                       r.technician_id,
                       p.conversion_factor,
                       COALESCE(p.is_equipment, FALSE) AS is_equipment
                FROM inventory_request_items ri
                JOIN inventory_requests r ON ri.request_id = r.id
                JOIN products p ON ri.product_id = p.id
                WHERE ri.id = %s
            """, (item_id,))
            item = cur.fetchone()
            if not item:
                continue
            cf       = float(item['conversion_factor'])
            tech_id  = item['technician_id']
            prod_id  = item['product_id']
            is_equip = item['is_equipment']
            # qty_approved is now in storage units (bags, cans, etc.) — use directly
            storage_qty = qty_approved
            # Deduct from warehouse (Holpers)
            cur.execute("SELECT id FROM warehouse_inventory WHERE product_id=%s AND location='Holpers'", (prod_id,))
            if cur.fetchone():
                cur.execute("UPDATE warehouse_inventory SET quantity_storage_units=quantity_storage_units-%s, last_updated=NOW() WHERE product_id=%s AND location='Holpers'", (storage_qty, prod_id))
            else:
                cur.execute("INSERT INTO warehouse_inventory (product_id, location, quantity_storage_units) VALUES (%s,'Holpers',%s)", (prod_id, -storage_qty))
            # Add to tech truck (consumables only)
            if not is_equip:
                cur.execute("""
                    INSERT INTO tech_inventory (technician_id, product_id, quantity_storage_units)
                    VALUES (%s,%s,%s) ON CONFLICT (technician_id, product_id)
                    DO UPDATE SET quantity_storage_units=tech_inventory.quantity_storage_units+%s, last_updated=NOW()
                """, (tech_id, prod_id, storage_qty, storage_qty))
            # Log transaction
            txn_type = 'equipment_provided' if is_equip else 'transfer'
            cur.execute("""
                INSERT INTO inventory_transactions
                (transaction_type, product_id, technician_id, quantity_storage_units,
                 notes, performed_by, transfer_batch_id)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
            """, (txn_type, prod_id, tech_id, storage_qty,
                  f'[Holpers] Approved from request #{req_id}',
                  session.get('username'), batch_id))
            # Mark item approved
            cur.execute("UPDATE inventory_request_items SET status='approved' WHERE id=%s", (item_id,))
            transferred += 1
        # Mark the request approved
        cur.execute("""
            UPDATE inventory_requests SET status='approved', reviewed_at=NOW(), reviewed_by=%s
            WHERE id=%s
        """, (session.get('username'), req_id))
        conn.commit()
        cur.close(); conn.close()
        return jsonify({'ok': True, 'transferred': transferred, 'batch_id': batch_id})
    except Exception as e:
        conn.rollback(); cur.close(); conn.close()
        return jsonify({'error': str(e)}), 500

@app.route('/api/requests/<int:req_id>/items/<int:item_id>', methods=['DELETE'])
@login_required
def remove_request_item(req_id, item_id):
    """Remove a single line item from a pending request (before approval)."""
    conn = get_db(); cur = conn.cursor()
    cur.execute("UPDATE inventory_request_items SET status='removed' WHERE id=%s AND request_id=%s", (item_id, req_id))
    # If every item on this request is now removed, cancel the whole request
    cur.execute("""
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN status='removed' THEN 1 ELSE 0 END) AS removed_count
        FROM inventory_request_items
        WHERE request_id=%s
    """, (req_id,))
    row = cur.fetchone()
    if row and row['total'] > 0 and row['total'] == row['removed_count']:
        cur.execute("""
            UPDATE inventory_requests
            SET status='cancelled', reviewed_at=NOW(), reviewed_by=%s
            WHERE id=%s
        """, (session.get('username'), req_id))
    conn.commit(); cur.close(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/requests/history', methods=['GET'])
@any_login_required
def get_request_history():
    """Return all requests for a specific tech, newest first, with items."""
    tech_id = request.args.get('tech_id', type=int)
    limit   = min(int(request.args.get('limit', 50)), 200)
    offset  = int(request.args.get('offset', 0))
    if not tech_id:
        return jsonify({'error': 'tech_id required'}), 400
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        SELECT r.id, r.status, r.notes, r.submitted_at, r.reviewed_at, r.reviewed_by
        FROM inventory_requests r
        WHERE r.technician_id = %s
        ORDER BY r.submitted_at DESC
        LIMIT %s OFFSET %s
    """, (tech_id, limit, offset))
    requests_list = [dict(r) for r in cur.fetchall()]
    if not requests_list:
        cur.close(); conn.close()
        return jsonify({'requests': [], 'total': 0})
    req_ids = [r['id'] for r in requests_list]
    cur.execute("""
        SELECT ri.request_id, ri.id AS item_id, ri.product_id, ri.status,
               ri.quantity_requested, ri.unit_mode,
               p.name AS product_name, p.usage_unit, p.storage_unit,
               p.conversion_factor
        FROM inventory_request_items ri
        JOIN products p ON ri.product_id = p.id
        WHERE ri.request_id = ANY(%s)
        ORDER BY ri.id
    """, (req_ids,))
    items = [dict(r) for r in cur.fetchall()]
    cur.execute("SELECT COUNT(*) AS n FROM inventory_requests WHERE technician_id = %s", (tech_id,))
    total = cur.fetchone()['n']
    cur.close(); conn.close()
    item_map = {}
    for it in items:
        it['quantity_requested'] = float(it['quantity_requested'])
        it['conversion_factor']  = float(it['conversion_factor'])
        item_map.setdefault(it['request_id'], []).append(it)
    for r in requests_list:
        r['submitted_at'] = (r['submitted_at'].isoformat() + 'Z') if r['submitted_at'] else None
        r['reviewed_at']  = (r['reviewed_at'].isoformat()  + 'Z') if r['reviewed_at']  else None
        r['items'] = item_map.get(r['id'], [])
    return jsonify({'requests': requests_list, 'total': total})

# ─── API: TRUCK PAR LEVELS (per-tech minimums) ────────────────────────────────

@app.route('/api/truck-par', methods=['GET'])
@login_required
def get_truck_par():
    """Admin: a tech's par levels (min qty per product) + current truck qty."""
    tech_id = request.args.get('tech_id', type=int)
    if not tech_id:
        return jsonify({'error': 'tech_id required'}), 400
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        SELECT tp.product_id, tp.min_qty, tp.alert_qty,
               p.name AS product_name, p.storage_unit, p.usage_unit,
               COALESCE(ti.quantity_storage_units, 0) AS current_qty
        FROM truck_par_levels tp
        JOIN products p ON tp.product_id = p.id
        LEFT JOIN tech_inventory ti ON ti.technician_id = tp.technician_id AND ti.product_id = tp.product_id
        WHERE tp.technician_id = %s AND p.active = TRUE
        ORDER BY p.name
    """, (tech_id,))
    pars = [dict(r) for r in cur.fetchall()]
    cur.close(); conn.close()
    for p in pars:
        p['min_qty'] = float(p['min_qty'] or 0)
        p['alert_qty'] = float(p['alert_qty']) if p['alert_qty'] is not None else p['min_qty']
        p['current_qty'] = float(p['current_qty'] or 0)
    return jsonify({'pars': pars})

@app.route('/api/truck-par', methods=['POST'])
@login_required
def set_truck_par():
    """Admin: set/clear one tech's min for one product. min_qty<=0 clears it."""
    d = request.json or {}
    tech_id = d.get('technician_id')
    product_id = d.get('product_id')
    if not tech_id or not product_id:
        return jsonify({'error': 'technician_id and product_id required'}), 400
    try:
        min_qty = float(d.get('min_qty', 0))
    except (TypeError, ValueError):
        return jsonify({'error': 'min_qty must be a number'}), 400
    alert_qty = d.get('alert_qty')
    try:
        alert_qty = float(alert_qty) if alert_qty not in (None, '') else None
    except (TypeError, ValueError):
        return jsonify({'error': 'alert_qty must be a number'}), 400
    conn = get_db(); cur = conn.cursor()
    try:
        if min_qty > 0 or (alert_qty or 0) > 0:
            cur.execute("""
                INSERT INTO truck_par_levels (technician_id, product_id, min_qty, alert_qty)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (technician_id, product_id)
                DO UPDATE SET min_qty = EXCLUDED.min_qty, alert_qty = EXCLUDED.alert_qty, updated_at = NOW()
            """, (tech_id, product_id, min_qty, alert_qty))
        else:
            cur.execute("DELETE FROM truck_par_levels WHERE technician_id=%s AND product_id=%s", (tech_id, product_id))
        conn.commit(); cur.close(); conn.close()
        return jsonify({'ok': True})
    except Exception as e:
        conn.rollback(); cur.close(); conn.close()
        return jsonify({'error': str(e)}), 500

@app.route('/api/truck-par/bulk', methods=['POST'])
@login_required
def set_truck_par_bulk():
    """Admin: set one product's min for ALL active techs at once."""
    d = request.json or {}
    product_id = d.get('product_id')
    if not product_id:
        return jsonify({'error': 'product_id required'}), 400
    try:
        min_qty = float(d.get('min_qty', 0))
    except (TypeError, ValueError):
        return jsonify({'error': 'min_qty must be a number'}), 400
    conn = get_db(); cur = conn.cursor()
    try:
        cur.execute("SELECT id FROM technicians WHERE active=TRUE")
        tech_ids = [r['id'] for r in cur.fetchall()]
        for tid in tech_ids:
            if min_qty > 0:
                cur.execute("""
                    INSERT INTO truck_par_levels (technician_id, product_id, min_qty)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (technician_id, product_id)
                    DO UPDATE SET min_qty = EXCLUDED.min_qty, updated_at = NOW()
                """, (tid, product_id, min_qty))
            else:
                cur.execute("DELETE FROM truck_par_levels WHERE technician_id=%s AND product_id=%s", (tid, product_id))
        conn.commit(); cur.close(); conn.close()
        return jsonify({'ok': True, 'techs_updated': len(tech_ids)})
    except Exception as e:
        conn.rollback(); cur.close(); conn.close()
        return jsonify({'error': str(e)}), 500

@app.route('/api/truck/recommendations', methods=['GET'])
@any_login_required
def truck_recommendations():
    """
    PWA: for the selected tech, split their par-level products into 'low'
    (current below min) and 'carry' (at/above min) so the app can suggest
    restocks. Purely read-only — does not touch inventory.
    """
    tech_id = request.args.get('tech_id', type=int)
    if not tech_id:
        return jsonify({'error': 'tech_id required'}), 400
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        SELECT tp.product_id, tp.min_qty, tp.alert_qty,
               p.name AS product_name, p.storage_unit, p.usage_unit, p.conversion_factor,
               COALESCE(p.is_equipment, FALSE) AS is_equipment,
               COALESCE(ti.quantity_storage_units, 0) AS current_qty
        FROM truck_par_levels tp
        JOIN products p ON tp.product_id = p.id
        LEFT JOIN tech_inventory ti ON ti.technician_id = tp.technician_id AND ti.product_id = tp.product_id
        WHERE tp.technician_id = %s AND p.active = TRUE
          AND (tp.min_qty > 0 OR COALESCE(tp.alert_qty, 0) > 0)
        ORDER BY p.name
    """, (tech_id,))
    rows = [dict(r) for r in cur.fetchall()]
    cur.close(); conn.close()
    low, carry = [], []
    for r in rows:
        min_q = float(r['min_qty'] or 0)
        cur_q = float(r['current_qty'] or 0)
        # Restock alert fires at/below alert_qty; if none set, fall back to min.
        alert_q = float(r['alert_qty']) if r['alert_qty'] is not None else min_q
        item = {
            'product_id': r['product_id'], 'product_name': r['product_name'],
            'storage_unit': r['storage_unit'], 'usage_unit': r['usage_unit'],
            'conversion_factor': float(r['conversion_factor'] or 1),
            'is_equipment': r['is_equipment'],
            'min_qty': round(min_q, 4), 'alert_qty': round(alert_q, 4), 'current_qty': round(cur_q, 4),
            'suggested_qty': round(max(min_q - cur_q, 0), 4),
        }
        (low if cur_q <= alert_q else carry).append(item)
    return jsonify({'low': low, 'carry': carry})

# ─── API: PRODUCTS ────────────────────────────────────────────────────────────

@app.route('/api/products', methods=['GET'])
@any_login_required
def get_products():
    conn = get_db()
    cur = conn.cursor()
    include_all = request.args.get('all', '').lower() in ('1', 'true')
    where = "" if include_all else "WHERE active=TRUE"
    cur.execute(f"SELECT *, COALESCE(cost_per_storage_unit, 0) as cost_per_storage_unit, COALESCE(use_applied_amount, FALSE) as use_applied_amount, COALESCE(is_equipment, FALSE) as is_equipment, COALESCE(equipment_type, 'deployable') as equipment_type, COALESCE(photo_url, '') as photo_url, COALESCE(show_storage_dist, FALSE) as show_storage_dist FROM products {where} ORDER BY name")
    products = [dict(r) for r in cur.fetchall()]
    cur.close()
    conn.close()
    return jsonify(products)

@app.route('/api/products', methods=['POST'])
@login_required
def create_product():
    d = request.json
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO products (name, fr_product_id, storage_unit, usage_unit, conversion_factor, conversion_note,
                                  cost_per_storage_unit, use_applied_amount, is_equipment, equipment_type, photo_url)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *
        """, (d['name'], d.get('fr_product_id'), d['storage_unit'], d['usage_unit'],
              d['conversion_factor'], d.get('conversion_note'),
              d.get('cost_per_storage_unit', 0),
              bool(d.get('use_applied_amount', False)),
              bool(d.get('is_equipment', False)),
              d.get('equipment_type', 'deployable'),
              d.get('photo_url') or None))
        product = dict(cur.fetchone())
        # Create warehouse slot
        cur.execute("INSERT INTO warehouse_inventory (product_id, quantity_storage_units) VALUES (%s, 0)", (product['id'],))
        conn.commit()
        kind = 'equipment' if d.get('is_equipment') else 'product'
        log_system_event('admin', f'Added {kind}: {product["name"]}', performed_by=session.get('username'))
        return jsonify(product)
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        cur.close()
        conn.close()

@app.route('/api/products/<int:pid>', methods=['PUT'])
@login_required
def update_product(pid):
    d = request.json
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("""
            UPDATE products SET name=%s, fr_product_id=%s, storage_unit=%s, usage_unit=%s,
            conversion_factor=%s, conversion_note=%s, cost_per_storage_unit=%s,
            use_applied_amount=%s, is_equipment=%s, equipment_type=%s, photo_url=%s,
            show_storage_dist=%s WHERE id=%s RETURNING *
        """, (d['name'], d.get('fr_product_id'), d['storage_unit'], d['usage_unit'],
              d['conversion_factor'], d.get('conversion_note'),
              d.get('cost_per_storage_unit', 0),
              bool(d.get('use_applied_amount', False)),
              bool(d.get('is_equipment', False)),
              d.get('equipment_type', 'deployable'),
              d.get('photo_url') or None,
              bool(d.get('show_storage_dist', False)), pid))
        row = cur.fetchone()
        if row is None:
            conn.rollback()
            return jsonify({'error': f'Product {pid} not found'}), 404
        product = dict(row)
        conn.commit()
        return jsonify(product)
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        cur.close()
        conn.close()

@app.route('/api/products/<int:pid>', methods=['DELETE'])
@login_required
def delete_product(pid):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE products SET active=FALSE WHERE id=%s", (pid,))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({'ok': True})

@app.route('/api/products/<int:pid>/restore', methods=['POST'])
@login_required
def restore_product(pid):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE products SET active=TRUE WHERE id=%s RETURNING *", (pid,))
    row = dict(cur.fetchone())
    conn.commit()
    cur.close()
    conn.close()
    return jsonify(row)

@app.route('/api/products/<int:pid>/delete-inventory', methods=['POST'])
@login_required
def delete_product_inventory(pid):
    """Hard delete a product and all its inventory records."""
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("DELETE FROM tech_inventory WHERE product_id=%s", (pid,))
        cur.execute("DELETE FROM warehouse_inventory WHERE product_id=%s", (pid,))
        cur.execute("DELETE FROM product_thresholds WHERE product_id=%s", (pid,))
        cur.execute("DELETE FROM inventory_transactions WHERE product_id=%s", (pid,))
        cur.execute("DELETE FROM audit_log WHERE product_id=%s", (pid,))
        cur.execute("DELETE FROM products WHERE id=%s", (pid,))
        conn.commit()
        cur.close(); conn.close()
        return jsonify({'ok': True})
    except Exception as e:
        conn.rollback(); cur.close(); conn.close()
        return jsonify({'error': str(e)}), 500

@app.route('/api/products/hidden', methods=['GET'])
@login_required
def get_hidden_products():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM products WHERE active=FALSE ORDER BY name")
    rows = [dict(r) for r in cur.fetchall()]
    cur.close()
    conn.close()
    return jsonify(rows)

# ─── API: TECHNICIANS ─────────────────────────────────────────────────────────

@app.route('/api/technicians', methods=['GET'])
@any_login_required
def get_technicians():
    conn = get_db()
    cur = conn.cursor()
    include_all = request.args.get('all', '').lower() in ('1', 'true')
    where = "" if include_all else "WHERE active=TRUE"
    cur.execute(f"SELECT * FROM technicians {where} ORDER BY name")
    techs = [dict(r) for r in cur.fetchall()]
    cur.close()
    conn.close()
    return jsonify(techs)

@app.route('/api/technicians', methods=['POST'])
@login_required
def create_technician():
    d = request.json
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO technicians (name, fr_employee_id, truck_id)
        VALUES (%s, %s, %s) RETURNING *
    """, (d['name'], d.get('fr_employee_id'), d.get('truck_id')))
    tech = dict(cur.fetchone())
    conn.commit()
    cur.close()
    conn.close()
    return jsonify(tech)

@app.route('/api/technicians/<int:tid>', methods=['PUT'])
@login_required
def update_technician(tid):
    d = request.json
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        UPDATE technicians SET name=%s, fr_employee_id=%s, truck_id=%s WHERE id=%s RETURNING *
    """, (d['name'], d.get('fr_employee_id'), d.get('truck_id'), tid))
    tech = dict(cur.fetchone())
    conn.commit()
    cur.close()
    conn.close()
    return jsonify(tech)

@app.route('/api/technicians/<int:tid>/toggle-active', methods=['POST'])
@login_required
def toggle_tech_active(tid):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE technicians SET active = NOT active WHERE id=%s RETURNING id, name, active", (tid,))
    row = dict(cur.fetchone())
    conn.commit()
    cur.close()
    conn.close()
    return jsonify(row)

@app.route('/api/technicians/hidden', methods=['GET'])
@login_required
def get_hidden_technicians():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM technicians WHERE active=FALSE ORDER BY name")
    techs = [dict(r) for r in cur.fetchall()]
    cur.close(); conn.close()
    return jsonify(techs)

@app.route('/api/technicians/<int:tid>/restore', methods=['POST'])
@login_required
def restore_technician(tid):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE technicians SET active=TRUE WHERE id=%s RETURNING id, name, active", (tid,))
    tech = dict(cur.fetchone())
    conn.commit()
    cur.close(); conn.close()
    return jsonify(tech)

@app.route('/api/technicians/<int:tid>', methods=['DELETE'])
@login_required
def delete_technician(tid):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE technicians SET active=FALSE WHERE id=%s", (tid,))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({'ok': True})

# ─── API: WAREHOUSE INVENTORY ─────────────────────────────────────────────────

@app.route('/api/debug/warehouse-raw', methods=['GET'])
@login_required
def debug_warehouse_raw():
    """
    Temporary diagnostic: dump every raw row touching one product's warehouse
    balance — warehouse_inventory rows (to catch duplicates), inventory_transactions
    (receive/transfer/equipment/location_transfer), and audit_log (Set Count) —
    so a discrepancy can be traced by hand instead of guessed at.
    """
    product_id = request.args.get('product_id', type=int)
    name = request.args.get('name', '').strip()
    conn = get_db(); cur = conn.cursor()

    if not product_id and name:
        cur.execute("SELECT id, name FROM products WHERE name ILIKE %s", (f'%{name}%',))
        matches = [dict(r) for r in cur.fetchall()]
        if len(matches) != 1:
            cur.close(); conn.close()
            return jsonify({'error': 'product_id required, or name must match exactly one product', 'matches': matches}), 400
        product_id = matches[0]['id']

    if not product_id:
        cur.close(); conn.close()
        return jsonify({'error': 'product_id or name required'}), 400

    cur.execute("SELECT id, name, storage_unit, usage_unit, conversion_factor, COALESCE(use_applied_amount, FALSE) AS use_applied_amount FROM products WHERE id=%s", (product_id,))
    prod = dict(cur.fetchone() or {})
    prod['conversion_factor'] = float(prod['conversion_factor']) if prod.get('conversion_factor') is not None else None
    _GRANULAR = {'units','unit','each','piece','pieces','number',
                 'blox','block','blocks','pack','packs','place pack','place packs'}
    prod['whole_unit_rounding_applies'] = (str(prod.get('usage_unit','')).lower().strip() in _GRANULAR) or (prod.get('conversion_factor') == 1)

    cur.execute("SELECT id, product_id, location, quantity_storage_units, last_updated FROM warehouse_inventory WHERE product_id=%s ORDER BY location, id", (product_id,))
    warehouse_rows = [dict(r) for r in cur.fetchall()]
    for r in warehouse_rows:
        r['quantity_storage_units'] = float(r['quantity_storage_units'])
        r['last_updated'] = r['last_updated'].isoformat() if r['last_updated'] else None

    cur.execute("""
        SELECT id, transaction_type, technician_id, quantity_storage_units, quantity_usage_units, notes, performed_by, created_at
        FROM inventory_transactions WHERE product_id=%s ORDER BY created_at
    """, (product_id,))
    txns = [dict(r) for r in cur.fetchall()]
    for r in txns:
        r['quantity_storage_units'] = float(r['quantity_storage_units'] or 0)
        r['quantity_usage_units'] = float(r['quantity_usage_units']) if r['quantity_usage_units'] is not None else None
        r['created_at'] = r['created_at'].isoformat() if r['created_at'] else None

    # Flag usage_sync deductions that came out fractional even though this product
    # should only ever move in whole units (granular usage_unit or conversion_factor=1).
    fractional_usage = [
        t for t in txns
        if t['transaction_type'] == 'usage_sync'
        and abs(t['quantity_storage_units'] - round(t['quantity_storage_units'])) > 0.0001
    ]

    cur.execute("""
        SELECT id, audit_type, old_quantity, new_quantity, reason, performed_by, created_at
        FROM audit_log WHERE product_id=%s ORDER BY created_at
    """, (product_id,))
    audits = [dict(r) for r in cur.fetchall()]
    for r in audits:
        r['old_quantity'] = float(r['old_quantity']) if r['old_quantity'] is not None else None
        r['new_quantity'] = float(r['new_quantity']) if r['new_quantity'] is not None else None
        r['created_at'] = r['created_at'].isoformat() if r['created_at'] else None

    cur.close(); conn.close()
    return jsonify({
        'product': prod,
        'warehouse_rows': warehouse_rows,
        'warehouse_row_count': len(warehouse_rows),
        'duplicate_locations': [loc for loc in set(r['location'] for r in warehouse_rows)
                                if sum(1 for r in warehouse_rows if r['location'] == loc) > 1],
        'usage_sync_count': sum(1 for t in txns if t['transaction_type'] == 'usage_sync'),
        'fractional_usage_sync_count': len(fractional_usage),
        'fractional_usage_sync_sample': fractional_usage[:15],
        'transactions': txns,
        'audit_log': audits,
    })

@app.route('/api/warehouse', methods=['GET'])
@login_required
def get_warehouse():
    conn = get_db()
    cur = conn.cursor()
    location = request.args.get('location')
    cur.execute("""
        SELECT w.id, w.product_id, w.quantity_storage_units, w.last_updated,
               w.location,
               p.name AS product_name, p.storage_unit, p.usage_unit,
               p.conversion_factor, p.conversion_note,
               COALESCE(p.cost_per_storage_unit, 0) AS cost_per_storage_unit,
               COALESCE(p.cost_per_storage_unit, 0) * w.quantity_storage_units AS total_value,
               COALESCE(p.is_equipment, FALSE) AS is_equipment
        FROM warehouse_inventory w
        JOIN products p ON w.product_id = p.id
        WHERE p.active = TRUE
        ORDER BY p.name, w.location
    """)
    rows = [dict(r) for r in cur.fetchall()]
    cur.close()
    conn.close()
    return jsonify(rows)

@app.route('/api/warehouse/receive', methods=['POST'])
@login_required
def receive_inventory():
    """Add inventory to warehouse."""
    d = request.json
    product_id = d['product_id']
    quantity = float(d['quantity'])  # in storage units
    notes = d.get('notes', '')
    location = d.get('location', 'Holpers')  # default to Holpers

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT name, storage_unit FROM products WHERE id=%s", (product_id,))
    prod = cur.fetchone()
    prod_name = prod['name'] if prod else f'Product #{product_id}'
    prod_unit = prod['storage_unit'] if prod else ''
    cur.execute("SELECT id FROM warehouse_inventory WHERE product_id=%s AND location=%s", (product_id, location))
    if cur.fetchone():
        cur.execute("UPDATE warehouse_inventory SET quantity_storage_units=quantity_storage_units+%s, last_updated=NOW() WHERE product_id=%s AND location=%s", (quantity, product_id, location))
    else:
        cur.execute("INSERT INTO warehouse_inventory (product_id, location, quantity_storage_units) VALUES (%s,%s,%s)", (product_id, location, quantity))
    cur.execute("""
        INSERT INTO inventory_transactions
        (transaction_type, product_id, quantity_storage_units, notes, performed_by)
        VALUES ('receive', %s, %s, %s, %s)
    """, (product_id, quantity, f"[{location}] {notes}", session.get('username')))
    conn.commit()
    cur.close()
    conn.close()
    log_system_event('receive', f'Received {quantity:g} {prod_unit} of {prod_name} → {location}' + (f' ({notes})' if notes else ''), performed_by=session.get('username'))
    return jsonify({'ok': True})

# ─── OLDHAM ORDER PDF IMPORT (auto-receive) ───────────────────────────────────

_OLDHAM_REC = re.compile(
    r'(?P<sku>\d{3}-[0-9A-Za-z][0-9A-Za-z \-]*?)\s*\$(?P<price>[\d,]+\.\d{2})\s+(?P<qty>\d+)\s+\$(?P<total>[\d,]+\.\d{2})'
)

def _oldham_pack_multiplier(name):
    """How many storage units one ordered line-item equals, from the pack text.

    A number that is immediately followed by an 'x' is a *count* and is
    multiplied in; a trailing number followed by a unit of measure is the item
    *size* and is ignored. Also handles 'N per case'.
      '6 x 21 oz.'       -> 6     (21 oz is the size)
      '5 x 4 x 30 gm.'   -> 20    (5 boxes x 4 each)
      '12 x 17-oz. can'  -> 12
      '4 x 1-gal.'       -> 4
      '16 per case'      -> 16
      '25-lb. bag' / '72 count' -> 1
    """
    s = name or ''
    counts = re.findall(r'(\d+)\s*[xX×]', s)   # numbers being multiplied
    if counts:
        mult = 1
        for c in counts:
            mult *= int(c)
        return mult
    m = re.search(r'(\d+)\s*(?:per|/)\s*(?:case|cs|carton|box|pack)\b', s, re.I)
    if m:
        return int(m.group(1))
    return 1

def _norm_match(s):
    s = re.sub(r'\([^)]*\)', ' ', s or '')      # drop the (pack) parenthetical
    s = re.sub(r'[^0-9a-zA-Z]+', ' ', s).lower()
    return re.sub(r'\s+', ' ', s).strip()

# Words that describe *what kind* of product it is, not *which* product. A match
# on these alone (e.g. two different "… Insecticide"s) must not look confident.
_GENERIC_TOKENS = {
    'insecticide', 'insecticides', 'miticide', 'rodenticide', 'termiticide',
    'fungicide', 'herbicide', 'pesticide', 'bait', 'baits', 'gel', 'spray',
    'sprayer', 'concentrate', 'dust', 'granules', 'granular', 'aerosol', 'rtu',
    'trap', 'traps', 'glue', 'glueboard', 'board', 'boards', 'station', 'stations',
    'tube', 'tubes', 'kit', 'professional', 'pro', 'max', 'plus', 'and', 'with',
    'for', 'the', 'oz', 'lb', 'lbs', 'gal', 'gallon', 'bag', 'case', 'box', 'jug',
    'bottle', 'can', 'cans', 'pack', 'count', 'ct', 'ii', 'iii',
    'per', 'carton', 'cs',
}

def _brand_tokens(s):
    """The distinctive words in a name — drops generic descriptors and numbers."""
    return [t for t in _norm_match(s).split() if t and t not in _GENERIC_TOKENS and not t.isdigit()]

def _fuzzy_match_product(name, products):
    """Best product match for an Oldham line name. Returns (product, score 0..1).
    Brand-token overlap gates the score, so a shared generic word ('Insecticide')
    can't manufacture a confident wrong match — an unmatched line is safer than a
    wrong one (it just stays blank for the user to fill in)."""
    target = _norm_match(name)
    t_brand = set(_brand_tokens(name))
    best, best_score = None, 0.0
    for p in products:
        ratio = difflib.SequenceMatcher(None, target, _norm_match(p['name'])).ratio()
        p_brand = set(_brand_tokens(p['name']))
        shared = t_brand & p_brand
        if shared:
            if t_brand <= p_brand or p_brand <= t_brand:
                # one name's distinctive words are wholly inside the other, so
                # "Yard Guard Animal Deter 40Lb" still matches the product "Yard Guard"
                overlap = 1.0
            else:
                # only a partial brand overlap (e.g. sharing just a family word like
                # "Catchmaster") — weak; don't let it manufacture a confident match
                overlap = len(shared) / len(t_brand)
            score = 0.55 * overlap + 0.45 * ratio
        else:
            score = 0.40 * ratio   # no brand word in common → can't cross the auto-select bar
        if score > best_score:
            best, best_score = p, score
    return best, round(best_score, 2)

def _oldham_parse_pdf(data):
    """Extract order number + line items from an Oldham order PDF (bytes)."""
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data))
    txt = "\n".join((pg.extract_text() or "") for pg in reader.pages)
    order = re.search(r'Order#\s*(\d+)', txt)
    order_no = order.group(1) if order else None
    date = re.search(r'Date:\s*(.+)', txt)
    order_date = date.group(1).strip() if date else None
    start = txt.find("Name SKU Price Qty Total")
    block = txt[start + len("Name SKU Price Qty Total"):] if start >= 0 else txt
    for stop in ("Special Instruction", "Sub-total", "Sub-Total"):
        i = block.find(stop)
        if i >= 0:
            block = block[:i]
    block = re.sub(r'\s+', ' ', block).strip()
    items, prev = [], 0
    for m in _OLDHAM_REC.finditer(block):
        name = block[prev:m.start()].strip()
        prev = m.end()
        items.append({
            'name': name,
            'sku': m.group('sku').replace(' ', ''),
            'price': float(m.group('price').replace(',', '')),
            'qty': int(m.group('qty')),
            'total': float(m.group('total').replace(',', '')),
        })
    return {'order_no': order_no, 'order_date': order_date, 'items': items}

@app.route('/api/oldham/parse', methods=['POST'])
@login_required
def oldham_parse():
    """Parse an uploaded Oldham order PDF and pre-match/guess each line."""
    f = request.files.get('file')
    if not f:
        return jsonify({'error': 'No file uploaded'}), 400
    try:
        parsed = _oldham_parse_pdf(f.read())
    except Exception as e:
        return jsonify({'error': f'Could not read PDF: {e}'}), 400

    conn = get_db(); cur = conn.cursor()
    cur.execute("""SELECT id, name, storage_unit, COALESCE(cost_per_storage_unit,0) AS cost_per_storage_unit
                   FROM products WHERE active=TRUE AND COALESCE(is_equipment,FALSE)=FALSE""")
    products = [dict(r) for r in cur.fetchall()]
    cur.execute("SELECT oldham_sku, product_id FROM oldham_sku_map")
    sku_map = {r['oldham_sku']: r['product_id'] for r in cur.fetchall()}
    prod_by_id = {p['id']: p for p in products}
    cur.close(); conn.close()

    lines = []
    for it in parsed['items']:
        mult = _oldham_pack_multiplier(it['name'])
        suggested_qty = it['qty'] * mult
        # match: remembered SKU first, else fuzzy by name
        matched, score, source = None, None, None
        if it['sku'] in sku_map and sku_map[it['sku']] in prod_by_id:
            matched = prod_by_id[sku_map[it['sku']]]; score = 1.0; source = 'remembered'
        else:
            best, best_score = _fuzzy_match_product(it['name'], products)
            if best and best_score >= 0.5:
                matched, score, source = best, best_score, 'name'
        unit_cost = round(it['total'] / suggested_qty, 4) if suggested_qty else None
        lines.append({
            'sku': it['sku'], 'oldham_name': it['name'], 'price': it['price'],
            'order_qty': it['qty'], 'total': it['total'], 'pack_multiplier': mult,
            'suggested_qty': suggested_qty, 'suggested_unit_cost': unit_cost,
            'product_id': matched['id'] if matched else None,
            'product_name': matched['name'] if matched else None,
            'storage_unit': matched['storage_unit'] if matched else None,
            'match_score': score, 'match_source': source,
        })
    return jsonify({'order_no': parsed['order_no'], 'order_date': parsed['order_date'], 'lines': lines})

@app.route('/api/warehouse/receive-bulk', methods=['POST'])
@login_required
def receive_bulk():
    """Receive several products into the warehouse in one pass.

    Used by the Receive tab's multi-row form and by the Oldham order import.
    Each line: {product_id, quantity (storage units), location?, notes?,
                oldham_sku?, oldham_name?, update_cost?, unit_cost?}.
    A line without a product_id (unmatched/untracked) is skipped. When a line
    carries an oldham_sku the SKU→product mapping is remembered for next time,
    and (if update_cost) the product's cost is refreshed from the invoice."""
    d = request.json or {}
    lines = d.get('lines', [])
    order_no = (d.get('order_no') or '').strip()
    received, summary = 0, []
    conn = get_db(); cur = conn.cursor()
    try:
        for ln in lines:
            pid = ln.get('product_id')
            if not pid:
                continue  # unmatched / untracked line — skip
            try:
                qty = float(ln.get('quantity', ln.get('qty', 0)))
            except (TypeError, ValueError):
                continue
            if qty <= 0:
                continue
            location = ln.get('location') or 'Holpers'
            sku = (ln.get('oldham_sku') or ln.get('sku') or '').strip()
            base_note = (ln.get('notes') or '').strip()
            parts = []
            if base_note:
                parts.append(base_note)
            if order_no:
                parts.append(f'Oldham order #{order_no}')
            if sku:
                parts.append(f'SKU {sku}')
            note = f"[{location}] " + ' · '.join(parts)
            # remember the SKU → product mapping for next time
            if sku:
                cur.execute("""
                    INSERT INTO oldham_sku_map (oldham_sku, product_id, oldham_name)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (oldham_sku)
                    DO UPDATE SET product_id=EXCLUDED.product_id, oldham_name=EXCLUDED.oldham_name, updated_at=NOW()
                """, (sku, pid, ln.get('oldham_name')))
            cur.execute("SELECT name, storage_unit FROM products WHERE id=%s", (pid,))
            prod = cur.fetchone()
            prod_name = prod['name'] if prod else f'Product #{pid}'
            prod_unit = prod['storage_unit'] if prod else ''
            # receive into warehouse (same pattern as the single-product receive)
            cur.execute("SELECT id FROM warehouse_inventory WHERE product_id=%s AND location=%s", (pid, location))
            if cur.fetchone():
                cur.execute("UPDATE warehouse_inventory SET quantity_storage_units=quantity_storage_units+%s, last_updated=NOW() WHERE product_id=%s AND location=%s", (qty, pid, location))
            else:
                cur.execute("INSERT INTO warehouse_inventory (product_id, location, quantity_storage_units) VALUES (%s,%s,%s)", (pid, location, qty))
            cur.execute("""
                INSERT INTO inventory_transactions (transaction_type, product_id, quantity_storage_units, notes, performed_by)
                VALUES ('receive', %s, %s, %s, %s)
            """, (pid, qty, note, session.get('username')))
            # optional cost refresh from the invoice
            if ln.get('update_cost') and ln.get('unit_cost') not in (None, ''):
                try:
                    cur.execute("UPDATE products SET cost_per_storage_unit=%s WHERE id=%s", (float(ln['unit_cost']), pid))
                except (TypeError, ValueError):
                    pass
            received += 1
            summary.append(f'{qty:g} {prod_unit} {prod_name} → {location}')
        conn.commit(); cur.close(); conn.close()
        label = (f'Oldham order #{order_no} imported — ' if order_no else 'Bulk receive — ') + \
                f'{received} product{"s" if received != 1 else ""} received'
        log_system_event('receive', label, performed_by=session.get('username'))
        return jsonify({'ok': True, 'received': received, 'items': summary})
    except Exception as e:
        conn.rollback(); cur.close(); conn.close()
        return jsonify({'error': str(e)}), 500

# ─── OLDHAM SKU ↔ PRODUCT MAPPINGS (view/manage in the product editor) ─────────

@app.route('/api/products/<int:pid>/skus', methods=['GET'])
@login_required
def product_skus(pid):
    """List the Oldham order SKU(s) mapped to a product."""
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT oldham_sku AS sku, oldham_name AS name FROM oldham_sku_map WHERE product_id=%s ORDER BY oldham_sku", (pid,))
    rows = [dict(r) for r in cur.fetchall()]
    cur.close(); conn.close()
    return jsonify(rows)

@app.route('/api/products/<int:pid>/skus', methods=['POST'])
@login_required
def product_sku_add(pid):
    """Map an Oldham SKU to this product (reassigns it if it was on another)."""
    d = request.json or {}
    sku = (d.get('sku') or '').strip()
    if not sku:
        return jsonify({'error': 'SKU is required'}), 400
    name = (d.get('name') or '').strip() or None
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        INSERT INTO oldham_sku_map (oldham_sku, product_id, oldham_name)
        VALUES (%s, %s, %s)
        ON CONFLICT (oldham_sku)
        DO UPDATE SET product_id=EXCLUDED.product_id,
                      oldham_name=COALESCE(EXCLUDED.oldham_name, oldham_sku_map.oldham_name),
                      updated_at=NOW()
    """, (sku, pid, name))
    conn.commit(); cur.close(); conn.close()
    log_system_event('admin', f'Mapped Oldham SKU {sku} → product #{pid}', performed_by=session.get('username'))
    return jsonify({'ok': True})

@app.route('/api/products/<int:pid>/skus', methods=['DELETE'])
@login_required
def product_sku_delete(pid):
    """Remove an Oldham SKU mapping from this product."""
    d = request.json or {}
    sku = (d.get('sku') or '').strip()
    if not sku:
        return jsonify({'error': 'SKU is required'}), 400
    conn = get_db(); cur = conn.cursor()
    cur.execute("DELETE FROM oldham_sku_map WHERE oldham_sku=%s AND product_id=%s", (sku, pid))
    conn.commit(); cur.close(); conn.close()
    return jsonify({'ok': True})

# ─── PER-TECH LOGINS (admin creates/removes tech app accounts) ─────────────────

def _suggest_tech_username(name):
    """First initial + last name, lowercased & alnum only. 'John Russell' → 'jrussell'."""
    parts = re.sub(r'[^a-zA-Z ]', '', name or '').split()
    if not parts:
        return ''
    if len(parts) == 1:
        return parts[0].lower()
    return (parts[0][0] + parts[-1]).lower()

@app.route('/api/tech-logins', methods=['GET'])
@login_required
def tech_logins():
    """Each active technician plus their login username (if one exists)."""
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        SELECT t.id AS technician_id, t.name, t.truck_id,
               u.id AS user_id, u.username
        FROM technicians t
        LEFT JOIN users u ON u.technician_id = t.id AND u.role = 'tech'
        WHERE t.active = TRUE
        ORDER BY t.name
    """)
    rows = [dict(r) for r in cur.fetchall()]
    cur.close(); conn.close()
    for r in rows:
        r['suggested_username'] = _suggest_tech_username(r['name'])
    return jsonify(rows)

@app.route('/api/technicians/<int:tid>/login', methods=['POST'])
@login_required
def tech_login_create(tid):
    """Create (or update) a dedicated login for a technician."""
    d = request.json or {}
    password = (d.get('password') or '').strip()
    if len(password) < 6:
        return jsonify({'error': 'Password must be at least 6 characters'}), 400
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT name FROM technicians WHERE id=%s", (tid,))
    tech = cur.fetchone()
    if not tech:
        cur.close(); conn.close()
        return jsonify({'error': 'Technician not found'}), 404
    username = (d.get('username') or '').strip() or _suggest_tech_username(tech['name'])
    if not username:
        cur.close(); conn.close()
        return jsonify({'error': 'Could not determine a username'}), 400
    # Username must be free (unless it's this tech's existing login)
    cur.execute("SELECT id, technician_id FROM users WHERE LOWER(username)=LOWER(%s)", (username,))
    clash = cur.fetchone()
    if clash and clash['technician_id'] != tid:
        cur.close(); conn.close()
        return jsonify({'error': f'Username "{username}" is already taken'}), 409
    pwhash = generate_password_hash(password)
    # Does this tech already have a login?
    cur.execute("SELECT id FROM users WHERE technician_id=%s AND role='tech'", (tid,))
    existing = cur.fetchone()
    if existing:
        cur.execute("UPDATE users SET username=%s, password_hash=%s WHERE id=%s", (username, pwhash, existing['id']))
        action = 'updated'
    else:
        cur.execute("INSERT INTO users (username, password_hash, role, technician_id) VALUES (%s, %s, 'tech', %s)",
                    (username, pwhash, tid))
        action = 'created'
    conn.commit(); cur.close(); conn.close()
    log_system_event('admin', f'Tech login {action} for {tech["name"]} (username {username})', performed_by=session.get('username'))
    return jsonify({'ok': True, 'username': username, 'action': action})

@app.route('/api/technicians/<int:tid>/login', methods=['DELETE'])
@login_required
def tech_login_delete(tid):
    """Remove a technician's dedicated login."""
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT id, username FROM users WHERE technician_id=%s AND role='tech'", (tid,))
    u = cur.fetchone()
    if not u:
        cur.close(); conn.close()
        return jsonify({'error': 'No login for this technician'}), 404
    cur.execute("DELETE FROM users WHERE id=%s", (u['id'],))
    conn.commit(); cur.close(); conn.close()
    log_system_event('admin', f'Tech login removed (username {u["username"]})', performed_by=session.get('username'))
    return jsonify({'ok': True})

# ─── INVENTORY RECOUNT FLAGS (admin flags an item → tech recounts in the app) ──

@app.route('/api/flags', methods=['POST'])
@login_required
def flag_create():
    """Admin flags a tech's truck item, prompting them to recount it in the app."""
    d = request.json or {}
    tech_id = d.get('technician_id')
    product_id = d.get('product_id')
    note = (d.get('note') or '').strip()
    if not tech_id or not product_id:
        return jsonify({'error': 'technician_id and product_id required'}), 400
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT quantity_storage_units FROM tech_inventory WHERE technician_id=%s AND product_id=%s", (tech_id, product_id))
    row = cur.fetchone()
    flagged_qty = float(row['quantity_storage_units']) if row else 0
    # One open flag per (tech, product) — refresh the note instead of duplicating.
    cur.execute("""
        SELECT id FROM inventory_flags
        WHERE technician_id=%s AND product_id=%s AND status='open'
    """, (tech_id, product_id))
    existing = cur.fetchone()
    if existing:
        cur.execute("UPDATE inventory_flags SET note=%s, flagged_qty=%s, created_by=%s, created_at=NOW() WHERE id=%s",
                    (note or None, flagged_qty, session.get('username'), existing['id']))
        fid = existing['id']
    else:
        cur.execute("""
            INSERT INTO inventory_flags (technician_id, product_id, note, flagged_qty, created_by)
            VALUES (%s, %s, %s, %s, %s) RETURNING id
        """, (tech_id, product_id, note or None, flagged_qty, session.get('username')))
        fid = cur.fetchone()['id']
    conn.commit(); cur.close(); conn.close()
    return jsonify({'ok': True, 'flag_id': fid})

@app.route('/api/flags', methods=['GET'])
@any_login_required
def flags_list():
    """Open flags. Admin may pass ?technician_id / ?status; a linked tech login
    only ever sees its own flags."""
    status = request.args.get('status', 'open')
    tech_filter = request.args.get('technician_id', type=int)
    if session.get('role') == 'tech' and session.get('technician_id'):
        tech_filter = session.get('technician_id')   # a tech is locked to their own
    conn = get_db(); cur = conn.cursor()
    q = """
        SELECT f.id, f.technician_id, f.product_id, f.note, f.status,
               f.flagged_qty, f.resolved_qty, f.created_by, f.created_at, f.resolved_at,
               t.name AS technician_name,
               p.name AS product_name, p.storage_unit, p.usage_unit, p.conversion_factor
        FROM inventory_flags f
        JOIN technicians t ON f.technician_id = t.id
        JOIN products p    ON f.product_id = p.id
        WHERE f.status = %s
    """
    params = [status]
    if tech_filter:
        q += " AND f.technician_id = %s"
        params.append(tech_filter)
    q += " ORDER BY f.created_at DESC"
    cur.execute(q, params)
    rows = [dict(r) for r in cur.fetchall()]
    cur.close(); conn.close()
    return jsonify(rows)

@app.route('/api/flags/<int:fid>/resolve', methods=['POST'])
@any_login_required
def flag_resolve(fid):
    """The tech (or admin) clears a flag by entering the real count. Sets the
    truck inventory to that amount and logs an audit — the warehouse is NOT
    touched (this is a recount correction, not a return)."""
    d = request.json or {}
    try:
        new_qty = float(d.get('quantity'))
    except (TypeError, ValueError):
        return jsonify({'error': 'A valid quantity is required'}), 400
    if new_qty < 0:
        return jsonify({'error': 'Quantity cannot be negative'}), 400
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT * FROM inventory_flags WHERE id=%s", (fid,))
    flag = cur.fetchone()
    if not flag:
        cur.close(); conn.close()
        return jsonify({'error': 'Flag not found'}), 404
    if flag['status'] != 'open':
        cur.close(); conn.close()
        return jsonify({'error': 'This flag is already resolved'}), 409
    # A linked tech login may only resolve its own flags.
    if session.get('role') == 'tech' and session.get('technician_id') \
            and session.get('technician_id') != flag['technician_id']:
        cur.close(); conn.close()
        return jsonify({'error': 'Not your flag'}), 403
    tech_id, product_id = flag['technician_id'], flag['product_id']
    cur.execute("SELECT quantity_storage_units FROM tech_inventory WHERE technician_id=%s AND product_id=%s", (tech_id, product_id))
    row = cur.fetchone()
    old_qty = float(row['quantity_storage_units']) if row else 0
    # Set the truck to the counted amount (no warehouse change).
    cur.execute("""
        INSERT INTO tech_inventory (technician_id, product_id, quantity_storage_units)
        VALUES (%s, %s, %s)
        ON CONFLICT (technician_id, product_id)
        DO UPDATE SET quantity_storage_units=%s, last_updated=NOW()
    """, (tech_id, product_id, new_qty, new_qty))
    cur.execute("""
        INSERT INTO audit_log (audit_type, product_id, technician_id, old_quantity, new_quantity, reason, performed_by)
        VALUES ('tech', %s, %s, %s, %s, %s, %s)
    """, (product_id, tech_id, old_qty, new_qty,
          f'Tech recount (flag #{fid})' + (f' — {flag["note"]}' if flag['note'] else ''),
          session.get('username')))
    cur.execute("UPDATE inventory_flags SET status='resolved', resolved_qty=%s, resolved_at=NOW() WHERE id=%s", (new_qty, fid))
    conn.commit(); cur.close(); conn.close()
    return jsonify({'ok': True, 'old_quantity': old_qty, 'new_quantity': new_qty})

@app.route('/api/flags/<int:fid>', methods=['DELETE'])
@login_required
def flag_cancel(fid):
    """Admin cancels an open flag without changing inventory."""
    conn = get_db(); cur = conn.cursor()
    cur.execute("UPDATE inventory_flags SET status='cancelled' WHERE id=%s AND status='open'", (fid,))
    conn.commit(); cur.close(); conn.close()
    return jsonify({'ok': True})

def _acting_tech_id():
    """Which technician the current request acts on. A linked tech login is
    locked to its own technician; the shared/admin login may pass one."""
    if session.get('role') == 'tech' and session.get('technician_id'):
        return session.get('technician_id')
    tid = request.args.get('technician_id', type=int)
    if tid is None and request.is_json:
        tid = (request.json or {}).get('technician_id')
    return tid

@app.route('/api/my-truck', methods=['GET'])
@any_login_required
def my_truck():
    """The acting technician's own truck inventory (for the tech app)."""
    tech_id = _acting_tech_id()
    if not tech_id:
        return jsonify({'items': [], 'open_flags': []})
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        SELECT ti.product_id, ti.quantity_storage_units,
               p.name AS product_name, p.storage_unit, p.usage_unit, p.conversion_factor,
               tp.min_qty, tp.alert_qty
        FROM tech_inventory ti
        JOIN products p ON ti.product_id = p.id
        LEFT JOIN truck_par_levels tp ON tp.technician_id = ti.technician_id AND tp.product_id = ti.product_id
        WHERE ti.technician_id = %s AND p.active = TRUE AND COALESCE(p.is_equipment, FALSE) = FALSE
        ORDER BY p.name
    """, (tech_id,))
    items = [dict(r) for r in cur.fetchall()]
    cur.execute("SELECT product_id FROM inventory_flags WHERE technician_id=%s AND status='open'", (tech_id,))
    open_flags = [r['product_id'] for r in cur.fetchall()]
    cur.close(); conn.close()
    for i in items:
        i['quantity_storage_units'] = float(i['quantity_storage_units'] or 0)
        i['min_qty'] = float(i['min_qty']) if i['min_qty'] is not None else 0
        i['alert_qty'] = float(i['alert_qty']) if i['alert_qty'] is not None else i['min_qty']
        i['conversion_factor'] = float(i['conversion_factor'] or 1)
    return jsonify({'items': items, 'open_flags': open_flags})

@app.route('/api/my-truck/par', methods=['POST'])
@any_login_required
def my_truck_par():
    """A tech sets their own truck levels for a product: 'keep on truck'
    (min_qty) and the restock-alert level (alert_qty)."""
    d = request.json or {}
    tech_id = _acting_tech_id()
    product_id = d.get('product_id')
    if not tech_id or not product_id:
        return jsonify({'error': 'technician and product required'}), 400
    def _num(v):
        return float(v) if v not in (None, '') else 0.0
    try:
        keep = _num(d.get('min_qty'))
        alert = _num(d.get('alert_qty'))
    except (TypeError, ValueError):
        return jsonify({'error': 'Levels must be numbers'}), 400
    if keep < 0 or alert < 0:
        return jsonify({'error': 'Levels cannot be negative'}), 400
    conn = get_db(); cur = conn.cursor()
    if keep > 0 or alert > 0:
        cur.execute("""
            INSERT INTO truck_par_levels (technician_id, product_id, min_qty, alert_qty)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (technician_id, product_id)
            DO UPDATE SET min_qty = EXCLUDED.min_qty, alert_qty = EXCLUDED.alert_qty, updated_at = NOW()
        """, (tech_id, product_id, keep, alert))
    else:
        cur.execute("DELETE FROM truck_par_levels WHERE technician_id=%s AND product_id=%s", (tech_id, product_id))
    conn.commit(); cur.close(); conn.close()
    return jsonify({'ok': True, 'min_qty': keep, 'alert_qty': alert})

@app.route('/api/my-truck/audit', methods=['POST'])
@any_login_required
def my_truck_audit():
    """A tech corrects their own truck count from the app. Sets the truck
    inventory and logs an audit — the warehouse is NOT touched."""
    d = request.json or {}
    tech_id = _acting_tech_id()
    product_id = d.get('product_id')
    if not tech_id or not product_id:
        return jsonify({'error': 'technician and product required'}), 400
    try:
        new_qty = float(d.get('quantity'))
    except (TypeError, ValueError):
        return jsonify({'error': 'A valid quantity is required'}), 400
    if new_qty < 0:
        return jsonify({'error': 'Quantity cannot be negative'}), 400
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT quantity_storage_units FROM tech_inventory WHERE technician_id=%s AND product_id=%s", (tech_id, product_id))
    row = cur.fetchone()
    old_qty = float(row['quantity_storage_units']) if row else 0
    cur.execute("""
        INSERT INTO tech_inventory (technician_id, product_id, quantity_storage_units)
        VALUES (%s, %s, %s)
        ON CONFLICT (technician_id, product_id)
        DO UPDATE SET quantity_storage_units=%s, last_updated=NOW()
    """, (tech_id, product_id, new_qty, new_qty))
    cur.execute("""
        INSERT INTO audit_log (audit_type, product_id, technician_id, old_quantity, new_quantity, reason, performed_by)
        VALUES ('tech', %s, %s, %s, %s, 'Tech self-audit (app)', %s)
    """, (product_id, tech_id, old_qty, new_qty, session.get('username')))
    # A self-audit also clears any open recount flag on that item.
    cur.execute("""
        UPDATE inventory_flags SET status='resolved', resolved_qty=%s, resolved_at=NOW()
        WHERE technician_id=%s AND product_id=%s AND status='open'
    """, (new_qty, tech_id, product_id))
    conn.commit(); cur.close(); conn.close()
    return jsonify({'ok': True, 'old_quantity': old_qty, 'new_quantity': new_qty})

# ─── SERVICE TYPES + COMPLIANCE RULES/REPORT ──────────────────────────────────

@app.route('/api/service-types', methods=['GET'])
@login_required
def list_service_types():
    """Service types (from the FR sync) for the compliance-rule dropdown, with
    how many completed appointments each has (last ~180 days) for context."""
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        SELECT st.type_id, st.description, st.category,
               (SELECT COUNT(*) FROM fr_appointments_raw a
                 WHERE a.service_type_id = st.type_id AND a.status = '1'
                   AND a.appt_date >= CURRENT_DATE - INTERVAL '180 days') AS recent_jobs
        FROM service_types st
        ORDER BY st.description NULLS LAST, st.type_id
    """)
    rows = [dict(r) for r in cur.fetchall()]
    cur.close(); conn.close()
    return jsonify(rows)

def _rule_out(r, prod_names):
    return {
        'id': r['id'], 'name': r['name'],
        'service_type_id': r['service_type_id'],
        'service_type_name': r.get('service_type_name') or (f"Type {r['service_type_id']}" if r['service_type_id'] else '—'),
        'expected_product_ids': r['expected_product_ids'] or [],
        'expected_product_names': [prod_names.get(pid, f'#{pid}') for pid in (r['expected_product_ids'] or [])],
        'skip_zero_invoice': r['skip_zero_invoice'], 'active': r['active'],
    }

@app.route('/api/compliance/rules', methods=['GET'])
@login_required
def compliance_rules_list():
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        SELECT cr.*, st.description AS service_type_name
        FROM compliance_rules cr
        LEFT JOIN service_types st ON st.type_id = cr.service_type_id
        ORDER BY cr.created_at
    """)
    rules = [dict(r) for r in cur.fetchall()]
    cur.execute("SELECT id, name FROM products")
    prod_names = {r['id']: r['name'] for r in cur.fetchall()}
    cur.close(); conn.close()
    return jsonify([_rule_out(r, prod_names) for r in rules])

@app.route('/api/compliance/rules', methods=['POST'])
@login_required
def compliance_rule_create():
    d = request.json or {}
    name = (d.get('name') or '').strip()
    service_type_id = (d.get('service_type_id') or '').strip()
    product_ids = [int(x) for x in (d.get('expected_product_ids') or []) if str(x).strip()]
    skip_zero = bool(d.get('skip_zero_invoice'))
    if not service_type_id or not product_ids:
        return jsonify({'error': 'Pick a service type and at least one expected product'}), 400
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        INSERT INTO compliance_rules (name, service_type_id, expected_product_ids, skip_zero_invoice)
        VALUES (%s, %s, %s, %s) RETURNING id
    """, (name or None, service_type_id, product_ids, skip_zero))
    rid = cur.fetchone()['id']
    conn.commit(); cur.close(); conn.close()
    return jsonify({'ok': True, 'id': rid})

@app.route('/api/compliance/rules/<int:rid>', methods=['PUT'])
@login_required
def compliance_rule_update(rid):
    d = request.json or {}
    name = (d.get('name') or '').strip()
    service_type_id = (d.get('service_type_id') or '').strip()
    product_ids = [int(x) for x in (d.get('expected_product_ids') or []) if str(x).strip()]
    skip_zero = bool(d.get('skip_zero_invoice'))
    if not service_type_id or not product_ids:
        return jsonify({'error': 'Pick a service type and at least one expected product'}), 400
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        UPDATE compliance_rules
        SET name=%s, service_type_id=%s, expected_product_ids=%s, skip_zero_invoice=%s
        WHERE id=%s
    """, (name or None, service_type_id, product_ids, skip_zero, rid))
    conn.commit(); cur.close(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/compliance/rules/<int:rid>', methods=['DELETE'])
@login_required
def compliance_rule_delete(rid):
    conn = get_db(); cur = conn.cursor()
    cur.execute("DELETE FROM compliance_rules WHERE id=%s", (rid,))
    conn.commit(); cur.close(); conn.close()
    return jsonify({'ok': True})

def _fr_invoice_totals(appt_ids):
    """appointment_id -> active-invoice total (USD), best effort via FR ticket/search."""
    totals = {}
    if not appt_ids or not FR_API_KEY or not FR_AUTH_KEY:
        return totals
    import time
    ids = [str(a) for a in appt_ids]
    for i in range(0, len(ids), 400):
        chunk = ids[i:i+400]
        try:
            res = fr_post('ticket/search', {'appointmentIDs': chunk, 'includeData': 1})
        except Exception:
            continue
        data = res.get('tickets', res.get('data', {}))
        records = data.values() if isinstance(data, dict) else (data or [])
        for t in records:
            if not isinstance(t, dict):
                continue
            aid = str(t.get('appointmentID') or '')
            if not aid:
                continue
            # active tickets (status 1) count toward the invoice; sum if several
            try:
                if str(t.get('active', t.get('status', 1))) in ('0', '-1'):
                    continue
                totals[aid] = totals.get(aid, 0.0) + float(t.get('total') or 0)
            except (TypeError, ValueError):
                pass
        if i + 400 < len(ids):
            time.sleep(1.2)
    return totals

def _fr_customer_names(cust_ids):
    """customer_id -> display name, best effort via FR customer/get."""
    names = {}
    ids = [str(c) for c in cust_ids if c]
    if not ids or not FR_API_KEY or not FR_AUTH_KEY:
        return names
    try:
        recs = fr_get_records('customer', ids, 'customers')
    except Exception:
        return names
    for c in (recs.values() if isinstance(recs, dict) else recs):
        if not isinstance(c, dict):
            continue
        cid = str(c.get('customerID') or '')
        nm = (c.get('companyName') or '').strip() or f"{(c.get('fname') or '').strip()} {(c.get('lname') or '').strip()}".strip()
        if cid:
            names[cid] = nm or cid
    return names

@app.route('/api/reports/compliance', methods=['GET'])
@login_required
def report_compliance():
    """Completed jobs whose service type should have used a product (per the
    compliance rules) but none of the expected products were recorded."""
    date_from = request.args.get('date_from')
    date_to   = request.args.get('date_to')
    only_rule = request.args.get('rule_id', type=int)
    if not date_from or not date_to:
        return jsonify({'error': 'date_from and date_to required'}), 400
    conn = get_db(); cur = conn.cursor()
    q = "SELECT * FROM compliance_rules WHERE active=TRUE"
    params = []
    if only_rule:
        q += " AND id=%s"; params.append(only_rule)
    cur.execute(q, params)
    rules = [dict(r) for r in cur.fetchall()]
    cur.execute("SELECT id, name FROM products")
    prod_names = {r['id']: r['name'] for r in cur.fetchall()}
    cur.execute("SELECT type_id, description FROM service_types")
    st_names = {r['type_id']: r['description'] for r in cur.fetchall()}

    violations = []
    for rule in rules:
        expected = rule['expected_product_ids'] or []
        if not expected:
            continue
        cur.execute("""
            SELECT a.appointment_id, a.customer_id, a.tech_fr_id, a.appt_date,
                   COALESCE(t.name, 'FR emp ' || COALESCE(a.tech_fr_id,'?')) AS tech_name
            FROM fr_appointments_raw a
            LEFT JOIN technicians t ON t.fr_employee_id = a.tech_fr_id
            WHERE a.status='1'
              AND a.appt_date BETWEEN %s AND %s
              AND a.service_type_id = %s
              AND NOT EXISTS (
                  SELECT 1 FROM inventory_transactions it
                  WHERE it.fr_appointment_id = a.appointment_id
                    AND it.transaction_type='usage_sync'
                    AND it.product_id = ANY(%s)
              )
            ORDER BY a.appt_date
        """, (date_from, date_to, rule['service_type_id'], expected))
        appts = [dict(r) for r in cur.fetchall()]
        for a in appts:
            # what WAS used on the job (for context)
            cur.execute("""
                SELECT DISTINCT p.name FROM inventory_transactions it
                JOIN products p ON p.id = it.product_id
                WHERE it.fr_appointment_id=%s AND it.transaction_type='usage_sync'
            """, (a['appointment_id'],))
            used = [r['name'] for r in cur.fetchall()]
            violations.append({
                'rule_id': rule['id'],
                'rule_name': rule['name'] or (st_names.get(rule['service_type_id']) or 'Rule'),
                'service_type_id': rule['service_type_id'],
                'service_type_name': st_names.get(rule['service_type_id']) or f"Type {rule['service_type_id']}",
                'expected': [prod_names.get(pid, f'#{pid}') for pid in expected],
                'skip_zero_invoice': rule['skip_zero_invoice'],
                'appointment_id': a['appointment_id'],
                'appt_date': a['appt_date'].isoformat() if a['appt_date'] else None,
                'tech_name': a['tech_name'],
                'customer_id': a['customer_id'],
                'used_instead': used,
            })
    cur.close(); conn.close()

    # Invoice totals (for display + $0 skip) — one FR pass over all violation appts.
    appt_ids = list({v['appointment_id'] for v in violations})
    totals = _fr_invoice_totals(appt_ids)
    kept = []
    skipped_zero = 0
    for v in violations:
        total = totals.get(str(v['appointment_id']))
        v['invoice_total'] = total
        if v['skip_zero_invoice'] and (total is None or total <= 0):
            skipped_zero += 1
            continue
        kept.append(v)

    names = _fr_customer_names(list({v['customer_id'] for v in kept}))
    for v in kept:
        v['customer_name'] = names.get(str(v['customer_id']), '')

    kept.sort(key=lambda v: (v['appt_date'] or '', v['tech_name'] or ''))
    return jsonify({
        'violations': kept,
        'count': len(kept),
        'skipped_zero_invoice': skipped_zero,
        'rules_run': len(rules),
        'date_from': date_from, 'date_to': date_to,
    })

# ─── API: TECH INVENTORY ──────────────────────────────────────────────────────

@app.route('/api/tech-inventory', methods=['GET'])
@login_required
def get_tech_inventory():
    tech_id = request.args.get('tech_id')
    conn = get_db()
    cur = conn.cursor()
    if tech_id:
        cur.execute("""
            SELECT ti.*, p.name AS product_name, p.storage_unit, p.usage_unit,
                   p.conversion_factor, p.conversion_note,
                   t.name AS tech_name, t.truck_id
            FROM tech_inventory ti
            JOIN products p ON ti.product_id = p.id
            JOIN technicians t ON ti.technician_id = t.id
            WHERE ti.technician_id = %s AND p.active = TRUE
            ORDER BY p.name
        """, (tech_id,))
    else:
        cur.execute("""
            SELECT ti.*, p.name AS product_name, p.storage_unit, p.usage_unit,
                   p.conversion_factor, p.conversion_note,
                   t.name AS tech_name, t.truck_id
            FROM tech_inventory ti
            JOIN products p ON ti.product_id = p.id
            JOIN technicians t ON ti.technician_id = t.id
            WHERE p.active = TRUE AND t.active = TRUE
            ORDER BY t.name, p.name
        """)
    rows = [dict(r) for r in cur.fetchall()]
    cur.close()
    conn.close()
    return jsonify(rows)

@app.route('/api/equipment-provided', methods=['GET'])
@login_required
def get_equipment_provided():
    """Return totals of equipment provided to each tech, grouped by tech + product."""
    tech_id = request.args.get('tech_id')
    conn = get_db()
    cur = conn.cursor()
    query = """
        SELECT
            it.technician_id, t.name AS tech_name, t.truck_id,
            it.product_id, p.name AS product_name, p.storage_unit,
            SUM(it.quantity_storage_units) AS total_provided,
            MAX(it.created_at) AS last_provided
        FROM inventory_transactions it
        JOIN products p ON it.product_id = p.id
        JOIN technicians t ON it.technician_id = t.id
        WHERE it.transaction_type = 'equipment_provided'
          AND p.active = TRUE AND t.active = TRUE
    """
    params = []
    if tech_id:
        query += " AND it.technician_id = %s"
        params.append(tech_id)
    query += " GROUP BY it.technician_id, t.name, t.truck_id, it.product_id, p.name, p.storage_unit ORDER BY t.name, p.name"
    cur.execute(query, params)
    rows = [dict(r) for r in cur.fetchall()]
    cur.close()
    conn.close()
    return jsonify(rows)

@app.route('/api/distribution')
@login_required
def get_distribution():
    """Return per-tech, per-product distribution totals for a given month + YTD."""
    year  = request.args.get('year',  datetime.now().year,  type=int)
    month = request.args.get('month', datetime.now().month, type=int)
    conn = get_db()
    cur  = conn.cursor()

    # Monthly quantities per product per tech
    cur.execute("""
        SELECT it.product_id, it.technician_id,
               SUM(it.quantity_storage_units) AS month_qty
        FROM inventory_transactions it
        WHERE it.transaction_type IN ('transfer','equipment_provided')
          AND EXTRACT(YEAR  FROM it.created_at) = %s
          AND EXTRACT(MONTH FROM it.created_at) = %s
          AND it.technician_id IS NOT NULL
        GROUP BY it.product_id, it.technician_id
    """, (year, month))
    month_rows = cur.fetchall()

    # YTD quantities: always Jan 1 of the selected year through TODAY,
    # so the YTD number is the same regardless of which month you're viewing.
    cur.execute("""
        SELECT it.product_id, it.technician_id,
               SUM(it.quantity_storage_units) AS ytd_qty
        FROM inventory_transactions it
        WHERE it.transaction_type IN ('transfer','equipment_provided')
          AND EXTRACT(YEAR FROM it.created_at) = %s
          AND it.created_at::date <= CURRENT_DATE
          AND it.technician_id IS NOT NULL
        GROUP BY it.product_id, it.technician_id
    """, (year,))
    ytd_rows = cur.fetchall()

    # Products that had any distribution so far this year (Jan 1 → today)
    cur.execute("""
        SELECT DISTINCT p.id, p.name, p.storage_unit, p.usage_unit,
               COALESCE(p.conversion_factor, 1)             AS conversion_factor,
               COALESCE(p.cost_per_storage_unit, 0)         AS cost_per_storage_unit,
               COALESCE(p.is_equipment, FALSE)               AS is_equipment,
               COALESCE(p.show_storage_dist, FALSE)          AS show_storage_dist
        FROM products p
        JOIN inventory_transactions it ON it.product_id = p.id
        WHERE it.transaction_type IN ('transfer','equipment_provided')
          AND EXTRACT(YEAR FROM it.created_at) = %s
          AND it.created_at::date <= CURRENT_DATE
          AND it.technician_id IS NOT NULL
        ORDER BY p.name
    """, (year,))
    products = [dict(r) for r in cur.fetchall()]

    # Active techs always shown; inactive (former) techs only shown if they
    # had a transfer in the specific selected month.
    cur.execute("""
        SELECT DISTINCT t.id, t.name
        FROM technicians t
        WHERE t.active = TRUE
        UNION
        SELECT DISTINCT t.id, t.name
        FROM technicians t
        JOIN inventory_transactions it ON it.technician_id = t.id
        WHERE t.active = FALSE
          AND it.transaction_type IN ('transfer','equipment_provided')
          AND EXTRACT(YEAR  FROM it.created_at) = %s
          AND EXTRACT(MONTH FROM it.created_at) = %s
        ORDER BY name
    """, (year, month))
    techs = [dict(r) for r in cur.fetchall()]

    cur.close()
    conn.close()

    month_map = {(r['product_id'], r['technician_id']): float(r['month_qty']) for r in month_rows}
    ytd_map   = {(r['product_id'], r['technician_id']): float(r['ytd_qty'])   for r in ytd_rows}

    prod_list = []
    for p in products:
        mb = {t['id']: month_map.get((p['id'], t['id']), 0) for t in techs}
        yb = {t['id']: ytd_map.get((p['id'],  t['id']), 0) for t in techs}
        if sum(yb.values()) == 0:
            continue
        prod_list.append({
            **p,
            'cost_per_storage_unit': float(p['cost_per_storage_unit']),
            'month_by_tech': mb,
            'ytd_by_tech':   yb,
            'month_total':   sum(mb.values()),
            'ytd_total':     sum(yb.values()),
        })

    return jsonify({'techs': techs, 'products': prod_list})


@app.route('/api/transfer', methods=['POST'])
@login_required
def transfer_inventory():
    """Transfer from warehouse to tech."""
    d = request.json
    product_id = d['product_id']
    tech_id = d['technician_id']
    quantity = float(d['quantity'])  # in storage units
    notes = d.get('notes', '')
    location = d.get('location', 'Holpers')  # which warehouse location to pull from
    batch_id = d.get('batch_id') or None   # UUID grouping all lines from one paper card submit

    conn = get_db()
    cur = conn.cursor()

    # Check if this product is equipment (tracked but not deducted via FR sync)
    cur.execute("SELECT COALESCE(is_equipment, FALSE) as is_equipment FROM products WHERE id=%s", (product_id,))
    prod_row = cur.fetchone()
    is_equipment = prod_row['is_equipment'] if prod_row else False

    # Deduct from warehouse location (allow going negative)
    cur.execute("SELECT id FROM warehouse_inventory WHERE product_id=%s AND location=%s", (product_id, location))
    if cur.fetchone():
        cur.execute("UPDATE warehouse_inventory SET quantity_storage_units=quantity_storage_units-%s, last_updated=NOW() WHERE product_id=%s AND location=%s", (quantity, product_id, location))
    else:
        cur.execute("INSERT INTO warehouse_inventory (product_id, location, quantity_storage_units) VALUES (%s,%s,%s)", (product_id, location, -quantity))

    # Only add to tech_inventory for consumable products — equipment is tracked via transactions only
    if not is_equipment:
        cur.execute("""
            INSERT INTO tech_inventory (technician_id, product_id, quantity_storage_units)
            VALUES (%s, %s, %s)
            ON CONFLICT (technician_id, product_id)
            DO UPDATE SET quantity_storage_units = tech_inventory.quantity_storage_units + %s,
                          last_updated = NOW()
        """, (tech_id, product_id, quantity, quantity))

    # Log transaction — use 'equipment_provided' type for equipment, 'transfer' for consumables
    txn_type = 'equipment_provided' if is_equipment else 'transfer'
    cur.execute("""
        INSERT INTO inventory_transactions
        (transaction_type, product_id, technician_id, quantity_storage_units, notes, performed_by, transfer_batch_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
    """, (txn_type, product_id, tech_id, quantity, f"[{location}] {notes}", session.get('username'), batch_id))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({'ok': True, 'is_equipment': is_equipment})

# ─── API: AUDIT ───────────────────────────────────────────────────────────────

@app.route('/api/warehouse/transfer-location', methods=['POST'])
@login_required
def transfer_between_locations():
    """Transfer stock between Holpers and Oldham."""
    d = request.json
    product_id = d['product_id']
    quantity = float(d['quantity'])
    from_location = d.get('from_location', 'Holpers')
    to_location = d.get('to_location', 'Oldham')
    notes = d.get('notes', '')

    if from_location == to_location:
        return jsonify({'error': 'From and To locations must be different'}), 400

    conn = get_db()
    cur = conn.cursor()
    try:
        # Deduct from source
        cur.execute("SELECT id FROM warehouse_inventory WHERE product_id=%s AND location=%s", (product_id, from_location))
        if cur.fetchone():
            cur.execute("UPDATE warehouse_inventory SET quantity_storage_units=quantity_storage_units-%s, last_updated=NOW() WHERE product_id=%s AND location=%s", (quantity, product_id, from_location))
        else:
            cur.execute("INSERT INTO warehouse_inventory (product_id, location, quantity_storage_units) VALUES (%s,%s,%s)", (product_id, from_location, -quantity))

        cur.execute("SELECT id FROM warehouse_inventory WHERE product_id=%s AND location=%s", (product_id, to_location))
        if cur.fetchone():
            cur.execute("UPDATE warehouse_inventory SET quantity_storage_units=quantity_storage_units+%s, last_updated=NOW() WHERE product_id=%s AND location=%s", (quantity, product_id, to_location))
        else:
            cur.execute("INSERT INTO warehouse_inventory (product_id, location, quantity_storage_units) VALUES (%s,%s,%s)", (product_id, to_location, quantity))

        # Log it
        cur.execute("""
            INSERT INTO inventory_transactions
            (transaction_type, product_id, quantity_storage_units, notes, performed_by)
            VALUES ('location_transfer', %s, %s, %s, %s)
        """, (product_id, quantity, f"{from_location} → {to_location} | {notes}", session.get('username')))

        conn.commit()
        cur.close(); conn.close()
        return jsonify({'ok': True})
    except Exception as e:
        conn.rollback(); cur.close(); conn.close()
        return jsonify({'error': str(e)}), 500

@app.route('/api/warehouse/delete-line', methods=['POST'])
@login_required
def delete_warehouse_line():
    """Remove a warehouse_inventory row entirely (by product_id + location)."""
    d = request.json
    product_id = d['product_id']
    location = d.get('location', 'Holpers')

    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute(
            "DELETE FROM warehouse_inventory WHERE product_id=%s AND location=%s",
            (product_id, location)
        )
        deleted = cur.rowcount
        cur.execute("""
            INSERT INTO audit_log (audit_type, product_id, old_quantity, new_quantity, reason, performed_by)
            VALUES ('warehouse', %s, 0, 0, %s, %s)
        """, (product_id, f"[{location}] Line deleted", session.get('username')))
        conn.commit()
        cur.close(); conn.close()
        return jsonify({'ok': True, 'deleted': deleted})
    except Exception as e:
        conn.rollback(); cur.close(); conn.close()
        return jsonify({'error': str(e)}), 500

@app.route('/api/audit/warehouse', methods=['POST'])
@login_required
def audit_warehouse():
    d = request.json
    product_id = d['product_id']
    new_qty = float(d['new_quantity'])
    reason = d.get('reason', 'Manual audit')
    location = d.get('location', 'Holpers')

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT quantity_storage_units FROM warehouse_inventory WHERE product_id=%s AND location=%s", (product_id, location))
    row = cur.fetchone()
    old_qty = float(row['quantity_storage_units']) if row else 0

    if row:
        cur.execute("UPDATE warehouse_inventory SET quantity_storage_units=%s, last_updated=NOW() WHERE product_id=%s AND location=%s", (new_qty, product_id, location))
    else:
        cur.execute("INSERT INTO warehouse_inventory (product_id, location, quantity_storage_units) VALUES (%s,%s,%s)", (product_id, location, new_qty))
    cur.execute("""
        INSERT INTO audit_log (audit_type, product_id, old_quantity, new_quantity, reason, performed_by)
        VALUES ('warehouse', %s, %s, %s, %s, %s)
    """, (product_id, old_qty, new_qty, f"[{location}] {reason}", session.get('username')))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({'ok': True})

@app.route('/api/warehouse/history', methods=['GET'])
@login_required
def warehouse_history():
    """
    Running-balance ledger for one product at one warehouse location: receives,
    transfers/equipment given to techs, location-to-location moves, and count
    corrections — each with a started-with/ended-with quantity so discrepancies
    are easy to spot.
    """
    product_id = request.args.get('product_id', type=int)
    location = request.args.get('location', 'Holpers')
    if not product_id:
        return jsonify({'error': 'product_id required'}), 400

    conn = get_db(); cur = conn.cursor()

    cur.execute("SELECT storage_unit, usage_unit, conversion_factor FROM products WHERE id=%s", (product_id,))
    prod = cur.fetchone()
    if not prod:
        cur.close(); conn.close()
        return jsonify({'error': 'Product not found'}), 404
    su, uu, cf = prod['storage_unit'], prod['usage_unit'], float(prod['conversion_factor'] or 1)

    cur.execute("SELECT quantity_storage_units FROM warehouse_inventory WHERE product_id=%s AND location=%s", (product_id, location))
    wrow = cur.fetchone()
    current_qty = float(wrow['quantity_storage_units']) if wrow else 0.0

    items = []

    # Receives + transfers/equipment given to techs — location is embedded as "[Location] ..." in notes
    cur.execute("""
        SELECT it.id, it.transaction_type, it.created_at, it.quantity_storage_units,
               it.notes, it.performed_by, it.technician_id, t.name AS tech_name
        FROM inventory_transactions it
        LEFT JOIN technicians t ON it.technician_id = t.id
        WHERE it.product_id=%s
          AND it.transaction_type IN ('receive','transfer','equipment_provided')
          AND it.notes LIKE %s
        ORDER BY it.created_at
    """, (product_id, f'[{location}]%'))
    for r in cur.fetchall():
        qty = float(r['quantity_storage_units'] or 0)
        note_body = r['notes'] or ''
        if note_body.startswith('[') and ']' in note_body:
            note_body = note_body[note_body.index(']')+1:].strip()
        delta = qty if r['transaction_type'] == 'receive' else -qty
        items.append({
            'id': r['id'], 'ts': r['created_at'], 'type': r['transaction_type'],
            'delta': delta, 'tech_name': r['tech_name'], 'other_location': None,
            'notes': note_body or None, 'performed_by': r['performed_by'],
            'editable': r['transaction_type'] in ('transfer', 'equipment_provided'),
        })

    # Location-to-location moves — one row affects two locations; work out direction for this location
    cur.execute("""
        SELECT id, created_at, quantity_storage_units, notes, performed_by
        FROM inventory_transactions
        WHERE product_id=%s AND transaction_type='location_transfer'
        ORDER BY created_at
    """, (product_id,))
    for r in cur.fetchall():
        qty = float(r['quantity_storage_units'] or 0)
        notes = r['notes'] or ''
        arrow_part, _, extra = notes.partition(' | ')
        frm, _, to = arrow_part.partition(' → ')
        frm, to = frm.strip(), to.strip()
        if location == frm:
            delta, other = -qty, to
        elif location == to:
            delta, other = qty, frm
        else:
            continue
        items.append({
            'id': r['id'], 'ts': r['created_at'], 'type': 'location_transfer',
            'delta': delta, 'tech_name': None, 'other_location': other,
            'notes': extra or None, 'performed_by': r['performed_by'], 'editable': False,
        })

    # Count corrections (Set Count) — audit_log
    cur.execute("""
        SELECT id, created_at, old_quantity, new_quantity, reason, performed_by
        FROM audit_log
        WHERE product_id=%s AND audit_type='warehouse' AND reason LIKE %s
        ORDER BY created_at
    """, (product_id, f'[{location}]%'))
    for r in cur.fetchall():
        old_q = float(r['old_quantity'] or 0)
        new_q = float(r['new_quantity'] or 0)
        reason = r['reason'] or ''
        if reason.startswith('[') and ']' in reason:
            reason = reason[reason.index(']')+1:].strip()
        if reason == 'Line deleted':
            continue  # not a real quantity change
        items.append({
            'id': r['id'], 'ts': r['created_at'], 'type': 'audit',
            'delta': new_q - old_q, 'tech_name': None, 'other_location': None,
            'notes': reason or None, 'performed_by': r['performed_by'], 'editable': False,
        })

    cur.close(); conn.close()

    items.sort(key=lambda x: x['ts'])

    # Walk backwards from the current known quantity to reconstruct each step's balance
    running = current_qty
    for it in reversed(items):
        it['ended_with'] = round(running, 4)
        it['started_with'] = round(running - it['delta'], 4)
        running = it['started_with']

    for it in items:
        it['ts'] = (it['ts'].isoformat() + 'Z') if it['ts'] else None
        it['delta'] = round(it['delta'], 4)

    return jsonify({
        'product_id': product_id, 'location': location,
        'storage_unit': su, 'usage_unit': uu, 'conversion_factor': cf,
        'current_quantity': current_qty,
        'items': items,
    })

@app.route('/api/audit/tech', methods=['POST'])
@login_required
def audit_tech():
    d = request.json
    tech_id = d['technician_id']
    product_id = d['product_id']
    new_qty = float(d['new_quantity'])
    reason = d.get('reason', 'Manual audit')

    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT quantity_storage_units FROM tech_inventory
        WHERE technician_id=%s AND product_id=%s
    """, (tech_id, product_id))
    row = cur.fetchone()
    old_qty = float(row['quantity_storage_units']) if row else 0

    cur.execute("""
        INSERT INTO tech_inventory (technician_id, product_id, quantity_storage_units)
        VALUES (%s, %s, %s)
        ON CONFLICT (technician_id, product_id)
        DO UPDATE SET quantity_storage_units=%s, last_updated=NOW()
    """, (tech_id, product_id, new_qty, new_qty))
    cur.execute("""
        INSERT INTO audit_log (audit_type, product_id, technician_id, old_quantity, new_quantity, reason, performed_by)
        VALUES ('tech', %s, %s, %s, %s, %s, %s)
    """, (product_id, tech_id, old_qty, new_qty, reason, session.get('username')))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({'ok': True})

@app.route('/api/audit/log', methods=['GET'])
@login_required
def audit_log():
    limit      = min(int(request.args.get('limit', 200)), 1000)
    atype      = request.args.get('type')
    tech_id    = request.args.get('tech_id')
    product_id = request.args.get('product_id')
    date_from  = request.args.get('date_from')
    date_to    = request.args.get('date_to')
    conn = get_db(); cur = conn.cursor()
    q = """
        SELECT al.id, al.audit_type, al.product_id, al.technician_id,
               al.old_quantity, al.new_quantity, al.reason, al.performed_by, al.created_at,
               p.name AS product_name, p.storage_unit,
               t.name AS tech_name
        FROM audit_log al
        LEFT JOIN products p ON al.product_id = p.id
        LEFT JOIN technicians t ON al.technician_id = t.id
        WHERE 1=1
    """
    params = []
    if atype:
        q += " AND al.audit_type = %s"; params.append(atype)
    if tech_id:
        q += " AND al.technician_id = %s"; params.append(int(tech_id))
    if product_id:
        q += " AND al.product_id = %s"; params.append(int(product_id))
    if date_from:
        q += " AND DATE(al.created_at) >= %s"; params.append(date_from)
    if date_to:
        q += " AND DATE(al.created_at) <= %s"; params.append(date_to)
    q += f" ORDER BY al.created_at DESC LIMIT {limit}"
    cur.execute(q, params)
    rows = [dict(r) for r in cur.fetchall()]
    cur.close(); conn.close()
    for r in rows:
        if r.get('created_at') and hasattr(r['created_at'], 'isoformat'):
            r['created_at'] = r['created_at'].isoformat()
        r['old_quantity'] = float(r['old_quantity'] or 0)
        r['new_quantity'] = float(r['new_quantity'] or 0)
    return jsonify(rows)

# ─── API: TRANSACTION HISTORY ─────────────────────────────────────────────────

@app.route('/api/transactions', methods=['GET'])
@login_required
def get_transactions():
    tech_id = request.args.get('tech_id')
    product_id = request.args.get('product_id')
    date_from = request.args.get('date_from')
    date_to = request.args.get('date_to')
    conn = get_db()
    cur = conn.cursor()
    q = """
        SELECT it.*, p.name AS product_name, p.storage_unit, p.usage_unit,
               p.conversion_factor, t.name AS tech_name, t.truck_id
        FROM inventory_transactions it
        JOIN products p ON it.product_id = p.id
        LEFT JOIN technicians t ON it.technician_id = t.id
        WHERE 1=1
    """
    params = []
    if tech_id:
        q += " AND it.technician_id=%s"; params.append(tech_id)
    if product_id:
        q += " AND it.product_id=%s"; params.append(product_id)
    if date_from:
        q += " AND DATE(it.created_at) >= %s"; params.append(date_from)
    if date_to:
        q += " AND DATE(it.created_at) <= %s"; params.append(date_to)
    q += " ORDER BY it.created_at DESC LIMIT 500"
    cur.execute(q, params)
    rows = [dict(r) for r in cur.fetchall()]
    cur.close(); conn.close()
    return jsonify(rows)

@app.route('/api/transactions/<int:tid>', methods=['PUT'])
@login_required
def update_transaction(tid):
    """
    Manually correct a usage transaction — adjusts tech inventory to match.
    Only works on usage_sync transactions from FR.
    """
    d = request.json
    new_usage_qty = float(d.get('quantity_usage_units', 0))
    reason = d.get('reason', 'Manual correction')

    conn = get_db()
    cur = conn.cursor()
    try:
        # Get the existing transaction
        cur.execute("""
            SELECT it.*, p.conversion_factor, p.id AS pid
            FROM inventory_transactions it
            JOIN products p ON it.product_id = p.id
            WHERE it.id=%s
        """, (tid,))
        txn = cur.fetchone()
        if not txn:
            return jsonify({'error': 'Transaction not found'}), 404

        old_usage_qty = float(txn['quantity_usage_units'] or 0)
        old_storage_qty = float(txn['quantity_storage_units'] or 0)
        conv = float(txn['conversion_factor'])

        new_storage_qty = new_usage_qty / conv
        diff_storage = new_storage_qty - old_storage_qty  # positive = used more, negative = used less

        # Update the transaction record
        cur.execute("""
            UPDATE inventory_transactions
            SET quantity_usage_units=%s, quantity_storage_units=%s,
                notes = notes || ' | CORRECTED: was ' || %s::text || ' usage units, now ' || %s::text || ' (' || %s || ')'
            WHERE id=%s
        """, (new_usage_qty, new_storage_qty, old_usage_qty, new_usage_qty, reason, tid))

        # Adjust tech inventory: if we used more than recorded, deduct more; if less, add back
        if txn['technician_id']:
            cur.execute("""
                UPDATE tech_inventory
                SET quantity_storage_units = quantity_storage_units - %s,
                    last_updated = NOW()
                WHERE technician_id=%s AND product_id=%s
            """, (diff_storage, txn['technician_id'], txn['product_id']))

            # Log the correction as an audit entry
            cur.execute("""
                INSERT INTO audit_log
                (audit_type, product_id, technician_id, old_quantity, new_quantity, reason, performed_by)
                SELECT 'usage_correction', product_id, technician_id,
                       quantity_storage_units + %s, quantity_storage_units, %s, %s
                FROM tech_inventory
                WHERE technician_id=%s AND product_id=%s
            """, (diff_storage, f'Usage correction on txn #{tid}: {reason}',
                  session.get('username'), txn['technician_id'], txn['product_id']))

        conn.commit()
        cur.close(); conn.close()
        return jsonify({'ok': True, 'old_usage': old_usage_qty, 'new_usage': new_usage_qty, 'diff_storage': diff_storage})
    except Exception as e:
        conn.rollback(); cur.close(); conn.close()
        return jsonify({'error': str(e)}), 500

@app.route('/api/transactions/<int:tid>/undo', methods=['POST'])
@login_required
def undo_transaction(tid):
    """
    Undo a transfer or equipment_provided transaction within 5 minutes of creation.
    Reverses the warehouse deduction and (for consumables) the tech_inventory addition.
    """
    UNDO_WINDOW_SECONDS = 300  # 5 minutes

    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT it.*, p.name AS product_name, p.storage_unit,
                   COALESCE(p.is_equipment, FALSE) AS is_equipment
            FROM inventory_transactions it
            JOIN products p ON it.product_id = p.id
            WHERE it.id = %s
        """, (tid,))
        txn = cur.fetchone()
        if not txn:
            return jsonify({'error': 'Transaction not found'}), 404

        if txn['transaction_type'] not in ('transfer', 'equipment_provided'):
            return jsonify({'error': 'Only transfer transactions can be undone'}), 400

        # Check 5-minute window
        age_seconds = (datetime.utcnow() - txn['created_at'].replace(tzinfo=None)).total_seconds()
        if age_seconds > UNDO_WINDOW_SECONDS:
            return jsonify({'error': f'Undo window expired — transfers can only be undone within 5 minutes'}), 400

        qty = float(txn['quantity_storage_units'])
        product_id = txn['product_id']
        tech_id = txn['technician_id']
        is_equipment = txn['is_equipment']

        # Extract location from notes (stored as "[Location] ...")
        notes_str = txn['notes'] or ''
        location = 'Holpers'
        if notes_str.startswith('[') and ']' in notes_str:
            location = notes_str[1:notes_str.index(']')]

        # Restore warehouse stock
        cur.execute("SELECT id FROM warehouse_inventory WHERE product_id=%s AND location=%s", (product_id, location))
        if cur.fetchone():
            cur.execute("UPDATE warehouse_inventory SET quantity_storage_units=quantity_storage_units+%s, last_updated=NOW() WHERE product_id=%s AND location=%s", (qty, product_id, location))
        else:
            cur.execute("INSERT INTO warehouse_inventory (product_id, location, quantity_storage_units) VALUES (%s,%s,%s)", (product_id, location, qty))

        # For consumables, also reverse the tech_inventory addition
        if not is_equipment and tech_id:
            cur.execute("""
                UPDATE tech_inventory
                SET quantity_storage_units = quantity_storage_units - %s, last_updated = NOW()
                WHERE technician_id = %s AND product_id = %s
            """, (qty, tech_id, product_id))

        # Mark the transaction as undone rather than deleting it
        cur.execute("""
            UPDATE inventory_transactions
            SET notes = notes || ' | UNDONE by ' || %s,
                transaction_type = 'transfer_undone'
            WHERE id = %s
        """, (session.get('username', 'unknown'), tid))

        conn.commit()
        cur.close(); conn.close()
        return jsonify({'ok': True, 'undone_qty': qty, 'product': txn['product_name'], 'location': location})
    except Exception as e:
        conn.rollback(); cur.close(); conn.close()
        return jsonify({'error': str(e)}), 500

@app.route('/api/transactions/<int:tid>/edit-transfer', methods=['PUT'])
@login_required
def edit_transfer_transaction(tid):
    """
    Correct the quantity on a 'transfer' or 'equipment_provided' transaction (a
    warehouse-to-tech line from the warehouse history ledger). Adjusts warehouse
    stock and the tech's truck inventory by the difference so both stay correct —
    reports and tech pages read from these same tables, so they update automatically.
    """
    d = request.json or {}
    unit_mode = d.get('unit_mode', 'storage')
    reason = d.get('reason', 'Manual correction')
    try:
        new_qty_input = float(d.get('quantity', 0))
    except (TypeError, ValueError):
        return jsonify({'error': 'quantity must be a number'}), 400
    if new_qty_input < 0:
        return jsonify({'error': 'quantity cannot be negative'}), 400

    conn = get_db(); cur = conn.cursor()
    try:
        cur.execute("""
            SELECT it.*, p.conversion_factor, COALESCE(p.is_equipment, FALSE) AS is_equipment
            FROM inventory_transactions it
            JOIN products p ON it.product_id = p.id
            WHERE it.id=%s
        """, (tid,))
        txn = cur.fetchone()
        if not txn:
            cur.close(); conn.close()
            return jsonify({'error': 'Transaction not found'}), 404
        if txn['transaction_type'] not in ('transfer', 'equipment_provided'):
            cur.close(); conn.close()
            return jsonify({'error': 'Only transfer/equipment transactions can be edited here'}), 400

        cf = float(txn['conversion_factor'])
        old_storage_qty = float(txn['quantity_storage_units'])
        new_storage_qty = new_qty_input if unit_mode == 'storage' else new_qty_input / cf
        diff = new_storage_qty - old_storage_qty  # positive = more was transferred out

        # Extract location from notes (stored as "[Location] ...")
        notes_str = txn['notes'] or ''
        location = 'Holpers'
        if notes_str.startswith('[') and ']' in notes_str:
            location = notes_str[1:notes_str.index(']')]

        # Warehouse loses more (or gets some back) as the transfer amount changes
        cur.execute("""
            UPDATE warehouse_inventory SET quantity_storage_units=quantity_storage_units-%s, last_updated=NOW()
            WHERE product_id=%s AND location=%s
        """, (diff, txn['product_id'], location))
        if cur.rowcount == 0:
            cur.execute("INSERT INTO warehouse_inventory (product_id, location, quantity_storage_units) VALUES (%s,%s,%s)",
                        (txn['product_id'], location, -diff))

        # Tech truck gains more (or loses some) — equipment isn't tracked in tech_inventory
        if not txn['is_equipment'] and txn['technician_id']:
            cur.execute("""
                UPDATE tech_inventory SET quantity_storage_units=quantity_storage_units+%s, last_updated=NOW()
                WHERE technician_id=%s AND product_id=%s
            """, (diff, txn['technician_id'], txn['product_id']))

        cur.execute("""
            UPDATE inventory_transactions
            SET quantity_storage_units=%s,
                notes = notes || ' | CORRECTED: was ' || %s::text || ' ' || %s::text || ', now ' || %s::text || ' (' || %s || ') by ' || %s
            WHERE id=%s
        """, (new_storage_qty, old_storage_qty, 'storage units', new_storage_qty, reason, session.get('username'), tid))

        cur.execute("""
            INSERT INTO audit_log (audit_type, product_id, technician_id, old_quantity, new_quantity, reason, performed_by)
            VALUES ('transfer_correction', %s, %s, %s, %s, %s, %s)
        """, (txn['product_id'], txn['technician_id'], old_storage_qty, new_storage_qty,
              f"[{location}] Txn #{tid}: {reason}", session.get('username')))

        conn.commit()
        cur.close(); conn.close()
        return jsonify({'ok': True, 'old_quantity_storage': old_storage_qty, 'new_quantity_storage': new_storage_qty, 'diff': diff})
    except Exception as e:
        conn.rollback(); cur.close(); conn.close()
        return jsonify({'error': str(e)}), 500

# ─── FIELDROUTES API SYNC ─────────────────────────────────────────────────────

def fr_post(endpoint, body):
    """
    Core POST request to FieldRoutes API.
    All FR endpoints use POST with auth in the request body.
    Pattern: POST https://holperspest.fieldroutes.com/api/{resource}/{action}
    """
    url = f"{FR_BASE_URL.rstrip('/')}/api/{endpoint}"
    payload = {
        'authenticationKey': FR_API_KEY,
        'authenticationToken': FR_AUTH_KEY,
    }
    payload.update(body or {})
    resp = requests.post(url, json=payload, timeout=30)
    resp.raise_for_status()
    return resp.json()

# ─── SAMSARA FLEET API ────────────────────────────────────────────────────────

def samsara_get(path, params=None):
    """GET against the Samsara fleet API (Bearer token auth)."""
    url = f"{SAMSARA_BASE_URL.rstrip('/')}/{path.lstrip('/')}"
    headers = {'Authorization': f'Bearer {SAMSARA_API_TOKEN}', 'Accept': 'application/json'}
    resp = requests.get(url, headers=headers, params=params or {}, timeout=30)
    resp.raise_for_status()
    return resp.json()

@app.route('/api/samsara/_probe', methods=['GET'])
def samsara_probe():
    """
    Temporary connection probe (token-gated, like the sync endpoints).
    Verifies the Samsara token works and surfaces how vehicles are named,
    so we can map them to technicians.truck_id. Remove once mapping is set.
    """
    if request.args.get('token', '') != os.environ.get('SYNC_TOKEN', ''):
        return jsonify({'error': 'Invalid token'}), 403
    out = {'token_present': bool(SAMSARA_API_TOKEN)}
    # 1) Vehicle list — id / name / plate / vin
    try:
        v = samsara_get('fleet/vehicles', {'limit': 50})
        vehicles = v.get('data', v if isinstance(v, list) else [])
        out['vehicle_count'] = len(vehicles)
        out['vehicles_sample'] = [
            {k: veh.get(k) for k in ('id', 'name', 'licensePlate', 'vin', 'make', 'model', 'year') if k in veh}
            for veh in vehicles[:60]
        ]
    except Exception as e:
        out['vehicles_error'] = str(e)
    # 2) Latest stats snapshot — confirms which stat types are available
    try:
        s = samsara_get('fleet/vehicles/stats', {'types': 'gps,engineStates,obdOdometerMeters,fuelPercents'})
        data = s.get('data', [])
        out['stats_count'] = len(data)
        out['stats_sample'] = data[:2]
    except Exception as e:
        out['stats_error'] = str(e)
    # 3) Our technician truck_ids, to compare against Samsara vehicle names
    try:
        conn = get_db(); cur = conn.cursor()
        cur.execute("SELECT id, name, truck_id FROM technicians WHERE active=TRUE AND truck_id IS NOT NULL ORDER BY truck_id")
        out['technicians'] = [dict(r) for r in cur.fetchall()]
        cur.close(); conn.close()
    except Exception as e:
        out['tech_error'] = str(e)
    return jsonify(out)

def _truck_num(s):
    """Extract the truck number from 'Truck 33' or Samsara's '#33 2022 Toyota'."""
    m = re.search(r'(\d+)', s or '')
    return int(m.group(1)) if m else None

_SAMSARA_STAT_TYPES = 'gps,engineStates,fuelPercents,obdOdometerMeters'

def _samsara_vehicle_index():
    """Latest stats snapshot for every vehicle, keyed by truck number."""
    data = samsara_get('fleet/vehicles/stats', {'types': _SAMSARA_STAT_TYPES}).get('data', [])
    idx = {}
    for v in data:
        n = _truck_num(v.get('name'))
        if n is not None:
            idx[n] = v
    return idx

def _samsara_vehicle_public(v, tech=None):
    """Shape a Samsara stats object into the fields the UI needs."""
    gps = v.get('gps') or {}
    odo_m = (v.get('obdOdometerMeters') or {}).get('value')
    return {
        'truck_num': _truck_num(v.get('name')),
        'vehicle_name': v.get('name'),
        'vehicle_id': v.get('id'),
        'vin': (v.get('externalIds') or {}).get('samsara.vin'),
        'tech_id': tech['id'] if tech else None,
        'tech_name': tech['name'] if tech else None,
        'lat': gps.get('latitude'),
        'lng': gps.get('longitude'),
        'address': (gps.get('reverseGeo') or {}).get('formattedLocation'),
        'speed_mph': gps.get('speedMilesPerHour'),
        'heading': gps.get('headingDegrees'),
        'engine': (v.get('engineState') or {}).get('value'),
        'fuel_pct': (v.get('fuelPercent') or {}).get('value'),
        'odometer_miles': round(odo_m / 1609.344) if odo_m else None,
        'gps_time': gps.get('time'),
    }

@app.route('/api/samsara/fleet', methods=['GET'])
@login_required
def samsara_fleet():
    """All vehicles with a live location, matched to technicians — drives the map."""
    if not SAMSARA_API_TOKEN:
        return jsonify({'error': 'Samsara not configured'}), 400
    try:
        idx = _samsara_vehicle_index()
    except Exception as e:
        return jsonify({'error': f'Samsara error: {e}'}), 502
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT id, name, truck_id FROM technicians WHERE active=TRUE AND truck_id IS NOT NULL")
    techs = {}
    for t in cur.fetchall():
        n = _truck_num(t['truck_id'])
        if n is not None:
            techs[n] = dict(t)
    cur.close(); conn.close()
    vehicles = []
    for n, v in idx.items():
        gps = v.get('gps') or {}
        if gps.get('latitude') is None:
            continue
        vehicles.append(_samsara_vehicle_public(v, techs.get(n)))
    vehicles.sort(key=lambda x: (x['tech_name'] is None, (x['tech_name'] or x['vehicle_name'] or '').lower()))
    return jsonify({'vehicles': vehicles, 'count': len(vehicles)})

@app.route('/api/samsara/truck', methods=['GET'])
@login_required
def samsara_truck():
    """Detailed live info + recent trips for one truck (by truck_id) — the tech card."""
    truck_id = request.args.get('truck_id', '')
    n = _truck_num(truck_id)
    if n is None:
        return jsonify({'linked': False, 'reason': 'no truck number'})
    if not SAMSARA_API_TOKEN:
        return jsonify({'error': 'Samsara not configured'}), 400
    try:
        idx = _samsara_vehicle_index()
    except Exception as e:
        return jsonify({'error': f'Samsara error: {e}'}), 502
    v = idx.get(n)
    if not v:
        return jsonify({'linked': False, 'truck_num': n})
    result = _samsara_vehicle_public(v, None)
    result['linked'] = True
    result['trips'] = []
    # Recent trips — best effort (endpoint availability varies by Samsara plan)
    try:
        end_ms = int(datetime.utcnow().timestamp() * 1000)
        start_ms = end_ms - 3 * 24 * 60 * 60 * 1000
        tr = samsara_get('fleet/trips', {'vehicleIds': v.get('id'), 'startMs': start_ms, 'endMs': end_ms})
        trips = tr.get('data') or tr.get('trips') or []
        result['trips'] = trips[:15]
    except Exception as e:
        result['trips_error'] = str(e)
    return jsonify(result)

def fr_search_ids(resource, params=None):
    """
    Step 1: Search for IDs. Returns list of IDs.
    POST /api/{resource}/search  → {resourceIDs: [...]}
    Handles pagination automatically (50k IDs per page max).
    """
    id_field = f"{resource}IDs"
    all_ids = []
    last_id = None

    while True:
        body = dict(params or {})
        if last_id:
            body[id_field] = {'operator': '>', 'value': last_id}
        result = fr_post(f"{resource}/search", body)
        if 'errorMessage' in result:
            raise Exception(f"FR API error ({resource}/search): {result['errorMessage']}")
        batch = result.get(id_field, [])
        all_ids.extend(batch)
        if len(batch) < 50000:
            break
        last_id = batch[-1]

    return all_ids

def fr_get_records(resource, ids, data_key=None, chunk_size=1000):
    """
    Step 2: Get full records for a list of IDs.
    POST /api/{resource}/get  → {data_key: [...records...]}
    Handles chunking automatically.
    """
    import time
    id_field = f"{resource}IDs"
    if data_key is None:
        # e.g. chemical→chemicals, chemicalUse→chemicalUses, appointment→appointments
        data_key = f"{resource}s"

    all_records = []
    for i in range(0, len(ids), chunk_size):
        chunk = ids[i:i + chunk_size]
        result = fr_post(f"{resource}/get", {id_field: chunk})
        if 'errorMessage' in result:
            raise Exception(f"FR API error ({resource}/get): {result['errorMessage']}")
        records = result.get(data_key, [])
        all_records.extend(records)
        if i + chunk_size < len(ids):
            time.sleep(1.5)

    return all_records

def fr_fetch_all(resource, search_params=None, data_key=None, chunk_size=1000):
    """
    Convenience: search + get in one call. Returns full records.
    """
    ids = fr_search_ids(resource, search_params)
    if not ids:
        return []
    return fr_get_records(resource, ids, data_key, chunk_size)

def get_sync_state(cur, key):
    """Get a stored sync state value."""
    cur.execute("SELECT value FROM sync_state WHERE key=%s", (key,))
    row = cur.fetchone()
    return row['value'] if row else None

def set_sync_state(cur, key, value):
    """Store a sync state value."""
    cur.execute("""
        INSERT INTO sync_state (key, value, updated_at)
        VALUES (%s, %s, NOW())
        ON CONFLICT (key) DO UPDATE SET value=%s, updated_at=NOW()
    """, (key, value, value))

def _fr_search_ids(endpoint, id_field, extra_params):
    """Paginate /search endpoint — mirrors crm_sync.py search_ids()."""
    all_ids = []
    cursor = None
    while True:
        params = dict(extra_params)
        if cursor:
            params[id_field] = {'operator': '>', 'value': cursor}
        data = fr_post(endpoint, params)
        if 'errorMessage' in data:
            raise Exception(f"API error {endpoint}: {data['errorMessage']}")
        batch = data.get(id_field, [])
        all_ids.extend(batch)
        if len(batch) < 50000:
            break
        cursor = batch[-1]
    return all_ids


def _fr_fetch_details(endpoint, id_field, record_field, ids):
    """Fetch records in chunks of 1000 — mirrors crm_sync.py fetch_details()."""
    results = []
    for i in range(0, len(ids), 1000):
        chunk = ids[i:i+1000]
        data = fr_post(endpoint, {id_field: chunk})
        if 'errorMessage' in data:
            continue
        results.extend(data.get(record_field, []))
    return results


def _store_appointments(cur, appts):
    """Upsert appointments into fr_appointments_raw."""
    stored = 0
    for appt in appts:
        appt_id = str(appt.get('appointmentID', ''))
        if not appt_id:
            continue
        sb = str(appt.get('servicedBy') or '').strip()
        cb = str(appt.get('completedBy') or '').strip()
        at = str(appt.get('assignedTech') or '').strip()
        ei = str(appt.get('employeeID') or '').strip()
        tech_fr_id = sb or cb or at or ei or ''
        dc = str(appt.get('dateCompleted') or '')[:10]
        sd = str(appt.get('date') or '')[:10]
        date_completed = dc if dc not in ('0000-00-00', '') else None
        scheduled_date = sd if sd not in ('0000-00-00', '') else None
        appt_date = date_completed or scheduled_date
        svc_type_id = str(appt.get('type', '') or '').strip() or None
        cur.execute("""
            INSERT INTO fr_appointments_raw
            (appointment_id,customer_id,status,status_text,date_completed,
             scheduled_date,serviced_by,completed_by,assigned_tech,employee_id,
             tech_fr_id,appt_date,service_type_id,raw_json,updated_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
            ON CONFLICT (appointment_id) DO UPDATE SET
                status=EXCLUDED.status, status_text=EXCLUDED.status_text,
                date_completed=EXCLUDED.date_completed,
                serviced_by=EXCLUDED.serviced_by, completed_by=EXCLUDED.completed_by,
                assigned_tech=EXCLUDED.assigned_tech, tech_fr_id=EXCLUDED.tech_fr_id,
                appt_date=EXCLUDED.appt_date, service_type_id=EXCLUDED.service_type_id,
                raw_json=EXCLUDED.raw_json, updated_at=NOW()
        """, (appt_id, str(appt.get('customerID','')), str(appt.get('status','')),
              str(appt.get('statusText','')), date_completed, scheduled_date,
              sb or None, cb or None, at or None, ei or None,
              tech_fr_id or None, appt_date, svc_type_id, json.dumps(appt)))
        stored += 1
    return stored


def _store_chemical_uses(cur, chem_uses):
    """Upsert chemical use records into fr_chemical_uses_raw."""
    stored = 0
    for cu in chem_uses:
        cu_id = str(cu.get('chemicalUseID', ''))
        if not cu_id:
            continue
        try:
            conc = float(cu.get('concentratedAmount', 0) or 0)
            amt = float(cu.get('amount', 0) or 0)
            mix_num = float(cu.get('mixRatioNumerator', 0) or 0)
            mix_den = float(cu.get('mixRatioDenominator', 0) or 0)

            if conc > 0:
                # Concentrated amount recorded directly — most reliable for liquids
                resolved_amount = conc
                resolved_unit = str(cu.get('concentratedUnit','') or cu.get('unit','')).strip().upper()
            elif amt > 0 and mix_num > 0 and mix_den > 0:
                # Calculate concentrate from mix ratio
                resolved_amount = amt * (mix_num / mix_den)
                resolved_unit = str(cu.get('mixRatioNumeratorUnit','') or cu.get('concentratedUnit','') or cu.get('unit','')).strip().upper()
            elif amt > 0:
                # Use applied amount directly (granules, baits, traps etc)
                resolved_amount = amt
                resolved_unit = str(cu.get('unit','')).strip().upper()
            else:
                # Last resort: dosage field — used for bait stations, blox, ant gel
                # (amount/concentratedAmount are 0, FR records placement count in dosage)
                dosage = float(cu.get('dosage', 0) or 0)
                resolved_amount = dosage
                resolved_unit = str(cu.get('unit','')).strip().upper()
        except Exception:
            resolved_amount = 0.0
            resolved_unit = str(cu.get('unit','')).strip().upper()
        if not resolved_unit:
            resolved_unit = 'UNITS'
        dc = str(cu.get('dateCreated','') or '')[:10]
        date_created = dc if dc not in ('0000-00-00','') else None
        cur.execute("""
            INSERT INTO fr_chemical_uses_raw
            (chemical_use_id,appointment_id,customer_id,chemical_id,
             amount,concentrated_amount,unit,concentrated_unit,
             resolved_amount,resolved_unit,date_created,created_by,raw_json)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (chemical_use_id) DO UPDATE SET
                resolved_amount=EXCLUDED.resolved_amount,
                resolved_unit=EXCLUDED.resolved_unit,
                raw_json=EXCLUDED.raw_json,
                -- If FR returned a different (corrected) amount, reset so it gets reprocessed
                inventory_updated = CASE
                    WHEN ABS(fr_chemical_uses_raw.resolved_amount - EXCLUDED.resolved_amount) > 0.0001
                    THEN FALSE
                    ELSE fr_chemical_uses_raw.inventory_updated
                END
        """, (cu_id, str(cu.get('appointmentID','')), str(cu.get('customerID','')),
              str(cu.get('chemicalID','')),
              float(cu.get('amount',0) or 0), float(cu.get('concentratedAmount',0) or 0),
              str(cu.get('unit','')).strip().upper(),
              str(cu.get('concentratedUnit','')).strip().upper(),
              resolved_amount, resolved_unit, date_created,
              str(cu.get('createdBy','')), json.dumps(cu)))
        stored += 1
    return stored


def sync_appointments_to_postgres():
    """
    Pull appointments from FR into Postgres.

    Pass 1: appointmentIDs > last_seen_id  (brand-new records)
    Pass 2: dateUpdated > last_run         (status/tech changes)
    Pass 3: dateCompleted >= 2 days ago    (pre-scheduled appts completed recently —
                                            their IDs are below the cursor so Pass 1
                                            misses them entirely)

    First run: no cursor → pulls everything.
    Every run: incremental + recent-completions safety net.
    """
    if not FR_API_KEY or not FR_AUTH_KEY:
        return {'error': 'FR API keys not configured'}

    conn = get_db()
    cur = conn.cursor()
    result = {'pass1_new': 0, 'pass2_updated': 0, 'pass3_recent': 0, 'errors': []}

    try:
        last_seen_id = get_sync_state(cur, 'appt_last_seen_id')
        last_run = get_sync_state(cur, 'appt_last_run')

        base_params = {
            'status': 1,
            'date': {'operator': '>', 'value': '2019-12-31'},
        }

        # Pass 1 — New records (ID > last_seen_id)
        pass1_params = dict(base_params)
        if last_seen_id:
            pass1_params['appointmentIDs'] = {'operator': '>', 'value': int(last_seen_id)}

        new_ids = _fr_search_ids('appointment/search', 'appointmentIDs', pass1_params)
        if new_ids:
            appts = _fr_fetch_details('appointment/get', 'appointmentIDs', 'appointments', new_ids)
            result['pass1_new'] = _store_appointments(cur, appts)
            conn.commit()
            new_max = max(int(i) for i in new_ids)
            old_max = int(last_seen_id) if last_seen_id else 0
            set_sync_state(cur, 'appt_last_seen_id', str(max(new_max, old_max)))
            conn.commit()

        # Pass 2 — Updated records (dateUpdated > last_run)
        if last_run:
            last_run_str = last_run[:19]
            updated_ids = _fr_search_ids('appointment/search', 'appointmentIDs', {
                **base_params,
                'dateUpdated': {'operator': '>', 'value': last_run_str},
            })
            if updated_ids:
                updated_appts = _fr_fetch_details('appointment/get', 'appointmentIDs', 'appointments', updated_ids)
                result['pass2_updated'] = _store_appointments(cur, updated_appts)
                conn.commit()

        # Pass 3 — Recently completed (dateCompleted >= 2 days ago)
        # Catches pre-scheduled appointments whose IDs are below our cursor.
        # 2-day window = cheap (2 API calls) and guaranteed safety net.
        two_days_ago = (datetime.utcnow() - timedelta(days=2)).strftime('%Y-%m-%d')
        recent_ids = _fr_search_ids('appointment/search', 'appointmentIDs', {
            'status': 1,
            'dateCompleted': {'operator': '>=', 'value': two_days_ago},
        })
        if recent_ids:
            recent_appts = _fr_fetch_details('appointment/get', 'appointmentIDs', 'appointments', recent_ids)
            result['pass3_recent'] = _store_appointments(cur, recent_appts)
            conn.commit()

        set_sync_state(cur, 'appt_last_run', datetime.utcnow().isoformat())
        conn.commit()

    except Exception as e:
        result['errors'].append(str(e))
        try: conn.rollback()
        except: pass
    finally:
        cur.close(); conn.close()

    return result


def sync_chemical_uses_to_postgres():
    """
    Pull chemical uses from FR into Postgres.

    Pass 1: chemicalUseIDs > last_seen_id  (new records)
    Pass 2: re-fetch all chemical uses for appointments completed in the
            last 2 days. Catches pre-planned records that had amount=0 at
            creation and were updated by the tech when completing the job.
            The upsert resets inventory_updated=FALSE if amounts changed,
            so the next process step picks up the corrected values.
    """
    if not FR_API_KEY or not FR_AUTH_KEY:
        return {'error': 'FR API keys not configured'}

    conn = get_db()
    cur = conn.cursor()
    result = {'pass1_new': 0, 'pass2_refreshed': 0, 'errors': []}

    try:
        last_seen_id = get_sync_state(cur, 'cu_last_seen_id')

        # Pass 1 — New records (ID > last_seen_id)
        pass1_params = {}
        if last_seen_id:
            pass1_params['chemicalUseIDs'] = {'operator': '>', 'value': int(last_seen_id)}

        new_ids = _fr_search_ids('chemicalUse/search', 'chemicalUseIDs', pass1_params)
        if new_ids:
            chem_uses = _fr_fetch_details('chemicalUse/get', 'chemicalUseIDs', 'chemicalUses', new_ids)
            result['pass1_new'] = _store_chemical_uses(cur, chem_uses)
            conn.commit()
            new_max = max(int(i) for i in new_ids)
            old_max = int(last_seen_id) if last_seen_id else 0
            set_sync_state(cur, 'cu_last_seen_id', str(max(new_max, old_max)))
            set_sync_state(cur, 'cu_last_run', datetime.utcnow().isoformat())
            conn.commit()

        # Pass 2 — Refresh chemical uses for recently completed appointments
        # Techs may update amounts after a record was first created (pre-planned jobs).
        # We re-fetch every chemical use we already have for the last 2 days and
        # let the upsert reset inventory_updated if the amount changed.
        two_days_ago = (datetime.utcnow() - timedelta(days=2)).strftime('%Y-%m-%d')
        cur.execute("""
            SELECT chemical_use_id FROM fr_chemical_uses_raw
            WHERE appointment_id IN (
                SELECT appointment_id FROM fr_appointments_raw
                WHERE appt_date >= %s
            )
        """, (two_days_ago,))
        refresh_ids = [int(r['chemical_use_id']) for r in cur.fetchall()
                       if str(r['chemical_use_id']).isdigit()]
        if refresh_ids:
            fresh_cus = _fr_fetch_details('chemicalUse/get', 'chemicalUseIDs', 'chemicalUses', refresh_ids)
            result['pass2_refreshed'] = _store_chemical_uses(cur, fresh_cus)
            conn.commit()

    except Exception as e:
        result['errors'].append(str(e))
        try: conn.rollback()
        except: pass
    finally:
        cur.close(); conn.close()

    return result


def pull_and_store_fr_data(target_date=None):
    """Wrapper that runs both syncs — used by the daily cron."""
    appt_result = sync_appointments_to_postgres()
    cu_result = sync_chemical_uses_to_postgres()
    return {
        'appointments_new': appt_result.get('pass1_new', 0),
        'appointments_updated': appt_result.get('pass2_updated', 0),
        'chemical_uses_new': cu_result.get('pass1_new', 0),
        'errors': appt_result.get('errors', []) + cu_result.get('errors', []),
    }


def _convert_liquid_unit(amount, from_unit, to_unit):
    """
    Convert a liquid amount between common units.
    Returns the converted value, or the original amount if conversion is unknown.

    Handles: fl oz, oz, ml, milliliter, l, liter, gallon, gal, pt, pint, qt, quart
    All case-insensitive, ignores spaces and dots.
    """
    def _normalize(u):
        u = str(u or '').strip().upper().replace(' ', '').replace('.', '').replace('_', '').replace('-', '')
        if u in ('FLOZ','FLUIDOZ','FLUIDOUNCE','FLUIDOUNCES','OZ','OUNCE','OUNCES'): return 'FLOZ'
        if u in ('ML','MILLILITER','MILLILITERS','MILLILITRE','MILLILITRES'):        return 'ML'
        if u in ('L','LITER','LITERS','LITRE','LITRES'):                            return 'L'
        if u in ('GAL','GALLON','GALLONS'):                                          return 'GAL'
        if u in ('PT','PINT','PINTS'):                                              return 'PT'
        if u in ('QT','QUART','QUARTS'):                                            return 'QT'
        return u  # unknown

    # mL per unit
    _TO_ML = {'FLOZ': 29.5735, 'ML': 1.0, 'L': 1000.0, 'GAL': 3785.41, 'PT': 473.176, 'QT': 946.353}

    f = _normalize(from_unit)
    t = _normalize(to_unit)
    if f == t:
        return amount
    if f in _TO_ML and t in _TO_ML:
        return amount * _TO_ML[f] / _TO_ML[t]
    return amount   # unknown unit — return as-is, caller logs the mismatch


def process_inventory_from_stored_data(target_date=None):
    """
    Step 2 of daily sync: Read from fr_appointments_raw + fr_chemical_uses_raw
    and deduct inventory from techs' trucks.

    This runs AFTER pull_and_store_fr_data so we're working from stored data,
    not live API calls. You can inspect the raw tables to see exactly what came in.

    Logic matches ChemicalUsageTech.py exactly:
    - For each chemicalUse record where inventory_updated=FALSE
    - Look up the appointment to get tech and date
    - Filter: date must be in range
    - Amount: concentratedAmount > 0 → use that, else use amount
    - Match chemicalID to our product_lookup
    - Deduct from tech's truck inventory
    """
    if not target_date:
        target_date = (datetime.utcnow() - timedelta(days=1)).strftime('%Y-%m-%d')

    target_date_str = str(target_date)[:10]

    usage_deducted = 0
    skipped_no_product = 0
    skipped_no_tech = 0
    skipped_no_appt = 0
    skipped_zero_amount = 0
    skipped_already_done = 0
    errors = []

    conn = get_db()
    cur = conn.cursor()

    try:
        # Internal lookups
        cur.execute("SELECT id, fr_employee_id FROM technicians WHERE active=TRUE AND fr_employee_id IS NOT NULL")
        tech_lookup = {str(r['fr_employee_id']): r['id'] for r in cur.fetchall()}

        cur.execute("SELECT id, fr_product_id, conversion_factor, storage_unit, usage_unit, COALESCE(use_applied_amount, FALSE) as use_applied_amount FROM products WHERE active=TRUE AND fr_product_id IS NOT NULL")
        product_lookup = {str(r['fr_product_id']): dict(r) for r in cur.fetchall()}

        # Get ALL unprocessed chemical use records joined with appointments
        # Get ALL unprocessed chemical uses for target date
        # inventory_updated=FALSE means not yet deducted from truck
        # We process everything for the date regardless of when it was pulled
        cur.execute("""
            SELECT
                cu.chemical_use_id, cu.appointment_id, cu.customer_id,
                cu.chemical_id, cu.resolved_amount, cu.resolved_unit,
                cu.raw_json,
                a.tech_fr_id, a.appt_date, a.status, a.customer_id AS appt_customer_id
            FROM fr_chemical_uses_raw cu
            INNER JOIN fr_appointments_raw a ON cu.appointment_id = a.appointment_id
            WHERE cu.inventory_updated = FALSE
              AND a.appt_date = %s
              AND a.tech_fr_id IS NOT NULL
            ORDER BY cu.chemical_use_id
        """, (target_date_str,))

        records = cur.fetchall()

        for row in records:
            cu_id = row['chemical_use_id']
            appt_id = row['appointment_id']
            chemical_id = row['chemical_id']

            # Resolve product early so it's available for unit conversion below
            prod = product_lookup.get(chemical_id)
            if not prod:
                skipped_no_product += 1
                continue

            # Re-resolve amount from raw_json using latest logic
            # This catches records stored before the mix ratio fix
            raw_val = row.get('raw_json') or {}
            raw = raw_val if isinstance(raw_val, dict) else json.loads(raw_val)
            try:
                conc = float(raw.get('concentratedAmount', 0) or 0)
                amt = float(raw.get('amount', 0) or 0)
                mix_num = float(raw.get('mixRatioNumerator', 0) or 0)
                mix_den = float(raw.get('mixRatioDenominator', 0) or 0)
                # Per-product flag: use applied amount (gels, baits, granules)
                # instead of concentrated amount (liquids mixed with water)
                prod_use_applied = prod.get('use_applied_amount', False)
                if prod_use_applied:
                    # Use the raw applied amount — what the tech physically put down.
                    # Do NOT fall back to dosage: dosage=1 is a FieldRoutes service
                    # template default and does NOT mean the product was actually used.
                    # When a tech actually uses the product they fill in the amount field.
                    resolved_amount = amt
                    resolved_unit = str(raw.get('unit','')).strip().upper()
                elif conc > 0:
                    # Best case: FR populated the concentrated amount directly.
                    # Convert to the product's usage unit if the tech entered a different unit
                    # (e.g. tech enters 15 mL but product is tracked in fl oz).
                    conc_unit  = str(raw.get('concentratedUnit','') or raw.get('unit','')).strip()
                    prod_unit  = str(prod.get('usage_unit', '')).strip()
                    resolved_amount = _convert_liquid_unit(conc, conc_unit, prod_unit)
                    resolved_unit = prod_unit.upper() if prod_unit else conc_unit.upper()
                elif amt > 0 and mix_num > 0 and mix_den > 0 and (mix_num / mix_den) < 0.9999:
                    # Real dilution ratio (e.g. 1 oz per 128 oz = 1/128).
                    # Compute concentrate from applied volume × ratio, then convert units.
                    ratio_unit = str(raw.get('mixRatioNumeratorUnit','') or raw.get('concentratedUnit','') or raw.get('unit','')).strip()
                    prod_unit  = str(prod.get('usage_unit', '')).strip()
                    raw_conc   = amt * (mix_num / mix_den)
                    resolved_amount = _convert_liquid_unit(raw_conc, ratio_unit, prod_unit)
                    resolved_unit = prod_unit.upper() if prod_unit else ratio_unit.upper()
                elif amt > 0 and (mix_num <= 0 or mix_den <= 0):
                    # No mix ratio stored at all — amount is the best estimate we have.
                    # This covers full-strength products or cases where FR omitted the ratio.
                    resolved_amount = amt
                    resolved_unit = str(raw.get('unit','')).strip().upper()
                else:
                    # concentratedAmount = 0 AND mix ratio is 1:1 (FR's default placeholder,
                    # not a real dilution rate). We cannot determine how much concentrate was
                    # actually used — deducting the diluted solution volume would massively
                    # overcount (e.g. 20 fl oz spray mixture ≠ 20 fl oz concentrate).
                    # Skip this record rather than record a wrong deduction.
                    resolved_amount = 0.0
                    resolved_unit = str(raw.get('unit','')).strip().upper()
            except Exception:
                resolved_amount = float(row['resolved_amount'] or 0)
                resolved_unit = str(row['resolved_unit'] or 'UNITS')
            if not resolved_unit:
                resolved_unit = 'UNITS'
            tech_fr_id = str(row['tech_fr_id'] or '').strip()
            appt_date = str(row['appt_date']) if row['appt_date'] else None
            customer_id = row['customer_id']

            # Skip if no appointment found
            if not appt_date:
                skipped_no_appt += 1
                continue

            # Date filter — only process records for target date
            if appt_date != target_date_str:
                skipped_no_appt += 1
                continue

            # Skip if no tech
            if not tech_fr_id or tech_fr_id in ('', '0', 'None'):
                skipped_no_tech += 1
                continue

            tech_id = tech_lookup.get(tech_fr_id)
            if not tech_id:
                skipped_no_tech += 1
                continue

            if resolved_amount <= 0:
                skipped_zero_amount += 1
                # Mark as processed so we don't retry zero-amount records
                cur.execute("UPDATE fr_chemical_uses_raw SET inventory_updated=TRUE WHERE chemical_use_id=%s", (cu_id,))
                continue

            # For granular/discrete products (blox, packs, each, etc.) round the
            # resolved amount to the nearest whole unit before converting.
            # FR sometimes returns 40.99 when the tech placed 41 items.
            _GRANULAR = {'units','unit','each','piece','pieces','number',
                         'blox','block','blocks','pack','packs','place pack','place packs'}
            _uu = str(prod.get('usage_unit', '')).lower().strip()
            _cf = float(prod['conversion_factor'])
            if _uu in _GRANULAR or _cf == 1:
                resolved_amount = round(resolved_amount)

            # Convert and deduct
            storage_units_used = resolved_amount / _cf

            cur.execute("""
                INSERT INTO tech_inventory (technician_id, product_id, quantity_storage_units)
                VALUES (%s, %s, 0) ON CONFLICT (technician_id, product_id) DO NOTHING
            """, (tech_id, prod['id']))

            cur.execute("""
                UPDATE tech_inventory
                SET quantity_storage_units = quantity_storage_units - %s, last_updated = NOW()
                WHERE technician_id=%s AND product_id=%s
            """, (storage_units_used, tech_id, prod['id']))

            cur.execute("""
                INSERT INTO inventory_transactions
                (transaction_type, product_id, technician_id, quantity_storage_units,
                 quantity_usage_units, fr_appointment_id, appt_date, notes, performed_by)
                VALUES ('usage_sync', %s, %s, %s, %s, %s, %s, %s, 'FR Daily Sync')
            """, (
                prod['id'], tech_id, storage_units_used, resolved_amount, appt_id,
                appt_date,
                f"apptDate:{appt_date} | customerID:{customer_id} | chemicalUseID:{cu_id} | unit:{resolved_unit} | techFRID:{tech_fr_id}"
            ))

            # Mark as processed
            cur.execute("UPDATE fr_chemical_uses_raw SET inventory_updated=TRUE WHERE chemical_use_id=%s", (cu_id,))
            usage_deducted += 1

        conn.commit()

        # Log
        cur.execute("""
            INSERT INTO fr_sync_log (sync_date, appointments_processed, usage_deducted, errors)
            VALUES (%s, %s, %s, %s)
        """, (target_date_str, usage_deducted, usage_deducted,
              json.dumps(errors) if errors else None))
        conn.commit()

    except Exception as e:
        errors.append(f'Process failed: {str(e)}')
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        cur.close()
        conn.close()

    return {
        'target_date': target_date_str,
        'usage_deducted': usage_deducted,
        'skipped_untracked_chemicals': skipped_no_product,
        'skipped_no_tech': skipped_no_tech,
        'skipped_no_appointment': skipped_no_appt,
        'skipped_zero_amount': skipped_zero_amount,
        'errors': errors
    }


def sync_usage_from_fieldroutes(sync_date=None, backfill_from=None):
    """
    Compatibility wrapper — runs pull then process.
    Used by the Sync Now button and backfill.
    """
    if not sync_date:
        sync_date = date.today().isoformat()

    target = backfill_from if backfill_from else sync_date

    # Step 1: Pull raw data from FR and store in Postgres
    pull_result = pull_and_store_fr_data(target)

    if 'error' in pull_result:
        return pull_result

    # Step 2: Process stored data into inventory deductions
    process_result = process_inventory_from_stored_data(target)

    # Merge results
    return {
        'sync_date': sync_date,
        'target_date': target,
        'appointments_stored': pull_result.get('appointments_stored', 0),
        'chemical_uses_stored': pull_result.get('chemical_uses_stored', 0),
        'usage_deducted': process_result.get('usage_deducted', 0),
        'skipped_untracked_chemicals': process_result.get('skipped_untracked_chemicals', 0),
        'skipped_no_tech': process_result.get('skipped_no_tech', 0),
        'skipped_no_appointment': process_result.get('skipped_no_appointment', 0),
        'skipped_zero_amount': process_result.get('skipped_zero_amount', 0),
        'pull_errors': pull_result.get('errors', []),
        'process_errors': process_result.get('errors', []),
    }


@app.route('/api/sync', methods=['POST'])
@login_required
def trigger_sync():
    """
    Sync Now button — kicks off the full pipeline in a background thread and
    returns immediately so Railway's 30-second request timeout is never hit.
    Poll /api/sync/jobs → sync_now for progress and results.
    """
    d = request.json or {}
    target_date = d.get('date', date.today().isoformat())

    def _sync_pipeline():
        try:
            # Step 1: pull new + recently-completed appointments (3 passes)
            appt_result = sync_appointments_to_postgres()
            # Step 2: pull new chemical uses
            cu_result = sync_chemical_uses_to_postgres()
            # Step 3: fill any chemical-use → appointment gaps (no extra API calls
            #          if Pass 3 already grabbed them, but runs fast either way)
            _do_fill_missing()
            # Step 4: deduct inventory for today
            result = process_inventory_from_stored_data(target_date)
            _bg_set_status('sync_now', {
                'status': 'done',
                'date': target_date,
                'usage_deducted': result.get('usage_deducted', 0),
                'skipped_no_tech': result.get('skipped_no_tech', 0),
                'skipped_zero_amount': result.get('skipped_zero_amount', 0),
                'skipped_untracked_chemicals': result.get('skipped_untracked_chemicals', 0),
                'appt_pass1': appt_result.get('pass1_new', 0),
                'appt_pass2': appt_result.get('pass2_updated', 0),
                'appt_pass3': appt_result.get('pass3_recent', 0),
                'cu_new': cu_result.get('pass1_new', 0),
                'errors': result.get('errors', []) + appt_result.get('errors', []) + cu_result.get('errors', []),
                'finished': datetime.utcnow().isoformat(),
            })
        except Exception as e:
            _bg_set_status('sync_now', {
                'status': 'error',
                'error': str(e),
                'date': target_date,
                'finished': datetime.utcnow().isoformat(),
            })

    _bg_set_status('sync_now', {'status': 'running', 'date': target_date, 'started': datetime.utcnow().isoformat()})
    import threading
    threading.Thread(target=_sync_pipeline, daemon=True).start()
    return jsonify({'status': 'started', 'date': target_date})

@app.route('/api/sync/debug', methods=['GET'])
@login_required
def sync_debug():
    """Test FR API connection — shows what each call returns for a given date."""
    results = {}
    check_date = request.args.get('date', (date.today() - timedelta(days=1)).isoformat())

    # Test 1: chemicalUse exact date
    try:
        data = fr_post('chemicalUse/search', {'dateCreated': check_date})
        ids = data.get('chemicalUseIDs', [])
        results['chemicaluse_exact_date'] = {
            'ok': True, 'count': len(ids), 'date': check_date,
            'min_id': min(ids) if ids else None,
            'max_id': max(ids) if ids else None,
        }
    except Exception as e:
        results['chemicaluse_exact_date'] = {'ok': False, 'error': str(e)}

    # Test 2: chemicalUse >= date
    try:
        data = fr_post('chemicalUse/search', {
            'dateCreated': {'operator': '>=', 'value': check_date}
        })
        ids = data.get('chemicalUseIDs', [])
        results['chemicaluse_gte_date'] = {
            'ok': True, 'count': len(ids),
            'min_id': min(ids) if ids else None,
            'max_id': max(ids) if ids else None,
        }
    except Exception as e:
        results['chemicaluse_gte_date'] = {'ok': False, 'error': str(e)}

    # Test 3: sample 3 chemicalUse records
    try:
        data = fr_post('chemicalUse/search', {'dateCreated': check_date})
        sample_ids = data.get('chemicalUseIDs', [])[:3]
        if sample_ids:
            detail = fr_post('chemicalUse/get', {'chemicalUseIDs': sample_ids})
            results['sample_records'] = detail.get('chemicalUses', [])
    except Exception as e:
        results['sample_records'] = {'error': str(e)}

    # Test 4: sync state
    conn = get_db()
    cur = conn.cursor()
    last_id = get_sync_state(cur, 'last_chemicaluse_id')
    last_sync = get_sync_state(cur, 'last_sync_at')
    cur.execute("SELECT COUNT(*) as cnt FROM products WHERE active=TRUE AND fr_product_id IS NOT NULL")
    linked_products = cur.fetchone()['cnt']
    cur.execute("SELECT COUNT(*) as cnt FROM technicians WHERE active=TRUE AND fr_employee_id IS NOT NULL")
    linked_techs = cur.fetchone()['cnt']
    cur.execute("SELECT COUNT(*) as cnt FROM inventory_transactions WHERE transaction_type='usage_sync'")
    synced = cur.fetchone()['cnt']
    cur.close(); conn.close()
    results['sync_state'] = {'last_chemicaluse_id': last_id, 'last_sync_at': last_sync, 'total_synced': synced}
    results['linkage'] = {'products_linked': linked_products, 'techs_linked': linked_techs}

    # Test: fetch appointment and show key fields
    try:
        test_appt_id = request.args.get('appt_id', '505137')
        data = fr_post('appointment/get', {'appointmentIDs': [int(test_appt_id)]})
        appts = data.get('appointments', [])
        if appts:
            a = appts[0]
            results['appointment_lookup_test'] = {
                'appointmentID': a.get('appointmentID'),
                'customerID': a.get('customerID'),
                'status': a.get('status'),
                'statusText': a.get('statusText'),
                'date': a.get('date'),
                'dateCompleted': a.get('dateCompleted'),
                'employeeID': a.get('employeeID'),
                'servicedBy': a.get('servicedBy'),
                'completedBy': a.get('completedBy'),
                'assignedTech': a.get('assignedTech'),
                'subscriptionPreferredTech': a.get('subscriptionPreferredTech'),
            }
        else:
            results['appointment_lookup_test'] = {'found': False}
    except Exception as e:
        results['appointment_lookup_test'] = {'error': str(e)}

    # Show sample chemicalUse records for a specific employee (createdBy)
    try:
        emp_id = request.args.get('emp_id')
        if emp_id:
            data = fr_post('chemicalUse/search', {'dateCreated': check_date})
            all_ids = data.get('chemicalUseIDs', [])[:200]
            if all_ids:
                records = fr_post('chemicalUse/get', {'chemicalUseIDs': all_ids})
                chem_uses = records.get('chemicalUses', [])
                matching = [cu for cu in chem_uses if str(cu.get('createdBy','')) == str(emp_id)][:3]
                results['sample_for_employee'] = {
                    'emp_id': emp_id,
                    'matching_records': matching
                }
    except Exception as e:
        results['sample_for_employee'] = {'error': str(e)}

    return jsonify(results)

@app.route('/api/sync/reset', methods=['POST'])
@login_required
def reset_sync_state():
    """Reset the sync cursor so next sync re-pulls from scratch."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM sync_state WHERE key IN ('last_chemicaluse_id','last_appointment_id','appt_last_seen_id','appt_last_run','cu_last_seen_id','cu_last_run')")
    conn.commit()
    cur.close(); conn.close()
    return jsonify({'ok': True, 'message': 'Sync cursor reset. Next sync will start fresh.'})

@app.route('/api/admin/fix-warehouse-constraint', methods=['POST'])
@login_required
def fix_warehouse_constraint():
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("ALTER TABLE warehouse_inventory DROP CONSTRAINT IF EXISTS warehouse_inventory_product_id_unique")
        conn.commit()
        cur.execute("ALTER TABLE warehouse_inventory DROP CONSTRAINT IF EXISTS wh_product_location_unique")
        conn.commit()
        # Set NULL locations to Holpers before adding constraint
        cur.execute("UPDATE warehouse_inventory SET location='Holpers' WHERE location IS NULL OR location=''")
        conn.commit()
        cur.execute("ALTER TABLE warehouse_inventory ADD CONSTRAINT wh_product_location_unique UNIQUE (product_id, location)")
        conn.commit()
        cur.close(); conn.close()
        return jsonify({'ok': True, 'message': 'Constraint fixed and NULL locations set to Holpers'})
    except Exception as e:
        conn.rollback(); cur.close(); conn.close()
        return jsonify({'error': str(e)}), 500

@app.route('/api/admin/reset-dosage-records', methods=['POST'])
@login_required
def reset_dosage_records():
    """
    Find chemical use records that were marked inventory_updated=TRUE with
    resolved_amount=0 but actually have a dosage value in raw_json.
    Resets them to inventory_updated=FALSE so the next reprocess picks them up.
    These are products like Final All Weather Blox and Advion Ant Gel where FR
    stores usage in the dosage field instead of amount/concentratedAmount.
    """
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("""
            UPDATE fr_chemical_uses_raw
            SET inventory_updated = FALSE
            WHERE inventory_updated = TRUE
              AND resolved_amount = 0
              AND (raw_json->>'dosage')::numeric > 0
        """)
        count = cur.rowcount
        conn.commit()
        cur.close(); conn.close()
        return jsonify({'ok': True, 'reset_count': count,
                        'message': f'Reset {count} dosage-only records to unprocessed. Now reprocess the affected dates.'})
    except Exception as e:
        conn.rollback(); cur.close(); conn.close()
        return jsonify({'error': str(e)}), 500


@app.route('/api/admin/wipe-transactions', methods=['POST'])
@login_required
def wipe_transactions():
    """Wipe all usage_sync transactions and reset sync cursor. Does NOT touch warehouse or truck qty."""
    conn = get_db()
    cur = conn.cursor()
    try:
        # Only delete auto-synced transactions — keep manual receives/transfers/audits
        cur.execute("SELECT COUNT(*) as cnt FROM inventory_transactions WHERE transaction_type='usage_sync'")
        count = cur.fetchone()['cnt']
        cur.execute("DELETE FROM inventory_transactions WHERE transaction_type='usage_sync'")
        # Reset sync cursor
        cur.execute("DELETE FROM sync_state WHERE key IN ('last_chemicaluse_id','last_appointment_id','appt_last_seen_id','appt_last_run','cu_last_seen_id','cu_last_run')")
        # Clear sync log
        cur.execute("DELETE FROM fr_sync_log")
        conn.commit()
        cur.close(); conn.close()
        return jsonify({'ok': True, 'deleted': count, 'message': f'Deleted {count} sync transactions. Warehouse and truck quantities are unchanged. Run backfill to re-sync.'})
    except Exception as e:
        conn.rollback(); cur.close(); conn.close()
        return jsonify({'error': str(e)}), 500


@app.route('/api/admin/raw-chemical-uses', methods=['GET'])
@login_required
def raw_chemical_uses_diagnostic():
    """
    Diagnostic: show raw FR chemical use JSON for a specific tech and date.
    Useful for diagnosing amount calculation issues.
    Query params: tech_id (int), date (YYYY-MM-DD)
    """
    tech_id   = request.args.get('tech_id', type=int)
    date_str  = request.args.get('date', date.today().isoformat())
    product_name = request.args.get('product', '')   # optional filter

    if not tech_id:
        return jsonify({'error': 'tech_id required'}), 400

    conn = get_db(); cur = conn.cursor()

    # Get tech's FR employee ID
    cur.execute("SELECT name, fr_employee_id FROM technicians WHERE id=%s", (tech_id,))
    tech = cur.fetchone()
    if not tech:
        cur.close(); conn.close()
        return jsonify({'error': 'tech not found'}), 404

    # Product lookup (for matching chemical_id to name)
    cur.execute("SELECT id, name, fr_product_id, storage_unit, usage_unit, conversion_factor, use_applied_amount FROM products WHERE fr_product_id IS NOT NULL")
    products = {str(r['fr_product_id']): dict(r) for r in cur.fetchall()}

    # Pull raw chemical use records for this tech on this date
    cur.execute("""
        SELECT cu.chemical_use_id, cu.appointment_id, cu.chemical_id,
               cu.resolved_amount, cu.resolved_unit, cu.inventory_updated,
               cu.raw_json,
               a.appt_date, a.service_type_id
        FROM fr_chemical_uses_raw cu
        JOIN fr_appointments_raw a ON cu.appointment_id = a.appointment_id
        WHERE a.tech_fr_id = %s AND a.appt_date = %s
        ORDER BY cu.chemical_use_id
    """, (str(tech['fr_employee_id']), date_str))

    rows = cur.fetchall()
    cur.close(); conn.close()

    results = []
    for row in rows:
        raw = row['raw_json'] or {}
        if isinstance(raw, str):
            try: raw = json.loads(raw)
            except: raw = {}

        chemical_id = str(row['chemical_id'] or '')
        prod = products.get(chemical_id, {})
        prod_name = prod.get('name', f'Unknown ({chemical_id})')

        # Apply optional product name filter
        if product_name and product_name.lower() not in prod_name.lower():
            continue

        conc      = float(raw.get('concentratedAmount', 0) or 0)
        amt       = float(raw.get('amount', 0) or 0)
        mix_num   = float(raw.get('mixRatioNumerator', 0) or 0)
        mix_den   = float(raw.get('mixRatioDenominator', 0) or 0)
        use_applied = prod.get('use_applied_amount', False)

        # Replicate the resolution logic (mirrors process_inventory_from_stored_data)
        prod_unit = str(prod.get('usage_unit', '')).strip()
        if use_applied:
            path = 'use_applied_amount=TRUE → using raw amount'
            resolved = amt
        elif conc > 0:
            conc_unit = str(raw.get('concentratedUnit','') or raw.get('unit','')).strip()
            converted = _convert_liquid_unit(conc, conc_unit, prod_unit)
            unit_note = f' → converted {conc} {conc_unit} to {converted:.4f} {prod_unit}' if abs(converted - conc) > 0.0001 else ''
            path = f'concentratedAmount > 0{unit_note}'
            resolved = converted
        elif amt > 0 and mix_num > 0 and mix_den > 0 and (mix_num / mix_den) < 0.9999:
            ratio_unit = str(raw.get('mixRatioNumeratorUnit','') or raw.get('concentratedUnit','') or raw.get('unit','')).strip()
            raw_conc = amt * (mix_num / mix_den)
            resolved = _convert_liquid_unit(raw_conc, ratio_unit, prod_unit)
            path = f'amt × mixRatio={mix_num}/{mix_den}'
        elif amt > 0 and (mix_num <= 0 or mix_den <= 0):
            path = 'fallback: raw amount (no concentratedAmount, no mix ratio)'
            resolved = amt
        else:
            path = 'skip: conc=0, ratio=1:1 (diluted volume not usable as concentrate)'
            resolved = 0

        results.append({
            'chemical_use_id':      row['chemical_use_id'],
            'appointment_id':       row['appointment_id'],
            'appt_date':            str(row['appt_date']),
            'service_type':         row['service_type_id'],
            'product_name':         prod_name,
            'product_storage_unit': prod.get('storage_unit', '?'),
            'product_usage_unit':   prod.get('usage_unit', '?'),
            'conversion_factor':    prod.get('conversion_factor', 1),
            'use_applied_amount':   use_applied,
            'inventory_updated':    row['inventory_updated'],
            'raw_concentratedAmount':  raw.get('concentratedAmount'),
            'raw_concentratedUnit':    raw.get('concentratedUnit'),
            'raw_amount':              raw.get('amount'),
            'raw_unit':                raw.get('unit'),
            'raw_mixRatioNumerator':   raw.get('mixRatioNumerator'),
            'raw_mixRatioDenominator': raw.get('mixRatioDenominator'),
            'raw_mixRatioNumeratorUnit': raw.get('mixRatioNumeratorUnit'),
            'resolution_path':      path,
            'resolved_amount':      resolved,
            'storage_units_deducted': resolved / max(float(prod.get('conversion_factor') or 1), 0.0001),
        })

    totals_by_product = {}
    for r in results:
        k = r['product_name']
        if k not in totals_by_product:
            totals_by_product[k] = {'resolved_total': 0, 'storage_total': 0, 'jobs': 0}
        totals_by_product[k]['resolved_total'] += r['resolved_amount']
        totals_by_product[k]['storage_total']  += r['storage_units_deducted']
        totals_by_product[k]['jobs'] += 1

    return jsonify({
        'tech':    dict(tech),
        'date':    date_str,
        'records': results,
        'totals_by_product': totals_by_product,
    })


@app.route('/api/admin/untracked-chemicals', methods=['GET'])
@login_required
def untracked_chemicals():
    """
    Show FR chemical IDs that appear in chemical use records but have no matching
    product in our products table (fr_product_id). These get silently skipped during sync.
    Optional: date_from / date_to to limit the date range.
    """
    date_from = request.args.get('date_from', (date.today() - timedelta(days=30)).isoformat())
    date_to   = request.args.get('date_to',   date.today().isoformat())

    conn = get_db(); cur = conn.cursor()

    # All tracked FR product IDs
    cur.execute("SELECT fr_product_id, name FROM products WHERE fr_product_id IS NOT NULL AND active=TRUE")
    tracked = {str(r['fr_product_id']): r['name'] for r in cur.fetchall()}

    # Chemical IDs that appear in chemical use records joined to appointments in date range
    cur.execute("""
        SELECT cu.chemical_id,
               COUNT(*)                          AS occurrences,
               COUNT(DISTINCT cu.appointment_id) AS appointments,
               MIN(a.appt_date)                  AS first_seen,
               MAX(a.appt_date)                  AS last_seen,
               -- sample amount values so we can tell if it's a real product
               AVG(NULLIF(CAST(cu.raw_json->>'concentratedAmount' AS NUMERIC), 0)) AS avg_conc,
               AVG(NULLIF(CAST(cu.raw_json->>'amount' AS NUMERIC), 0))             AS avg_amount
        FROM fr_chemical_uses_raw cu
        JOIN fr_appointments_raw a ON cu.appointment_id = a.appointment_id
        WHERE a.appt_date BETWEEN %s AND %s
        GROUP BY cu.chemical_id
        ORDER BY occurrences DESC
    """, (date_from, date_to))

    rows = cur.fetchall()
    cur.close(); conn.close()

    untracked = []
    already_tracked = []
    for r in rows:
        cid = str(r['chemical_id'] or '')
        entry = {
            'chemical_id':   cid,
            'occurrences':   r['occurrences'],
            'appointments':  r['appointments'],
            'first_seen':    str(r['first_seen']) if r['first_seen'] else None,
            'last_seen':     str(r['last_seen'])  if r['last_seen']  else None,
            'avg_conc':      float(r['avg_conc']   or 0),
            'avg_amount':    float(r['avg_amount'] or 0),
        }
        if cid in tracked:
            entry['product_name'] = tracked[cid]
            already_tracked.append(entry)
        else:
            untracked.append(entry)

    return jsonify({
        'date_from':       date_from,
        'date_to':         date_to,
        'untracked_count': len(untracked),
        'tracked_count':   len(already_tracked),
        'untracked':       untracked,   # these get skipped during sync
        'tracked':         already_tracked,
        'note': ('Untracked chemicals are skipped during inventory sync. '
                 'Add their FR product ID to a product in Setup to start tracking them.')
    })


@app.route('/api/admin/check-duplicates', methods=['GET'])
@login_required
def check_duplicate_transactions():
    """
    Diagnostic: scan inventory_transactions for any (appointment, product, tech)
    combinations that appear more than once in usage_sync records.
    If count=0 duplicates, inventory numbers are clean.
    """
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        SELECT fr_appointment_id, product_id, technician_id,
               COUNT(*) AS occurrences,
               SUM(quantity_storage_units) AS total_qty,
               array_agg(DISTINCT appt_date::text) AS dates
        FROM inventory_transactions
        WHERE transaction_type = 'usage_sync'
          AND fr_appointment_id IS NOT NULL
        GROUP BY fr_appointment_id, product_id, technician_id
        HAVING COUNT(*) > 1
        ORDER BY COUNT(*) DESC
        LIMIT 100
    """)
    dupes = [dict(r) for r in cur.fetchall()]
    for d in dupes:
        d['total_qty'] = float(d['total_qty'] or 0)
        d['dates'] = list(d['dates'])

    # Also get total transaction count and date range
    cur.execute("""
        SELECT COUNT(*) AS total,
               MIN(appt_date) AS earliest,
               MAX(appt_date) AS latest,
               COUNT(DISTINCT appt_date) AS distinct_dates
        FROM inventory_transactions
        WHERE transaction_type = 'usage_sync'
    """)
    summary = dict(cur.fetchone())
    cur.close(); conn.close()

    return jsonify({
        'duplicate_count': len(dupes),
        'clean': len(dupes) == 0,
        'duplicates': dupes,
        'summary': summary,
        'verdict': '✅ No duplicate deductions found — inventory numbers are clean.'
                   if not dupes else
                   f'⚠️ {len(dupes)} duplicate deduction(s) found — inventory may be overstated. Contact admin.'
    })


@app.route('/api/admin/bulk-create-products', methods=['POST'])
@login_required
def bulk_create_products():
    """Create products that don't exist yet (by name). Used by the historical import script.
    Products are created with active=FALSE so they don't clutter the live product lists."""
    items   = request.json.get('products', [])
    conn    = get_db(); cur = conn.cursor()
    created = []; skipped = []
    try:
        for p in items:
            cur.execute("SELECT id FROM products WHERE LOWER(name)=LOWER(%s)", (p['name'],))
            if cur.fetchone():
                skipped.append(p['name'])
                continue
            cur.execute("""
                INSERT INTO products
                    (name, storage_unit, usage_unit, conversion_factor,
                     cost_per_storage_unit, is_equipment, active)
                VALUES (%s,%s,%s,%s,%s,%s,FALSE) RETURNING id, name
            """, (p['name'],
                  p.get('storage_unit', 'units'),
                  p.get('usage_unit') or p.get('storage_unit', 'units'),
                  p.get('conversion_factor', 1),
                  p.get('cost_per_storage_unit', 0),
                  bool(p.get('is_equipment', False))))
            row = dict(cur.fetchone())
            cur.execute("INSERT INTO warehouse_inventory (product_id, quantity_storage_units) VALUES (%s,0)", (row['id'],))
            created.append(row)
        conn.commit(); cur.close(); conn.close()
        return jsonify({'ok': True, 'created': created, 'skipped': skipped})
    except Exception as e:
        conn.rollback(); cur.close(); conn.close()
        return jsonify({'error': str(e)}), 500


@app.route('/api/admin/bulk-create-technicians', methods=['POST'])
@login_required
def bulk_create_technicians():
    """Create technicians that don't exist yet (by name). Used by the historical import script.
    Technicians are created with active=FALSE so they don't appear in the live UI."""
    items   = request.json.get('technicians', [])
    conn    = get_db(); cur = conn.cursor()
    created = []; skipped = []
    try:
        for t in items:
            cur.execute("SELECT id FROM technicians WHERE LOWER(name)=LOWER(%s)", (t['name'],))
            if cur.fetchone():
                skipped.append(t['name'])
                continue
            cur.execute("""
                INSERT INTO technicians (name, fr_employee_id, truck_id, active)
                VALUES (%s,%s,%s,FALSE) RETURNING id, name
            """, (t['name'],
                  t.get('fr_employee_id') or None,
                  t.get('truck_id') or None))
            created.append(dict(cur.fetchone()))
        conn.commit(); cur.close(); conn.close()
        return jsonify({'ok': True, 'created': created, 'skipped': skipped})
    except Exception as e:
        conn.rollback(); cur.close(); conn.close()
        return jsonify({'error': str(e)}), 500


@app.route('/api/distribution/correct', methods=['POST'])
@login_required
def correct_distribution():
    """Manually correct a tech+product quantity for a specific month."""
    d = request.json
    tech_id       = int(d['tech_id'])
    product_id    = int(d['product_id'])
    year          = int(d['year'])
    month         = int(d['month'])
    new_qty_storage = float(d['new_qty_storage'])

    conn = get_db(); cur = conn.cursor()
    try:
        # Base total = everything EXCEPT previous manual corrections for this month.
        # We compute the correction as (desired_total - base_total) so float drift
        # from the import data doesn't compound each time the user edits.
        cur.execute("""
            SELECT COALESCE(SUM(quantity_storage_units), 0) AS base_total
            FROM inventory_transactions
            WHERE technician_id=%s AND product_id=%s
              AND transaction_type IN ('transfer','equipment_provided')
              AND EXTRACT(YEAR  FROM created_at)=%s
              AND EXTRACT(MONTH FROM created_at)=%s
              AND notes IS DISTINCT FROM 'Manual distribution correction'
        """, (tech_id, product_id, year, month))
        base_total = float(cur.fetchone()['base_total'])

        # Round to 6 dp to kill any binary float noise before storing in NUMERIC(12,4)
        correction_qty = round(new_qty_storage - base_total, 6)

        if abs(correction_qty) < 0.000001:
            # Desired total equals the base — delete any existing correction
            cur.execute("""
                DELETE FROM inventory_transactions
                WHERE technician_id=%s AND product_id=%s
                  AND transaction_type='transfer'
                  AND EXTRACT(YEAR  FROM created_at)=%s
                  AND EXTRACT(MONTH FROM created_at)=%s
                  AND notes='Manual distribution correction'
            """, (tech_id, product_id, year, month))
            conn.commit(); cur.close(); conn.close()
            return jsonify({'ok': True, 'message': 'No change needed'})

        # Reuse existing manual correction row for this month if one exists
        cur.execute("""
            SELECT id FROM inventory_transactions
            WHERE technician_id=%s AND product_id=%s
              AND transaction_type='transfer'
              AND EXTRACT(YEAR  FROM created_at)=%s
              AND EXTRACT(MONTH FROM created_at)=%s
              AND notes='Manual distribution correction'
            LIMIT 1
        """, (tech_id, product_id, year, month))
        existing = cur.fetchone()

        if existing:
            cur.execute("UPDATE inventory_transactions SET quantity_storage_units=%s WHERE id=%s",
                        (correction_qty, existing['id']))
        else:
            cur.execute("""
                INSERT INTO inventory_transactions
                  (transaction_type, product_id, technician_id, quantity_storage_units,
                   notes, performed_by, created_at)
                VALUES ('transfer',%s,%s,%s,'Manual distribution correction',%s,
                        make_timestamp(%s,%s,15,12,0,0))
            """, (product_id, tech_id, correction_qty,
                  session.get('username','admin'), year, month))

        conn.commit()
        cur.close(); conn.close()
        return jsonify({'ok': True, 'new_total': new_qty_storage})
    except Exception as e:
        conn.rollback(); cur.close(); conn.close()
        return jsonify({'error': str(e)}), 500

@app.route('/api/admin/wipe-historical-imports', methods=['POST'])
@login_required
def wipe_historical_imports():
    """Delete all records previously inserted by the historical distribution import."""
    conn = get_db(); cur = conn.cursor()
    try:
        cur.execute("""
            DELETE FROM inventory_transactions
            WHERE notes = 'Historical import from distribution spreadsheet'
        """)
        deleted = cur.rowcount
        conn.commit()
        cur.close(); conn.close()
        return jsonify({'ok': True, 'deleted': deleted})
    except Exception as e:
        conn.rollback(); cur.close(); conn.close()
        return jsonify({'error': str(e)}), 500


@app.route('/api/admin/import-distribution', methods=['POST'])
@login_required
def import_distribution():
    """Bulk-insert historical distribution records into inventory_transactions."""
    data = request.get_json(force=True)
    records = data.get('records', [])
    if not records:
        return jsonify({'error': 'No records provided'}), 400
    conn = get_db(); cur = conn.cursor()
    inserted = 0
    skipped  = 0
    errors   = []
    try:
        for r in records:
            try:
                pid  = int(r['product_id'])
                tid  = int(r['technician_id'])
                qty  = float(r['quantity_storage_units'])
                ts   = r['created_at']          # ISO string: "2026-01-15T12:00:00"
                ttype = r.get('transaction_type', 'transfer')
                note  = r.get('notes', 'Historical import from distribution spreadsheet')
                if qty <= 0:
                    skipped += 1
                    continue
                cur.execute("""
                    INSERT INTO inventory_transactions
                        (product_id, technician_id, quantity_storage_units,
                         transaction_type, notes, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s)
                """, (pid, tid, qty, ttype, note, ts))
                inserted += 1
            except Exception as e:
                errors.append({'record': r, 'error': str(e)})
        conn.commit()
    except Exception as e:
        conn.rollback(); cur.close(); conn.close()
        return jsonify({'error': str(e)}), 500
    cur.close(); conn.close()
    return jsonify({'ok': True, 'inserted': inserted, 'skipped': skipped, 'errors': errors[:20]})


@app.route('/api/sync/log', methods=['GET'])
@login_required
def sync_log():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM fr_sync_log ORDER BY created_at DESC LIMIT 50")
    rows = [dict(r) for r in cur.fetchall()]
    cur.close(); conn.close()
    return jsonify(rows)

@app.route('/api/sync/log-status', methods=['GET'])
@login_required
def sync_status():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM fr_sync_log ORDER BY created_at DESC LIMIT 1")
    last = cur.fetchone()
    cur.close(); conn.close()
    return jsonify(dict(last) if last else {})

# ─── SEED ENDPOINT (run once after first deploy) ──────────────────────────────
@app.route('/api/seed', methods=['POST'])
@login_required
def run_seed():
    """Run the seed script to load initial data. Safe to call multiple times."""
    try:
        import subprocess, sys
        env = os.environ.copy()
        result = subprocess.run(
            [sys.executable, 'seed_data.py'],
            capture_output=True, text=True, timeout=120, env=env
        )
        return jsonify({
            'stdout': result.stdout,
            'stderr': result.stderr,
            'returncode': result.returncode
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ─── FR: IMPORT & LINK CHEMICALS ─────────────────────────────────────────────
@app.route('/api/fr/import-chemicals', methods=['POST'])
@login_required
def import_fr_chemicals():
    """
    Two-step FR fetch: search chemical IDs → get full chemical records.
    Match by name to existing products and set fr_product_id (the chemicalID).
    This is what makes the hourly sync work.
    """
    if not FR_API_KEY or not FR_AUTH_KEY:
        return jsonify({'error': 'FR API keys not configured'}), 400
    try:
        # Fetch all chemicals from FR using search → get pattern
        chemicals = fr_fetch_all('chemical', data_key='chemicals')

        conn = get_db()
        cur = conn.cursor()

        cur.execute("SELECT id, name, fr_product_id FROM products WHERE active=TRUE")
        existing = cur.fetchall()
        name_to_id = {r['name'].lower().strip(): r['id'] for r in existing}
        already_mapped = {str(r['fr_product_id']): r['id'] for r in existing if r['fr_product_id']}

        matched = 0; created = 0; skipped = 0; unmatched = []

        for chem in chemicals:
            fr_id = str(chem.get('chemicalID', ''))
            name = (chem.get('name') or f'Chemical {fr_id}').strip()
            inv_unit = (chem.get('inventoryUnit') or 'units').strip()
            usage_unit = (chem.get('dilutedUnit') or chem.get('concentratedUnit') or 'units').strip()

            if fr_id in already_mapped:
                skipped += 1; continue

            # Exact name match
            prod_id = name_to_id.get(name.lower())
            # Partial match fallback
            if not prod_id:
                for db_name, db_id in name_to_id.items():
                    if db_name in name.lower() or name.lower() in db_name:
                        prod_id = db_id; break

            if prod_id:
                cur.execute("UPDATE products SET fr_product_id=%s WHERE id=%s", (fr_id, prod_id))
                matched += 1
            else:
                cur.execute("SELECT id FROM products WHERE name ILIKE %s", (name,))
                if cur.fetchone():
                    skipped += 1; continue
                cur.execute("""
                    INSERT INTO products (name, fr_product_id, storage_unit, usage_unit, conversion_factor, conversion_note)
                    VALUES (%s, %s, %s, %s, 1, 'From FR — set conversion factor in Setup') RETURNING id
                """, (name, fr_id, inv_unit, usage_unit))
                new_id = cur.fetchone()['id']
                cur.execute("INSERT INTO warehouse_inventory (product_id, quantity_storage_units) VALUES (%s, 0)", (new_id,))
                created += 1
                unmatched.append(f"{name} (FR ID {fr_id})")

        conn.commit(); cur.close(); conn.close()
        return jsonify({
            'matched': matched, 'created': created, 'skipped': skipped,
            'total': len(chemicals), 'unmatched_new': unmatched,
            'message': f'Linked {matched} products to FR. Created {created} new. {skipped} already mapped.'
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/products/<int:pid>/set-fr-id', methods=['POST'])
@login_required
def set_product_fr_id(pid):
    d = request.json
    fr_id = d.get('fr_product_id', '').strip()
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE products SET fr_product_id=%s WHERE id=%s RETURNING *", (fr_id or None, pid))
    row = dict(cur.fetchone())
    conn.commit(); cur.close(); conn.close()
    return jsonify(row)

# ─── THRESHOLDS ───────────────────────────────────────────────────────────────
@app.route('/api/thresholds', methods=['GET'])
@login_required
def get_thresholds():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT pt.*, p.name AS product_name, p.storage_unit
        FROM product_thresholds pt
        JOIN products p ON pt.product_id = p.id ORDER BY p.name
    """)
    rows = [dict(r) for r in cur.fetchall()]
    cur.close(); conn.close()
    return jsonify(rows)

@app.route('/api/thresholds', methods=['POST'])
@login_required
def set_threshold():
    d = request.json
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO product_thresholds (product_id, min_warehouse_qty, reorder_qty, reorder_note, include_in_alerts)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (product_id) DO UPDATE SET
            min_warehouse_qty=EXCLUDED.min_warehouse_qty,
            reorder_qty=EXCLUDED.reorder_qty,
            reorder_note=EXCLUDED.reorder_note,
            include_in_alerts=EXCLUDED.include_in_alerts,
            updated_at=NOW()
        RETURNING *
    """, (d['product_id'], d['min_warehouse_qty'], d.get('reorder_qty', 0), d.get('reorder_note', ''), bool(d.get('include_in_alerts', True))))
    row = dict(cur.fetchone())
    conn.commit(); cur.close(); conn.close()
    return jsonify(row)

@app.route('/api/thresholds/alerts', methods=['GET'])
@login_required
def threshold_alerts():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT p.name AS product_name, p.storage_unit, p.id AS product_id,
               w.quantity_storage_units AS current_qty,
               pt.min_warehouse_qty, pt.reorder_qty, pt.reorder_note,
               CASE
                 WHEN w.quantity_storage_units <= 0 THEN 'out_of_stock'
                 WHEN w.quantity_storage_units <= pt.min_warehouse_qty THEN 'low_stock'
                 ELSE 'in_stock'
               END AS stock_status
        FROM product_thresholds pt
        JOIN products p ON pt.product_id = p.id
        JOIN warehouse_inventory w ON w.product_id = p.id
        WHERE p.active=TRUE AND w.location='Holpers'
          AND COALESCE(pt.include_in_alerts, TRUE) = TRUE
        ORDER BY CASE WHEN w.quantity_storage_units <= 0 THEN 0
                      WHEN w.quantity_storage_units <= pt.min_warehouse_qty THEN 1
                      ELSE 2 END, p.name
    """)
    rows = [dict(r) for r in cur.fetchall()]
    cur.close(); conn.close()
    return jsonify(rows)

# ─── REPORTS ──────────────────────────────────────────────────────────────────
@app.route('/api/reports/summary', methods=['GET'])
@login_required
def report_summary():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT p.id AS product_id, p.name, p.storage_unit, p.usage_unit, p.conversion_factor,
               COALESCE(p.cost_per_storage_unit, 0) AS cost_per_storage_unit,
               w.quantity_storage_units,
               w.quantity_storage_units * p.conversion_factor AS quantity_usage_units,
               COALESCE(pt.min_warehouse_qty, 0) AS min_warehouse_qty,
               COALESCE(pt.reorder_qty, 0) AS reorder_qty,
               CASE
                 WHEN COALESCE(pt.include_in_alerts, TRUE) = FALSE THEN 'in_stock'
                 WHEN w.quantity_storage_units <= 0 THEN 'out_of_stock'
                 WHEN pt.min_warehouse_qty IS NOT NULL AND w.quantity_storage_units <= pt.min_warehouse_qty THEN 'low_stock'
                 ELSE 'in_stock'
               END AS stock_status
        FROM warehouse_inventory w
        JOIN products p ON w.product_id = p.id
        LEFT JOIN product_thresholds pt ON pt.product_id = p.id
        WHERE p.active=TRUE AND w.location='Holpers' ORDER BY p.name
    """)
    warehouse = [dict(r) for r in cur.fetchall()]
    cur.execute("""
        SELECT t.id AS tech_id, t.name AS tech_name, t.truck_id,
               p.id AS product_id, p.name AS product_name,
               p.storage_unit, p.usage_unit, p.conversion_factor,
               ti.quantity_storage_units,
               ti.quantity_storage_units * p.conversion_factor AS quantity_usage_units
        FROM tech_inventory ti
        JOIN technicians t ON ti.technician_id = t.id
        JOIN products p ON ti.product_id = p.id
        WHERE t.active=TRUE AND p.active=TRUE ORDER BY t.name, p.name
    """)
    tech_inv = [dict(r) for r in cur.fetchall()]
    cur.close(); conn.close()
    return jsonify({'warehouse': warehouse, 'tech_inventory': tech_inv})

@app.route('/api/reports/usage-by-tech', methods=['GET'])
@login_required
def report_usage_by_tech():
    tech_id = request.args.get('tech_id')
    date_from = request.args.get('date_from', (date.today() - timedelta(days=30)).isoformat())
    date_to = request.args.get('date_to', date.today().isoformat())
    conn = get_db()
    cur = conn.cursor()
    q = """
        SELECT t.name AS tech_name, t.truck_id, p.name AS product_name,
               p.usage_unit, SUM(it.quantity_usage_units) AS total_used,
               COUNT(*) AS job_count,
               COALESCE(it.appt_date, DATE(it.created_at)) AS usage_date
        FROM inventory_transactions it
        JOIN technicians t ON it.technician_id = t.id
        JOIN products p ON it.product_id = p.id
        WHERE it.transaction_type='usage_sync'
          AND COALESCE(it.appt_date, DATE(it.created_at)) BETWEEN %s AND %s
    """
    params = [date_from, date_to]
    if tech_id:
        q += " AND it.technician_id=%s"; params.append(tech_id)
    q += " GROUP BY t.name, t.truck_id, p.name, p.usage_unit, COALESCE(it.appt_date, DATE(it.created_at)) ORDER BY t.name, usage_date DESC"
    cur.execute(q, params)
    rows = [dict(r) for r in cur.fetchall()]
    cur.close(); conn.close()
    return jsonify(rows)

@app.route('/api/reports/usage-by-product', methods=['GET'])
@login_required
def report_usage_by_product():
    product_id = request.args.get('product_id')
    date_from = request.args.get('date_from', (date.today() - timedelta(days=30)).isoformat())
    date_to = request.args.get('date_to', date.today().isoformat())
    conn = get_db()
    cur = conn.cursor()

    # No product selected → legacy flat aggregation (array). Units differ across
    # products, so no grand-total summary makes sense here.
    if not product_id:
        cur.execute("""
            SELECT p.name AS product_name, p.usage_unit,
                   t.name AS tech_name, SUM(it.quantity_usage_units) AS total_used,
                   COUNT(*) AS job_count,
                   COALESCE(it.appt_date, DATE(it.created_at)) AS usage_date
            FROM inventory_transactions it
            JOIN products p ON it.product_id = p.id
            LEFT JOIN technicians t ON it.technician_id = t.id
            WHERE it.transaction_type='usage_sync'
              AND COALESCE(it.appt_date, DATE(it.created_at)) BETWEEN %s AND %s
            GROUP BY p.name, p.usage_unit, t.name, COALESCE(it.appt_date, DATE(it.created_at))
            ORDER BY usage_date DESC, total_used DESC
        """, [date_from, date_to])
        rows = [dict(r) for r in cur.fetchall()]
        cur.close(); conn.close()
        return jsonify(rows)

    # Product selected → detailed, editable view + summary totals.
    cur.execute("SELECT name, usage_unit FROM products WHERE id=%s", (product_id,))
    prod = cur.fetchone()
    if not prod:
        cur.close(); conn.close()
        return jsonify({'error': 'Product not found'}), 404
    usage_unit = prod['usage_unit']

    cur.execute("""
        SELECT it.id, it.technician_id, t.name AS tech_name,
               COALESCE(it.appt_date, DATE(it.created_at)) AS usage_date,
               it.quantity_usage_units, it.fr_appointment_id, it.notes
        FROM inventory_transactions it
        LEFT JOIN technicians t ON it.technician_id = t.id
        WHERE it.transaction_type='usage_sync' AND it.product_id=%s
          AND COALESCE(it.appt_date, DATE(it.created_at)) BETWEEN %s AND %s
        ORDER BY usage_date DESC, t.name
    """, (product_id, date_from, date_to))
    txns = [dict(r) for r in cur.fetchall()]
    cur.close(); conn.close()

    row_map = {}       # (tech_id, date) -> aggregated row with line_items
    tech_totals = {}   # tech_id -> {name, used, jobs} across the whole range
    grand_used = 0.0
    grand_jobs = 0
    for r in txns:
        used = float(r['quantity_usage_units'] or 0)
        grand_used += used
        grand_jobs += 1
        tid = r['technician_id']
        dkey = str(r['usage_date']) if r['usage_date'] else None
        key = (tid, dkey)
        if key not in row_map:
            row_map[key] = {
                'product_name': prod['name'], 'usage_unit': usage_unit,
                'tech_id': tid, 'tech_name': r['tech_name'] or '—',
                'usage_date': dkey, 'total_used': 0.0, 'job_count': 0, 'line_items': [],
            }
        row = row_map[key]
        row['total_used'] += used
        row['job_count'] += 1
        cust = re.search(r'customerID:(\d+)', r['notes'] or '')
        row['line_items'].append({
            'id': r['id'], 'date': dkey, 'quantity_used': used,
            'customer_id': cust.group(1) if cust else None,
            'fr_appointment_id': r['fr_appointment_id'],
        })
        if tid not in tech_totals:
            tech_totals[tid] = {'tech_id': tid, 'tech_name': r['tech_name'] or '—', 'total_used': 0.0, 'job_count': 0}
        tech_totals[tid]['total_used'] += used
        tech_totals[tid]['job_count'] += 1

    rows = list(row_map.values())
    for row in rows:
        row['total_used'] = round(row['total_used'], 4)
    rows.sort(key=lambda x: x['total_used'], reverse=True)
    rows.sort(key=lambda x: x['usage_date'] or '', reverse=True)

    techs = list(tech_totals.values())
    for t in techs:
        t['total_used'] = round(t['total_used'], 4)
    techs.sort(key=lambda t: t['total_used'], reverse=True)

    return jsonify({
        'product_id': int(product_id),
        'product_name': prod['name'],
        'usage_unit': usage_unit,
        'rows': rows,
        'summary': {
            'total_used': round(grand_used, 4),
            'total_jobs': grand_jobs,
            'usage_unit': usage_unit,
            'techs': techs,
        },
    })

@app.route('/api/reports/chemical-usage', methods=['GET'])
@login_required
def report_chemical_usage():
    """
    Unified chemical-usage report (merges the old By-Product and By-Tech views).
    Filters: optional product_id and/or tech_id, plus a date range.
    Returns rows grouped by (product, tech, date) with editable per-job line
    items, plus an adaptive summary:
      - total_jobs always
      - by_product totals (each in its own unit — safe across mixed products)
      - by_tech totals (job counts always; amount only meaningful with 1 unit)
      - single_unit + grand total_used only when every row shares one usage unit
    """
    product_id = request.args.get('product_id')
    tech_id = request.args.get('tech_id')
    date_from = request.args.get('date_from', (date.today() - timedelta(days=30)).isoformat())
    date_to = request.args.get('date_to', date.today().isoformat())

    q = """
        SELECT it.id, it.technician_id, t.name AS tech_name,
               it.product_id, p.name AS product_name, p.usage_unit,
               COALESCE(it.appt_date, DATE(it.created_at)) AS usage_date,
               it.quantity_usage_units, it.fr_appointment_id, it.notes
        FROM inventory_transactions it
        JOIN products p ON it.product_id = p.id
        LEFT JOIN technicians t ON it.technician_id = t.id
        WHERE it.transaction_type='usage_sync'
          AND COALESCE(it.appt_date, DATE(it.created_at)) BETWEEN %s AND %s
    """
    params = [date_from, date_to]
    if product_id:
        q += " AND it.product_id=%s"; params.append(product_id)
    if tech_id:
        q += " AND it.technician_id=%s"; params.append(tech_id)
    q += " ORDER BY usage_date DESC, p.name, t.name"

    conn = get_db(); cur = conn.cursor()
    cur.execute(q, params)
    txns = [dict(r) for r in cur.fetchall()]
    cur.close(); conn.close()

    row_map = {}    # (product_id, tech_id, date) -> row + line_items
    prod_tot = {}   # product_id -> {name, unit, used, jobs}
    tech_tot = {}   # tech_id   -> {name, jobs, used}
    total_jobs = 0
    units = set()
    for r in txns:
        used = float(r['quantity_usage_units'] or 0)
        total_jobs += 1
        units.add(r['usage_unit'])
        pid = r['product_id']; tid = r['technician_id']
        dkey = str(r['usage_date']) if r['usage_date'] else None
        key = (pid, tid, dkey)
        if key not in row_map:
            row_map[key] = {
                'product_id': pid, 'product_name': r['product_name'], 'usage_unit': r['usage_unit'],
                'tech_id': tid, 'tech_name': r['tech_name'] or '—', 'usage_date': dkey,
                'total_used': 0.0, 'job_count': 0, 'line_items': [],
            }
        row = row_map[key]
        row['total_used'] += used
        row['job_count'] += 1
        cust = re.search(r'customerID:(\d+)', r['notes'] or '')
        row['line_items'].append({
            'id': r['id'], 'date': dkey, 'quantity_used': used,
            'customer_id': cust.group(1) if cust else None,
            'fr_appointment_id': r['fr_appointment_id'],
        })
        if pid not in prod_tot:
            prod_tot[pid] = {'product_name': r['product_name'], 'usage_unit': r['usage_unit'], 'total_used': 0.0, 'job_count': 0}
        prod_tot[pid]['total_used'] += used
        prod_tot[pid]['job_count'] += 1
        if tid not in tech_tot:
            tech_tot[tid] = {'tech_name': r['tech_name'] or '—', 'total_used': 0.0, 'job_count': 0}
        tech_tot[tid]['total_used'] += used
        tech_tot[tid]['job_count'] += 1

    rows = list(row_map.values())
    for row in rows:
        row['total_used'] = round(row['total_used'], 4)
    rows.sort(key=lambda x: x['total_used'], reverse=True)
    rows.sort(key=lambda x: x['usage_date'] or '', reverse=True)

    by_product = list(prod_tot.values())
    for p in by_product:
        p['total_used'] = round(p['total_used'], 4)
    by_product.sort(key=lambda p: p['total_used'], reverse=True)

    by_tech = list(tech_tot.values())
    for t in by_tech:
        t['total_used'] = round(t['total_used'], 4)
    by_tech.sort(key=lambda t: t['total_used'], reverse=True)

    single_unit = next(iter(units)) if len(units) == 1 else None

    return jsonify({
        'rows': rows,
        'summary': {
            'total_jobs': total_jobs,
            'single_unit': single_unit,
            'total_used': round(sum(p['total_used'] for p in by_product), 4) if single_unit else None,
            'product_count': len(by_product),
            'by_product': by_product,
            'by_tech': by_tech,
        },
    })

@app.route('/api/reports/received-by-product', methods=['GET'])
@login_required
def report_received_by_product():
    """
    How much of each product has been received into the warehouse in a date range,
    with a per-product summary (both units + last received) and the full list of
    individual line items behind it (date, qty, source, who, notes).
    Combines two sources so nothing is missed regardless of how it was entered:
      1. Explicit "Receive" transactions (inventory_transactions, transaction_type='receive')
      2. Upward "Set Count" corrections (audit_log, audit_type='warehouse', new_quantity > old_quantity)
    """
    product_id = request.args.get('product_id')
    date_from = request.args.get('date_from', (date.today() - timedelta(days=30)).isoformat())
    date_to = request.args.get('date_to', date.today().isoformat())
    conn = get_db()
    cur = conn.cursor()

    q = """
        SELECT p.id AS product_id, p.name AS product_name, p.storage_unit, p.usage_unit,
               p.conversion_factor, 'receive' AS source, it.created_at AS ts,
               it.quantity_storage_units AS qty_storage, it.notes, it.performed_by
        FROM inventory_transactions it
        JOIN products p ON it.product_id = p.id
        WHERE it.transaction_type = 'receive'
          AND DATE(it.created_at) BETWEEN %s AND %s
          {product_filter_a}
        UNION ALL
        SELECT p.id, p.name, p.storage_unit, p.usage_unit,
               p.conversion_factor, 'correction' AS source, a.created_at AS ts,
               (a.new_quantity - a.old_quantity) AS qty_storage, a.reason AS notes, a.performed_by
        FROM audit_log a
        JOIN products p ON a.product_id = p.id
        WHERE a.audit_type = 'warehouse'
          AND a.new_quantity > a.old_quantity
          AND DATE(a.created_at) BETWEEN %s AND %s
          {product_filter_b}
        ORDER BY ts DESC
    """
    filter_a = filter_b = ''
    params = [date_from, date_to, date_from, date_to]
    if product_id:
        filter_a = 'AND it.product_id=%s'
        filter_b = 'AND a.product_id=%s'
        params = [date_from, date_to, product_id, date_from, date_to, product_id]
    q = q.format(product_filter_a=filter_a, product_filter_b=filter_b)

    cur.execute(q, params)
    line_items = [dict(r) for r in cur.fetchall()]

    products = {}
    for li in line_items:
        pid = li['product_id']
        if pid not in products:
            products[pid] = {
                'product_id': pid, 'product_name': li['product_name'],
                'storage_unit': li['storage_unit'], 'usage_unit': li['usage_unit'],
                'conversion_factor': float(li['conversion_factor'] or 1),
                'total_storage': 0.0, 'receive_count': 0, 'correction_count': 0,
                'last_received_at': None, 'line_items': [],
            }
        p = products[pid]
        qty = float(li['qty_storage'] or 0)
        p['total_storage'] += qty
        if li['source'] == 'receive':
            p['receive_count'] += 1
        else:
            p['correction_count'] += 1
        ts_iso = (li['ts'].isoformat() + 'Z') if li['ts'] else None
        if not p['last_received_at'] or (ts_iso and ts_iso > p['last_received_at']):
            p['last_received_at'] = ts_iso
        p['line_items'].append({
            'date': ts_iso, 'source': li['source'],
            'qty_storage': qty, 'qty_usage': qty * p['conversion_factor'],
            'notes': li['notes'], 'performed_by': li['performed_by'],
        })

    out = list(products.values())
    for r in out:
        r['total_usage'] = r['total_storage'] * r['conversion_factor']
    out.sort(key=lambda r: r['total_storage'], reverse=True)
    cur.close(); conn.close()
    return jsonify(out)

@app.route('/api/reports/usage-trends', methods=['GET'])
@login_required
def report_usage_trends():
    """Weekly/monthly usage totals for charting. group_by=week|month, view=product|tech"""
    date_from = request.args.get('date_from', (date.today() - timedelta(days=90)).isoformat())
    date_to   = request.args.get('date_to', date.today().isoformat())
    group_by  = request.args.get('group_by', 'week')   # week | month
    view      = request.args.get('view', 'product')     # product | tech
    item_id   = request.args.get('id')                  # product_id or tech_id

    trunc = "date_trunc('week', COALESCE(it.appt_date::timestamp, it.created_at))" if group_by == 'week' \
            else "date_trunc('month', COALESCE(it.appt_date::timestamp, it.created_at))"

    conn = get_db(); cur = conn.cursor()
    if view == 'product':
        q = f"""
            SELECT {trunc} AS period,
                   p.name AS label, p.usage_unit, p.storage_unit, p.conversion_factor,
                   SUM(it.quantity_usage_units) AS total
            FROM inventory_transactions it
            JOIN products p ON it.product_id = p.id
            WHERE it.transaction_type = 'usage_sync'
              AND COALESCE(it.appt_date, DATE(it.created_at)) BETWEEN %s AND %s
        """
        params = [date_from, date_to]
        if item_id:
            q += " AND it.product_id = %s"; params.append(item_id)
        q += f" GROUP BY {trunc}, p.name, p.usage_unit, p.storage_unit, p.conversion_factor ORDER BY period"
    else:
        q = f"""
            SELECT {trunc} AS period,
                   t.name AS label, '' AS usage_unit,
                   SUM(it.quantity_storage_units * COALESCE(p.cost_per_storage_unit,0)) AS total
            FROM inventory_transactions it
            JOIN technicians t ON it.technician_id = t.id
            JOIN products p ON it.product_id = p.id
            WHERE it.transaction_type = 'usage_sync'
              AND COALESCE(it.appt_date, DATE(it.created_at)) BETWEEN %s AND %s
        """
        params = [date_from, date_to]
        if item_id:
            q += " AND it.technician_id = %s"; params.append(item_id)
        q += f" GROUP BY {trunc}, t.name ORDER BY period"

    cur.execute(q, params)
    rows = [dict(r) for r in cur.fetchall()]
    cur.close(); conn.close()
    # Convert period to ISO string and numerics
    for r in rows:
        if r.get('period'):
            r['period'] = r['period'].strftime('%Y-%m-%d') if hasattr(r['period'], 'strftime') else str(r['period'])[:10]
        r['total'] = float(r['total'] or 0)
        if 'conversion_factor' in r and r['conversion_factor']:
            r['conversion_factor'] = float(r['conversion_factor'])
    return jsonify(rows)

@app.route('/api/reports/tech-comparison', methods=['GET'])
@login_required
def report_tech_comparison():
    """Total usage and cost per tech over a date range, broken down by product."""
    date_from = request.args.get('date_from', (date.today() - timedelta(days=30)).isoformat())
    date_to   = request.args.get('date_to', date.today().isoformat())
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        SELECT t.name AS tech_name, t.truck_id,
               p.name AS product_name, p.usage_unit, p.storage_unit,
               p.conversion_factor,
               COALESCE(p.use_applied_amount, FALSE) AS use_applied_amount,
               COALESCE(p.is_equipment, FALSE)        AS is_equipment,
               SUM(it.quantity_usage_units)   AS total_applied,
               SUM(it.quantity_storage_units) AS total_storage,
               SUM(it.quantity_storage_units * COALESCE(p.cost_per_storage_unit,0)) AS total_cost,
               COUNT(*) AS job_count
        FROM inventory_transactions it
        JOIN technicians t ON it.technician_id = t.id
        JOIN products p    ON it.product_id    = p.id
        WHERE it.transaction_type = 'usage_sync'
          AND COALESCE(it.appt_date, DATE(it.created_at)) BETWEEN %s AND %s
        GROUP BY t.name, t.truck_id, p.name, p.usage_unit, p.storage_unit,
                 p.conversion_factor, p.use_applied_amount, p.is_equipment
        ORDER BY t.name, total_cost DESC
    """, (date_from, date_to))
    rows = [dict(r) for r in cur.fetchall()]
    cur.close(); conn.close()
    for r in rows:
        cf  = float(r['conversion_factor'] or 1)
        use_applied = bool(r['use_applied_amount'])
        total_storage = float(r['total_storage'] or 0)
        total_applied = float(r['total_applied'] or 0)
        # use_applied products: show raw applied amount (gels, baits, granules)
        # all others: derive from storage × CF — always in correct usage unit
        r['total_used']          = round(float(total_applied or 0), 4)   # always use stored usage qty directly
        r['total_storage']       = total_storage
        r['total_cost']          = float(r['total_cost'] or 0)
        r['conversion_factor']   = cf
        r['use_applied_amount']  = use_applied
    return jsonify(rows)

@app.route('/api/reports/monthly-summary', methods=['GET'])
@login_required
def report_monthly_summary():
    """
    Per-tech, per-product usage summary for a calendar month.
    Optional product_id: narrows the flat 'rows' table to that product AND adds
    a 'product_detail' block — a range total plus a per-tech breakdown, each
    with its own line items (date, customer, qty) for drill-down.
    """
    month = request.args.get('month', date.today().strftime('%Y-%m'))
    product_id = request.args.get('product_id', type=int)
    try:
        month_start = date.fromisoformat(month + '-01')
    except ValueError:
        return jsonify({'error': 'Invalid month format (use YYYY-MM)'}), 400
    # Last day of month
    if month_start.month == 12:
        month_end = month_start.replace(year=month_start.year+1, month=1, day=1) - timedelta(days=1)
    else:
        month_end = month_start.replace(month=month_start.month+1, day=1) - timedelta(days=1)
    date_from, date_to = month_start.isoformat(), month_end.isoformat()

    conn = get_db(); cur = conn.cursor()
    q = """
        SELECT t.name AS tech_name, t.truck_id,
               p.name AS product_name, p.usage_unit,
               SUM(it.quantity_usage_units) AS total_used,
               SUM(it.quantity_storage_units * COALESCE(p.cost_per_storage_unit,0)) AS total_cost,
               COUNT(*) AS job_count
        FROM inventory_transactions it
        JOIN technicians t ON it.technician_id = t.id
        JOIN products p    ON it.product_id    = p.id
        WHERE it.transaction_type = 'usage_sync'
          AND COALESCE(it.appt_date, DATE(it.created_at)) BETWEEN %s AND %s
    """
    params = [date_from, date_to]
    if product_id:
        q += " AND it.product_id=%s"; params.append(product_id)
    q += " GROUP BY t.name, t.truck_id, p.name, p.usage_unit ORDER BY t.name, total_cost DESC"
    cur.execute(q, params)
    rows = [dict(r) for r in cur.fetchall()]
    for r in rows:
        r['total_used'] = float(r['total_used'] or 0)
        r['total_cost'] = float(r['total_cost'] or 0)

    result = {'month': month, 'month_start': date_from, 'month_end': date_to, 'rows': rows}

    if product_id:
        cur.execute("SELECT name, storage_unit, usage_unit, conversion_factor, COALESCE(cost_per_storage_unit,0) AS cost_per_storage_unit FROM products WHERE id=%s", (product_id,))
        prod = cur.fetchone()
        if not prod:
            cur.close(); conn.close()
            return jsonify({'error': 'Product not found'}), 404

        cur.execute("""
            SELECT it.id, it.technician_id, t.name AS tech_name, t.truck_id,
                   COALESCE(it.appt_date, DATE(it.created_at)) AS usage_date,
                   it.quantity_usage_units, it.quantity_storage_units,
                   it.fr_appointment_id, it.notes
            FROM inventory_transactions it
            JOIN technicians t ON it.technician_id = t.id
            WHERE it.transaction_type='usage_sync' AND it.product_id=%s
              AND COALESCE(it.appt_date, DATE(it.created_at)) BETWEEN %s AND %s
            ORDER BY t.name, usage_date, it.id
        """, (product_id, date_from, date_to))
        line_rows = [dict(r) for r in cur.fetchall()]
        cur.close(); conn.close()

        cost_per_unit = float(prod['cost_per_storage_unit'])
        techs = {}
        total_used = total_storage = total_cost = 0.0
        for r in line_rows:
            usage_qty = float(r['quantity_usage_units'] or 0)
            storage_qty = float(r['quantity_storage_units'] or 0)
            cost = storage_qty * cost_per_unit
            total_used += usage_qty; total_storage += storage_qty; total_cost += cost
            tid = r['technician_id']
            if tid not in techs:
                techs[tid] = {
                    'tech_id': tid, 'tech_name': r['tech_name'], 'truck_id': r['truck_id'],
                    'total_used': 0.0, 'total_cost': 0.0, 'job_count': 0, 'line_items': [],
                }
            tech = techs[tid]
            tech['total_used'] += usage_qty
            tech['total_cost'] += cost
            tech['job_count'] += 1
            customer_match = re.search(r'customerID:(\d+)', r['notes'] or '')
            tech['line_items'].append({
                'id': r['id'],
                'date': str(r['usage_date']) if r['usage_date'] else None,
                'quantity_used': usage_qty, 'cost': round(cost, 2),
                'customer_id': customer_match.group(1) if customer_match else None,
                'fr_appointment_id': r['fr_appointment_id'],
            })

        tech_list = sorted(techs.values(), key=lambda t: t['total_used'], reverse=True)
        for t in tech_list:
            t['total_used'] = round(t['total_used'], 4)
            t['total_cost'] = round(t['total_cost'], 2)

        result['product_detail'] = {
            'product_id': product_id, 'product_name': prod['name'],
            'storage_unit': prod['storage_unit'], 'usage_unit': prod['usage_unit'],
            'total_used': round(total_used, 4), 'total_storage': round(total_storage, 4),
            'total_cost': round(total_cost, 2), 'job_count': len(line_rows),
            'techs': tech_list,
        }
    else:
        cur.close(); conn.close()

    return jsonify(result)

@app.route('/api/reports/cost-per-job', methods=['GET'])
@login_required
def report_cost_per_job():
    """Chemical cost per appointment, grouped by date + tech."""
    date_from = request.args.get('date_from', (date.today() - timedelta(days=30)).isoformat())
    date_to   = request.args.get('date_to', date.today().isoformat())
    tech_id   = request.args.get('tech_id')
    conn = get_db(); cur = conn.cursor()
    q = """
        SELECT it.appt_date,
               t.name AS tech_name, t.truck_id,
               COUNT(DISTINCT it.fr_appointment_id) AS appt_count,
               COUNT(DISTINCT it.product_id)         AS product_count,
               SUM(it.quantity_storage_units * COALESCE(p.cost_per_storage_unit,0)) AS total_cost,
               CASE WHEN COUNT(DISTINCT it.fr_appointment_id) > 0
                    THEN SUM(it.quantity_storage_units * COALESCE(p.cost_per_storage_unit,0))
                         / NULLIF(COUNT(DISTINCT it.fr_appointment_id),0)
                    ELSE SUM(it.quantity_storage_units * COALESCE(p.cost_per_storage_unit,0))
               END AS cost_per_appt
        FROM inventory_transactions it
        JOIN technicians t ON it.technician_id = t.id
        JOIN products p    ON it.product_id    = p.id
        WHERE it.transaction_type = 'usage_sync'
          AND it.appt_date BETWEEN %s AND %s
    """
    params = [date_from, date_to]
    if tech_id:
        q += " AND it.technician_id=%s"; params.append(tech_id)
    q += " GROUP BY it.appt_date, t.name, t.truck_id ORDER BY it.appt_date DESC, total_cost DESC"
    cur.execute(q, params)
    rows = [dict(r) for r in cur.fetchall()]
    cur.close(); conn.close()
    for r in rows:
        r['total_cost']    = float(r['total_cost'] or 0)
        r['cost_per_appt'] = float(r['cost_per_appt'] or 0)
        if r.get('appt_date') and hasattr(r['appt_date'], 'isoformat'):
            r['appt_date'] = r['appt_date'].isoformat()
    return jsonify(rows)

@app.route('/api/reports/shrinkage', methods=['GET'])
@login_required
def report_shrinkage():
    """
    Compare what was transferred to each tech vs what FR says was used.
    Shrinkage = transferred_in - used_by_fr - current_inventory
    Positive shrinkage = product is missing / unaccounted for.
    """
    date_from = request.args.get('date_from', (date.today() - timedelta(days=90)).isoformat())
    date_to   = request.args.get('date_to', date.today().isoformat())
    conn = get_db(); cur = conn.cursor()

    # Transfers in per tech+product
    cur.execute("""
        SELECT it.technician_id, it.product_id,
               SUM(it.quantity_storage_units) AS transferred
        FROM inventory_transactions it
        WHERE it.transaction_type = 'transfer'
          AND DATE(it.created_at) BETWEEN %s AND %s
        GROUP BY it.technician_id, it.product_id
    """, (date_from, date_to))
    transfers = {(r['technician_id'], r['product_id']): float(r['transferred'] or 0)
                 for r in cur.fetchall()}

    # Usage sync out per tech+product
    cur.execute("""
        SELECT it.technician_id, it.product_id,
               SUM(it.quantity_storage_units) AS used
        FROM inventory_transactions it
        WHERE it.transaction_type = 'usage_sync'
          AND COALESCE(it.appt_date, DATE(it.created_at)) BETWEEN %s AND %s
        GROUP BY it.technician_id, it.product_id
    """, (date_from, date_to))
    used = {(r['technician_id'], r['product_id']): float(r['used'] or 0)
            for r in cur.fetchall()}

    # Current tech inventory
    cur.execute("""
        SELECT ti.technician_id, ti.product_id, ti.quantity_storage_units AS current_qty,
               t.name AS tech_name, t.truck_id, p.name AS product_name, p.storage_unit,
               COALESCE(p.cost_per_storage_unit,0) AS cost_per_unit
        FROM tech_inventory ti
        JOIN technicians t ON ti.technician_id = t.id
        JOIN products p    ON ti.product_id    = p.id
        WHERE p.active = TRUE AND t.active = TRUE
          AND COALESCE(p.is_equipment, FALSE) = FALSE
    """)
    rows = []
    for r in cur.fetchall():
        key = (r['technician_id'], r['product_id'])
        tx  = transfers.get(key, 0)
        us  = used.get(key, 0)
        cur_qty = float(r['current_qty'] or 0)
        expected = tx - us  # what should remain from transfers minus usage
        shrinkage = expected - cur_qty  # positive = unaccounted loss
        rows.append({
            'tech_name':    r['tech_name'],
            'truck_id':     r['truck_id'],
            'product_name': r['product_name'],
            'storage_unit': r['storage_unit'],
            'transferred':  tx,
            'used_fr':      us,
            'current_qty':  cur_qty,
            'expected':     expected,
            'shrinkage':    shrinkage,
            'shrinkage_value': shrinkage * float(r['cost_per_unit']),
        })
    cur.close(); conn.close()
    # Only return rows with any activity or discrepancy
    rows = [r for r in rows if r['transferred'] > 0 or r['used_fr'] > 0]
    rows.sort(key=lambda r: (-abs(r['shrinkage']), r['tech_name']))
    return jsonify(rows)

@app.route('/api/reports/restock-suggestions', methods=['GET'])
@login_required
def report_restock_suggestions():
    """
    For each tech, compute average daily usage over last N days
    and flag products that will run out soon.
    """
    days    = int(request.args.get('days', 30))
    tech_id = request.args.get('tech_id')
    date_from = (date.today() - timedelta(days=days)).isoformat()
    date_to   = date.today().isoformat()
    conn = get_db(); cur = conn.cursor()

    # Average daily usage per tech+product
    cur.execute("""
        SELECT it.technician_id, it.product_id,
               SUM(it.quantity_storage_units) / %s AS avg_daily
        FROM inventory_transactions it
        WHERE it.transaction_type = 'usage_sync'
          AND COALESCE(it.appt_date, DATE(it.created_at)) BETWEEN %s AND %s
        GROUP BY it.technician_id, it.product_id
    """, (days, date_from, date_to))
    avg_usage = {(r['technician_id'], r['product_id']): float(r['avg_daily'] or 0)
                 for r in cur.fetchall()}

    # Current truck inventory
    cur.execute("""
        SELECT ti.technician_id, ti.product_id, ti.quantity_storage_units AS current_qty,
               t.name AS tech_name, t.truck_id,
               p.name AS product_name, p.storage_unit
        FROM tech_inventory ti
        JOIN technicians t ON ti.technician_id = t.id
        JOIN products p    ON ti.product_id    = p.id
        WHERE p.active = TRUE AND t.active = TRUE
          AND COALESCE(p.is_equipment, FALSE) = FALSE
    """)
    rows = []
    for r in cur.fetchall():
        key = (r['technician_id'], r['product_id'])
        avg = avg_usage.get(key, 0)
        cur_qty = float(r['current_qty'] or 0)
        days_left = round(cur_qty / avg) if avg > 0 else None
        rows.append({
            'technician_id': r['technician_id'],
            'tech_name':     r['tech_name'],
            'truck_id':      r['truck_id'],
            'product_id':    r['product_id'],
            'product_name':  r['product_name'],
            'storage_unit':  r['storage_unit'],
            'current_qty':   cur_qty,
            'avg_daily':     avg,
            'days_left':     days_left,
        })
    cur.close(); conn.close()
    # Only return products with actual usage in the period
    rows = [r for r in rows if avg_usage.get((r['technician_id'], r['product_id']), 0) > 0]
    if tech_id:
        rows = [r for r in rows if r['technician_id'] == int(tech_id)]
    rows.sort(key=lambda r: (r['days_left'] if r['days_left'] is not None else 9999, r['tech_name']))
    return jsonify(rows)

# ─── SYSTEM LOG HELPER ───────────────────────────────────────────────────────
def log_system_event(event_type, description, performed_by=None, details=None):
    """Write a system event to the system_log table. Non-fatal — swallows errors."""
    try:
        conn = get_db(); cur = conn.cursor()
        cur.execute("""
            INSERT INTO system_log (event_type, description, performed_by, details)
            VALUES (%s, %s, %s, %s)
        """, (event_type, description, performed_by,
              json.dumps(details) if details else None))
        conn.commit(); cur.close(); conn.close()
    except Exception:
        pass  # never let logging break the actual operation

@app.route('/api/system/log', methods=['GET'])
@login_required
def get_system_log():
    limit      = min(int(request.args.get('limit', 50)), 500)
    offset     = int(request.args.get('offset', 0))
    event_type = request.args.get('type')
    conn = get_db(); cur = conn.cursor()
    q = "SELECT * FROM system_log WHERE 1=1"
    params = []
    if event_type:
        q += " AND event_type = %s"; params.append(event_type)
    q += f" ORDER BY created_at DESC LIMIT {limit} OFFSET {offset}"
    cur.execute(q, params)
    rows = [dict(r) for r in cur.fetchall()]
    cur.close(); conn.close()
    for r in rows:
        if r.get('created_at'):
            r['created_at'] = r['created_at'].isoformat()
        if r.get('details') and isinstance(r['details'], str):
            try: r['details'] = json.loads(r['details'])
            except: pass
    return jsonify({'events': rows})

@app.route('/api/system/health', methods=['GET'])
@login_required
def system_health():
    """Key system health metrics for the dashboard."""
    conn = get_db(); cur = conn.cursor()
    stats = {}

    cur.execute("SELECT pg_size_pretty(pg_database_size(current_database())) AS sz")
    stats['db_size'] = cur.fetchone()['sz']

    cur.execute("SELECT COUNT(*) AS n FROM inventory_transactions WHERE archived IS NOT TRUE")
    stats['total_transactions'] = cur.fetchone()['n']

    cur.execute("SELECT COUNT(*) AS n FROM inventory_transactions WHERE archived = TRUE")
    stats['archived_transactions'] = cur.fetchone()['n']

    cur.execute("SELECT COUNT(*) AS n FROM fr_appointments_raw")
    stats['total_appointments'] = cur.fetchone()['n']

    cur.execute("SELECT COUNT(*) AS n FROM fr_chemical_uses_raw")
    stats['total_chemical_uses'] = cur.fetchone()['n']

    cur.execute("""
        SELECT COUNT(*) AS n FROM product_thresholds pt
        JOIN warehouse_inventory wi ON wi.product_id = pt.product_id
        WHERE wi.location = 'Holpers' AND wi.quantity_storage_units <= pt.min_warehouse_qty
          AND COALESCE(pt.include_in_alerts, TRUE) = TRUE
    """)
    stats['low_stock_count'] = cur.fetchone()['n']

    cur.execute("SELECT value FROM sync_state WHERE key = 'appt_last_run'")
    r = cur.fetchone(); stats['last_appt_sync'] = r['value'] if r else None

    cur.execute("SELECT value FROM sync_state WHERE key = 'cu_last_run'")
    r = cur.fetchone(); stats['last_cu_sync'] = r['value'] if r else None

    cur.execute("SELECT COUNT(*) AS n FROM products WHERE active = TRUE")
    stats['active_products'] = cur.fetchone()['n']

    cur.execute("SELECT COUNT(*) AS n FROM technicians WHERE active = TRUE")
    stats['active_technicians'] = cur.fetchone()['n']

    cur.close(); conn.close()
    return jsonify(stats)


# ─── TECH EFFICIENCY SCORE ───────────────────────────────────────────────────
@app.route('/api/reports/tech-efficiency', methods=['GET'])
@login_required
def report_tech_efficiency():
    """
    Efficiency score per tech, normalized by service type so techs with
    different job mixes are compared fairly.
    Score = min(100, (expected_cost / actual_cost) * 75)
    where expected_cost = sum of team-avg cost for each of their service types.
    Average tech scores 75. Higher = more efficient.
    """
    date_from = request.args.get('date_from', (date.today().replace(day=1) - timedelta(days=1)).replace(day=1).isoformat())
    date_to   = request.args.get('date_to', date.today().isoformat())
    min_jobs  = int(request.args.get('min_jobs', 5))
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        WITH appt_costs AS (
            SELECT it.technician_id, it.fr_appointment_id,
                   COALESCE(a.service_type_id, 'unknown') AS service_type_id,
                   SUM(it.quantity_storage_units * COALESCE(p.cost_per_storage_unit,0)) AS appt_cost
            FROM inventory_transactions it
            JOIN fr_appointments_raw a ON it.fr_appointment_id = a.appointment_id
            JOIN products p ON it.product_id = p.id
            WHERE it.transaction_type = 'usage_sync'
              AND COALESCE(it.appt_date, DATE(it.created_at)) BETWEEN %s AND %s
              AND it.archived IS NOT TRUE
              AND COALESCE(p.is_equipment, FALSE) = FALSE
            GROUP BY it.technician_id, it.fr_appointment_id, a.service_type_id
        ),
        type_avgs AS (
            SELECT service_type_id, AVG(appt_cost) AS avg_cost, COUNT(*) AS sample_size
            FROM appt_costs GROUP BY service_type_id
        ),
        tech_stats AS (
            SELECT ac.technician_id,
                   COUNT(*)                       AS job_count,
                   SUM(ac.appt_cost)              AS actual_cost,
                   SUM(ta.avg_cost)               AS expected_cost,
                   STDDEV(ac.appt_cost)           AS cost_stddev,
                   AVG(ac.appt_cost)              AS avg_cost_per_job
            FROM appt_costs ac
            JOIN type_avgs ta ON ac.service_type_id = ta.service_type_id
            GROUP BY ac.technician_id
            HAVING COUNT(*) >= %s
        )
        SELECT t.id, t.name, t.truck_id,
               ts.job_count, ts.actual_cost, ts.expected_cost,
               ts.cost_stddev, ts.avg_cost_per_job,
               CASE WHEN ts.actual_cost > 0
                    THEN LEAST(100, ROUND((ts.expected_cost / ts.actual_cost) * 75))
                    ELSE NULL END AS efficiency_score,
               CASE WHEN ts.actual_cost > 0
                    THEN ROUND(((ts.expected_cost - ts.actual_cost) / ts.actual_cost) * 100)
                    ELSE NULL END AS vs_avg_pct
        FROM tech_stats ts
        JOIN technicians t ON ts.technician_id = t.id
        ORDER BY efficiency_score DESC NULLS LAST
    """, (date_from, date_to, min_jobs))
    rows = [dict(r) for r in cur.fetchall()]
    cur.close(); conn.close()
    for r in rows:
        r['actual_cost']    = float(r['actual_cost'] or 0)
        r['expected_cost']  = float(r['expected_cost'] or 0)
        r['avg_cost_per_job'] = float(r['avg_cost_per_job'] or 0)
        r['cost_stddev']    = float(r['cost_stddev'] or 0)
        r['efficiency_score'] = int(r['efficiency_score']) if r['efficiency_score'] is not None else None
        r['vs_avg_pct']     = int(r['vs_avg_pct']) if r['vs_avg_pct'] is not None else None
        score = r['efficiency_score']
        r['grade'] = 'A' if score and score >= 90 else 'B' if score and score >= 75 else 'C' if score and score >= 60 else 'D' if score and score >= 45 else 'F' if score is not None else '—'
    return jsonify(rows)


@app.route('/api/reports/tech-daily-usage', methods=['GET'])
@login_required
def tech_daily_usage():
    """
    For a given tech and date, return per-product started/used/ended quantities.

    Correct reconstruction (works for any past date, not just yesterday):
      ended_D   = current_inventory + SUM(usage_sync deductions that happened AFTER date D)
      started_D = ended_D + used_on_D

    This works backwards from today's known inventory state.
    Note: transfers to/from the tech that occurred after D are not yet accounted for,
    but usage_sync is the dominant daily movement.
    """
    tech_id     = request.args.get('tech_id', type=int)
    target_date = request.args.get('date', (date.today() - timedelta(days=1)).isoformat())
    if not tech_id:
        return jsonify({'error': 'tech_id required'}), 400

    conn = get_db(); cur = conn.cursor()

    # Single query: usage on target date + post-date deductions + current inventory
    cur.execute("""
        WITH target_usage AS (
            SELECT it.product_id,
                   p.name                                              AS product_name,
                   p.storage_unit,
                   p.usage_unit,
                   COALESCE(p.conversion_factor, 1)                   AS conversion_factor,
                   COALESCE(p.use_applied_amount, FALSE)              AS use_applied_amount,
                   COALESCE(p.cost_per_storage_unit, 0)               AS cost_per_unit,
                   SUM(it.quantity_storage_units)                     AS used_storage,
                   SUM(it.quantity_usage_units)                       AS used_applied,
                   SUM(it.quantity_storage_units
                       * COALESCE(p.cost_per_storage_unit, 0))        AS cost,
                   COUNT(DISTINCT it.fr_appointment_id)               AS job_count
            FROM inventory_transactions it
            JOIN products p ON it.product_id = p.id
            WHERE it.technician_id = %s
              AND it.transaction_type = 'usage_sync'
              AND it.appt_date = %s
            GROUP BY it.product_id, p.name, p.storage_unit, p.usage_unit,
                     p.conversion_factor, p.use_applied_amount, p.cost_per_storage_unit
        ),
        post_usage AS (
            -- All usage_sync deductions that occurred AFTER the target date
            SELECT it.product_id,
                   SUM(it.quantity_storage_units) AS deducted_after
            FROM inventory_transactions it
            WHERE it.technician_id = %s
              AND it.transaction_type = 'usage_sync'
              AND it.appt_date > %s
            GROUP BY it.product_id
        )
        SELECT tu.*,
               COALESCE(ti.quantity_storage_units, 0) AS current_qty,
               COALESCE(pu.deducted_after, 0)         AS deducted_after
        FROM target_usage tu
        LEFT JOIN tech_inventory ti
               ON ti.product_id = tu.product_id AND ti.technician_id = %s
        LEFT JOIN post_usage pu ON pu.product_id = tu.product_id
        ORDER BY tu.cost DESC
    """, (tech_id, target_date, tech_id, target_date, tech_id))
    usage_rows = cur.fetchall()

    cur.close(); conn.close()

    if not usage_rows:
        return jsonify({'date': target_date, 'products': [], 'total_cost': 0, 'total_jobs': 0})

    products_out = []
    total_cost   = 0.0

    for r in usage_rows:
        cf             = float(r['conversion_factor'])
        use_applied    = bool(r['use_applied_amount'])
        used_storage   = float(r['used_storage']   or 0)
        used_applied_q = float(r['used_applied']   or 0)
        current_qty    = float(r['current_qty']    or 0)
        deducted_after = float(r['deducted_after'] or 0)
        cost           = float(r['cost']           or 0)
        total_cost    += cost

        # Reconstruct inventory state at END of target date
        ended_storage   = current_qty + deducted_after
        started_storage = ended_storage + used_storage

        # Convert to display units
        if use_applied and used_applied_q:
            used_display    = round(used_applied_q, 2)
        else:
            used_display    = round(used_storage * cf, 2)
        started_display = round(started_storage * cf, 2)
        ended_display   = round(ended_storage   * cf, 2)

        products_out.append({
            'product_id':    r['product_id'],
            'product_name':  r['product_name'],
            'usage_unit':    r['usage_unit'],
            'storage_unit':  r['storage_unit'],
            'started':       started_display,
            'used':          used_display,
            'ended':         ended_display,
            'cost':          round(cost, 2),
            'job_count':     r['job_count'],
        })

    return jsonify({
        'date':       target_date,
        'products':   products_out,
        'total_cost': round(total_cost, 2),
        'total_jobs': sum(r['job_count'] for r in usage_rows),
    })


@app.route('/api/reports/tech-efficiency/detail', methods=['GET'])
@login_required
def tech_efficiency_detail():
    """
    Per-tech efficiency drill-down:
    - by_service_type: their avg cost/job vs team avg for each service type they ran
    - cost_drivers: products where this tech uses more per job than the team average
    """
    tech_id   = request.args.get('tech_id', type=int)
    date_from = request.args.get('date_from')
    date_to   = request.args.get('date_to', date.today().isoformat())
    if not tech_id or not date_from:
        return jsonify({'error': 'tech_id and date_from required'}), 400

    conn = get_db(); cur = conn.cursor()

    # ── Service-type breakdown ────────────────────────────────────────────────
    cur.execute("""
        WITH appt_costs AS (
            SELECT it.technician_id, it.fr_appointment_id,
                   COALESCE(a.service_type_id, 'unknown') AS service_type_id,
                   SUM(it.quantity_storage_units * COALESCE(p.cost_per_storage_unit, 0)) AS appt_cost
            FROM inventory_transactions it
            JOIN fr_appointments_raw a ON it.fr_appointment_id = a.appointment_id
            JOIN products p ON it.product_id = p.id
            WHERE it.transaction_type = 'usage_sync'
              AND COALESCE(it.appt_date, DATE(it.created_at)) BETWEEN %s AND %s
              AND it.archived IS NOT TRUE
              AND COALESCE(p.is_equipment, FALSE) = FALSE
            GROUP BY it.technician_id, it.fr_appointment_id, a.service_type_id
        ),
        type_avgs AS (
            SELECT service_type_id, AVG(appt_cost) AS team_avg_per_job
            FROM appt_costs GROUP BY service_type_id
        ),
        tech_by_type AS (
            SELECT ac.service_type_id,
                   COUNT(*)              AS job_count,
                   AVG(ac.appt_cost)     AS their_avg_per_job,
                   SUM(ac.appt_cost)     AS their_total,
                   SUM(ta.team_avg_per_job) AS expected_total
            FROM appt_costs ac
            JOIN type_avgs ta ON ac.service_type_id = ta.service_type_id
            WHERE ac.technician_id = %s
            GROUP BY ac.service_type_id
        )
        SELECT tbt.service_type_id,
               COALESCE(st.description, 'Type ' || tbt.service_type_id) AS service_type_name,
               tbt.job_count,
               tbt.their_avg_per_job,
               ta.team_avg_per_job,
               tbt.their_total,
               tbt.expected_total,
               tbt.their_total - tbt.expected_total AS excess_cost
        FROM tech_by_type tbt
        JOIN type_avgs ta ON tbt.service_type_id = ta.service_type_id
        LEFT JOIN service_types st ON tbt.service_type_id = st.type_id
        ORDER BY excess_cost DESC
    """, (date_from, date_to, tech_id))
    by_type = []
    for r in cur.fetchall():
        d = dict(r)
        d['their_avg_per_job'] = float(d['their_avg_per_job'] or 0)
        d['team_avg_per_job']  = float(d['team_avg_per_job']  or 0)
        d['their_total']       = float(d['their_total']       or 0)
        d['expected_total']    = float(d['expected_total']    or 0)
        d['excess_cost']       = float(d['excess_cost']       or 0)
        d['pct_over'] = round((d['their_avg_per_job'] - d['team_avg_per_job']) / d['team_avg_per_job'] * 100, 1) \
                        if d['team_avg_per_job'] > 0 else None
        by_type.append(d)

    # ── Product cost drivers ──────────────────────────────────────────────────
    # For each product: this tech's avg cost per job vs the overall team avg for same product
    cur.execute("""
        WITH date_range AS (SELECT %s::date AS d_from, %s::date AS d_to),
        tech_prod AS (
            SELECT it.product_id,
                   COUNT(DISTINCT it.fr_appointment_id) AS job_count,
                   SUM(it.quantity_storage_units)       AS total_qty,
                   AVG(it.quantity_storage_units)       AS avg_qty_per_job,
                   AVG(it.quantity_storage_units * COALESCE(p.cost_per_storage_unit, 0)) AS avg_cost_per_job,
                   SUM(it.quantity_storage_units * COALESCE(p.cost_per_storage_unit, 0)) AS total_cost
            FROM inventory_transactions it
            JOIN products p ON it.product_id = p.id, date_range dr
            WHERE it.transaction_type = 'usage_sync'
              AND COALESCE(it.appt_date, DATE(it.created_at)) BETWEEN dr.d_from AND dr.d_to
              AND it.archived IS NOT TRUE
              AND COALESCE(p.is_equipment, FALSE) = FALSE
              AND it.technician_id = %s
            GROUP BY it.product_id
        ),
        team_prod AS (
            SELECT it.product_id,
                   AVG(it.quantity_storage_units)       AS avg_qty_per_job,
                   AVG(it.quantity_storage_units * COALESCE(p.cost_per_storage_unit, 0)) AS avg_cost_per_job
            FROM inventory_transactions it
            JOIN products p ON it.product_id = p.id, date_range dr
            WHERE it.transaction_type = 'usage_sync'
              AND COALESCE(it.appt_date, DATE(it.created_at)) BETWEEN dr.d_from AND dr.d_to
              AND it.archived IS NOT TRUE
              AND COALESCE(p.is_equipment, FALSE) = FALSE
            GROUP BY it.product_id
        )
        SELECT p.name AS product_name, p.storage_unit,
               COALESCE(p.cost_per_storage_unit, 0) AS cost_per_unit,
               tp.job_count,
               tp.avg_qty_per_job  AS tech_avg_qty,
               tp.avg_cost_per_job AS tech_avg_cost,
               team.avg_qty_per_job  AS team_avg_qty,
               team.avg_cost_per_job AS team_avg_cost,
               tp.avg_cost_per_job - team.avg_cost_per_job AS cost_delta_per_job,
               (tp.avg_cost_per_job - team.avg_cost_per_job) * tp.job_count AS total_excess
        FROM tech_prod tp
        JOIN team_prod team ON tp.product_id = team.product_id
        JOIN products p ON tp.product_id = p.id
        WHERE tp.avg_cost_per_job > team.avg_cost_per_job * 1.05
        ORDER BY total_excess DESC
        LIMIT 8
    """, (date_from, date_to, tech_id))
    cost_drivers = []
    for r in cur.fetchall():
        d = dict(r)
        for k in ('tech_avg_qty','tech_avg_cost','team_avg_qty','team_avg_cost',
                  'cost_delta_per_job','total_excess','cost_per_unit'):
            d[k] = float(d[k] or 0)
        d['pct_over'] = round((d['tech_avg_cost'] - d['team_avg_cost']) / d['team_avg_cost'] * 100, 1) \
                        if d['team_avg_cost'] > 0 else None
        cost_drivers.append(d)

    cur.close(); conn.close()
    return jsonify({'by_service_type': by_type, 'cost_drivers': cost_drivers})


@app.route('/api/reports/tech-efficiency/service-type-jobs', methods=['GET'])
@login_required
def tech_efficiency_service_type_jobs():
    """
    Drill-down: all appointments for a specific tech + service type within a date range.
    Returns each appointment with its chemical usage transactions (for inline correction).
    """
    tech_id         = request.args.get('tech_id', type=int)
    service_type_id = request.args.get('service_type_id', '')
    date_from       = request.args.get('date_from')
    date_to         = request.args.get('date_to', date.today().isoformat())
    if not tech_id or not date_from:
        return jsonify({'error': 'tech_id and date_from required'}), 400

    conn = get_db(); cur = conn.cursor()

    # Distinct appointments for this tech + service type
    cur.execute("""
        SELECT DISTINCT a.appointment_id, a.appt_date, a.customer_id,
               COALESCE(st.description, 'Type ' || COALESCE(a.service_type_id,'?')) AS service_type_name
        FROM inventory_transactions it
        JOIN fr_appointments_raw a ON it.fr_appointment_id = a.appointment_id
        LEFT JOIN service_types st ON a.service_type_id = st.type_id
        WHERE it.transaction_type = 'usage_sync'
          AND it.technician_id = %s
          AND COALESCE(a.service_type_id, 'unknown') = %s
          AND COALESCE(it.appt_date, DATE(it.created_at)) BETWEEN %s AND %s
          AND it.archived IS NOT TRUE
        ORDER BY a.appt_date DESC
    """, (tech_id, service_type_id, date_from, date_to))
    appts = [dict(r) for r in cur.fetchall()]

    if not appts:
        cur.close(); conn.close()
        return jsonify([])

    appt_ids = [a['appointment_id'] for a in appts]

    # All usage transactions for those appointments
    cur.execute("""
        SELECT it.id AS transaction_id,
               it.fr_appointment_id AS appointment_id,
               p.name AS product_name, p.usage_unit, p.storage_unit,
               p.conversion_factor,
               COALESCE(p.cost_per_storage_unit, 0) AS cost_per_unit,
               it.quantity_usage_units, it.quantity_storage_units,
               it.quantity_storage_units * COALESCE(p.cost_per_storage_unit, 0) AS cost,
               it.notes
        FROM inventory_transactions it
        JOIN products p ON it.product_id = p.id
        WHERE it.transaction_type = 'usage_sync'
          AND it.technician_id = %s
          AND it.fr_appointment_id = ANY(%s)
          AND it.archived IS NOT TRUE
        ORDER BY it.fr_appointment_id, p.name
    """, (tech_id, appt_ids))
    txns = [dict(r) for r in cur.fetchall()]
    cur.close(); conn.close()

    # Group transactions by appointment
    txn_map = {}
    for t in txns:
        aid = t['appointment_id']
        txn_map.setdefault(aid, [])
        t['quantity_usage_units']  = float(t['quantity_usage_units']  or 0)
        t['quantity_storage_units']= float(t['quantity_storage_units'] or 0)
        t['cost']                  = float(t['cost']                  or 0)
        t['cost_per_unit']         = float(t['cost_per_unit']         or 0)
        t['conversion_factor']     = float(t['conversion_factor']     or 1)
        txn_map[aid].append(t)

    result = []
    for a in appts:
        aid  = a['appointment_id']
        chems = txn_map.get(aid, [])
        result.append({
            'appointment_id':   aid,
            'appt_date':        str(a['appt_date']) if a['appt_date'] else None,
            'customer_id':      a['customer_id'],
            'service_type_name': a['service_type_name'],
            'total_cost':       round(sum(c['cost'] for c in chems), 4),
            'chemicals':        chems,
        })

    return jsonify(result)


# ─── WEEKLY MANAGER DIGEST ────────────────────────────────────────────────────
@app.route('/api/dashboard/weekly-digest', methods=['GET'])
@login_required
def weekly_digest():
    """Key weekly metrics for the manager digest dashboard card.
    Optional query param: week_offset (int, 0=current, -1=last week, -2=two weeks ago, etc.)
    """
    today      = date.today()
    offset     = request.args.get('week_offset', 0, type=int)
    # Always anchor to Mon–Sun of the requested week
    this_monday = today - timedelta(days=today.weekday())
    week_start  = this_monday + timedelta(weeks=offset)
    week_end    = week_start + timedelta(days=6)   # Sunday
    # For the current week cap at today so we don't show future days
    effective_end = min(week_end, today)
    prev_start  = week_start - timedelta(days=7)
    prev_end    = week_start - timedelta(days=1)
    is_current_week = (offset == 0)
    conn = get_db(); cur = conn.cursor()

    def week_spend(d_from, d_to):
        cur.execute("""
            SELECT COALESCE(SUM(it.quantity_storage_units * COALESCE(p.cost_per_storage_unit,0)),0) AS total
            FROM inventory_transactions it JOIN products p ON it.product_id=p.id
            WHERE it.transaction_type='usage_sync' AND it.archived IS NOT TRUE
              AND COALESCE(it.appt_date,DATE(it.created_at)) BETWEEN %s AND %s
        """, (d_from, d_to))
        return float(cur.fetchone()['total'] or 0)

    this_week_spend = week_spend(week_start, effective_end)
    last_week_spend = week_spend(prev_start, prev_end)

    # Top 5 products this week by cost
    cur.execute("""
        SELECT p.name, p.usage_unit, p.storage_unit,
               SUM(it.quantity_storage_units * p.conversion_factor) AS total_usage,
               SUM(it.quantity_storage_units * COALESCE(p.cost_per_storage_unit,0)) AS total_cost
        FROM inventory_transactions it JOIN products p ON it.product_id=p.id
        WHERE it.transaction_type='usage_sync' AND it.archived IS NOT TRUE
          AND COALESCE(it.appt_date,DATE(it.created_at)) BETWEEN %s AND %s
        GROUP BY p.name, p.usage_unit, p.storage_unit
        ORDER BY total_cost DESC LIMIT 5
    """, (week_start, effective_end))
    top_products = [dict(r) for r in cur.fetchall()]
    for r in top_products:
        r['total_cost']  = float(r['total_cost'] or 0)
        r['total_usage'] = float(r['total_usage'] or 0)

    # Top spending tech this week
    cur.execute("""
        SELECT t.name,
               SUM(it.quantity_storage_units * COALESCE(p.cost_per_storage_unit,0)) AS cost
        FROM inventory_transactions it
        JOIN technicians t ON it.technician_id=t.id
        JOIN products p ON it.product_id=p.id
        WHERE it.transaction_type='usage_sync' AND it.archived IS NOT TRUE
          AND COALESCE(it.appt_date,DATE(it.created_at)) BETWEEN %s AND %s
        GROUP BY t.name ORDER BY cost DESC LIMIT 1
    """, (week_start, effective_end))
    top_tech = dict(cur.fetchone() or {})
    if top_tech: top_tech['cost'] = float(top_tech.get('cost') or 0)

    # Low stock count (warehouse only, Holpers location) — always current
    cur.execute("""
        SELECT COUNT(*) AS n FROM (
            SELECT wi.product_id, SUM(wi.quantity_storage_units) AS qty,
                   pt.min_warehouse_qty
            FROM warehouse_inventory wi
            JOIN product_thresholds pt ON wi.product_id=pt.product_id
            WHERE wi.location='Holpers'
            GROUP BY wi.product_id, pt.min_warehouse_qty
            HAVING SUM(wi.quantity_storage_units) < pt.min_warehouse_qty
        ) sub
    """)
    low_stock_count = int((cur.fetchone() or {}).get('n', 0))

    # Receive count this week
    cur.execute("""
        SELECT COUNT(*) AS n FROM inventory_transactions
        WHERE transaction_type='receive' AND DATE(created_at) BETWEEN %s AND %s
    """, (week_start, effective_end))
    receives_this_week = int((cur.fetchone() or {}).get('n', 0))

    # Jobs with chemical usage this week
    cur.execute("""
        SELECT COUNT(DISTINCT fr_appointment_id) AS n FROM inventory_transactions
        WHERE transaction_type='usage_sync' AND archived IS NOT TRUE
          AND COALESCE(appt_date,DATE(created_at)) BETWEEN %s AND %s
    """, (week_start, effective_end))
    jobs_this_week = int((cur.fetchone() or {}).get('n', 0))

    # Techs who had chemical usage this week
    cur.execute("""
        SELECT COUNT(DISTINCT technician_id) AS n FROM inventory_transactions
        WHERE transaction_type='usage_sync' AND archived IS NOT TRUE
          AND COALESCE(appt_date,DATE(created_at)) BETWEEN %s AND %s
    """, (week_start, effective_end))
    techs_active_this_week = int((cur.fetchone() or {}).get('n', 0))

    # Warehouse → truck transfers this week
    cur.execute("""
        SELECT COUNT(*) AS n FROM inventory_transactions
        WHERE transaction_type='transfer' AND DATE(created_at) BETWEEN %s AND %s
          AND notes IS DISTINCT FROM 'Manual distribution correction'
    """, (week_start, effective_end))
    transfers_this_week = int((cur.fetchone() or {}).get('n', 0))

    cur.close(); conn.close()
    spend_change_pct = round(((this_week_spend - last_week_spend) / last_week_spend * 100) if last_week_spend > 0 else 0, 1)
    return jsonify({
        'week_start':           week_start.isoformat(),
        'week_end':             effective_end.isoformat(),
        'week_offset':          offset,
        'is_current_week':      is_current_week,
        'this_week_spend':      round(this_week_spend, 2),
        'last_week_spend':      round(last_week_spend, 2),
        'spend_change_pct':     spend_change_pct,
        'top_products':         top_products,
        'top_tech':             top_tech,
        'low_stock_count':      low_stock_count,
        'receives_this_week':   receives_this_week,
        'jobs_this_week':       jobs_this_week,
        'techs_active_this_week': techs_active_this_week,
        'transfers_this_week':  transfers_this_week,
    })


# ─── YEAR-OVER-YEAR REPORT ───────────────────────────────────────────────────
@app.route('/api/reports/year-over-year', methods=['GET'])
@login_required
def report_year_over_year():
    """Compare current period vs same period last year."""
    date_from = request.args.get('date_from', date.today().replace(month=1, day=1).isoformat())
    date_to   = request.args.get('date_to', date.today().isoformat())
    d_from = date.fromisoformat(date_from)
    d_to   = date.fromisoformat(date_to)
    prev_from = d_from.replace(year=d_from.year - 1)
    prev_to   = d_to.replace(year=d_to.year - 1)
    conn = get_db(); cur = conn.cursor()

    def period_summary(pf, pt):
        cur.execute("""
            SELECT
                COALESCE(SUM(it.quantity_storage_units * COALESCE(p.cost_per_storage_unit,0)),0) AS total_cost,
                COUNT(DISTINCT it.fr_appointment_id)                                              AS job_count,
                COUNT(DISTINCT it.technician_id)                                                  AS tech_count,
                COUNT(DISTINCT it.product_id)                                                     AS product_count
            FROM inventory_transactions it JOIN products p ON it.product_id=p.id
            WHERE it.transaction_type='usage_sync' AND it.archived IS NOT TRUE
              AND COALESCE(it.appt_date,DATE(it.created_at)) BETWEEN %s AND %s
        """, (pf, pt))
        r = dict(cur.fetchone())
        r['total_cost']   = float(r['total_cost'] or 0)
        r['job_count']    = int(r['job_count'] or 0)
        r['tech_count']   = int(r['tech_count'] or 0)
        r['product_count']= int(r['product_count'] or 0)
        r['cost_per_job'] = round(r['total_cost']/r['job_count'],2) if r['job_count'] else 0
        return r

    def period_by_tech(pf, pt):
        cur.execute("""
            SELECT t.name, t.truck_id,
                   SUM(it.quantity_storage_units * COALESCE(p.cost_per_storage_unit,0)) AS cost,
                   COUNT(DISTINCT it.fr_appointment_id) AS jobs
            FROM inventory_transactions it
            JOIN technicians t ON it.technician_id=t.id
            JOIN products p ON it.product_id=p.id
            WHERE it.transaction_type='usage_sync' AND it.archived IS NOT TRUE
              AND COALESCE(it.appt_date,DATE(it.created_at)) BETWEEN %s AND %s
            GROUP BY t.name, t.truck_id ORDER BY cost DESC
        """, (pf, pt))
        return [{**dict(r), 'cost': float(r['cost'] or 0), 'jobs': int(r['jobs'] or 0)} for r in cur.fetchall()]

    def period_top_products(pf, pt, n=10):
        cur.execute("""
            SELECT p.name, p.storage_unit,
                   SUM(it.quantity_storage_units) AS total_storage,
                   SUM(it.quantity_storage_units * COALESCE(p.cost_per_storage_unit,0)) AS cost
            FROM inventory_transactions it JOIN products p ON it.product_id=p.id
            WHERE it.transaction_type='usage_sync' AND it.archived IS NOT TRUE
              AND COALESCE(it.appt_date,DATE(it.created_at)) BETWEEN %s AND %s
            GROUP BY p.name, p.storage_unit ORDER BY cost DESC LIMIT %s
        """, (pf, pt, n))
        return [{**dict(r), 'cost': float(r['cost'] or 0), 'total_storage': float(r['total_storage'] or 0)} for r in cur.fetchall()]

    def period_monthly(pf, pt):
        cur.execute("""
            SELECT DATE_TRUNC('month', COALESCE(it.appt_date::timestamp, it.created_at)) AS month,
                   SUM(it.quantity_storage_units * COALESCE(p.cost_per_storage_unit,0)) AS cost
            FROM inventory_transactions it JOIN products p ON it.product_id=p.id
            WHERE it.transaction_type='usage_sync' AND it.archived IS NOT TRUE
              AND COALESCE(it.appt_date,DATE(it.created_at)) BETWEEN %s AND %s
            GROUP BY month ORDER BY month
        """, (pf, pt))
        return [{'month': r['month'].strftime('%Y-%m'), 'cost': float(r['cost'] or 0)} for r in cur.fetchall()]

    cur_summary  = period_summary(d_from, d_to)
    prev_summary = period_summary(prev_from, prev_to)
    cur.close(); conn.close()

    def pct_change(cur_v, prev_v):
        if prev_v == 0: return None
        return round((cur_v - prev_v) / prev_v * 100, 1)

    summary_delta = {k: pct_change(cur_summary[k], prev_summary[k]) for k in cur_summary}

    conn = get_db(); cur = conn.cursor()
    result = {
        'current':       {'from': date_from, 'to': date_to, 'summary': cur_summary,
                          'by_tech': period_by_tech(d_from, d_to),
                          'top_products': period_top_products(d_from, d_to),
                          'monthly': period_monthly(d_from, d_to)},
        'previous':      {'from': prev_from.isoformat(), 'to': prev_to.isoformat(), 'summary': prev_summary,
                          'by_tech': period_by_tech(prev_from, prev_to),
                          'top_products': period_top_products(prev_from, prev_to),
                          'monthly': period_monthly(prev_from, prev_to)},
        'summary_delta': summary_delta,
    }
    cur.close(); conn.close()
    return jsonify(result)


# ─── DATA ARCHIVING ───────────────────────────────────────────────────────────
@app.route('/api/admin/archive', methods=['POST'])
@login_required
def archive_old_transactions():
    """
    Mark transactions older than N months as archived.
    Archived records are excluded from reports but remain in DB.
    """
    d = request.json or {}
    months = max(12, int(d.get('months', 36)))  # minimum 12 months, default 3 years
    cutoff = (date.today().replace(day=1) - timedelta(days=months * 31)).replace(day=1)
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        UPDATE inventory_transactions
        SET archived = TRUE
        WHERE transaction_type = 'usage_sync'
          AND COALESCE(appt_date, DATE(created_at)) < %s
          AND (archived IS NULL OR archived = FALSE)
    """, (cutoff,))
    affected = cur.rowcount
    conn.commit(); cur.close(); conn.close()
    log_system_event('archive', f'Archived {affected} transactions older than {cutoff}',
                     performed_by='admin', details={'cutoff': cutoff.isoformat(), 'count': affected})
    return jsonify({'archived': affected, 'cutoff': cutoff.isoformat()})

@app.route('/api/admin/unarchive', methods=['POST'])
@login_required
def unarchive_transactions():
    """Restore archived transactions."""
    conn = get_db(); cur = conn.cursor()
    cur.execute("UPDATE inventory_transactions SET archived = FALSE WHERE archived = TRUE")
    affected = cur.rowcount
    conn.commit(); cur.close(); conn.close()
    log_system_event('archive', f'Unarchived all {affected} transactions', performed_by='admin')
    return jsonify({'unarchived': affected})

@app.route('/api/admin/archive-stats', methods=['GET'])
@login_required
def archive_stats():
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        SELECT
            COUNT(*) FILTER (WHERE archived IS NOT TRUE) AS active,
            COUNT(*) FILTER (WHERE archived = TRUE)      AS archived,
            MIN(COALESCE(appt_date, DATE(created_at))) FILTER (WHERE archived IS NOT TRUE
                AND transaction_type='usage_sync')       AS oldest_active,
            MAX(COALESCE(appt_date, DATE(created_at))) FILTER (WHERE archived = TRUE
                AND transaction_type='usage_sync')       AS newest_archived
        FROM inventory_transactions WHERE transaction_type='usage_sync'
    """)
    r = dict(cur.fetchone()); cur.close(); conn.close()
    if r.get('oldest_active'): r['oldest_active'] = r['oldest_active'].isoformat()
    if r.get('newest_archived'): r['newest_archived'] = r['newest_archived'].isoformat()
    return jsonify(r)


# ─── SERVICE TYPE SYNC ───────────────────────────────────────────────────────
@app.route('/api/sync/service-types', methods=['POST'])
@login_required
def sync_service_types():
    """
    Pull service type names from FR for every type_id we've seen in
    fr_appointments_raw, then store in the service_types lookup table.
    Only fetches IDs we don't already have (or all if force=true).
    """
    force = (request.json or {}).get('force', False) if request.is_json else False
    if not FR_API_KEY or not FR_AUTH_KEY:
        return jsonify({'error': 'FR API keys not configured'}), 400

    conn = get_db(); cur = conn.cursor()
    try:
        # Get distinct type IDs from our appointment data
        cur.execute("""
            SELECT DISTINCT service_type_id
            FROM fr_appointments_raw
            WHERE service_type_id IS NOT NULL AND service_type_id != ''
        """)
        all_ids = [r['service_type_id'] for r in cur.fetchall()]

        if not force:
            # Only fetch ones we don't have yet
            cur.execute("SELECT type_id FROM service_types")
            known = {r['type_id'] for r in cur.fetchall()}
            fetch_ids = [i for i in all_ids if i not in known]
        else:
            fetch_ids = all_ids

        if not fetch_ids:
            cur.close(); conn.close()
            return jsonify({'synced': 0, 'total_known': len(all_ids), 'message': 'All types already cached'})

        # Fetch from FR in chunks of 100
        upserted = 0
        for i in range(0, len(fetch_ids), 100):
            chunk = [int(x) for x in fetch_ids[i:i+100] if str(x).isdigit()]
            if not chunk:
                continue
            resp = requests.post(f'{FR_BASE_URL}/api/serviceType/get',
                json={'authenticationKey': FR_API_KEY, 'authenticationToken': FR_AUTH_KEY, 'typeIDs': chunk}, timeout=30)
            data = resp.json()
            svc_types = data.get('serviceTypes', {})
            if isinstance(svc_types, list):
                svc_types = {str(s.get('typeID','')): s for s in svc_types}
            for type_id, svc in svc_types.items():
                desc     = str(svc.get('description') or '').strip() or None
                category = str(svc.get('category') or '').strip() or None
                cur.execute("""
                    INSERT INTO service_types (type_id, description, category, synced_at)
                    VALUES (%s, %s, %s, NOW())
                    ON CONFLICT (type_id) DO UPDATE SET
                        description=EXCLUDED.description,
                        category=EXCLUDED.category,
                        synced_at=NOW()
                """, (str(type_id), desc, category))
                upserted += 1
        conn.commit()
        return jsonify({'synced': upserted, 'total_known': len(all_ids)})
    except Exception as e:
        try: conn.rollback()
        except: pass
        return jsonify({'error': str(e)}), 500
    finally:
        cur.close(); conn.close()


# ─── COST BY SERVICE TYPE REPORT ─────────────────────────────────────────────
@app.route('/api/reports/cost-by-service-type', methods=['GET'])
@login_required
def report_cost_by_service_type():
    """
    Chemical cost grouped by FR service type and category, over a date range.
    Aggregates at the appointment level first, then by service type.
    """
    date_from = request.args.get('date_from', (date.today() - timedelta(days=30)).isoformat())
    date_to   = request.args.get('date_to', date.today().isoformat())
    conn = get_db(); cur = conn.cursor()
    try:
        # Step 1: cost per appointment
        cur.execute("""
            WITH appt_costs AS (
                SELECT
                    it.fr_appointment_id,
                    a.service_type_id,
                    SUM(it.quantity_storage_units * COALESCE(p.cost_per_storage_unit, 0)) AS appt_cost,
                    COUNT(DISTINCT it.product_id) AS product_count
                FROM inventory_transactions it
                JOIN fr_appointments_raw a ON it.fr_appointment_id = a.appointment_id
                JOIN products p ON it.product_id = p.id
                WHERE it.transaction_type = 'usage_sync'
                  AND COALESCE(it.appt_date, DATE(it.created_at)) BETWEEN %s AND %s
                  AND a.service_type_id IS NOT NULL
                GROUP BY it.fr_appointment_id, a.service_type_id
            )
            SELECT
                ac.service_type_id,
                COALESCE(st.description, 'Type ' || ac.service_type_id) AS service_type,
                COALESCE(NULLIF(st.category,''), 'Other')               AS category,
                COUNT(*)                   AS appointment_count,
                SUM(ac.appt_cost)          AS total_cost,
                AVG(ac.appt_cost)          AS avg_cost_per_appt,
                MIN(ac.appt_cost)          AS min_cost,
                MAX(ac.appt_cost)          AS max_cost,
                AVG(ac.product_count)      AS avg_products_per_appt
            FROM appt_costs ac
            LEFT JOIN service_types st ON ac.service_type_id = st.type_id
            GROUP BY ac.service_type_id, st.description, st.category
            ORDER BY total_cost DESC
        """, (date_from, date_to))
        rows = [dict(r) for r in cur.fetchall()]

        # Also count appointments with NO chemical usage (informational)
        cur.execute("""
            SELECT COUNT(DISTINCT a.appointment_id) AS no_chem_count
            FROM fr_appointments_raw a
            WHERE a.appt_date BETWEEN %s AND %s
              AND a.status = '1'
              AND NOT EXISTS (
                SELECT 1 FROM inventory_transactions it
                WHERE it.fr_appointment_id = a.appointment_id
                  AND it.transaction_type = 'usage_sync'
              )
        """, (date_from, date_to))
        no_chem = cur.fetchone()

        for r in rows:
            r['total_cost']           = float(r['total_cost'] or 0)
            r['avg_cost_per_appt']    = round(float(r['avg_cost_per_appt'] or 0), 2)
            r['min_cost']             = round(float(r['min_cost'] or 0), 2)
            r['max_cost']             = round(float(r['max_cost'] or 0), 2)
            r['avg_products_per_appt']= round(float(r['avg_products_per_appt'] or 0), 1)
            r['appointment_count']    = int(r['appointment_count'])

        grand_total = sum(r['total_cost'] for r in rows)
        grand_appts = sum(r['appointment_count'] for r in rows)
        for r in rows:
            r['pct_of_total'] = round(r['total_cost'] / grand_total * 100, 1) if grand_total else 0

        return jsonify({
            'rows': rows,
            'grand_total': round(grand_total, 2),
            'grand_appts': grand_appts,
            'no_chem_appts': int(no_chem['no_chem_count']) if no_chem else 0,
            'date_from': date_from,
            'date_to': date_to,
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        cur.close(); conn.close()


# ─── HOURLY CRON ENDPOINT ────────────────────────────────────────────────────
@app.route('/api/sync/hourly', methods=['POST', 'GET'])
def hourly_sync():
    """
    Daily 8pm cron endpoint. Runs the full pipeline in background:
    1. Pull new appointments from FR (incremental)
    2. Pull new chemical uses from FR (incremental)
    3. Fill any missing appointments
    4. Process today's records into inventory deductions
    """
    token = request.headers.get('X-Sync-Token') or request.args.get('token', '')
    if not token and request.is_json:
        token = (request.json or {}).get('token', '')
    if token != os.environ.get('SYNC_TOKEN', ''):
        return jsonify({'error': 'Unauthorized - check SYNC_TOKEN variable'}), 401

    # Use Central time (CDT = UTC-5)
    local_now = datetime.utcnow() - timedelta(hours=5)
    today = local_now.strftime('%Y-%m-%d')
    yesterday = (local_now - timedelta(days=1)).strftime('%Y-%m-%d')

    def _daily_pipeline():
        try:
            log_system_event('sync', f'Nightly auto-sync started for {today}', performed_by='cron')
            # Step 1: Pull ALL new appointments since last cursor
            sync_appointments_to_postgres()
            # Step 2: Pull ALL new chemical uses since last cursor + refresh recent
            sync_chemical_uses_to_postgres()
            # Step 3: Fetch any appointments referenced in chemical uses but not in our table
            _do_fill_missing()
            # Step 4: Re-process YESTERDAY with a clean slate.
            # By 8pm the following day all techs have logged their FR usage —
            # this guarantees yesterday is always 100% correct regardless of
            # whether techs logged late the previous night.
            yesterday_result = _reprocess_date_internal(yesterday)
            # Step 5: Process TODAY for current-day visibility (best effort —
            # some techs may still be in the field, cron will catch tomorrow)
            result = process_inventory_from_stored_data(today)
            total_deducted = result.get('usage_deducted', 0) + yesterday_result.get('usage_deducted', 0)
            _bg_set_status('daily_cron', {
                'status': 'done',
                'date': today,
                'yesterday': yesterday,
                'usage_deducted_today': result.get('usage_deducted', 0),
                'usage_deducted_yesterday': yesterday_result.get('usage_deducted', 0),
                'usage_deducted': total_deducted,
                'skipped_no_tech': result.get('skipped_no_tech', 0),
                'finished': datetime.utcnow().isoformat()
            })
            log_system_event('sync', f'Nightly auto-sync complete — {total_deducted} usage records processed', performed_by='cron')
        except Exception as e:
            _bg_set_status('daily_cron', {'status': 'error', 'error': str(e), 'date': today})
            log_system_event('error', f'Nightly auto-sync failed: {str(e)}', performed_by='cron')

    # Guard: refuse to start if a cron run is already in progress
    current_status = _bg_get_status('daily_cron')
    if current_status.get('status') == 'running':
        started_at = current_status.get('started', 'unknown')
        return jsonify({
            'status': 'already_running',
            'started': started_at,
            'message': 'Cron already running — duplicate trigger ignored. This prevents double-deductions.'
        }), 409

    _bg_set_status('daily_cron', {'status': 'running', 'date': today, 'started': datetime.utcnow().isoformat()})
    import threading
    threading.Thread(target=_daily_pipeline, daemon=True).start()
    return jsonify({'status': 'started', 'date': today, 'message': 'Daily pipeline running. Check /api/sync/jobs for status.'})

# ─── RAW DATA INSPECTION ENDPOINTS ────────────────────────────────────────────

@app.route('/api/raw/appointments', methods=['GET'])
@login_required
def get_raw_appointments():
    """View stored FR appointments for a date — for troubleshooting."""
    target_date = request.args.get('date', (datetime.utcnow() - timedelta(days=1)).strftime('%Y-%m-%d'))
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT appointment_id, customer_id, status, status_text,
               appt_date, tech_fr_id, serviced_by, completed_by, assigned_tech
        FROM fr_appointments_raw
        WHERE appt_date = %s OR scheduled_date = %s
        ORDER BY appointment_id
        LIMIT 200
    """, (target_date, target_date))
    rows = [dict(r) for r in cur.fetchall()]
    cur.close(); conn.close()
    return jsonify({'date': target_date, 'count': len(rows), 'appointments': rows})

def _do_fill_missing():
    """Standalone fill-missing logic — called by cron and endpoint."""
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT DISTINCT cu.appointment_id
            FROM fr_chemical_uses_raw cu
            LEFT JOIN fr_appointments_raw a ON cu.appointment_id = a.appointment_id
            WHERE a.appointment_id IS NULL
              AND cu.appointment_id IS NOT NULL
              AND cu.appointment_id != '0'
              AND cu.appointment_id != ''
        """)
        missing_ids = [r['appointment_id'] for r in cur.fetchall()]
        if not missing_ids:
            _bg_set_status('fill_missing', {'status': 'done', 'message': 'No missing', 'fetched': 0})
            return

        int_ids = [int(i) for i in missing_ids if i and i.isdigit()]
        stored = 0
        try:
            # Fetch all missing appointments at once — _fr_fetch_details chunks at 1000
            # (was chunking at 100 = 10× more API calls than needed)
            appts = _fr_fetch_details('appointment/get', 'appointmentIDs', 'appointments', int_ids)
            stored = _store_appointments(cur, appts)
            conn.commit()
        except Exception:
            conn.rollback()

        _bg_set_status('fill_missing', {
            'status': 'done', 'missing_found': len(missing_ids),
            'fetched': stored, 'finished': datetime.utcnow().isoformat()
        })
    except Exception as e:
        _bg_set_status('fill_missing', {'status': 'error', 'error': str(e)})
        try: conn.rollback()
        except: pass
    finally:
        cur.close(); conn.close()


@app.route('/api/sync/fill-missing-appointments', methods=['POST'])
@login_required
def fill_missing_appointments():
    """
    Find chemical use records that reference appointments not in our table,
    fetch and store those appointments directly by ID (background job).
    """
    _bg_set_status('fill_missing', {'status': 'running', 'started': datetime.utcnow().isoformat()})
    import threading
    threading.Thread(target=lambda: _do_fill_missing(), daemon=True).start()
    return jsonify({'status': 'started', 'message': 'Fetching missing appointments in background. Check /api/sync/jobs'})


@app.route('/api/raw/chemical-uses-by-appointment/<appt_id>', methods=['GET'])
@login_required
def get_chemical_uses_by_appointment(appt_id):
    """Get all chemical use records for a specific appointment ID."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT cu.*, a.tech_fr_id, a.appt_date, a.status_text
        FROM fr_chemical_uses_raw cu
        LEFT JOIN fr_appointments_raw a ON cu.appointment_id = a.appointment_id
        WHERE cu.appointment_id = %s
    """, (appt_id,))
    rows = [dict(r) for r in cur.fetchall()]
    cur.close(); conn.close()
    return jsonify({'appointment_id': appt_id, 'count': len(rows), 'records': rows})

@app.route('/api/raw/chemical-use/<cu_id>', methods=['GET'])
@login_required
def get_raw_chemical_use_detail(cu_id):
    """Get the full raw JSON for a specific chemical use record."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT raw_json FROM fr_chemical_uses_raw WHERE chemical_use_id=%s", (cu_id,))
    row = cur.fetchone()
    cur.close(); conn.close()
    if not row:
        return jsonify({'error': 'Not found'}), 404
    return jsonify(json.loads(row['raw_json']))

@app.route('/api/raw/chemical-uses', methods=['GET'])
@login_required
def get_raw_chemical_uses():
    """View stored FR chemical use records for a date — for troubleshooting."""
    target_date = request.args.get('date', (datetime.utcnow() - timedelta(days=1)).strftime('%Y-%m-%d'))
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT cu.chemical_use_id, cu.appointment_id, cu.customer_id,
               cu.chemical_id, cu.resolved_amount, cu.resolved_unit,
               cu.inventory_updated,
               a.tech_fr_id, a.appt_date, a.status_text,
               p.name AS product_name
        FROM fr_chemical_uses_raw cu
        LEFT JOIN fr_appointments_raw a ON cu.appointment_id = a.appointment_id
        LEFT JOIN products p ON p.fr_product_id = cu.chemical_id AND p.active = TRUE
        WHERE cu.date_created = %s
        ORDER BY cu.chemical_use_id
        LIMIT 500
    """, (target_date,))
    rows = [dict(r) for r in cur.fetchall()]
    cur.close(); conn.close()
    return jsonify({'date': target_date, 'count': len(rows), 'chemical_uses': rows})

# Background job tracker — stored in DB so all workers can see it
def _bg_set_status(key, data):
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO sync_state (key, value, updated_at)
            VALUES (%s, %s, NOW())
            ON CONFLICT (key) DO UPDATE SET value=%s, updated_at=NOW()
        """, (f'job_{key}', json.dumps(data), json.dumps(data)))
        conn.commit()
    except Exception:
        conn.rollback()
    finally:
        cur.close(); conn.close()

def _bg_get_status(key):
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("SELECT value FROM sync_state WHERE key=%s", (f'job_{key}',))
        row = cur.fetchone()
        return json.loads(row['value']) if row else None
    finally:
        cur.close(); conn.close()

def _bg_run(key, fn):
    _bg_set_status(key, {'status': 'running', 'started': datetime.utcnow().isoformat()})
    def _run():
        try:
            r = fn()
            _bg_set_status(key, {'status': 'done', 'result': r, 'finished': datetime.utcnow().isoformat()})
        except Exception as e:
            _bg_set_status(key, {'status': 'error', 'error': str(e), 'finished': datetime.utcnow().isoformat()})
    import threading
    threading.Thread(target=_run, daemon=True).start()

@app.route('/api/sync/pull-appointments', methods=['POST'])
@login_required
def pull_appointments_endpoint():
    current = _bg_get_status('appointments')
    if current and current.get('status') == 'running':
        return jsonify({'status': 'already_running', 'started': current.get('started')})
    log_system_event('sync', 'Manual appointment sync started', performed_by=session.get('username'))
    _bg_run('appointments', sync_appointments_to_postgres)
    return jsonify({'status': 'started', 'message': 'Running in background. Poll /api/sync/jobs to check progress.'})

@app.route('/api/sync/pull-chemical-uses', methods=['POST'])
@login_required
def pull_chemical_uses_endpoint():
    current = _bg_get_status('chemical_uses')
    if current and current.get('status') == 'running':
        return jsonify({'status': 'already_running', 'started': current.get('started')})
    log_system_event('sync', 'Manual chemical use sync started', performed_by=session.get('username'))
    _bg_run('chemical_uses', sync_chemical_uses_to_postgres)
    return jsonify({'status': 'started', 'message': 'Running in background. Poll /api/sync/jobs to check progress.'})

@app.route('/api/sync/pull-only', methods=['POST'])
@login_required
def pull_only():
    _bg_run('appointments', sync_appointments_to_postgres)
    _bg_run('chemical_uses', sync_chemical_uses_to_postgres)
    return jsonify({'status': 'started', 'message': 'Both syncs running in background. Poll /api/sync/jobs.'})

@app.route('/api/sync/jobs', methods=['GET'])
@login_required
def bg_sync_status():
    return jsonify({
        'sync_now': _bg_get_status('sync_now'),
        'appointments': _bg_get_status('appointments'),
        'chemical_uses': _bg_get_status('chemical_uses'),
        'fill_missing': _bg_get_status('fill_missing'),
        'daily_cron': _bg_get_status('daily_cron'),
    })

@app.route('/api/sync/process-only', methods=['POST'])
@login_required
def process_only():
    """Process already-stored raw data into inventory deductions."""
    d = request.json or {}
    target_date = d.get('date', (datetime.utcnow() - timedelta(days=1)).strftime('%Y-%m-%d'))
    result = process_inventory_from_stored_data(target_date)
    log_system_event('sync', f'Manual process for {target_date} — {result.get("usage_deducted", 0)} records', performed_by=session.get('username'))
    return jsonify(result)

def _reprocess_date_internal(target_date):
    """
    Internal reprocess: reverse existing deductions, reset flags, re-process.
    Used by both the HTTP endpoint and the nightly cron.
    Returns dict with reversed_count + process results.
    """
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT id, technician_id, product_id, quantity_storage_units
            FROM inventory_transactions
            WHERE transaction_type = 'usage_sync' AND appt_date = %s
        """, (target_date,))
        existing = cur.fetchall()
        reversed_count = 0
        for txn in existing:
            cur.execute("""
                UPDATE tech_inventory
                SET quantity_storage_units = quantity_storage_units + %s,
                    last_updated = NOW()
                WHERE technician_id = %s AND product_id = %s
            """, (txn['quantity_storage_units'], txn['technician_id'], txn['product_id']))
            reversed_count += 1
        cur.execute("""
            DELETE FROM inventory_transactions
            WHERE transaction_type = 'usage_sync' AND appt_date = %s
        """, (target_date,))
        cur.execute("""
            UPDATE fr_chemical_uses_raw cu
            SET inventory_updated = FALSE
            FROM fr_appointments_raw a
            WHERE cu.appointment_id = a.appointment_id
              AND a.appt_date = %s
        """, (target_date,))
        conn.commit()
        cur.close(); conn.close()
    except Exception as e:
        try: conn.rollback()
        except: pass
        cur.close(); conn.close()
        return {'error': str(e), 'reversed_transactions': 0}

    result = process_inventory_from_stored_data(target_date)
    result['reversed_transactions'] = reversed_count
    return result


@app.route('/api/sync/reprocess', methods=['POST'])
@login_required
def reprocess_date():
    """
    Safely re-process a date via HTTP — reverse deductions, reset flags, re-run.
    """
    d = request.json or {}
    target_date = d.get('date')
    if not target_date:
        return jsonify({'error': 'date required'}), 400
    result = _reprocess_date_internal(target_date)
    return jsonify(result)



# ─── ADMIN SEED ENDPOINTS ─────────────────────────────────────────────────────

@app.route('/api/admin/wipe', methods=['POST'])
@login_required
def wipe_data():
    conn = get_db(); cur = conn.cursor()
    try:
        cur.execute('TRUNCATE TABLE inventory_transactions, audit_log, fr_sync_log, tech_inventory, warehouse_inventory, product_thresholds, technicians, products RESTART IDENTITY CASCADE')
        conn.commit(); cur.close(); conn.close()
        return jsonify({'ok': True, 'message': 'All data wiped'})
    except Exception as e:
        conn.rollback(); cur.close(); conn.close()
        return jsonify({'error': str(e)}), 500

@app.route('/api/admin/seed-products', methods=['POST'])
@login_required
def seed_products():
    PRODUCTS = [
        ('Advion Insect Granular Bait',    None,'Bottle',  'oz',     16.0,'1 Bottle = 16 oz'),
        ('Advion Microflow',               None,'Jar',     'oz',      8.0,'1 Jar = 8 oz'),
        ('Bedlam Plus',                    None,'Can',     'oz',     17.0,'1 Can = 17 oz'),
        ('Bifen I/T',                      None,'Gallons', 'fl oz', 128.0,'1 Gallon = 128 fl oz'),
        ('Bifen L/P Granules',             None,'Bag',     'lbs',    25.0,'1 Bag = 25 lbs'),
        ('Bird-Out Aromatic Bird Repellent',None,'Canister','units',  1.0,'1 Canister = 1 unit'),
        ('Catchmaster 72 (TC) Glueboards', None,'Box',     'each',   72.0,'1 Box = 72 boards'),
        ('Catchmaster 72 TC3 Glueboard',   None,'units',   'units',   1.0,'Tracked per unit'),
        ('Catchmaster 72MB Glueboard',     None,'units',   'units',   1.0,'Tracked per unit'),
        ('Catchmaster 909 Glueboard',      None,'units',   'units',   1.0,'Tracked per unit'),
        ('Contrac RTU Place Packs',        None,'Case',    'units',  86.0,'1 Case = 86 packs'),
        ('Crossfire Insecticide',          None,'Bottle',  'fl oz',  13.0,'1 Bottle = 13 fl oz'),
        ('D-Fense Dust',                   None,'Bottle',  'lbs',     1.0,'1 Bottle = 1 lb'),
        ('Exciter',                        None,'Bottle',  'fl oz',  16.0,'1 Bottle = 16 fl oz'),
        ('FINAL All-Weather BLOX',         None,'Pail',    'units', 400.0,'1 Pail = 400 blox'),
        ('Gentrol IGR',                    None,'Bottle',  'fl oz',  16.0,'1 Bottle = 16 fl oz'),
        ('Maxforce Granular Fly Bait',     None,'Pounds',  'lbs',     1.0,'Tracked by pound'),
        ('Nibor-D Insecticide',            None,'Pail',    'lbs',     5.0,'1 Pail = 5 lbs'),
        ('Nibor-D Insecticide Foam + IGR', None,'Can',     'oz',     21.0,'1 Can = 21 oz'),
        ('Nuvan Prostrips',                None,'Case',    'units',  12.0,'1 Case = 12 strips'),
        ('Nyguard Plus Flea & Tick PS',    None,'Can',     'oz',     17.0,'1 Can = 17 oz'),
        ('Onslaught Fastcap Spider & SCO', None,'Gallons', 'fl oz', 128.0,'1 Gallon = 128 fl oz'),
        ('PCQ Pro Bait',                   None,'Pail',    'oz',    192.0,'1 Pail = 192 oz'),
        ('PT Alpine Flea & Bed Bug',       None,'Can',     'oz',     14.0,'1 Can = 14 oz'),
        ('PT Alpine Fly Bait',             None,'Can',     'oz',     16.0,'1 Can = 16 oz'),
        ('Precor 2625 Spray',              None,'Can',     'oz',     21.0,'1 Can = 21 oz'),
        ('Precor IGR Concentrate',         None,'Bottle',  'oz',     16.0,'1 Bottle = 16 oz'),
        ('Pro Zap Insect Guard',           None,'Case',    'units',  12.0,'1 Case = 12 units'),
        ('Profoam Platinum',               None,'Bottle',  'fl oz', 128.0,'1 Bottle = 128 fl oz'),
        ('Ridesco WG',                     None,'Bottle',  'g',     190.0,'1 Bottle = 190g'),
        ('Rodent Bait Station',            None,'units',   'units',   1.0,'Tracked per unit'),
        ('Shockwave 1',                    None,'Can',     'oz',     17.0,'1 Can = 17 oz'),
        ('Snake A Way',                    None,'Pail',    'lbs',    28.0,'1 Pail = 28 lbs'),
        ('Stryker 54',                     None,'Can',     'oz',     15.0,'1 Can = 15 oz'),
        ('Sumari',                         None,'Jug',     'oz',     32.0,'1 Jug = 32 oz'),
        ('Sumari ant gel bait',            None,'Tube',    'g',      30.0,'1 Tube = 30g'),
        ('Surekill Total Release Aerosol', None,'Can',     'oz',      6.0,'1 Can = 6 oz'),
        ('Suspend Polyzone',               None,'Gallons', 'fl oz', 128.0,'1 Gallon = 128 fl oz'),
        ('T1 Mouse Bait Station',          None,'units',   'units',   1.0,'Tracked per unit'),
        ('Talon Weatherblok XT',           None,'Pail',    'units', 365.0,'1 Pail = 365 blox'),
        ('Talprid Mole Bait',              None,'Box',     'units',  20.0,'1 Box = 20 worms'),
        ('Taurus SC',                      None,'Gallons', 'fl oz', 128.0,'1 Gallon = 128 fl oz'),
        ('Temprid Ready Spray',            None,'Can',     'oz',     15.0,'1 Can = 15 oz'),
        ('Termidor HE',                    None,'Bottle',  'fl oz',  79.0,'1 Bottle = 79 fl oz'),
        ('Trelona ATBS Bait Cartridges',   None,'Canister','units',   1.0,'Tracked per canister'),
        ('Trelona Bait Station',           None,'units',   'units',   1.0,'Tracked per unit'),
        ('Vanecto Cockroach Gel Bait',     None,'Tube',    'g',      30.0,'1 Tube = 30g'),
        ('Vendetta plus cockroach gel ba', None,'Tube',    'g',      30.0,'1 Tube = 30g'),
        ('Yard Guard',                     None,'Bag',     'lbs',    40.0,'1 Bag = 40 lbs'),
    ]
    WAREHOUSE = {
        'Advion Insect Granular Bait':4.0,'Advion Microflow':4.25,'Bedlam Plus':10.0,
        'Bifen I/T':4.13,'Bifen L/P Granules':13.0,'Bird-Out Aromatic Bird Repellent':36.0,
        'Catchmaster 72 (TC) Glueboards':5.0,'Catchmaster 72 TC3 Glueboard':504.0,
        'Catchmaster 72MB Glueboard':99.0,'Catchmaster 909 Glueboard':228.0,
        'Contrac RTU Place Packs':0.22,'Crossfire Insecticide':8.85,'D-Fense Dust':15.0,
        'Exciter':5.0,'FINAL All-Weather BLOX':2.0,'Gentrol IGR':3.0,
        'Maxforce Granular Fly Bait':5.0,'Nibor-D Insecticide':2.0,
        'Nibor-D Insecticide Foam + IGR':10.0,'Nuvan Prostrips':10.0,
        'Nyguard Plus Flea & Tick PS':5.0,'Onslaught Fastcap Spider & SCO':4.48,
        'PCQ Pro Bait':2.67,'PT Alpine Flea & Bed Bug':2.0,'PT Alpine Fly Bait':7.0,
        'Precor 2625 Spray':8.0,'Precor IGR Concentrate':5.0,'Pro Zap Insect Guard':1.83,
        'Profoam Platinum':3.0,'Ridesco WG':5.0,'Rodent Bait Station':277.0,
        'Shockwave 1':16.0,'Snake A Way':2.0,'Stryker 54':1.0,'Sumari':1.0,
        'Sumari ant gel bait':24.0,'Surekill Total Release Aerosol':18.0,
        'Suspend Polyzone':65.42,'T1 Mouse Bait Station':88.0,'Talon Weatherblok XT':9.0,
        'Talprid Mole Bait':28.0,'Taurus SC':20.94,'Temprid Ready Spray':4.0,
        'Termidor HE':46.0,'Trelona ATBS Bait Cartridges':100.0,'Trelona Bait Station':542.0,
        'Vanecto Cockroach Gel Bait':26.0,'Vendetta plus cockroach gel ba':14.0,'Yard Guard':3.5,
    }
    conn = get_db(); cur = conn.cursor()
    try:
        added = 0
        for (name,fr_id,storage,usage,conv,note) in PRODUCTS:
            cur.execute('SELECT id FROM products WHERE name=%s',(name,))
            if cur.fetchone(): continue
            cur.execute('''INSERT INTO products (name,fr_product_id,storage_unit,usage_unit,conversion_factor,conversion_note)
                VALUES (%s,%s,%s,%s,%s,%s) RETURNING id''', (name,fr_id,storage,usage,conv,note))
            pid = cur.fetchone()['id']
            wqty = WAREHOUSE.get(name, 0)
            cur.execute('INSERT INTO warehouse_inventory (product_id,quantity_storage_units) VALUES (%s,%s)',(pid,wqty))
            added += 1
        conn.commit(); cur.close(); conn.close()
        return jsonify({'ok':True,'products_added':added})
    except Exception as e:
        conn.rollback(); cur.close(); conn.close()
        return jsonify({'error':str(e)}), 500

@app.route('/api/admin/seed-techs', methods=['POST'])
@login_required
def seed_techs():
    TECHNICIANS = [
        ('Otton Hennessey',   'Truck 14','698'),('Joseph Whitman',    'Truck 15','470'),
        ('Kenneth Munzlinger','Truck 19','739'),('Zach Ghast',        'Truck 20','299'),
        ('John Russell',      'Truck 21','732'),('Cole Valle',        'Truck 23','741'),
        ('Grace Winegardner', 'Truck 24','727'),('Kaleb Blackwell',   'Truck 27','723'),
        ('Jeff Barrett',      'Truck 32','624'),('Landon Leiweke',    'Truck 33','707'),
        ('Dillon Shadrick',   'Truck 35','662'),('Stacey Hale',       'Truck 36','644'),
        ('Drew Sandweg',      'Truck 37','742'),('Dave Stout',        'Truck 38','668'),
        ('Joe Bossart',       'Truck 39','729'),('Kyle Martin',       'Truck 40','720'),
    ]
    conn = get_db(); cur = conn.cursor()
    try:
        added = 0
        for (name,truck,fr_id) in TECHNICIANS:
            cur.execute('SELECT id FROM technicians WHERE name=%s',(name,))
            if cur.fetchone(): continue
            cur.execute('INSERT INTO technicians (name,truck_id,fr_employee_id) VALUES (%s,%s,%s)',(name,truck,fr_id))
            added += 1
        conn.commit(); cur.close(); conn.close()
        return jsonify({'ok':True,'techs_added':added})
    except Exception as e:
        conn.rollback(); cur.close(); conn.close()
        return jsonify({'error':str(e)}), 500

@app.route('/api/admin/seed-trucks', methods=['POST'])
@login_required
def seed_trucks():
    # All quantities in storage units, negatives preserved from CSV
    TRUCKS = {
        'Truck 14':{
            'Advion Insect Granular Bait':0+8/16,'Bedlam Plus':1.0,'Bifen I/T':0+20/128,
            'Bifen L/P Granules':1+5/25,'Catchmaster 72 (TC) Glueboards':2+66/72,
            'Contrac RTU Place Packs':0+23/86,'D-Fense Dust':1.0,'Exciter':0+13/16,
            'FINAL All-Weather BLOX':-1+348/400,'Gentrol IGR':0+10/16,
            'Nibor-D Insecticide Foam + IGR':10+12.25/21,'Nyguard Plus Flea & Tick PS':1.0,
            'Onslaught Fastcap Spider & SCO':0+2.29/128,'PT Alpine Flea & Bed Bug':0+7/14,
            'PT Alpine Fly Bait':0+8/16,'Precor IGR Concentrate':0+8/16,
            'Pro Zap Insect Guard':0+6/12,'Shockwave 1':0+16.7/17,'Stryker 54':1.0,
            'Suspend Polyzone':0+25.25/128,'Talon Weatherblok XT':1.0,
            'Talprid Mole Bait':1+15/20,'Taurus SC':0+46.88/128,
            'Temprid Ready Spray':1+7.9/15,'Trelona ATBS Bait Cartridges':10.0,
            'Trelona Bait Station':8.0,'Yard Guard':2+5/40,
        },
        'Truck 15':{
            'Bedlam Plus':1.0,'Bifen I/T':0+20/128,'Bifen L/P Granules':0+10/25,
            'Catchmaster 72MB Glueboard':144.0,'Catchmaster 909 Glueboard':12.0,
            'Contrac RTU Place Packs':0+5/86,'Crossfire Insecticide':1.0,'D-Fense Dust':2.0,
            'Exciter':0+7/16,'FINAL All-Weather BLOX':0+313/400,'Gentrol IGR':0+2/16,
            'Nibor-D Insecticide Foam + IGR':1.0,'Onslaught Fastcap Spider & SCO':-1+127/128,
            'Shockwave 1':1+15.85/17,'Stryker 54':0+14.8/15,'Sumari':0+4/32,
            'Suspend Polyzone':0+85/128,'T1 Mouse Bait Station':1.0,'Taurus SC':0+83/128,
            'Temprid Ready Spray':0+14.5/15,'Vanecto Cockroach Gel Bait':2.0,'Yard Guard':0+20/40,
        },
        'Truck 19':{
            'Bedlam Plus':2.0,'Bifen I/T':0+31/128,'Bifen L/P Granules':0+21/25,
            'Catchmaster 72 (TC) Glueboards':0+65/72,'Catchmaster 72 TC3 Glueboard':68.0,
            'Catchmaster 72MB Glueboard':55.0,'Crossfire Insecticide':0+6.5/13,
            'D-Fense Dust':1.0,'Exciter':1.0,'FINAL All-Weather BLOX':0+102/400,
            'Gentrol IGR':0+7.5/16,'Nibor-D Insecticide Foam + IGR':2.0,
            'Onslaught Fastcap Spider & SCO':0+8/128,'PCQ Pro Bait':0+20/192,
            'Precor 2625 Spray':1.0,'Stryker 54':1.0,'Sumari':0+28/32,
            'Suspend Polyzone':0+124.3/128,'T1 Mouse Bait Station':1.0,
            'Talprid Mole Bait':1+10/20,'Taurus SC':0+89.6/128,'Yard Guard':1+4/40,
        },
        'Truck 20':{
            'Advion Insect Granular Bait':-1+7/16,'Bifen I/T':0+6/128,
            'Bifen L/P Granules':-3+16/25,'Catchmaster 72 (TC) Glueboards':-1+69/72,
            'Catchmaster 909 Glueboard':-2.0,'FINAL All-Weather BLOX':0+85/400,
            'Gentrol IGR':-1+15/16,'Nibor-D Insecticide Foam + IGR':-1+20/21,
            'Onslaught Fastcap Spider & SCO':-1+17.5/128,'PCQ Pro Bait':0+24/192,
            'PT Alpine Flea & Bed Bug':-1+13/14,'Precor IGR Concentrate':-1+14/16,
            'Sumari':-1+21/32,'Suspend Polyzone':-1+120.37/128,
            'Talprid Mole Bait':-1+7/20,'Taurus SC':-1+98.8/128,
            'Temprid Ready Spray':-1+14/15,'Termidor HE':-6+49.75/79,
            'Trelona ATBS Bait Cartridges':-19.0,'Trelona Bait Station':-12.0,
            'Yard Guard':-1+24/40,
        },
        'Truck 21':{
            'Bifen I/T':-1+119/128,'Bifen L/P Granules':-4+23/25,
            'Catchmaster 72MB Glueboard':1.0,'Contrac RTU Place Packs':0+25/86,
            'D-Fense Dust':-1+0.76,'Exciter':0+14/16,'FINAL All-Weather BLOX':0.0,
            'Gentrol IGR':1.0,'Nibor-D Insecticide Foam + IGR':0+9.5/21,
            'Onslaught Fastcap Spider & SCO':-1+81/128,'PT Alpine Flea & Bed Bug':0+7/14,
            'PT Alpine Fly Bait':1.0,'Precor 2625 Spray':1.0,'Precor IGR Concentrate':0+8/16,
            'Surekill Total Release Aerosol':2.0,'Suspend Polyzone':0+32/128,
            'T1 Mouse Bait Station':12.0,'Talon Weatherblok XT':2.0,
            'Talprid Mole Bait':-1+15/20,'Taurus SC':0+75.8/128,
            'Temprid Ready Spray':1+13.53/15,'Vanecto Cockroach Gel Bait':1.0,
        },
        'Truck 23':{
            'Advion Insect Granular Bait':1.0,'Bedlam Plus':0+15/17,'Bifen I/T':0+31/128,
            'Bifen L/P Granules':4+5.55/25,'Catchmaster 72 TC3 Glueboard':1.0,
            'Catchmaster 72MB Glueboard':37.0,'Contrac RTU Place Packs':0+32/86,
            'Exciter':-1+10.5/16,'FINAL All-Weather BLOX':-1+59/400,
            'Nibor-D Insecticide Foam + IGR':1+20.5/21,
            'Onslaught Fastcap Spider & SCO':-1+100.4/128,'Shockwave 1':0+16.85/17,
            'Sumari':0+25/32,'Sumari ant gel bait':0+15/30,
            'Surekill Total Release Aerosol':2+4/6,'Suspend Polyzone':1+40.37/128,
            'T1 Mouse Bait Station':5.0,'Talprid Mole Bait':1+10/20,
            'Taurus SC':0+39.6/128,'Temprid Ready Spray':0+14.5/15,
            'Trelona Bait Station':1.0,'Vendetta plus cockroach gel ba':3+24/30,
            'Yard Guard':0+10.75/40,
        },
        'Truck 24':{
            'Bifen I/T':0+64/128,'Bifen L/P Granules':4+11.58/25,
            'Catchmaster 72 (TC) Glueboards':0+71/72,'Catchmaster 72 TC3 Glueboard':72.0,
            'Catchmaster 72MB Glueboard':72.0,'Crossfire Insecticide':1.0,'Exciter':1.0,
            'FINAL All-Weather BLOX':0+229/400,'Gentrol IGR':1.0,
            'Nibor-D Insecticide Foam + IGR':2.0,'Onslaught Fastcap Spider & SCO':0+121/128,
            'PT Alpine Flea & Bed Bug':1.0,'Precor 2625 Spray':1.0,
            'Precor IGR Concentrate':1.0,'Shockwave 1':0+16/17,'Stryker 54':1.0,
            'Sumari':1.0,'Sumari ant gel bait':3.0,'Surekill Total Release Aerosol':1.0,
            'Suspend Polyzone':0+72/128,'T1 Mouse Bait Station':2.0,
            'Talprid Mole Bait':4+11/20,'Taurus SC':0+66.4/128,'Temprid Ready Spray':1.0,
            'Termidor HE':1.0,'Vanecto Cockroach Gel Bait':1.0,'Yard Guard':1+1.5/40,
        },
        'Truck 27':{
            'Bifen I/T':-1+120/128,'Bifen L/P Granules':-5+13/25,
            'Contrac RTU Place Packs':-1+78/86,'FINAL All-Weather BLOX':-1+365/400,
            'Gentrol IGR':-1+14.5/16,'Onslaught Fastcap Spider & SCO':-1+123.37/128,
            'Suspend Polyzone':-2+41.75/128,'T1 Mouse Bait Station':-4.0,
            'Talprid Mole Bait':-2+12/20,'Taurus SC':-1+0.9/128,
            'Trelona ATBS Bait Cartridges':-44.0,'Trelona Bait Station':-12.0,
            'Yard Guard':-1+35/40,
        },
        'Truck 32':{
            'Advion Insect Granular Bait':1.0,'Advion Microflow':0+2/8,
            'Bedlam Plus':1+13/17,'Bifen I/T':0+29/128,'Bifen L/P Granules':1.0,
            'Contrac RTU Place Packs':0+25/86,'Exciter':0+15.5/16,
            'FINAL All-Weather BLOX':0+200/400,'Gentrol IGR':0+11/16,
            'Nibor-D Insecticide Foam + IGR':1.0,'Nyguard Plus Flea & Tick PS':2.0,
            'Onslaught Fastcap Spider & SCO':0+16/128,'PT Alpine Flea & Bed Bug':3+10.75/14,
            'PT Alpine Fly Bait':1.0,'Precor 2625 Spray':1.0,'Precor IGR Concentrate':0+8/16,
            'Ridesco WG':-1+171/190,'Shockwave 1':0+16.75/17,'Stryker 54':2.0,
            'Sumari':0+16/32,'Sumari ant gel bait':2.0,'Surekill Total Release Aerosol':4.0,
            'Suspend Polyzone':0+32/128,'T1 Mouse Bait Station':5.0,
            'Talon Weatherblok XT':0+150/365,'Taurus SC':0+78/128,
            'Temprid Ready Spray':4+13/15,'Trelona ATBS Bait Cartridges':25.0,
            'Yard Guard':0+30/40,
        },
        'Truck 33':{
            'Advion Insect Granular Bait':-1+6/16,'Bedlam Plus':0+8/17,
            'Bifen I/T':0+117/128,'Bifen L/P Granules':-1+8/25,
            'Bird-Out Aromatic Bird Repellent':4.0,'Catchmaster 72 (TC) Glueboards':0+20/72,
            'Contrac RTU Place Packs':0+22/86,'Crossfire Insecticide':1.0,'Exciter':0+8/16,
            'FINAL All-Weather BLOX':0+39/400,'Gentrol IGR':0+4/16,
            'Nibor-D Insecticide Foam + IGR':2.0,'Onslaught Fastcap Spider & SCO':-1+105.4/128,
            'PT Alpine Flea & Bed Bug':1.0,'Precor 2625 Spray':0+20.5/21,
            'Pro Zap Insect Guard':0+6/12,'Ridesco WG':1.0,'Shockwave 1':3+2.37/17,
            'Snake A Way':-1+18/28,'Sumari':-2+13/32,'Sumari ant gel bait':4.0,
            'Surekill Total Release Aerosol':1+5/6,'Suspend Polyzone':1+90.05/128,
            'T1 Mouse Bait Station':12.0,'Talon Weatherblok XT':1.0,
            'Talprid Mole Bait':5+4/20,'Taurus SC':-1+120/128,'Temprid Ready Spray':1.0,
            'Vanecto Cockroach Gel Bait':2.0,'Vendetta plus cockroach gel ba':1.0,
            'Yard Guard':-1+27/40,
        },
        'Truck 35':{
            'Bedlam Plus':-2+9/17,'Bifen I/T':1+32/128,'Bifen L/P Granules':4+4.08/25,
            'Catchmaster 72 (TC) Glueboards':0+25/72,'Catchmaster 72 TC3 Glueboard':25.0,
            'Contrac RTU Place Packs':0+24/86,'Crossfire Insecticide':2.0,'D-Fense Dust':4.0,
            'Exciter':0+5/16,'FINAL All-Weather BLOX':0+84/400,'Gentrol IGR':1+9/16,
            'Nibor-D Insecticide Foam + IGR':2+9.9/21,'Nyguard Plus Flea & Tick PS':0+9/17,
            'Onslaught Fastcap Spider & SCO':0+70/128,'PT Alpine Flea & Bed Bug':1+12.4/14,
            'PT Alpine Fly Bait':1+3.75/16,'Precor 2625 Spray':2+7/21,
            'Precor IGR Concentrate':0+8/16,'Pro Zap Insect Guard':1.0,
            'Ridesco WG':-1+152/190,'Shockwave 1':0+16.65/17,'Snake A Way':0+13/28,
            'Stryker 54':0+14.6/15,'Sumari':0+9/32,'Sumari ant gel bait':5.0,
            'Surekill Total Release Aerosol':2.0,'Suspend Polyzone':1+39.25/128,
            'T1 Mouse Bait Station':4.0,'Talon Weatherblok XT':1.0,'Talprid Mole Bait':4+5/20,
            'Taurus SC':0+104.1/128,'Temprid Ready Spray':2+12.5/15,
            'Trelona ATBS Bait Cartridges':15.0,'Trelona Bait Station':3.0,
            'Vanecto Cockroach Gel Bait':2.0,'Vendetta plus cockroach gel ba':3+15/30,
            'Yard Guard':-1+37/40,
        },
        'Truck 36':{
            'Advion Insect Granular Bait':-2+0/16,'Bedlam Plus':0+12/17,'Bifen I/T':1.0,
            'Bifen L/P Granules':-1+15.9/25,'Catchmaster 72 (TC) Glueboards':0+31/72,
            'Catchmaster 72 TC3 Glueboard':4.0,'Catchmaster 72MB Glueboard':148.0,
            'Catchmaster 909 Glueboard':-2.0,'Contrac RTU Place Packs':0+30/86,
            'Exciter':0+6/16,'FINAL All-Weather BLOX':0+188/400,'Gentrol IGR':0+11.5/16,
            'Nibor-D Insecticide Foam + IGR':2+19/21,'Nyguard Plus Flea & Tick PS':-1+5/17,
            'Onslaught Fastcap Spider & SCO':-1+68/128,'PCQ Pro Bait':-1+191/192,
            'PT Alpine Flea & Bed Bug':1.0,'PT Alpine Fly Bait':1.0,'Precor 2625 Spray':1.0,
            'Shockwave 1':1.0,'Stryker 54':0+14.8/15,'Sumari':0+13/32,
            'Sumari ant gel bait':7+15/30,'Surekill Total Release Aerosol':2+3/6,
            'Suspend Polyzone':0+99/128,'T1 Mouse Bait Station':10.0,
            'Talon Weatherblok XT':0+45/365,'Talprid Mole Bait':6+7/20,
            'Taurus SC':0+76/128,'Temprid Ready Spray':1+12.4/15,
            'Trelona ATBS Bait Cartridges':11.0,'Yard Guard':0+34/40,
        },
        'Truck 37':{
            'Bifen I/T':-1+109.4/128,'Bifen L/P Granules':1+21.4/25,
            'Catchmaster 72 (TC) Glueboards':1.0,'Catchmaster 72 TC3 Glueboard':72.0,
            'Catchmaster 72MB Glueboard':72.0,'D-Fense Dust':2.0,'Exciter':1.0,
            'FINAL All-Weather BLOX':0+137/400,'Onslaught Fastcap Spider & SCO':1+6.5/128,
            'Shockwave 1':-1+16.95/17,'Sumari ant gel bait':2.0,
            'Surekill Total Release Aerosol':1.0,'Suspend Polyzone':0+93/128,
            'T1 Mouse Bait Station':24.0,'Talprid Mole Bait':7+8/20,'Taurus SC':0+53.6/128,
            'Temprid Ready Spray':0+7.95/15,'Termidor HE':0+50/79,'Yard Guard':0+9/40,
        },
        'Truck 38':{
            'Advion Insect Granular Bait':1.0,'Bifen I/T':1.0,'Bifen L/P Granules':0+19.5/25,
            'Catchmaster 72 (TC) Glueboards':2+10/72,'Catchmaster 72 TC3 Glueboard':119.0,
            'Catchmaster 72MB Glueboard':14.0,'Catchmaster 909 Glueboard':-13.0,
            'Contrac RTU Place Packs':0+20/86,'Exciter':2.0,
            'FINAL All-Weather BLOX':0+138.75/400,'Gentrol IGR':-1+10.9/16,
            'Nibor-D Insecticide':-11+4/5,'Nibor-D Insecticide Foam + IGR':5+15.15/21,
            'Nuvan Prostrips':1+9/12,'Nyguard Plus Flea & Tick PS':1.0,
            'Onslaught Fastcap Spider & SCO':-1+75.2/128,'Precor 2625 Spray':0+20.5/21,
            'Precor IGR Concentrate':1.0,'Pro Zap Insect Guard':4+3/12,'Shockwave 1':2+7.65/17,
            'Stryker 54':1.0,'Sumari':1+8/32,'Sumari ant gel bait':2.0,
            'Surekill Total Release Aerosol':2.0,'Suspend Polyzone':0+70.75/128,
            'T1 Mouse Bait Station':1.0,'Talon Weatherblok XT':0+150/365,
            'Talprid Mole Bait':2+19/20,'Taurus SC':0+101.72/128,'Temprid Ready Spray':2.0,
            'Vanecto Cockroach Gel Bait':4.0,'Vendetta plus cockroach gel ba':2.0,
            'Yard Guard':0+30/40,
        },
        'Truck 39':{
            'Advion Insect Granular Bait':0+11.8/16,'Bedlam Plus':0+8/17,
            'Bifen I/T':0+28/128,'Bifen L/P Granules':3+15.78/25,
            'Catchmaster 72 TC3 Glueboard':72.0,'Catchmaster 72MB Glueboard':72.0,
            'Catchmaster 909 Glueboard':-4.0,'Contrac RTU Place Packs':0+11/86,
            'Crossfire Insecticide':0+12/13,'Exciter':0+12/16,
            'FINAL All-Weather BLOX':0+142/400,'Gentrol IGR':0+12/16,
            'Nibor-D Insecticide Foam + IGR':4+8.75/21,'Onslaught Fastcap Spider & SCO':0+1.1/128,
            'PCQ Pro Bait':0+12/192,'PT Alpine Flea & Bed Bug':4.0,'PT Alpine Fly Bait':1.0,
            'Precor 2625 Spray':1.0,'Precor IGR Concentrate':0+14/16,'Shockwave 1':2+16/17,
            'Snake A Way':-1+25/28,'Stryker 54':0+7.9/15,'Sumari':0+23.45/32,
            'Surekill Total Release Aerosol':1+5/6,'Suspend Polyzone':0+29.99/128,
            'T1 Mouse Bait Station':4.0,'Talprid Mole Bait':-1+12/20,
            'Taurus SC':-1+109.34/128,'Temprid Ready Spray':1.0,
            'Trelona ATBS Bait Cartridges':3.0,'Yard Guard':3+13/40,
        },
        'Truck 40':{
            'Bifen I/T':0+127/128,'Bifen L/P Granules':2+15/25,
            'Bird-Out Aromatic Bird Repellent':2.0,'Catchmaster 72 (TC) Glueboards':0+54/72,
            'Catchmaster 72 TC3 Glueboard':72.0,'Contrac RTU Place Packs':1.0,
            'Crossfire Insecticide':1.0,'Exciter':0+4.5/16,'FINAL All-Weather BLOX':-1+189/400,
            'Gentrol IGR':0+7/16,'Nibor-D Insecticide Foam + IGR':1.0,
            'Nyguard Plus Flea & Tick PS':1.0,'Onslaught Fastcap Spider & SCO':-1+87.25/128,
            'PCQ Pro Bait':0+2/192,'PT Alpine Flea & Bed Bug':1.0,'PT Alpine Fly Bait':1.0,
            'Precor 2625 Spray':1.0,'Precor IGR Concentrate':0+10/16,
            'Pro Zap Insect Guard':0+3/12,'Profoam Platinum':1.0,'Ridesco WG':1.0,
            'Shockwave 1':1.0,'Stryker 54':1.0,'Sumari':0+23/32,'Sumari ant gel bait':7.0,
            'Surekill Total Release Aerosol':3.0,'Suspend Polyzone':0+94.5/128,
            'T1 Mouse Bait Station':2.0,'Talon Weatherblok XT':0+120/365,
            'Talprid Mole Bait':10+6/20,'Taurus SC':-1+117.2/128,
            'Temprid Ready Spray':3+14.88/15,'Trelona ATBS Bait Cartridges':13.0,
            'Trelona Bait Station':2.0,'Vendetta plus cockroach gel ba':4.0,'Yard Guard':0+35/40,
        },
    }
    conn = get_db(); cur = conn.cursor()
    try:
        # Build lookups
        cur.execute('SELECT id, name FROM products WHERE active=TRUE')
        prod_map = {r['name']: r['id'] for r in cur.fetchall()}
        cur.execute('SELECT id, truck_id FROM technicians WHERE active=TRUE')
        tech_map = {r['truck_id']: r['id'] for r in cur.fetchall()}

        loaded = 0
        for truck_label, items in TRUCKS.items():
            tech_id = tech_map.get(truck_label)
            if not tech_id:
                continue
            for prod_name, qty in items.items():
                pid = prod_map.get(prod_name)
                if not pid:
                    continue
                cur.execute('''
                    INSERT INTO tech_inventory (technician_id, product_id, quantity_storage_units)
                    VALUES (%s,%s,%s)
                    ON CONFLICT (technician_id, product_id)
                    DO UPDATE SET quantity_storage_units=%s, last_updated=NOW()
                ''', (tech_id, pid, qty, qty))
                loaded += 1
        conn.commit(); cur.close(); conn.close()
        return jsonify({'ok':True,'truck_items_loaded':loaded})
    except Exception as e:
        conn.rollback(); cur.close(); conn.close()
        return jsonify({'error':str(e)}), 500

# ─── NEW ANALYTICS ENDPOINTS ─────────────────────────────────────────────────

@app.route('/api/reports/tech-monthly-trend', methods=['GET'])
@login_required
def report_tech_monthly_trend():
    """Monthly cost + appointment count per tech for the last N months."""
    tech_id = request.args.get('tech_id')
    months  = min(int(request.args.get('months', 6)), 24)
    conn = get_db(); cur = conn.cursor()
    # Start from N full months ago (beginning of that month)
    start = (date.today().replace(day=1) - timedelta(days=months * 31)).replace(day=1)
    q = """
        SELECT
            date_trunc('month', COALESCE(it.appt_date::timestamp, it.created_at)) AS month,
            t.id   AS tech_id,
            t.name AS tech_name,
            SUM(it.quantity_storage_units * COALESCE(p.cost_per_storage_unit, 0)) AS total_cost,
            COUNT(DISTINCT it.fr_appointment_id) AS appt_count,
            COUNT(DISTINCT it.product_id)        AS product_count
        FROM inventory_transactions it
        JOIN technicians t ON it.technician_id = t.id
        JOIN products p    ON it.product_id    = p.id
        WHERE it.transaction_type = 'usage_sync'
          AND COALESCE(it.appt_date, DATE(it.created_at)) >= %s
    """
    params = [start.isoformat()]
    if tech_id:
        q += " AND it.technician_id = %s"; params.append(int(tech_id))
    q += " GROUP BY month, t.id, t.name ORDER BY month, t.name"
    cur.execute(q, params)
    rows = [dict(r) for r in cur.fetchall()]
    cur.close(); conn.close()
    for r in rows:
        r['month']         = r['month'].strftime('%Y-%m') if r['month'] else None
        r['total_cost']    = float(r['total_cost'] or 0)
        r['appt_count']    = int(r['appt_count'] or 0)
        r['product_count'] = int(r['product_count'] or 0)
    return jsonify(rows)


@app.route('/api/reports/product-monthly-trend', methods=['GET'])
@login_required
def report_product_monthly_trend():
    """Monthly usage quantity + cost per product for the last N months."""
    product_id = request.args.get('product_id')
    months     = min(int(request.args.get('months', 6)), 24)
    conn = get_db(); cur = conn.cursor()
    start = (date.today().replace(day=1) - timedelta(days=months * 31)).replace(day=1)
    q = """
        SELECT
            date_trunc('month', COALESCE(it.appt_date::timestamp, it.created_at)) AS month,
            p.id   AS product_id,
            p.name AS product_name,
            p.usage_unit,
            SUM(it.quantity_usage_units)                                            AS total_used,
            SUM(it.quantity_storage_units * COALESCE(p.cost_per_storage_unit, 0)) AS total_cost
        FROM inventory_transactions it
        JOIN products p ON it.product_id = p.id
        WHERE it.transaction_type = 'usage_sync'
          AND COALESCE(it.appt_date, DATE(it.created_at)) >= %s
    """
    params = [start.isoformat()]
    if product_id:
        q += " AND it.product_id = %s"; params.append(int(product_id))
    q += " GROUP BY month, p.id, p.name, p.usage_unit ORDER BY month, p.name"
    cur.execute(q, params)
    rows = [dict(r) for r in cur.fetchall()]
    cur.close(); conn.close()
    for r in rows:
        r['month']      = r['month'].strftime('%Y-%m') if r['month'] else None
        r['total_used'] = float(r['total_used'] or 0)
        r['total_cost'] = float(r['total_cost'] or 0)
    return jsonify(rows)


@app.route('/api/reports/transfers-by-tech', methods=['GET'])
@login_required
def report_transfers_by_tech():
    """
    Transfer history per tech: how many times each tech received product
    in a given date range. Returns a per-tech summary plus per-day detail
    so you can see exactly what was pulled on each restock visit.
    """
    date_from = request.args.get('date_from', (date.today().replace(day=1)).isoformat())
    date_to   = request.args.get('date_to',   date.today().isoformat())
    tech_id   = request.args.get('tech_id',   type=int)

    conn = get_db(); cur = conn.cursor()

    params_base = [date_from, date_to]
    tech_filter = " AND it.technician_id = %s" if tech_id else ""
    if tech_id:
        params_base.append(tech_id)

    # Per-tech summary
    # restock_count = distinct batch IDs where available, else distinct days for historical rows
    cur.execute(f"""
        SELECT t.id   AS tech_id,
               t.name AS tech_name,
               t.truck_id,
               COUNT(*)                                                AS transfer_lines,
               COUNT(DISTINCT it.transfer_batch_id)
                 FILTER (WHERE it.transfer_batch_id IS NOT NULL)
                 + COUNT(DISTINCT DATE(it.created_at))
                   FILTER (WHERE it.transfer_batch_id IS NULL)       AS restock_count,
               COUNT(DISTINCT it.product_id)                          AS distinct_products,
               COALESCE(SUM(it.quantity_storage_units
                   * COALESCE(p.conversion_factor,1)),0)              AS total_usage_units
        FROM inventory_transactions it
        JOIN technicians t ON it.technician_id = t.id
        JOIN products p    ON it.product_id    = p.id
        WHERE it.transaction_type IN ('transfer','equipment_provided')
          AND it.archived IS NOT TRUE
          AND DATE(it.created_at) BETWEEN %s AND %s
          {tech_filter}
        GROUP BY t.id, t.name, t.truck_id
        ORDER BY restock_count DESC, t.name
    """, params_base)
    summary = [dict(r) for r in cur.fetchall()]
    for r in summary:
        r['transfer_lines']    = int(r['transfer_lines'])
        r['restock_count']     = int(r['restock_count'])
        r['distinct_products'] = int(r['distinct_products'])
        r['total_usage_units'] = float(r['total_usage_units'])

    # Per-tech detail — include batch_id and timestamp so we can group sessions properly
    cur.execute(f"""
        SELECT t.id                   AS tech_id,
               t.name                 AS tech_name,
               DATE(it.created_at)   AS transfer_date,
               it.transfer_batch_id,
               it.created_at,
               p.name                AS product_name,
               p.usage_unit, p.storage_unit, p.conversion_factor,
               it.quantity_storage_units,
               it.quantity_storage_units * COALESCE(p.conversion_factor,1) AS quantity_usage_units,
               COALESCE(p.is_equipment, FALSE) AS is_equipment,
               it.performed_by,
               it.transaction_type
        FROM inventory_transactions it
        JOIN technicians t ON it.technician_id = t.id
        JOIN products p    ON it.product_id    = p.id
        WHERE it.transaction_type IN ('transfer','equipment_provided')
          AND it.archived IS NOT TRUE
          AND DATE(it.created_at) BETWEEN %s AND %s
          {tech_filter}
        ORDER BY t.name, it.created_at DESC, p.name
    """, params_base)
    detail_rows = [dict(r) for r in cur.fetchall()]
    cur.close(); conn.close()

    for r in detail_rows:
        r['quantity_storage_units'] = float(r['quantity_storage_units'])
        r['quantity_usage_units']   = float(r['quantity_usage_units'])
        r['conversion_factor']      = float(r['conversion_factor'])
        r['transfer_date']          = str(r['transfer_date']) if r['transfer_date'] else None
        r['transfer_batch_id']      = str(r['transfer_batch_id']) if r['transfer_batch_id'] else None
        r['created_at']             = r['created_at'].isoformat() if r['created_at'] else None

    # Group detail by tech_id → session key → list of products
    # Session key = batch_id if present, else date (historical fallback)
    detail_map = {}
    session_meta = {}  # session_key → {date, batch_id}
    for row in detail_rows:
        tid = row['tech_id']
        key = row['transfer_batch_id'] if row['transfer_batch_id'] else f"date:{row['transfer_date']}"
        detail_map.setdefault(tid, {}).setdefault(key, []).append(row)
        if key not in session_meta:
            session_meta[key] = {
                'date':     row['transfer_date'],
                'batch_id': row['transfer_batch_id'],
                'has_batch': bool(row['transfer_batch_id']),
            }

    # Attach sessions to summary, sorted newest first
    for s in summary:
        tid = s['tech_id']
        by_key = detail_map.get(tid, {})
        # Sort by date descending (use session_meta date)
        sorted_keys = sorted(by_key.keys(),
            key=lambda k: session_meta[k]['date'] or '', reverse=True)
        s['sessions'] = [
            {
                'date':     session_meta[k]['date'],
                'batch_id': session_meta[k]['batch_id'],
                'has_batch': session_meta[k]['has_batch'],
                'products': by_key[k],
            }
            for k in sorted_keys
        ]

    return jsonify({
        'date_from': date_from,
        'date_to':   date_to,
        'techs':     summary,
    })


@app.route('/api/reports/turnover', methods=['GET'])
@login_required
def report_turnover():
    """
    Inventory turnover report — combines usage rate data with actual receive history.

    Metrics returned per product:
      days_on_hand       — current warehouse qty / avg daily usage (lookback window)
      last_received      — date of most recent receive transaction
      receive_count_ytd  — how many times received this calendar year
      avg_days_between_orders — avg gap between consecutive receives (needs 2+ receives)
      total_received_ytd — total storage units received this year
    """
    months = min(int(request.args.get('months', 3)), 12)
    conn = get_db(); cur = conn.cursor()
    start = (date.today().replace(day=1) - timedelta(days=months * 31)).replace(day=1)

    # 1. Average monthly usage over the lookback window
    cur.execute("""
        SELECT it.product_id,
               SUM(it.quantity_storage_units) / %s AS avg_monthly_usage
        FROM inventory_transactions it
        WHERE it.transaction_type = 'usage_sync'
          AND COALESCE(it.appt_date, DATE(it.created_at)) >= %s
        GROUP BY it.product_id
    """, (months, start.isoformat()))
    usage = {r['product_id']: float(r['avg_monthly_usage'] or 0) for r in cur.fetchall()}

    # 2. Current warehouse totals per product
    cur.execute("""
        SELECT product_id, SUM(quantity_storage_units) AS qty
        FROM warehouse_inventory GROUP BY product_id
    """)
    warehouse = {r['product_id']: float(r['qty'] or 0) for r in cur.fetchall()}

    # 3. Receive history — YTD counts, last date, total quantity received
    cur.execute("""
        SELECT
            product_id,
            MAX(DATE(created_at))         AS last_received,
            COUNT(*)                      AS receive_count,
            SUM(quantity_storage_units)   AS total_received
        FROM inventory_transactions
        WHERE transaction_type = 'receive'
          AND EXTRACT(YEAR FROM created_at) = EXTRACT(YEAR FROM CURRENT_DATE)
        GROUP BY product_id
    """)
    receives = {r['product_id']: dict(r) for r in cur.fetchall()}

    # 4. Average days between consecutive receives (requires 2+ receives per product)
    cur.execute("""
        SELECT product_id,
               ROUND(AVG(gap_days)) AS avg_days_between_orders
        FROM (
            SELECT product_id,
                   DATE(created_at) - LAG(DATE(created_at)) OVER (
                       PARTITION BY product_id ORDER BY created_at
                   ) AS gap_days
            FROM inventory_transactions
            WHERE transaction_type = 'receive'
        ) sub
        WHERE gap_days IS NOT NULL
        GROUP BY product_id
    """)
    avg_gaps = {r['product_id']: int(r['avg_days_between_orders']) for r in cur.fetchall()}

    # 5. All active products
    cur.execute("""
        SELECT id, name, storage_unit, cost_per_storage_unit
        FROM products WHERE active = TRUE ORDER BY name
    """)
    prods = cur.fetchall()
    cur.close(); conn.close()

    rows = []
    for p in prods:
        pid      = p['id']
        wh_qty   = warehouse.get(pid, 0)
        avg_mo   = usage.get(pid, 0)
        avg_day  = avg_mo / 30.0
        recv     = receives.get(pid, {})

        days_on_hand = round(wh_qty / avg_day, 0) if avg_day > 0 else None

        last_recv = recv.get('last_received')
        rows.append({
            'product_id':             pid,
            'product_name':           p['name'],
            'storage_unit':           p['storage_unit'],
            'warehouse_qty':          round(wh_qty, 3),
            'avg_monthly_usage':      round(avg_mo, 3),
            'days_on_hand':           days_on_hand,
            'cost_per_unit':          float(p['cost_per_storage_unit'] or 0),
            'last_received':          last_recv.isoformat() if last_recv else None,
            'receive_count_ytd':      int(recv.get('receive_count', 0)),
            'total_received_ytd':     round(float(recv.get('total_received', 0)), 3),
            'avg_days_between_orders': avg_gaps.get(pid),
        })

    # Sort: products with usage but critically low stock first, then by days on hand
    rows.sort(key=lambda r: (
        r['days_on_hand'] if r['days_on_hand'] is not None
        else (0 if r['avg_monthly_usage'] > 0 else 99999)
    ))
    return jsonify({'months': months, 'start': start.isoformat(), 'rows': rows})


# ─── STARTUP ──────────────────────────────────────────────────────────────────
with app.app_context():
    try:
        init_db()
    except Exception as e:
        print(f"DB init error: {e}")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
