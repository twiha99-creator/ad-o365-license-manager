import os
import ldap
from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
from msal import ConfidentialClientApplication
import requests
import logging
from datetime import datetime

app = Flask(__name__)
CORS(app)

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# In-memory audit log storage (for demo; use a DB in production)
audit_logs = []

def add_log(message, level="INFO"):
    timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    entry = {"timestamp": timestamp, "level": level, "message": message}
    audit_logs.insert(0, entry)
    logger.info(message)
    return entry

# --- Helper: LDAP Query for Disabled Users ---
def get_disabled_ad_users(ldap_server, base_dn, admin_user, admin_pass):
    try:
        add_log(f"Connecting to LDAP server: {ldap_server}")
        l = ldap.initialize(ldap_server)
        l.protocol_version = ldap.VERSION3
        l.set_option(ldap.OPT_REFERRALS, 0)
        l.set_option(ldap.OPT_TIMEOUT, 10)
        l.simple_bind_s(admin_user, admin_pass)
        add_log("LDAP bind successful. Querying for disabled users...")

        # UserAccountControl bit 2 = Account Disabled (value 0x0002)
        search_filter = "(&(objectCategory=person)(objectClass=user)(userAccountControl:1.2.840.113556.1.4.803:=2))"
        attributes = ["sAMAccountName", "userPrincipalName", "displayName"]
        results = l.search_s(base_dn, ldap.SCOPE_SUBTREE, search_filter, attributes)

        users = []
        for dn, entry in results:
            if isinstance(entry, dict):
                upn = entry.get("userPrincipalName", [b""])[0]
                display = entry.get("displayName", [b""])[0]
                sam = entry.get("sAMAccountName", [b""])[0]
                if upn:
                    users.append({
                        "name": display.decode("utf-8") if isinstance(display, bytes) else display,
                        "upn": upn.decode("utf-8") if isinstance(upn, bytes) else upn,
                        "sam": sam.decode("utf-8") if isinstance(sam, bytes) else sam,
                        "has_license": False,
                        "licenses": [],
                        "sku_ids": []
                    })
        add_log(f"Found {len(users)} disabled AD users with a UPN.")
        l.unbind_s()
        return users
    except ldap.INVALID_CREDENTIALS:
        msg = "LDAP Error: Invalid AD credentials."
        add_log(msg, "ERROR")
        return {"error": msg}
    except ldap.LDAPError as e:
        msg = f"LDAP Error: {str(e)}"
        add_log(msg, "ERROR")
        return {"error": msg}

# --- Helper: Microsoft Graph Token ---
def get_graph_token(client_id, client_secret, tenant_id):
    try:
        authority = f"https://login.microsoftonline.com/{tenant_id}"
        app_msal = ConfidentialClientApplication(
            client_id,
            authority=authority,
            client_credential=client_secret
        )
        result = app_msal.acquire_token_for_client(
            scopes=["https://graph.microsoft.com/.default"]
        )
        if "access_token" in result:
            add_log("Microsoft Graph token acquired successfully.")
            return result["access_token"]
        else:
            error = result.get("error_description", "Unknown auth error")
            add_log(f"Graph auth error: {error}", "ERROR")
            return None
    except Exception as e:
        add_log(f"Exception acquiring Graph token: {str(e)}", "ERROR")
        return None

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/logs", methods=["GET"])
def get_logs():
    return jsonify(audit_logs[-100:])  # Return last 100 log entries

@app.route("/api/sync", methods=["POST"])
def sync_users():
    data = request.json
    required = ["ldap_server", "base_dn", "ad_user", "ad_pass",
                "azure_client_id", "azure_client_secret", "azure_tenant_id"]
    for field in required:
        if not data.get(field):
            return jsonify({"error": f"Missing required field: {field}"}), 400

    add_log("=== Starting cross-directory license sync ===")

    # 1. Get Disabled Users from AD
    ad_users = get_disabled_ad_users(
        data["ldap_server"], data["base_dn"],
        data["ad_user"], data["ad_pass"]
    )
    if isinstance(ad_users, dict) and "error" in ad_users:
        return jsonify(ad_users), 400

    # 2. Get Graph Token
    token = get_graph_token(
        data["azure_client_id"],
        data["azure_client_secret"],
        data["azure_tenant_id"]
    )
    if not token:
        return jsonify({"error": "Failed to acquire Microsoft Graph token. Check Azure credentials."}), 401

    headers = {"Authorization": f"Bearer {token}"}

    # 3. Check O365 licenses for each disabled user
    add_log(f"Checking O365 licenses for {len(ad_users)} disabled users...")
    final_report = []
    for user in ad_users:
        upn = user["upn"]
        try:
            graph_url = f"https://graph.microsoft.com/v1.0/users/{upn}/licenseDetails"
            res = requests.get(graph_url, headers=headers, timeout=15)
            if res.status_code == 200:
                licenses = res.json().get("value", [])
                if licenses:
                    user["has_license"] = True
                    user["licenses"] = [l["skuPartNumber"] for l in licenses]
                    user["sku_ids"] = [l["skuId"] for l in licenses]
                    add_log(f"  [LICENSED] {upn} has {len(licenses)} license(s): {', '.join(user['licenses'])}", "WARN")
                else:
                    add_log(f"  [CLEAN] {upn} - no active licenses.")
            elif res.status_code == 404:
                add_log(f"  [SKIP] {upn} not found in Azure AD (may be on-prem only).")
                continue  # Skip users not in Entra ID
            else:
                add_log(f"  [ERROR] Could not check {upn}: HTTP {res.status_code}", "ERROR")
        except Exception as e:
            add_log(f"  [ERROR] Exception checking {upn}: {str(e)}", "ERROR")
        final_report.append(user)

    licensed_count = sum(1 for u in final_report if u["has_license"])
    add_log(f"=== Sync complete. {licensed_count}/{len(final_report)} disabled users have active licenses. ===")
    return jsonify(final_report)

@app.route("/api/remove-license", methods=["POST"])
def remove_license():
    data = request.json
    token = get_graph_token(
        data["azure_client_id"],
        data["azure_client_secret"],
        data["azure_tenant_id"]
    )
    if not token:
        return jsonify({"status": "error", "message": "Failed to acquire Graph token."}), 401

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    upn = data["upn"]
    sku_ids = data["sku_ids"]

    add_log(f"Removing {len(sku_ids)} license(s) from {upn}...")
    payload = {"addLicenses": [], "removeLicenses": sku_ids}
    url = f"https://graph.microsoft.com/v1.0/users/{upn}/assignLicense"

    try:
        res = requests.post(url, headers=headers, json=payload, timeout=15)
        if res.status_code == 200:
            msg = f"SUCCESS: Licenses removed from {upn}"
            add_log(msg)
            return jsonify({"status": "success", "message": msg})
        else:
            msg = f"FAILED to remove licenses from {upn}: {res.text}"
            add_log(msg, "ERROR")
            return jsonify({"status": "error", "message": msg}), res.status_code
    except Exception as e:
        msg = f"Exception removing license from {upn}: {str(e)}"
        add_log(msg, "ERROR")
        return jsonify({"status": "error", "message": msg}), 500

@app.route("/api/remove-all-licensed", methods=["POST"])
def remove_all_licensed():
    """Auto-remove licenses from all disabled users that are licensed."""
    data = request.json
    users = data.get("users", [])
    token = get_graph_token(
        data["azure_client_id"],
        data["azure_client_secret"],
        data["azure_tenant_id"]
    )
    if not token:
        return jsonify({"status": "error", "message": "Failed to acquire Graph token."}), 401

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }

    results = []
    licensed_users = [u for u in users if u.get("has_license")]
    add_log(f"=== Auto-removing licenses from {len(licensed_users)} users ===")

    for user in licensed_users:
        upn = user["upn"]
        sku_ids = user["sku_ids"]
        payload = {"addLicenses": [], "removeLicenses": sku_ids}
        url = f"https://graph.microsoft.com/v1.0/users/{upn}/assignLicense"
        try:
            res = requests.post(url, headers=headers, json=payload, timeout=15)
            if res.status_code == 200:
                msg = f"SUCCESS: Removed from {upn}"
                add_log(msg)
                results.append({"upn": upn, "status": "success"})
            else:
                msg = f"FAILED for {upn}: HTTP {res.status_code}"
                add_log(msg, "ERROR")
                results.append({"upn": upn, "status": "error", "detail": res.text})
        except Exception as e:
            add_log(f"Exception for {upn}: {str(e)}", "ERROR")
            results.append({"upn": upn, "status": "error", "detail": str(e)})

    add_log(f"=== Auto-remove complete. Processed {len(results)} users. ===")
    return jsonify({"status": "done", "results": results})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=False, host="0.0.0.0", port=port)
