# AD & O365 License Manager

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/twiha99-creator/ad-o365-license-manager)

A Flask web application that identifies **disabled Active Directory users** who still hold **active Office 365 licenses**, and automatically removes those licenses via Microsoft Graph API.

---

## ✨ Features

- 🔐 **Dual credential login** — separate AD (LDAP) and Azure/Entra ID credentials
- 🔍 **Scan disabled users** — queries on-prem AD via LDAP for all disabled accounts
- 📊 **License cross-check** — validates each user against Microsoft Graph API
- 🗑️ **One-click removal** — remove licenses per user or bulk-remove all
- 📋 **Audit log panel** — real-time colored log panel in the UI
- 🛡️ **Secure by design** — credentials never stored, only used per session

---

## 🚀 Deploy to Production (Free)

### Option A: Render.com (Recommended — 1-Click)

Click the badge above or follow these steps:

1. Go to [https://render.com](https://render.com) and sign up (free)
2. Click **"New +"** → **"Web Service"**
3. Connect your GitHub account and select this repo: `twiha99-creator/ad-o365-license-manager`
4. Configure:
   - **Runtime:** Python 3
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `gunicorn app:app`
   - **Instance Type:** Free
5. Click **"Create Web Service"**
6. Your app will be live at: `https://ad-o365-license-manager.onrender.com`

> **For auto-deploy on every git push:**
> - Copy the Deploy Hook URL from Render (Settings → Deploy Hook)
> - Go to GitHub → Settings → Secrets → Actions → New secret
> - Name: `RENDER_DEPLOY_HOOK`, Value: (paste URL)

### Option B: Railway.app

1. Go to [https://railway.app](https://railway.app)
2. New Project → Deploy from GitHub Repo
3. Select this repo — Railway auto-detects Python + Procfile
4. Your app URL will be generated automatically

---

## ⚙️ Architecture

```
Browser (Vue.js + Bootstrap UI)
    │
    └── Flask Backend (app.py)
            ├── LDAP3 → On-Prem Active Directory (port 389/636)
            └── MSAL → Microsoft Graph API → Azure Entra ID / O365
```

---

## 🔧 Azure App Registration Setup

The Azure side uses **Application (non-delegated) permissions** so no user login is required:

1. Go to **Azure Portal** → **App registrations** → **New registration**
2. Name it something like `LicenseManager`
3. After creating, go to **API Permissions** → **Add a permission** → **Microsoft Graph** → **Application permissions**
4. Add: `User.ReadWrite.All` and `Directory.ReadWrite.All`
5. Click **"Grant admin consent"**
6. Go to **Certificates & Secrets** → **New client secret** — copy the value
7. You now have: **Tenant ID**, **Client (App) ID**, and **Client Secret**

---

## 🖥️ Using the App

1. Open the app URL
2. Fill in **Active Directory** credentials:
   - LDAP Server: `ldap://your-dc.domain.local`
   - Base DN: `DC=yourdomain,DC=com`
   - Admin UPN: `admin@yourdomain.com`
   - Password
3. Fill in **Azure Entra ID** credentials:
   - Tenant ID, Client ID, Client Secret
4. Click **"Scan for Disabled Users"**
5. Review results — users with active licenses are highlighted
6. Click **"Remove"** per user, or **"Remove All Licenses"** for bulk action
7. Monitor the audit log panel at the bottom

---

## 📁 Project Structure

```
ad-o365-license-manager/
├── app.py                          # Flask backend
├── requirements.txt                # Python dependencies
├── Procfile                        # Gunicorn start command
├── templates/
│   └── index.html                  # Vue.js + Bootstrap frontend
└── .github/
    └── workflows/
        └── deploy.yml              # GitHub Actions CI/CD
```

---

## ⚠️ Important: On-Prem AD Network Access

For the LDAP connection to your domain controller to work, the hosting server **must have network access** to your DC. Options:
- Run the app **internally** on your corporate network
- Use a **VPN tunnel** from the cloud host to your DC
- For **Azure AD-only** environments (Entra ID cloud users), no LDAP is needed — you can adapt the app to use Graph API exclusively

---

## 🔒 Security Notes

- Credentials are **never stored** — passed per-request only
- Use **HTTPS** in production (Render provides this automatically)
- Restrict access to the app URL using network policies or an auth layer
- In production, use **environment variables** instead of form inputs for the Azure credentials
