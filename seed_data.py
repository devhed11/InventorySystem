"""
seed_data.py — Run ONCE after deployment to pre-populate all products,
technicians, warehouse inventory, and truck inventory.

Usage:
  DATABASE_URL=postgresql://... python3 seed_data.py
"""
import os
import sys
import psycopg2
from psycopg2.extras import RealDictCursor

DATABASE_URL = os.environ.get('DATABASE_URL', '')
if not DATABASE_URL:
    print("ERROR: Set DATABASE_URL environment variable")
    sys.exit(1)

if DATABASE_URL.startswith('postgres://'):
    DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)

conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
cur = conn.cursor()

print("Starting seed...")

# ─── PRODUCTS ─────────────────────────────────────────────────────────────────
# Format: (name, fr_product_id, storage_unit, usage_unit, conversion_factor, conversion_note)
# conversion_factor = # of usage units per 1 storage unit
# FR chemical IDs left as None — fill in after importing from FR in the app

PRODUCTS = [
    # name, fr_chemical_id (fill later), storage_unit, usage_unit, conv_factor, note
    ("Advion Insect Granular Bait",   None, "Bottle",   "oz",     16.0,   "1 Bottle = 16 oz"),
    ("Advion Microflow",              None, "Jar",      "oz",      8.0,   "1 Jar = 8 oz"),
    ("Bedlam Plus",                   None, "Can",      "oz",     17.0,   "1 Can = 17 oz"),
    ("Bifen I/T",                     None, "Gallons",  "fl oz", 128.0,   "1 Gallon = 128 fl oz"),
    ("Bifen L/P Granules",            None, "Bag",      "lbs",    25.0,   "1 Bag = 25 lbs"),
    ("Bird-Out Aromatic Bird Repellent", None, "Canister", "units", 1.0,  "1 Canister = 1 unit"),
    ("Catchmaster 72 (TC) Glueboards",None, "Box",      "each",   72.0,   "1 Box = 72 boards"),
    ("Catchmaster 72 TC3 Glueboard",  None, "units",    "units",   1.0,   "Tracked per unit"),
    ("Catchmaster 72MB Glueboard",    None, "units",    "units",   1.0,   "Tracked per unit"),
    ("Catchmaster 909 Glueboard",     None, "units",    "units",   1.0,   "Tracked per unit"),
    ("Contrac RTU Place Packs",       None, "Case",     "units",  86.0,   "1 Case ≈ 86 packs"),
    ("Crossfire Insecticide",         None, "Bottle",   "fl oz",  13.0,   "1 Bottle ≈ 13 fl oz"),
    ("D-Fense Dust",                  None, "Bottle",   "lbs",     1.0,   "1 Bottle = 1 lb"),
    ("Exciter",                       None, "Bottle",   "fl oz",  16.0,   "1 Bottle = 16 fl oz"),
    ("FINAL All-Weather BLOX",        None, "Pail",     "units", 400.0,   "1 Pail = 400 blox"),
    ("Gentrol IGR",                   None, "Bottle",   "fl oz",  16.0,   "1 Bottle = 16 fl oz"),
    ("Maxforce Granular Fly Bait",    None, "Pounds",   "lbs",     1.0,   "Tracked by pound"),
    ("Nibor-D Insecticide",           None, "Pail",     "lbs",     5.0,   "1 Pail = 5 lbs"),
    ("Nibor-D Insecticide Foam + IGR",None, "Can",      "oz",     21.0,   "1 Can = 21 oz"),
    ("Nuvan Prostrips",               None, "Case",     "units",  12.0,   "1 Case = 12 strips"),
    ("Nyguard Plus Flea & Tick PS",   None, "Can",      "oz",     17.0,   "1 Can = 17 oz"),
    ("Onslaught Fastcap Spider & SCO",None, "Gallons",  "fl oz", 128.0,   "1 Gallon = 128 fl oz"),
    ("PCQ Pro Bait",                  None, "Pail",     "oz",    192.0,   "1 Pail ≈ 192 oz"),
    ("PT Alpine Flea & Bed Bug",      None, "Can",      "oz",     14.0,   "1 Can = 14 oz"),
    ("PT Alpine Fly Bait",            None, "Can",      "oz",     16.0,   "1 Can = 16 oz"),
    ("Precor 2625 Spray",             None, "Can",      "oz",     21.0,   "1 Can = 21 oz"),
    ("Precor IGR Concentrate",        None, "Bottle",   "oz",     16.0,   "1 Bottle = 16 oz"),
    ("Pro Zap Insect Guard",          None, "Case",     "units",  12.0,   "1 Case = 12 units"),
    ("Profoam Platinum",              None, "Bottle",   "fl oz", 128.0,   "1 Bottle = 128 fl oz"),
    ("Ridesco WG",                    None, "Bottle",   "g",     190.0,   "1 Bottle = 190g"),
    ("Rodent Bait Station",           None, "units",    "units",   1.0,   "Tracked per unit"),
    ("Shockwave 1",                   None, "Can",      "oz",     17.0,   "1 Can = 17 oz"),
    ("Snake A Way",                   None, "Pail",     "lbs",    28.0,   "1 Pail = 28 lbs"),
    ("Stryker 54",                    None, "Can",      "oz",     15.0,   "1 Can = 15 oz"),
    ("Sumari",                        None, "Jug",      "oz",     32.0,   "1 Jug = 32 oz"),
    ("Sumari ant gel bait",           None, "Tube",     "g",      30.0,   "1 Tube = 30g"),
    ("Surekill Total Release Aerosol",None, "Can",      "oz",      6.0,   "1 Can = 6 oz"),
    ("Suspend Polyzone",              None, "Gallons",  "fl oz", 128.0,   "1 Gallon = 128 fl oz"),
    ("T1 Mouse Bait Station",         None, "units",    "units",   1.0,   "Tracked per unit"),
    ("Talon Weatherblok XT",          None, "Pail",     "units", 365.0,   "1 Pail = 365 blox"),
    ("Talprid Mole Bait",             None, "Box",      "units",  20.0,   "1 Box = 20 worms"),
    ("Taurus SC",                     None, "Gallons",  "fl oz", 128.0,   "1 Gallon = 128 fl oz"),
    ("Temprid Ready Spray",           None, "Can",      "oz",     15.0,   "1 Can = 15 oz"),
    ("Termidor HE",                   None, "Bottle",   "fl oz",  79.0,   "1 Bottle = 79 fl oz"),
    ("Trelona ATBS Bait Cartridges",  None, "Canister", "units",   1.0,   "Tracked per canister"),
    ("Trelona Bait Station",          None, "units",    "units",   1.0,   "Tracked per unit"),
    ("Vanecto Cockroach Gel Bait",    None, "Tube",     "g",      30.0,   "1 Tube = 30g"),
    ("Vendetta plus cockroach gel ba",None, "Tube",     "g",      30.0,   "1 Tube = 30g"),
    ("Yard Guard",                    None, "Bag",      "lbs",    40.0,   "1 Bag = 40 lbs"),
]

print(f"Inserting {len(PRODUCTS)} products...")
product_id_map = {}  # name → db id

for (name, fr_id, storage, usage, conv, note) in PRODUCTS:
    cur.execute("SELECT id FROM products WHERE name=%s", (name,))
    existing = cur.fetchone()
    if existing:
        product_id_map[name] = existing['id']
        print(f"  [skip] {name} (already exists)")
        continue
    cur.execute("""
        INSERT INTO products (name, fr_product_id, storage_unit, usage_unit, conversion_factor, conversion_note)
        VALUES (%s, %s, %s, %s, %s, %s) RETURNING id
    """, (name, fr_id, storage, usage, conv, note))
    pid = cur.fetchone()['id']
    product_id_map[name] = pid
    cur.execute("INSERT INTO warehouse_inventory (product_id, quantity_storage_units) VALUES (%s, 0)", (pid,))
    print(f"  [+] {name} (id={pid})")

conn.commit()

# ─── TECHNICIANS ──────────────────────────────────────────────────────────────
# (name, truck_id, fr_employee_id)
# FR Employee IDs: leave blank for now — fill in from the employee list in FR
# You can update these in Setup → Technicians after deployment

TECHNICIANS = [
    # (name, truck_id, fr_employee_id)  — IDs confirmed from Employees.json
    ("Otton Hennessey",    "Truck 14", "698"),
    ("Joseph Whitman",     "Truck 15", "470"),
    ("Kenneth Munzlinger", "Truck 19", "739"),
    ("Zach Ghast",         "Truck 20", "299"),
    ("John Russell",       "Truck 21", "732"),
    ("Cole Valle",         "Truck 23", "741"),
    ("Grace Winegardner",  "Truck 24", "727"),
    ("Kaleb Blackwell",    "Truck 27", "723"),
    ("Jeff Barrett",       "Truck 32", "624"),
    ("Landon Leiweke",     "Truck 33", "707"),
    ("Dillon Shadrick",    "Truck 35", "662"),
    ("Stacey Hale",        "Truck 36", "644"),
    ("Drew Sandweg",       "Truck 37", "742"),
    ("Dave Stout",         "Truck 38", "668"),
    ("Joe Bossart",        "Truck 39", "729"),
    ("Kyle Martin",        "Truck 40", "720"),
]

print(f"\nInserting {len(TECHNICIANS)} technicians...")
tech_id_map = {}  # truck_id → db id

for (name, truck, fr_emp_id) in TECHNICIANS:
    cur.execute("SELECT id FROM technicians WHERE name=%s", (name,))
    existing = cur.fetchone()
    if existing:
        tech_id_map[truck] = existing['id']
        print(f"  [skip] {name} (already exists)")
        continue
    cur.execute("""
        INSERT INTO technicians (name, truck_id, fr_employee_id)
        VALUES (%s, %s, %s) RETURNING id
    """, (name, truck, fr_emp_id))
    tid = cur.fetchone()['id']
    tech_id_map[truck] = tid
    print(f"  [+] {name} → {truck} (id={tid})")

conn.commit()

# ─── WAREHOUSE INVENTORY ──────────────────────────────────────────────────────
# Quantities in storage units (packages/gallons/bags/etc)
WAREHOUSE_QTY = {
    "Advion Insect Granular Bait":     4.0,
    "Advion Microflow":                4.25,
    "Bedlam Plus":                    10.0,
    "Bifen I/T":                       4.13,
    "Bifen L/P Granules":             13.0,
    "Bird-Out Aromatic Bird Repellent":36.0,
    "Catchmaster 72 (TC) Glueboards":  5.0,
    "Catchmaster 72 TC3 Glueboard":  504.0,
    "Catchmaster 72MB Glueboard":     99.0,
    "Catchmaster 909 Glueboard":     228.0,
    "Contrac RTU Place Packs":         0.22,
    "Crossfire Insecticide":           8.85,
    "D-Fense Dust":                   15.0,
    "Exciter":                         5.0,
    "FINAL All-Weather BLOX":          2.0,
    "Gentrol IGR":                     3.0,
    "Maxforce Granular Fly Bait":      5.0,
    "Nibor-D Insecticide":             2.0,
    "Nibor-D Insecticide Foam + IGR": 10.0,
    "Nuvan Prostrips":                10.0,
    "Nyguard Plus Flea & Tick PS":     5.0,
    "Onslaught Fastcap Spider & SCO":  4.48,
    "PCQ Pro Bait":                    2.67,
    "PT Alpine Flea & Bed Bug":        2.0,
    "PT Alpine Fly Bait":              7.0,
    "Precor 2625 Spray":               8.0,
    "Precor IGR Concentrate":          5.0,
    "Pro Zap Insect Guard":            1.83,
    "Profoam Platinum":                3.0,
    "Ridesco WG":                      5.0,
    "Rodent Bait Station":           277.0,
    "Shockwave 1":                    16.0,
    "Snake A Way":                     2.0,
    "Stryker 54":                      1.0,
    "Sumari":                          1.0,
    "Sumari ant gel bait":            24.0,
    "Surekill Total Release Aerosol": 18.0,
    "Suspend Polyzone":               65.42,
    "T1 Mouse Bait Station":          88.0,
    "Talon Weatherblok XT":            9.0,
    "Talprid Mole Bait":              28.0,
    "Taurus SC":                      20.94,
    "Temprid Ready Spray":             4.0,
    "Termidor HE":                    46.0,
    "Trelona ATBS Bait Cartridges":  100.0,
    "Trelona Bait Station":          542.0,
    "Vanecto Cockroach Gel Bait":     26.0,
    "Vendetta plus cockroach gel ba": 14.0,
    "Yard Guard":                      3.5,
}

print("\nSetting warehouse inventory...")
for name, qty in WAREHOUSE_QTY.items():
    pid = product_id_map.get(name)
    if not pid:
        print(f"  [WARN] Product not found: {name}")
        continue
    cur.execute("""
        INSERT INTO warehouse_inventory (product_id, quantity_storage_units)
        VALUES (%s, %s)
        ON CONFLICT DO NOTHING
    """, (pid, 0))
    cur.execute("""
        UPDATE warehouse_inventory SET quantity_storage_units=%s, last_updated=NOW()
        WHERE product_id=%s
    """, (qty, pid))
    # Log the initial receive
    cur.execute("""
        INSERT INTO inventory_transactions
        (transaction_type, product_id, quantity_storage_units, notes, performed_by)
        VALUES ('receive', %s, %s, 'Initial inventory load from CSV', 'System Seed')
    """, (pid, qty))

conn.commit()
print(f"  Loaded {len(WAREHOUSE_QTY)} warehouse items")

# ─── TRUCK INVENTORY ──────────────────────────────────────────────────────────
# Quantities in storage units — calculated as:
# current_qty (whole packages) + individual_qty/conversion (partial packages)
# We use the "total usage units / conversion_factor" as the true storage qty

# From the CSV: for trucks, current_qty = whole packages, individual_qty = remaining usage units
# True total = current_qty + (individual_qty / conversion_factor)

TRUCK_INVENTORY = {
    "Truck 36": {
        "Bedlam Plus":                     0 + 12/17,
        "Bifen I/T":                       1 + 0/128,
        "Bifen L/P Granules":              max(0, -1 + 15.9/25),
        "Catchmaster 72 (TC) Glueboards":  0 + 31/72,
        "Catchmaster 72 TC3 Glueboard":    4.0,
        "Catchmaster 72MB Glueboard":      max(0, 148.0),
        "Contrac RTU Place Packs":         0 + 30/86,
        "Exciter":                         0 + 6/16,
        "FINAL All-Weather BLOX":          0 + 188/400,
        "Gentrol IGR":                     0 + 11.5/16,
        "Nibor-D Insecticide Foam + IGR":  2 + 19/21,
        "Onslaught Fastcap Spider & SCO":  max(0, -1 + 68/128),
        "PCQ Pro Bait":                    max(0, -1 + 191/192),
        "PT Alpine Flea & Bed Bug":        1.0,
        "PT Alpine Fly Bait":              1.0,
        "Precor 2625 Spray":               1.0,
        "Shockwave 1":                     1.0,
        "Stryker 54":                      0 + 14.8/15,
        "Sumari":                          0 + 13/32,
        "Sumari ant gel bait":             7 + 15/30,
        "Surekill Total Release Aerosol":  2 + 3/6,
        "Suspend Polyzone":                0 + 99/128,
        "T1 Mouse Bait Station":           10.0,
        "Talon Weatherblok XT":            0 + 45/365,
        "Talprid Mole Bait":               6 + 7/20,
        "Taurus SC":                       0 + 76/128,
        "Temprid Ready Spray":             1 + 12.4/15,
        "Trelona ATBS Bait Cartridges":    11.0,
        "Yard Guard":                      0 + 34/40,
    },
    "Truck 14": {
        "Advion Insect Granular Bait":     0 + 8/16,
        "Bedlam Plus":                     1.0,
        "Bifen I/T":                       0 + 20/128,
        "Bifen L/P Granules":              1 + 5/25,
        "Catchmaster 72 (TC) Glueboards":  2 + 66/72,
        "Contrac RTU Place Packs":         0 + 23/86,
        "D-Fense Dust":                    1.0,
        "Exciter":                         0 + 13/16,
        "FINAL All-Weather BLOX":          max(0, -1 + 348/400),
        "Gentrol IGR":                     0 + 10/16,
        "Nibor-D Insecticide Foam + IGR":  10 + 12.25/21,
        "Nyguard Plus Flea & Tick PS":     1.0,
        "Onslaught Fastcap Spider & SCO":  0 + 2.29/128,
        "PT Alpine Flea & Bed Bug":        0 + 7/14,
        "PT Alpine Fly Bait":              0 + 8/16,
        "Precor IGR Concentrate":          0 + 8/16,
        "Pro Zap Insect Guard":            0 + 6/12,
        "Shockwave 1":                     0 + 16.7/17,
        "Stryker 54":                      1.0,
        "Suspend Polyzone":                0 + 25.25/128,
        "Talon Weatherblok XT":            1.0,
        "Talprid Mole Bait":               1 + 15/20,
        "Taurus SC":                       0 + 46.88/128,
        "Temprid Ready Spray":             1 + 7.9/15,
        "Trelona ATBS Bait Cartridges":    10.0,
        "Trelona Bait Station":            8.0,
        "Yard Guard":                      2 + 5/40,
    },
    "Truck 20": {
        "Advion Insect Granular Bait":     max(0, -1 + 7/16),
        "Bifen I/T":                       0 + 6/128,
        "Bifen L/P Granules":              max(0, -3 + 16/25),
        "FINAL All-Weather BLOX":          0 + 85/400,
        "Gentrol IGR":                     max(0, -1 + 15/16),
        "Nibor-D Insecticide Foam + IGR":  max(0, -1 + 20/21),
        "Onslaught Fastcap Spider & SCO":  max(0, -1 + 17.5/128),
        "PCQ Pro Bait":                    0 + 24/192,
        "Precor IGR Concentrate":          max(0, -1 + 14/16),
        "Sumari":                          max(0, -1 + 21/32),
        "Suspend Polyzone":                max(0, -1 + 120.37/128),
        "Talprid Mole Bait":               max(0, -1 + 7/20),
        "Taurus SC":                       max(0, -1 + 98.8/128),
        "Termidor HE":                     max(0, -6 + 49.75/79),
        "Trelona ATBS Bait Cartridges":    max(0, -19.0),
        "Yard Guard":                      max(0, -1 + 24/40),
    },
    "Truck 40": {
        "Bifen I/T":                       0 + 127/128,
        "Bifen L/P Granules":              2 + 15/25,
        "Bird-Out Aromatic Bird Repellent":2.0,
        "Catchmaster 72 (TC) Glueboards":  0 + 54/72,
        "Catchmaster 72 TC3 Glueboard":    72.0,
        "Contrac RTU Place Packs":         1.0,
        "Crossfire Insecticide":           1.0,
        "Exciter":                         0 + 4.5/16,
        "FINAL All-Weather BLOX":          max(0, -1 + 189/400),
        "Gentrol IGR":                     0 + 7/16,
        "Nibor-D Insecticide Foam + IGR":  1.0,
        "Nyguard Plus Flea & Tick PS":     1.0,
        "Onslaught Fastcap Spider & SCO":  max(0, -1 + 87.25/128),
        "PCQ Pro Bait":                    0 + 2/192,
        "PT Alpine Flea & Bed Bug":        1.0,
        "PT Alpine Fly Bait":              1.0,
        "Precor 2625 Spray":               1.0,
        "Precor IGR Concentrate":          0 + 10/16,
        "Pro Zap Insect Guard":            0 + 3/12,
        "Profoam Platinum":                1.0,
        "Ridesco WG":                      1.0,
        "Shockwave 1":                     1.0,
        "Stryker 54":                      1.0,
        "Sumari":                          0 + 23/32,
        "Sumari ant gel bait":             7.0,
        "Surekill Total Release Aerosol":  3.0,
        "Suspend Polyzone":                0 + 94.5/128,
        "T1 Mouse Bait Station":           2.0,
        "Talon Weatherblok XT":            0 + 120/365,
        "Talprid Mole Bait":               10 + 6/20,
        "Taurus SC":                       max(0, -1 + 117.2/128),
        "Temprid Ready Spray":             3 + 14.88/15,
        "Trelona ATBS Bait Cartridges":    13.0,
        "Trelona Bait Station":            2.0,
        "Vendetta plus cockroach gel ba":  4.0,
        "Yard Guard":                      0 + 35/40,
    },
    "Truck 21": {
        "Bifen I/T":                       max(0, -1 + 119/128),
        "Bifen L/P Granules":              max(0, -4 + 23/25),
        "Catchmaster 72MB Glueboard":      1.0,
        "Contrac RTU Place Packs":         0 + 25/86,
        "D-Fense Dust":                    max(0, -1 + 0.76/1),
        "Exciter":                         0 + 14/16,
        "FINAL All-Weather BLOX":          0.0,
        "Gentrol IGR":                     1.0,
        "Nibor-D Insecticide Foam + IGR":  0 + 9.5/21,
        "Onslaught Fastcap Spider & SCO":  max(0, -1 + 81/128),
        "PT Alpine Flea & Bed Bug":        0 + 7/14,
        "PT Alpine Fly Bait":              1.0,
        "Precor 2625 Spray":               1.0,
        "Precor IGR Concentrate":          0 + 8/16,
        "Surekill Total Release Aerosol":  2.0,
        "Suspend Polyzone":                0 + 32/128,
        "T1 Mouse Bait Station":           12.0,
        "Talon Weatherblok XT":            2.0,
        "Talprid Mole Bait":               max(0, -1 + 15/20),
        "Taurus SC":                       0 + 75.8/128,
        "Temprid Ready Spray":             1 + 13.53/15,
        "Vanecto Cockroach Gel Bait":      1.0,
    },
    "Truck 15": {
        "Bedlam Plus":                     1.0,
        "Bifen I/T":                       0 + 20/128,
        "Bifen L/P Granules":              0 + 10/25,
        "Catchmaster 72MB Glueboard":      144.0,
        "Catchmaster 909 Glueboard":       12.0,
        "Contrac RTU Place Packs":         0 + 5/86,
        "Crossfire Insecticide":           1.0,
        "D-Fense Dust":                    2.0,
        "Exciter":                         0 + 7/16,
        "FINAL All-Weather BLOX":          0 + 313/400,
        "Gentrol IGR":                     0 + 2/16,
        "Nibor-D Insecticide Foam + IGR":  1.0,
        "Onslaught Fastcap Spider & SCO":  max(0, -1 + 127/128),
        "Shockwave 1":                     1 + 15.85/17,
        "Stryker 54":                      0 + 14.8/15,
        "Sumari":                          0 + 4/32,
        "Suspend Polyzone":                0 + 85/128,
        "T1 Mouse Bait Station":           1.0,
        "Taurus SC":                       0 + 83/128,
        "Temprid Ready Spray":             0 + 14.5/15,
        "Vanecto Cockroach Gel Bait":      2.0,
        "Yard Guard":                      0 + 20/40,
    },
    "Truck 39": {
        "Advion Insect Granular Bait":     0 + 11.8/16,
        "Bedlam Plus":                     0 + 8/17,
        "Bifen I/T":                       0 + 28/128,
        "Bifen L/P Granules":              3 + 15.78/25,
        "Catchmaster 72 TC3 Glueboard":    72.0,
        "Catchmaster 72MB Glueboard":      72.0,
        "Contrac RTU Place Packs":         0 + 11/86,
        "Crossfire Insecticide":           0 + 12/13,
        "Exciter":                         0 + 12/16,
        "FINAL All-Weather BLOX":          0 + 142/400,
        "Gentrol IGR":                     0 + 12/16,
        "Nibor-D Insecticide Foam + IGR":  4 + 8.75/21,
        "Onslaught Fastcap Spider & SCO":  0 + 1.1/128,
        "PCQ Pro Bait":                    0 + 12/192,
        "PT Alpine Flea & Bed Bug":        4.0,
        "PT Alpine Fly Bait":              1.0,
        "Precor 2625 Spray":               1.0,
        "Precor IGR Concentrate":          0 + 14/16,
        "Shockwave 1":                     2 + 16/17,
        "Stryker 54":                      0 + 7.9/15,
        "Sumari":                          0 + 23.45/32,
        "Surekill Total Release Aerosol":  1 + 5/6,
        "Suspend Polyzone":                0 + 29.99/128,
        "T1 Mouse Bait Station":           4.0,
        "Talprid Mole Bait":               max(0, -1 + 12/20),
        "Taurus SC":                       max(0, -1 + 109.34/128),
        "Temprid Ready Spray":             1.0,
        "Trelona ATBS Bait Cartridges":    3.0,
        "Yard Guard":                      3 + 13/40,
    },
    "Truck 38": {
        "Advion Insect Granular Bait":     1.0,
        "Bifen I/T":                       1.0,
        "Bifen L/P Granules":              0 + 19.5/25,
        "Catchmaster 72 (TC) Glueboards":  2 + 10/72,
        "Catchmaster 72 TC3 Glueboard":    119.0,
        "Catchmaster 72MB Glueboard":      14.0,
        "Contrac RTU Place Packs":         0 + 20/86,
        "Exciter":                         2.0,
        "FINAL All-Weather BLOX":          0 + 138.75/400,
        "Gentrol IGR":                     max(0, -1 + 10.9/16),
        "Nibor-D Insecticide":             max(0, -11 + 4/5),
        "Nibor-D Insecticide Foam + IGR":  5 + 15.15/21,
        "Nuvan Prostrips":                 1 + 9/12,
        "Nyguard Plus Flea & Tick PS":     1.0,
        "Onslaught Fastcap Spider & SCO":  max(0, -1 + 75.2/128),
        "Precor 2625 Spray":               0 + 20.5/21,
        "Precor IGR Concentrate":          1.0,
        "Pro Zap Insect Guard":            4 + 3/12,
        "Shockwave 1":                     2 + 7.65/17,
        "Stryker 54":                      1.0,
        "Sumari":                          1 + 8/32,
        "Sumari ant gel bait":             2.0,
        "Surekill Total Release Aerosol":  2.0,
        "Suspend Polyzone":                0 + 70.75/128,
        "T1 Mouse Bait Station":           1.0,
        "Talon Weatherblok XT":            0 + 150/365,
        "Talprid Mole Bait":               2 + 19/20,
        "Taurus SC":                       0 + 101.72/128,
        "Temprid Ready Spray":             2.0,
        "Vanecto Cockroach Gel Bait":      4.0,
        "Vendetta plus cockroach gel ba":  2.0,
        "Yard Guard":                      0 + 30/40,
    },
    "Truck 23": {
        "Advion Insect Granular Bait":     1.0,
        "Bedlam Plus":                     0 + 15/17,
        "Bifen I/T":                       0 + 31/128,
        "Bifen L/P Granules":              4 + 5.55/25,
        "Catchmaster 72 TC3 Glueboard":    1.0,
        "Catchmaster 72MB Glueboard":      37.0,
        "Contrac RTU Place Packs":         0 + 32/86,
        "Exciter":                         max(0, -1 + 10.5/16),
        "FINAL All-Weather BLOX":          max(0, -1 + 59/400),
        "Nibor-D Insecticide Foam + IGR":  1 + 20.5/21,
        "Onslaught Fastcap Spider & SCO":  max(0, -1 + 100.4/128),
        "Shockwave 1":                     0 + 16.85/17,
        "Sumari":                          0 + 25/32,
        "Sumari ant gel bait":             0 + 15/30,
        "Surekill Total Release Aerosol":  2 + 4/6,
        "Suspend Polyzone":                1 + 40.37/128,
        "T1 Mouse Bait Station":           5.0,
        "Talprid Mole Bait":               1 + 10/20,
        "Taurus SC":                       0 + 39.6/128,
        "Temprid Ready Spray":             0 + 14.5/15,
        "Trelona Bait Station":            1.0,
        "Vendetta plus cockroach gel ba":  3 + 24/30,
        "Yard Guard":                      0 + 10.75/40,
    },
    "Truck 37": {
        "Bifen I/T":                       max(0, -1 + 109.4/128),
        "Bifen L/P Granules":              1 + 21.4/25,
        "Catchmaster 72 (TC) Glueboards":  1.0,
        "Catchmaster 72 TC3 Glueboard":    72.0,
        "Catchmaster 72MB Glueboard":      72.0,
        "D-Fense Dust":                    2.0,
        "Exciter":                         1.0,
        "FINAL All-Weather BLOX":          0 + 137/400,
        "Onslaught Fastcap Spider & SCO":  1 + 6.5/128,
        "Sumari ant gel bait":             2.0,
        "Surekill Total Release Aerosol":  1.0,
        "Suspend Polyzone":                0 + 93/128,
        "T1 Mouse Bait Station":           24.0,
        "Talprid Mole Bait":               7 + 8/20,
        "Taurus SC":                       0 + 53.6/128,
        "Temprid Ready Spray":             0 + 7.95/15,
        "Termidor HE":                     0 + 50/79,
        "Yard Guard":                      0 + 9/40,
    },
    "Truck 33": {
        "Advion Insect Granular Bait":     max(0, -1 + 6/16),
        "Bedlam Plus":                     0 + 8/17,
        "Bifen I/T":                       0 + 117/128,
        "Bifen L/P Granules":              max(0, -1 + 8/25),
        "Bird-Out Aromatic Bird Repellent":4.0,
        "Catchmaster 72 (TC) Glueboards":  0 + 20/72,
        "Contrac RTU Place Packs":         0 + 22/86,
        "Crossfire Insecticide":           1.0,
        "Exciter":                         0 + 8/16,
        "FINAL All-Weather BLOX":          0 + 39/400,
        "Gentrol IGR":                     0 + 4/16,
        "Nibor-D Insecticide Foam + IGR":  2.0,
        "Onslaught Fastcap Spider & SCO":  max(0, -1 + 105.4/128),
        "PT Alpine Flea & Bed Bug":        1.0,
        "Precor 2625 Spray":               0 + 20.5/21,
        "Pro Zap Insect Guard":            0 + 6/12,
        "Ridesco WG":                      1.0,
        "Shockwave 1":                     3 + 2.37/17,
        "Sumari":                          max(0, -2 + 13/32),
        "Sumari ant gel bait":             4.0,
        "Surekill Total Release Aerosol":  1 + 5/6,
        "Suspend Polyzone":                1 + 90.05/128,
        "T1 Mouse Bait Station":           12.0,
        "Talon Weatherblok XT":            1.0,
        "Talprid Mole Bait":               5 + 4/20,
        "Taurus SC":                       max(0, -1 + 120/128),
        "Temprid Ready Spray":             1.0,
        "Vanecto Cockroach Gel Bait":      2.0,
        "Vendetta plus cockroach gel ba":  1.0,
        "Yard Guard":                      max(0, -1 + 27/40),
    },
    "Truck 24": {
        "Bifen I/T":                       0 + 64/128,
        "Bifen L/P Granules":              4 + 11.58/25,
        "Catchmaster 72 (TC) Glueboards":  0 + 71/72,
        "Catchmaster 72 TC3 Glueboard":    72.0,
        "Catchmaster 72MB Glueboard":      72.0,
        "Crossfire Insecticide":           1.0,
        "Exciter":                         1.0,
        "FINAL All-Weather BLOX":          0 + 229/400,
        "Gentrol IGR":                     1.0,
        "Nibor-D Insecticide Foam + IGR":  2.0,
        "Onslaught Fastcap Spider & SCO":  0 + 121/128,
        "PT Alpine Flea & Bed Bug":        1.0,
        "Precor 2625 Spray":               1.0,
        "Precor IGR Concentrate":          1.0,
        "Shockwave 1":                     0 + 16/17,
        "Stryker 54":                      1.0,
        "Sumari":                          1.0,
        "Sumari ant gel bait":             3.0,
        "Surekill Total Release Aerosol":  1.0,
        "Suspend Polyzone":                0 + 72/128,
        "T1 Mouse Bait Station":           2.0,
        "Talprid Mole Bait":               4 + 11/20,
        "Taurus SC":                       0 + 66.4/128,
        "Temprid Ready Spray":             1.0,
        "Termidor HE":                     1.0,
        "Vanecto Cockroach Gel Bait":      1.0,
        "Yard Guard":                      1 + 1.5/40,
    },
    "Truck 35": {
        "Bedlam Plus":                     max(0, -2 + 9/17),
        "Bifen I/T":                       1 + 32/128,
        "Bifen L/P Granules":              4 + 4.08/25,
        "Catchmaster 72 (TC) Glueboards":  0 + 25/72,
        "Catchmaster 72 TC3 Glueboard":    25.0,
        "Contrac RTU Place Packs":         0 + 24/86,
        "Crossfire Insecticide":           2.0,
        "D-Fense Dust":                    4.0,
        "Exciter":                         0 + 5/16,
        "FINAL All-Weather BLOX":          0 + 84/400,
        "Gentrol IGR":                     1 + 9/16,
        "Nibor-D Insecticide Foam + IGR":  2 + 9.9/21,
        "Nyguard Plus Flea & Tick PS":     0 + 9/17,
        "Onslaught Fastcap Spider & SCO":  0 + 70/128,
        "PT Alpine Flea & Bed Bug":        1 + 12.4/14,
        "PT Alpine Fly Bait":              1 + 3.75/16,
        "Precor 2625 Spray":               2 + 7/21,
        "Precor IGR Concentrate":          0 + 8/16,
        "Pro Zap Insect Guard":            1.0,
        "Ridesco WG":                      max(0, -1 + 152/190),
        "Shockwave 1":                     0 + 16.65/17,
        "Snake A Way":                     0 + 13/28,
        "Stryker 54":                      0 + 14.6/15,
        "Sumari":                          0 + 9/32,
        "Sumari ant gel bait":             5.0,
        "Surekill Total Release Aerosol":  2.0,
        "Suspend Polyzone":                1 + 39.25/128,
        "T1 Mouse Bait Station":           4.0,
        "Talon Weatherblok XT":            1.0,
        "Talprid Mole Bait":               4 + 5/20,
        "Taurus SC":                       0 + 104.1/128,
        "Temprid Ready Spray":             2 + 12.5/15,
        "Trelona ATBS Bait Cartridges":    15.0,
        "Trelona Bait Station":            3.0,
        "Vanecto Cockroach Gel Bait":      2.0,
        "Vendetta plus cockroach gel ba":  3 + 15/30,
        "Yard Guard":                      max(0, -1 + 37/40),
    },
    "Truck 19": {
        "Bedlam Plus":                     2.0,
        "Bifen I/T":                       0 + 31/128,
        "Bifen L/P Granules":              0 + 21/25,
        "Catchmaster 72 (TC) Glueboards":  0 + 65/72,
        "Catchmaster 72 TC3 Glueboard":    68.0,
        "Catchmaster 72MB Glueboard":      55.0,
        "Crossfire Insecticide":           0 + 6.5/13,
        "D-Fense Dust":                    1.0,
        "Exciter":                         1.0,
        "FINAL All-Weather BLOX":          0 + 102/400,
        "Gentrol IGR":                     0 + 7.5/16,
        "Nibor-D Insecticide Foam + IGR":  2.0,
        "Onslaught Fastcap Spider & SCO":  0 + 8/128,
        "PCQ Pro Bait":                    0 + 20/192,
        "Precor 2625 Spray":               1.0,
        "Stryker 54":                      1.0,
        "Sumari":                          0 + 28/32,
        "Suspend Polyzone":                0 + 124.3/128,
        "T1 Mouse Bait Station":           1.0,
        "Talprid Mole Bait":               1 + 10/20,
        "Taurus SC":                       0 + 89.6/128,
        "Yard Guard":                      1 + 4/40,
    },
    "Truck 27": {
        "Bifen I/T":                       max(0, -1 + 120/128),
        "Bifen L/P Granules":              max(0, -5 + 13/25),
        "Contrac RTU Place Packs":         max(0, -1 + 78/86),
        "FINAL All-Weather BLOX":          max(0, -1 + 365/400),
        "Gentrol IGR":                     max(0, -1 + 14.5/16),
        "Onslaught Fastcap Spider & SCO":  max(0, -1 + 123.37/128),
        "Suspend Polyzone":                max(0, -2 + 41.75/128),
        "Talprid Mole Bait":               max(0, -2 + 12/20),
        "Taurus SC":                       max(0, -1 + 0.9/128),
        "Yard Guard":                      max(0, -1 + 35/40),
    },
    "Truck 32": {
        "Advion Insect Granular Bait":     1.0,
        "Advion Microflow":                0 + 2/8,
        "Bedlam Plus":                     1 + 13/17,
        "Bifen I/T":                       0 + 29/128,
        "Bifen L/P Granules":              1.0,
        "Contrac RTU Place Packs":         0 + 25/86,
        "Exciter":                         0 + 15.5/16,
        "FINAL All-Weather BLOX":          0 + 200/400,
        "Gentrol IGR":                     0 + 11/16,
        "Nibor-D Insecticide Foam + IGR":  1.0,
        "Nyguard Plus Flea & Tick PS":     2.0,
        "Onslaught Fastcap Spider & SCO":  0 + 16/128,
        "PT Alpine Flea & Bed Bug":        3 + 10.75/14,
        "PT Alpine Fly Bait":              1.0,
        "Precor 2625 Spray":               1.0,
        "Precor IGR Concentrate":          0 + 8/16,
        "Shockwave 1":                     0 + 16.75/17,
        "Stryker 54":                      2.0,
        "Sumari":                          0 + 16/32,
        "Sumari ant gel bait":             2.0,
        "Surekill Total Release Aerosol":  4.0,
        "Suspend Polyzone":                0 + 32/128,
        "T1 Mouse Bait Station":           5.0,
        "Talon Weatherblok XT":            0 + 150/365,
        "Taurus SC":                       0 + 78/128,
        "Temprid Ready Spray":             4 + 13/15,
        "Trelona ATBS Bait Cartridges":    25.0,
        "Yard Guard":                      0 + 30/40,
    },
}

print("\nLoading truck inventories...")
for truck_label, items in TRUCK_INVENTORY.items():
    tech_id = tech_id_map.get(truck_label)
    if not tech_id:
        print(f"  [WARN] No tech found for {truck_label}")
        continue
    loaded = 0
    for prod_name, qty in items.items():
        if qty <= 0:
            continue
        pid = product_id_map.get(prod_name)
        if not pid:
            print(f"  [WARN] Product not found: {prod_name}")
            continue
        cur.execute("""
            INSERT INTO tech_inventory (technician_id, product_id, quantity_storage_units)
            VALUES (%s, %s, %s)
            ON CONFLICT (technician_id, product_id)
            DO UPDATE SET quantity_storage_units=%s, last_updated=NOW()
        """, (tech_id, pid, qty, qty))
        cur.execute("""
            INSERT INTO inventory_transactions
            (transaction_type, product_id, technician_id, quantity_storage_units, notes, performed_by)
            VALUES ('receive', %s, %s, %s, 'Initial truck load from CSV', 'System Seed')
        """, (pid, tech_id, qty))
        loaded += 1
    print(f"  [{truck_label}] {loaded} products loaded")

conn.commit()

# ─── DEFAULT THRESHOLDS ───────────────────────────────────────────────────────
# Set reasonable reorder thresholds for key products
THRESHOLDS = [
    # product_name, min_warehouse_qty (storage units), reorder_qty, note
    ("Suspend Polyzone",              10.0,  20.0, "Order 20 gal when below 10"),
    ("Taurus SC",                      5.0,  10.0, "Order 10 gal when below 5"),
    ("Termidor HE",                    5.0,  10.0, "Order 10 bottles when below 5"),
    ("Onslaught Fastcap Spider & SCO", 2.0,   5.0, "Order 5 gal when below 2"),
    ("Bifen I/T",                      2.0,   5.0, "Order 5 gal when below 2"),
    ("Talprid Mole Bait",              5.0,  20.0, "Order 20 boxes when below 5"),
    ("Crossfire Insecticide",          2.0,   5.0, "Order 5 bottles when below 2"),
    ("D-Fense Dust",                   3.0,  10.0, "Order 10 bottles when below 3"),
    ("Gentrol IGR",                    1.0,   3.0, "Order 3 bottles when below 1"),
    ("Catchmaster 909 Glueboard",     50.0, 100.0, "Order 100 units when below 50"),
    ("FINAL All-Weather BLOX",         1.0,   2.0, "Order 2 pails when below 1"),
    ("Talon Weatherblok XT",           2.0,   5.0, "Order 5 pails when below 2"),
    ("Sumari",                         1.0,   3.0, "Order 3 jugs when below 1"),
    ("Trelona Bait Station",          50.0, 100.0, "Order 100 when below 50"),
    ("Rodent Bait Station",           50.0, 100.0, "Order 100 when below 50"),
]

print("\nSetting default thresholds...")
for (prod_name, min_qty, reorder_qty, note) in THRESHOLDS:
    pid = product_id_map.get(prod_name)
    if not pid:
        continue
    cur.execute("""
        INSERT INTO product_thresholds (product_id, min_warehouse_qty, reorder_qty, reorder_note)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (product_id) DO UPDATE SET
            min_warehouse_qty=EXCLUDED.min_warehouse_qty,
            reorder_qty=EXCLUDED.reorder_qty,
            reorder_note=EXCLUDED.reorder_note
    """, (pid, min_qty, reorder_qty, note))
    print(f"  {prod_name}: reorder at {min_qty}")

conn.commit()
cur.close()
conn.close()

print("\n✅ Seed complete!")
print("   49 products | 16 technicians | warehouse + all 16 trucks loaded")
print("\nNEXT STEPS:")
print("  1. Log into the app → Setup → Technicians")
print("     Add the FieldRoutes Employee ID for each tech (from the employee list in FR)")
print("  2. Setup → Products → click 'Import from FR'")
print("     This pulls the FR chemicalID for each product so the sync can match them")
print("  3. Click 'Sync Now' to test the FR connection")
