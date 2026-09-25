# Inventory Manager — Deployment Guide

## Files in this zip
- app.py              — Flask backend (all API + sync logic)
- requirements.txt    — Python packages
- Procfile            — How Railway starts the app
- railway.toml        — Railway config
- .gitignore
- templates/
  - login.html
  - index.html        — Full single-page UI

---

## Step 1 — GitHub
1. Go to github.com/new → name it "inventory-manager" → Private → Create
2. Click "uploading an existing file"
3. Unzip the download and drag ALL files/folders into the uploader
   (including the templates/ folder)
4. Commit changes

---

## Step 2 — Railway
1. railway.app → Login with GitHub → New Project → Deploy from GitHub repo
2. Select inventory-manager
3. Click + New → Database → Add PostgreSQL

---

## Step 3 — Environment Variables
In Railway click your app service → Variables tab → add these:

  SECRET_KEY      = any long random string (e.g. inv2026xK9mPqR)
  ADMIN_PASSWORD  = your chosen login password
  TECH_PASSWORD   = the shared password techs use to log into /r
  SYNC_TOKEN      = another random string for the cron endpoint

  FR_API_KEY      = your FieldRoutes authenticationToken
  FR_AUTH_KEY     = your FieldRoutes authenticationKey
  FR_BASE_URL     = https://YOUR-SUBDOMAIN.pestroutes.com/api

  DATABASE_URL is set automatically by Railway when you add PostgreSQL.

---

## Step 4 — Port + Domain
1. App service → Settings → Networking → Set port to 5000
2. Click Generate Domain → bookmark the URL

---

## Step 5 — Hourly Auto-Sync (Railway Cron)
1. In your Railway project, click + New → Cron Job
2. Command: curl -s -X POST https://YOUR-APP-URL/api/sync/hourly -H "X-Sync-Token: YOUR_SYNC_TOKEN"
3. Schedule: 0 * * * *  (runs at the top of every hour)

Alternatively use an external cron service like cron-job.org (free).

---

## Step 6 — First-time Setup
1. Log in with username "admin" and your ADMIN_PASSWORD
2. Go to Setup → Technicians → add your techs (use FR employee IDs from your employee list)
3. Go to Setup → Products → click "Import from FR" to pull all your chemicals
4. For each product, set the correct conversion factor:
   - Stored in gallons, FR records in oz → conversion_factor = 128
   - Stored in lbs, FR records in oz → conversion_factor = 16
   - Stored in each/units → conversion_factor = 1
5. Set reorder thresholds per product
6. Go to Receive → add your starting warehouse inventory
7. Transfer stock to trucks as needed
8. Click Sync Now to test the FR connection

---

## FieldRoutes API notes
The sync pulls from these endpoints:
  GET /chemicalUse?dateCreated=YYYY-MM-DD   → chemical usage records
  GET /appointment?appointmentIDs=...        → who actually did each job (servicedBy field)

The "servicedBy" employee ID on each appointment is matched to the
FR Employee ID you enter for each technician in Setup.
The "chemicalID" on each chemical use record is matched to the
FR Chemical ID on each product in Setup.

---

## Login
Username: admin
Password: whatever you set as ADMIN_PASSWORD in Railway
