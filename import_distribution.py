"""
import_distribution.py  --  One-time historical distribution importer
=====================================================================
Reads "Inventory Distribution 2025-2026.xlsx", matches product/tech names
to the live DB, applies correct unit conversion, and POSTs records to
/api/admin/import-distribution.

Two-pass approach:
  Pass 1 - scan all sheets; collect products that aren't in DB yet
           (None entries in PRODUCT_OVERRIDES + fully unmatched names).
           Auto-creates them as inactive archived products via bulk-create.
  Pass 2 - normal import; every product now exists in DB by exact name.

Usage:
    py -3 import_distribution.py \
        --url YOUR_APP_URL \
        --token YOUR_SYNC_TOKEN \
        --password YOUR_ADMIN_PASSWORD \
        --excel "C:/path/to/Inventory Distribution.xlsx" \
        [--wipe-first]  [--dry-run]
"""

import argparse, difflib, sys
import requests
import openpyxl

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
parser = argparse.ArgumentParser()
parser.add_argument('--url',        required=True)
parser.add_argument('--token',      required=True)
parser.add_argument('--excel',      required=True)
parser.add_argument('--password',   default='')
parser.add_argument('--username',   default='admin')
parser.add_argument('--dry-run',    action='store_true')
parser.add_argument('--wipe-first', action='store_true',
                    help='Delete previous historical imports before re-importing')
parser.add_argument('--end-month',  default='',
                    help='Stop importing at this month (inclusive). Format: YYYY-MM '
                         'e.g. --end-month 2026-04 skips May 2026 and beyond. '
                         'Use this when the live system already tracks those months.')
args = parser.parse_args()

BASE = args.url.rstrip('/')
sess = requests.Session()

# Parse --end-month into (year, month) tuple or None
end_ym = None
if args.end_month:
    try:
        ey, em = args.end_month.split('-')
        end_ym = (int(ey), int(em))
    except Exception:
        print(f"ERROR: --end-month must be YYYY-MM, got {args.end_month!r}")
        sys.exit(1)

def within_range(year, month):
    """Return True if this sheet should be imported (within the end-month limit)."""
    if end_ym is None:
        return True
    return (year, month) <= end_ym

# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------
if args.password:
    print("Logging in...")
    sess.post(f'{BASE}/login',
              data={'username': args.username, 'password': args.password},
              allow_redirects=True, timeout=30)
    print("  done")

# ---------------------------------------------------------------------------
# Fetch live DB products and techs
# ---------------------------------------------------------------------------
print("Fetching products...")
db_products = sess.get(f'{BASE}/api/products', timeout=30).json()
print("Fetching technicians...")
db_techs = sess.get(f'{BASE}/api/technicians', timeout=30).json()
print(f"  {len(db_products)} products, {len(db_techs)} techs")

db_prod_map   = {p['name']: p for p in db_products}
db_prod_names = list(db_prod_map.keys())
db_tech_map   = {t['name']: t for t in db_techs}
db_tech_names = list(db_tech_map.keys())

# ---------------------------------------------------------------------------
# Product name overrides
# None  = auto-create an archived product with this exact Excel name
# str   = use this existing DB product name instead
# ---------------------------------------------------------------------------
PRODUCT_OVERRIDES = {
    # Advion gel variants not in DB -> will be auto-created
    'Advion Ant Gel Bait':   None,
    'Advion Roach Gel Bait': None,
    'Advion Trio Gel Bait':  None,
    # Name variations -> correct DB name
    'Advion Micro Flow':                              'Advion Microflow',
    'Advion Insect Granular':                         'Advion Insect Granular Bait',
    'Bifen L/P (Bag = 25 lbs)':                      'Bifen L/P Granules',
    'Talpirid Mole BT':                               'Talprid Mole Bait',
    'Talpirid Mole Bait':                             'Talprid Mole Bait',
    'Talpirid Mole BT (Office Storage Room)':         'Talprid Mole Bait',
    'Onslaught Fastcap':                              'Onslaught Fastcap Spider & SCO',
    'Maxforce Granular Fly Bait (Bucket = 5 lbs)':   'Maxforce Granular Fly Bait',
    'Trelona Bait Station (Bait Cartridges)':         'Trelona ATBS Bait Cartridges',
    'Trelona ATBS Cartridges':                        'Trelona ATBS Bait Cartridges',
    'Talon Weatherblock XT':                          'Talon Weatherblok XT',
    'Talon Weatherblox XT':                           'Talon Weatherblok XT',
    'Shockwave Wave 1':                               'Shockwave 1',
    'Vendetta 360 Cockroach Gel Bait':                None,   # separate product from Vendetta Plus; auto-create
    'Vendetta Plus Cockroach Gel Bait':               'Vendetta plus cockroach gel ba',
    'Nyguard Plus Flea & Ticks PS':                   'Nyguard Plus Flea & Tick PS',
    'PCQ Pro':                                        'PCQ Pro Bait',
    'Precor 2625 Spray (Can)':                        'Precor 2625 Spray',
    'Nibor-D Insecticide Foam + IGR (Can)':           'Nibor-D Insecticide Foam + IGR',
    'Suspend Polyzone (Ounces)':                      'Suspend Polyzone',
    'Snake A Way (Bucket)':                           'Snake A Way',
    'Pro Foam':                                       'Profoam Platinum',
    'Sumari':                                         'Sumari ant gel bait',
    'Rodent Bait Station EZ-Secured':                 'Rodent Bait Station',
    'Rodent Bait Station EZ-Secured (Units)':         'Rodent Bait Station',
    # WRONG fuzzy matches -> auto-create with exact Excel name (different products)
    'Delta Dust':                                     None,
    'Final All-Weather Blox':                         None,
    'Temprid FX':                                     None,
    'Temprid SC':                                     None,
    'Nibor-D Insecticide (Bucket = 5 lbs) (Per one pound)': None,
    'Avitrol':                                        None,
    'ZP Rodent Bait AG Oat (Voles)':                 None,
    'PT Alpine Flea & Bed Bug':                       None,
    'Nyguard IGR':                                    None,
}

# ---------------------------------------------------------------------------
# Tech name overrides
# None = auto-create an inactive archived technician with this exact Excel name
# str  = use this existing DB technician name instead
# ---------------------------------------------------------------------------
TECH_OVERRIDES = {
    'Joe H':   'Joseph Whitman',
    'Joe W':   'Joseph Whitman',
    'Joe B':   'Joe Bossart',
    'Brandon':    None,   # former tech -> auto-create as inactive
    'James':      None,   # former tech -> auto-create as inactive
    'James (MW)': None,   # former tech (different James) -> auto-create as inactive
    'Gavin':      None,   # former tech -> auto-create as inactive
    'Ryan':       None,   # former tech -> auto-create as inactive
    'Keith':      None,   # former tech -> auto-create as inactive
    'Cat':        'Cat Morris',  # Cat Morris, FR ID 747
    'Kenny':      'Kenneth Munzlinger',  # short name for Kenneth
    'Lake':       None,   # former tech -> auto-create as inactive
    'Richard':    None,   # former tech -> auto-create as inactive
    'Tara':       None,   # former tech -> auto-create as inactive
    'Tony':       None,   # former tech -> auto-create as inactive
    'Trace':      None,   # former tech -> auto-create as inactive
    'Trevor':     None,   # former tech -> auto-create as inactive
    'Zach':    'Zach Ghast',
    'Dave':    'Dave Stout',
    'Dillon':  'Dillon Shadrick',
    'Jeff':    'Jeff Barrett',
    'John':    'John Russell',
    'Kaleb':   'Kaleb Blackwell',
    'Kenneth': 'Kenneth Munzlinger',
    'Kyle':    'Kyle Martin',
    'Landon':  'Landon Leiweke',
    'Otton':   'Otton Hennessey',
    'Stacey':  'Stacey Hale',
}

# ---------------------------------------------------------------------------
# Unit normalization groups
# ---------------------------------------------------------------------------
UNIT_GROUPS = [
    # liquid volume
    {'oz', 'ounce', 'ounces', 'fl oz', 'fl. oz', 'ml', 'milliliter', 'milliliters'},
    # container types (Cans/Bottles/Tubes all mean 1 container = 1 storage unit)
    {'can', 'cans', 'canister', 'canisters', 'cartridge', 'cartridges',
     'bottle', 'bottles', 'tube', 'tubes', 'syringe', 'syringes', 'jar', 'jars'},
    {'bag', 'bags'},
    {'box', 'boxes'},
    {'pail', 'pails', 'bucket', 'buckets'},
    {'gallon', 'gallons', 'gal'},
    {'pound', 'pounds', 'lb', 'lbs'},
    {'unit', 'units', 'number', 'each', 'pack', 'packs', 'piece', 'pieces'},
    {'gram', 'grams', 'g'},
    {'case', 'cases'},
    {'strip', 'strips'},
    {'trap', 'traps'},
]

def unit_group(s):
    s = s.lower().strip().split('(')[0].strip()
    for g in UNIT_GROUPS:
        if s in g:
            return g
    return None

def same_unit_family(a, b):
    if not a or not b:
        return False
    ga, gb = unit_group(a), unit_group(b)
    if ga is None or gb is None:
        return False
    return ga == gb

# Map Excel Type words to clean DB unit labels
UNIT_NORMALIZE = {
    'ounces': 'oz', 'ounce': 'oz', 'fl oz': 'oz', 'fl. oz': 'oz',
    'cans': 'Can', 'can': 'Can', 'canister': 'Can', 'canisters': 'Can',
    'bottles': 'Bottle', 'bottle': 'Bottle',
    'tubes': 'Tube', 'tube': 'Tube',
    'syringes': 'Syringe', 'syringe': 'Syringe',
    'jars': 'Jar', 'jar': 'Jar',
    'cartridges': 'Cartridge', 'cartridge': 'Cartridge',
    'bags': 'Bag', 'bag': 'Bag',
    'boxes': 'Box', 'box': 'Box',
    'pails': 'Pail', 'pail': 'Pail',
    'buckets': 'Bucket', 'bucket': 'Bucket',
    'gallons': 'gallon', 'gallon': 'gallon', 'gal': 'gallon',
    'pounds': 'lbs', 'pound': 'lbs', 'lb': 'lbs', 'lbs': 'lbs',
    'units': 'units', 'unit': 'units', 'number': 'units',
    'each': 'units', 'piece': 'units', 'pieces': 'units',
    'packs': 'Pack', 'pack': 'Pack',
    'grams': 'g', 'gram': 'g',
    'cases': 'Case', 'case': 'Case',
    'strips': 'Strip', 'strip': 'Strip',
    'traps': 'Trap', 'trap': 'Trap',
    'ml': 'ml', 'milliliter': 'ml', 'milliliters': 'ml',
}

def normalize_unit(raw):
    """Convert Excel Type string to a clean storage unit label."""
    if not raw:
        return 'units'
    cleaned = raw.split('(')[0].strip()
    return UNIT_NORMALIZE.get(cleaned.lower(), cleaned)

# ---------------------------------------------------------------------------
# Manual unit conversion factors
# ---------------------------------------------------------------------------
MANUAL_FACTORS = {
    ('Maxforce Granular Fly Bait', 'buckets'): 5.0,
    ('Maxforce Granular Fly Bait', 'bucket'):  5.0,
    ('PCQ Pro Bait', 'pounds'): 1 / 12,
    ('PCQ Pro Bait', 'lbs'):    1 / 12,
    ('PCQ Pro Bait', 'pound'):  1 / 12,
    ('PCQ Pro Bait', 'units'):  1.0,
    ('PCQ Pro Bait', 'unit'):   1.0,
    ('PCQ Pro Bait', 'number'): 1.0,
    ('Yard Guard', 'buckets'): 1.0,
    ('Yard Guard', 'bucket'):  1.0,
    ('Profoam Platinum', 'cans'): 1.0,
    ('Profoam Platinum', 'can'):  1.0,
    ('Sumari ant gel bait', 'ounces'): 1.0,
    ('Sumari ant gel bait', 'oz'):     1.0,
}

def excel_qty_to_storage(qty_raw, excel_type_raw, prod):
    """Convert Excel quantity in excel_type_raw units to DB storage_units."""
    if qty_raw <= 0:
        return 0.0

    su = (prod.get('storage_unit') or '').strip()
    uu = (prod.get('usage_unit')   or su).strip()
    cf = float(prod.get('conversion_factor') or 1)
    db_name = prod.get('name', '')

    et_clean = excel_type_raw.split('(')[0].strip()
    et_key   = et_clean.lower()

    key = (db_name, et_key)
    if key in MANUAL_FACTORS:
        return qty_raw * MANUAL_FACTORS[key]

    if same_unit_family(et_clean, su):
        return qty_raw

    if same_unit_family(et_clean, uu) and cf != 1:
        return qty_raw / cf

    print(f"    [UNIT?] {db_name!r}: Excel={excel_type_raw!r}, "
          f"DB storage={su!r}, usage={uu!r}, CF={cf} -> storing raw")
    return qty_raw

# ---------------------------------------------------------------------------
# Matching helpers
# ---------------------------------------------------------------------------
MONTH_MAP = {
    'jan': 1, 'january': 1, 'feb': 2, 'february': 2,
    'mar': 3, 'march': 3,   'apr': 4, 'april': 4,
    'may': 5, 'jun': 6,     'june': 6, 'jul': 7, 'july': 7,
    'aug': 8, 'august': 8,  'sep': 9, 'september': 9,
    'oct': 10,'october': 10,'nov': 11,'november': 11,
    'dec': 12,'december': 12,
}

def parse_sheet_name(name):
    parts = name.strip().split()
    if len(parts) != 2:
        return None
    mon = MONTH_MAP.get(parts[0].lower())
    try:
        yr = int(parts[1])
    except ValueError:
        return None
    if mon is None or yr < 2020 or yr > 2030:
        return None
    return yr, mon

def fuzzy_match(name, choices, cutoff=0.50):
    name = name.strip()
    for c in choices:
        if c.strip().lower() == name.lower():
            return c, 1.0
    hits = difflib.get_close_matches(name, choices, n=1, cutoff=cutoff)
    if hits:
        score = difflib.SequenceMatcher(None, name.lower(), hits[0].lower()).ratio()
        return hits[0], score
    return None, 0.0

SECTION_WORDS = {'chemicals', 'equipment', 'other', 'category', 'products', 'product', 'item', 'items'}
SKIP_PREFIXES = ('total', 'month total', 'ytd', 'year to date', 'grand total', 'subtotal')

# ---------------------------------------------------------------------------
# Helper: parse one workbook sheet into (header_row_idx, type_col, tech_cols, all_tech_names)
# tech_cols: col_index -> excel_name, for techs that are resolved (Pass 2 use)
# all_tech_names: every name found in the header row (Pass 1 use — includes None-override techs)
# ---------------------------------------------------------------------------
def parse_sheet_header(rows):
    if not rows:
        return None, None, {}, []

    type_col = None
    for ci, v in enumerate(rows[0]):
        if v and str(v).strip().lower() == 'type':
            type_col = ci
            break

    header_row_idx = None
    tech_cols      = {}
    all_tech_names = []

    for ri, row in enumerate(rows[:8]):
        non_empty = [(ci, str(v).strip()) for ci, v in enumerate(row)
                     if v and str(v).strip()]
        hits = 0
        for ci, val in non_empty[1:]:
            if val in TECH_OVERRIDES:
                if TECH_OVERRIDES[val] is not None:
                    hits += 1
                else:
                    hits += 1  # count None-override techs toward header detection
            else:
                _, sc = fuzzy_match(val, db_tech_names, cutoff=0.45)
                if sc > 0.45:
                    hits += 1
        if hits >= 3:
            header_row_idx = ri
            for ci, val in non_empty[1:]:
                if val in TECH_OVERRIDES:
                    all_tech_names.append(val)
                    tech_cols[ci] = val   # include ALL techs (even None-override); Pass 2 resolves via excel_to_db_tech
                else:
                    _, sc = fuzzy_match(val, db_tech_names, cutoff=0.45)
                    if sc > 0.45:
                        tech_cols[ci] = val
                        all_tech_names.append(val)
            break

    return header_row_idx, type_col, tech_cols, all_tech_names

# ===========================================================================
# PASS 1: Scan all sheets; find products AND techs that need to be auto-created
# ===========================================================================
print(f"\nOpening {args.excel} ...")
wb = openpyxl.load_workbook(args.excel, data_only=True)

print("\nPASS 1: Scanning for unresolved products and technicians...")

# products_to_create: excel_name -> {excel_type, is_equipment}
products_to_create = {}
# techs_to_create: excel_name (the short name from spreadsheet)
techs_to_create = set()
# excel_to_db_name: fuzzy match results to reuse in Pass 2
excel_to_db_name = {}
# excel_to_db_tech: fuzzy tech match results to reuse in Pass 2
excel_to_db_tech = {}

for sheet_name in wb.sheetnames:
    ym = parse_sheet_name(sheet_name)
    if ym is None:
        continue
    if not within_range(*ym):
        print(f"  Skipping '{sheet_name}' (beyond --end-month {args.end_month})")
        continue

    ws   = wb[sheet_name]
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        continue

    header_row_idx, type_col, tech_cols, all_tech_names = parse_sheet_header(rows)
    if header_row_idx is None:
        continue

    # ---- Collect techs that need auto-creation ----
    for val in all_tech_names:
        if val in excel_to_db_tech or val in techs_to_create:
            continue
        if val in TECH_OVERRIDES:
            if TECH_OVERRIDES[val] is None:
                techs_to_create.add(val)
            else:
                excel_to_db_tech[val] = TECH_OVERRIDES[val]
        else:
            db_match, sc = fuzzy_match(val, db_tech_names, cutoff=0.45)
            if db_match is not None:
                excel_to_db_tech[val] = db_match
            else:
                techs_to_create.add(val)

    # ---- Collect products that need auto-creation ----
    current_section = 'chemical'
    for ri in range(header_row_idx + 1, len(rows)):
        row = rows[ri]
        if not row or row[0] is None:
            continue
        raw_name = str(row[0]).strip()
        if not raw_name:
            continue
        low = raw_name.lower()

        if low in SECTION_WORDS:
            if 'equipment' in low:
                current_section = 'equipment'
            elif low in ('chemicals', 'chemical', 'product', 'products', 'item', 'items'):
                current_section = 'chemical'
            continue
        if any(low.startswith(p) for p in SKIP_PREFIXES):
            continue

        if raw_name in excel_to_db_name or raw_name in products_to_create:
            continue

        excel_type = ''
        if type_col is not None and len(row) > type_col and row[type_col]:
            excel_type = str(row[type_col]).strip()
        if not excel_type or excel_type.lower() == 'type':
            excel_type = ''

        if raw_name in PRODUCT_OVERRIDES:
            ov = PRODUCT_OVERRIDES[raw_name]
            if ov is None:
                products_to_create[raw_name] = {
                    'excel_type': excel_type,
                    'is_equipment': current_section == 'equipment',
                }
            else:
                excel_to_db_name[raw_name] = ov
        else:
            db_match, sc = fuzzy_match(raw_name, db_prod_names, cutoff=0.55)
            if db_match is not None:
                excel_to_db_name[raw_name] = db_match
            else:
                products_to_create[raw_name] = {
                    'excel_type': excel_type,
                    'is_equipment': current_section == 'equipment',
                }

print(f"  Products to auto-create: {len(products_to_create)}")
for n, info in sorted(products_to_create.items()):
    sec = 'equipment' if info['is_equipment'] else 'chemical'
    print(f"    [{sec}] {n!r}  (type={info['excel_type']!r})")
print(f"  Technicians to auto-create: {len(techs_to_create)}")
for n in sorted(techs_to_create):
    print(f"    {n!r}")

# ===========================================================================
# Create missing products (unless dry-run)
# ===========================================================================
if products_to_create and not args.dry_run:
    payload = []
    for name, info in products_to_create.items():
        su = normalize_unit(info['excel_type'])
        payload.append({
            'name':                  name,
            'storage_unit':          su,
            'usage_unit':            su,
            'conversion_factor':     1,
            'cost_per_storage_unit': 0,
            'is_equipment':          info['is_equipment'],
        })

    print(f"\nCreating {len(payload)} archived products...")
    r = sess.post(f'{BASE}/api/admin/bulk-create-products',
                  json={'token': args.token, 'products': payload}, timeout=30)
    result = r.json()
    if not result.get('ok'):
        print(f"  ERROR: {result}")
        sys.exit(1)
    print(f"  created={len(result.get('created', []))}  skipped={len(result.get('skipped', []))}")

    # Re-fetch ALL products (including newly created inactive ones)
    print("Re-fetching products (including inactive)...")
    db_products = sess.get(f'{BASE}/api/products?all=1', timeout=30).json()
    print(f"  {len(db_products)} total products")
    db_prod_map   = {p['name']: p for p in db_products}
    db_prod_names = list(db_prod_map.keys())

    # Every auto-create target is now findable by exact name
    for name in products_to_create:
        if name in db_prod_map:
            excel_to_db_name[name] = name
        else:
            print(f"  [WARN] {name!r} not found in DB after creation")

elif products_to_create and args.dry_run:
    print("  [DRY RUN] would create the products above")
    for name in products_to_create:
        excel_to_db_name[name] = name  # pretend they exist

# Also build identity mappings for all PRODUCT_OVERRIDES None entries
# (safety net in case they were already in DB as inactive)
for name, ov in PRODUCT_OVERRIDES.items():
    if ov is None and name not in excel_to_db_name:
        if name in db_prod_map:
            excel_to_db_name[name] = name

# ===========================================================================
# Create missing technicians
# ===========================================================================
if techs_to_create and not args.dry_run:
    tech_payload = [{'name': name} for name in techs_to_create]
    print(f"\nCreating {len(tech_payload)} archived technicians...")
    r = sess.post(f'{BASE}/api/admin/bulk-create-technicians',
                  json={'token': args.token, 'technicians': tech_payload}, timeout=30)
    result = r.json()
    if not result.get('ok'):
        print(f"  ERROR: {result}")
        sys.exit(1)
    print(f"  created={len(result.get('created', []))}  skipped={len(result.get('skipped', []))}")

    # Re-fetch ALL technicians (including newly created inactive ones)
    print("Re-fetching technicians (including inactive)...")
    db_techs = sess.get(f'{BASE}/api/technicians?all=1', timeout=30).json()
    print(f"  {len(db_techs)} total technicians")
    db_tech_map   = {t['name']: t for t in db_techs}
    db_tech_names = list(db_tech_map.keys())

    # Every auto-create target is now findable by exact name
    for name in techs_to_create:
        if name in db_tech_map:
            excel_to_db_tech[name] = name
        else:
            print(f"  [WARN] {name!r} not found in DB after creation")

elif techs_to_create and args.dry_run:
    print("  [DRY RUN] would create technicians:", sorted(techs_to_create))
    for name in techs_to_create:
        excel_to_db_tech[name] = name

# Also build identity mappings for all TECH_OVERRIDES None entries
# (safety net in case they were already in DB as inactive)
for name, ov in TECH_OVERRIDES.items():
    if ov is None and name not in excel_to_db_tech:
        if name in db_tech_map:
            excel_to_db_tech[name] = name

# ===========================================================================
# PASS 2: Import all records
# ===========================================================================
print(f"\nPASS 2: Importing data...")

all_records     = []
unmatched_prods = {}
unmatched_techs = {}

for sheet_name in wb.sheetnames:
    ym = parse_sheet_name(sheet_name)
    if ym is None:
        print(f"  Skipping '{sheet_name}'")
        continue
    year, month = ym

    if not within_range(year, month):
        print(f"  Skipping '{sheet_name}' (beyond --end-month {args.end_month})")
        continue

    ws   = wb[sheet_name]
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        continue

    header_row_idx, type_col, tech_cols, _ = parse_sheet_header(rows)
    if header_row_idx is None:
        print(f"  [{sheet_name}] no tech header found -- skipping")
        continue

    print(f"  [{sheet_name}] {year}-{month:02d}  "
          f"techs={len(tech_cols)}  type_col={type_col}")

    sheet_records = 0
    seen_db_prods = set()  # deduplicate: skip if same DB product appears twice in this sheet

    for ri in range(header_row_idx + 1, len(rows)):
        row = rows[ri]
        if not row or row[0] is None:
            continue
        raw_name = str(row[0]).strip()
        if not raw_name:
            continue
        low = raw_name.lower()

        if low in SECTION_WORDS:
            continue
        if any(low.startswith(p) for p in SKIP_PREFIXES):
            continue

        excel_type = ''
        if type_col is not None and len(row) > type_col and row[type_col]:
            excel_type = str(row[type_col]).strip()
        if not excel_type or excel_type.lower() == 'type':
            excel_type = ''

        # Resolve product name
        db_prod_name = None

        if raw_name in PRODUCT_OVERRIDES:
            ov = PRODUCT_OVERRIDES[raw_name]
            if ov is not None:
                db_prod_name = ov               # direct mapping to existing product
            else:
                db_prod_name = excel_to_db_name.get(raw_name)  # auto-created
        else:
            db_prod_name = excel_to_db_name.get(raw_name)      # fuzzy match or auto-created

        if db_prod_name is None or db_prod_name not in db_prod_map:
            unmatched_prods.setdefault(raw_name, set()).add(sheet_name)
            continue

        # Deduplicate: skip if this DB product already imported from this sheet
        if db_prod_name in seen_db_prods:
            print(f"    [DUP] {raw_name!r} -> {db_prod_name!r} already seen in {sheet_name}, skipping")
            continue
        seen_db_prods.add(db_prod_name)

        prod  = db_prod_map[db_prod_name]
        ttype = 'equipment_provided' if prod.get('is_equipment') else 'transfer'

        for ci, sheet_tech in tech_cols.items():
            cell_val = row[ci] if ci < len(row) else None
            if cell_val is None:
                continue
            try:
                qty_raw = float(cell_val)
            except (ValueError, TypeError):
                continue
            if qty_raw <= 0:
                continue

            db_tech_name = excel_to_db_tech.get(sheet_tech)
            if db_tech_name is None:
                unmatched_techs.setdefault(sheet_tech, set()).add(sheet_name)
                continue

            tech = db_tech_map[db_tech_name]

            qty_storage = excel_qty_to_storage(qty_raw, excel_type, prod) if excel_type else qty_raw

            all_records.append({
                'product_id':             prod['id'],
                'technician_id':          tech['id'],
                'quantity_storage_units': qty_storage,
                'transaction_type':       ttype,
                'notes':                  'Historical import from distribution spreadsheet',
                'created_at':             f"{year}-{month:02d}-15T12:00:00",
                '_sheet':    sheet_name,
                '_prod_xl':  raw_name,
                '_prod_db':  db_prod_name,
                '_tech_db':  db_tech_name,
                '_xl_type':  excel_type,
                '_xl_qty':   qty_raw,
                '_stor_qty': round(qty_storage, 4),
            })
            sheet_records += 1

    print(f"           -> {sheet_records} data points")

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
print(f"\n{'='*60}")
print(f"TOTAL records: {len(all_records)}")

if unmatched_prods:
    print(f"\nUNMATCHED PRODUCTS ({len(unmatched_prods)}) -- skipped:")
    for n, sheets in sorted(unmatched_prods.items()):
        print(f"  {n!r}")

if unmatched_techs:
    print(f"\nUNMATCHED TECHS ({len(unmatched_techs)}) -- skipped:")
    for n, sheets in sorted(unmatched_techs.items()):
        print(f"  {n!r}")

print(f"\nSAMPLE (first 10):")
for rec in all_records[:10]:
    p  = db_prod_map[rec['_prod_db']]
    su = p.get('storage_unit', '?')
    print(f"  {rec['_sheet']:<16} | {rec['_prod_xl']:<45} | {rec['_tech_db']:<20}"
          f" | {rec['_xl_type']:<12} {rec['_xl_qty']:>6} -> {rec['_stor_qty']:>8} {su}")

if args.dry_run:
    print("\n[DRY RUN] done.")
    sys.exit(0)

# ---------------------------------------------------------------------------
# Wipe previous imports if requested
# ---------------------------------------------------------------------------
if args.wipe_first:
    print(f"\nWiping previous historical imports...")
    r = sess.post(f'{BASE}/api/admin/wipe-historical-imports',
                  json={'token': args.token}, timeout=30)
    result = r.json()
    print(f"  Deleted {result.get('deleted', '?')} records")

# ---------------------------------------------------------------------------
# POST in batches
# ---------------------------------------------------------------------------
DEBUG_KEYS = {'_sheet','_prod_xl','_prod_db','_tech_db','_xl_type','_xl_qty','_stor_qty'}
clean = [{k: v for k, v in r.items() if k not in DEBUG_KEYS} for r in all_records]

BATCH = 500
total_inserted = 0
print(f"\nPOSTing {len(clean)} records in batches of {BATCH}...")
for i in range(0, len(clean), BATCH):
    batch   = clean[i:i+BATCH]
    payload = {'token': args.token, 'records': batch}
    resp    = sess.post(f'{BASE}/api/admin/import-distribution',
                        json=payload, timeout=60)
    if resp.status_code != 200:
        print(f"  Batch {i//BATCH+1}: HTTP {resp.status_code} -- {resp.text[:200]}")
        sys.exit(1)
    result = resp.json()
    total_inserted += result.get('inserted', 0)
    print(f"  Batch {i//BATCH+1}: inserted={result['inserted']}  "
          f"skipped={result['skipped']}  errors={len(result.get('errors',[]))}")

print(f"\nDONE. Total inserted: {total_inserted}")
