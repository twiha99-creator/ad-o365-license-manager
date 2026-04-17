import os
from ldap3 import Server, Connection, ALL, NTLM, SUBTREE, Tls
from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
from msal import ConfidentialClientApplication
import requests
import logging
import ssl
from datetime import datetime

app = Flask(__name__)
CORS(app)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

audit_logs = []

def add_log(message, level="INFO"):
    timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    entry = {"timestamp": timestamp, "level": level, "message": message}
    audit_logs.insert(0, entry)
    logger.info(f"[{level}] {message}")
    return entry

def get_disabled_ad_users(ldap_server_url, base_dn, admin_user, admin_pass):
    """Query Active Directory via LDAP for disabled user accounts."""
    try:
        add_log(f"Connecting to LDAP: {ldap_server_url}")
        # Support ldaps:// or ldap://
        use_ssl = ldap_server_url.lower().startswith("ldaps://")
        host = ldap_server_url.replace("ldaps://", "").replace("ldap://", "").split(":")[0]
        port = 636 if use_ssl else 389

        server = Server(host, port=port, use_ssl=use_ssl, get_info=ALL, connect_timeout=10)
        conn = Connection(server, user=admin_user, password=admin_pass, auto_bind=True)
        add_log("LDAP bind successful. Searching for disabled users...")

        # UserAccountControl bit 2 = disabled (0x0002)
        search_filter = "(&(objectCategory=person)(objectClass=user)(userAccountControl:1.2.840.113556.1.4.803:=2))"
        conn.search(
            search_base=base_dn,
            search_filter=search_filter,
            search_scope=SUBTREE,
            attributes=["sAMAccountName", "userPrincipalName", "displayName"]
        )

        users = []
        for entry in conn.entries:
            upn = str(entry.userPrincipalName) if entry.userPrincipalName else ""
            name = str(entry.displayName) if entry.displayName else str(entry.sAMAccountName)
            sam = str(entry.sAMAccountName) if entry.sAMAccountName else ""
            if upn and upn != "[]":
                users.append({
                    "name": name,
                    "upn": upn,
                    "sam": sam,
                    "has_license": False,
                    "licenses": [],
                    "sku_ids": []
                })

        conn.unbind()
        add_log(f"Found {len(users)} disabled AD users with a UPN.")
        return users

    except Exception as e:
        msg = f"LDAP Error: {str(e)}"
        add_log(msg, "ERROR")
        return {"error": msg}

def get_graph_token(client_id, client_secret, tenant_id):
    """Acquire Microsoft Graph API token via MSAL."""
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
            add_log("Microsoft Graph token acquired.")
            return result["access_token"]
        else:
            err = result.get("error_description", "Unknown Azure auth error")
            add_log(f"Graph auth error: {err}", "ERROR")
            return None
    except Exception as e:
        add_log(f"Exception acquiring Graph token: {str(e)}", "ERROR")
        return None

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/logs", methods=["GET"])
def get_logs():
    return jsonify(audit_logs[:100])

@app.route("/api/sync", methods=["POST"])
def sync_users():
    data = request.json
    required = ["ldap_server", "base_dn", "ad_user", "ad_pass",
                "azure_client_id", "azure_client_secret", "azure_tenant_id"]
    for field in required:
        if not data.get(field):
            return jsonify({"error": f"Missing required field: {field}"}), 400

    add_log("=== Starting cross-directory license sync ===")

    ad_users = get_disabled_ad_users(
        data["ldap_server"], data["base_dn"],
        data["ad_user"], data["ad_pass"]
    )
    if isinstance(ad_users, dict) and "error" in ad_users:
        return jsonify(ad_users), 400

    token = get_graph_token(
        data["azure_client_id"],
        data["azure_client_secret"],
        data["azure_tenant_id"]
    )
    if not token:
        return jsonify({"error": "Failed to acquire Microsoft Graph token."}), 401

    headers = {"Authorization": f"Bearer {token}"}
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
                    add_log(f"[LICENSED] {upn}: {', '.join(user['licenses'])}", "WARN")
                else:
                    add_log(f"[CLEAN] {upn} - no active licenses.")
            elif res.status_code == 404:
                add_log(f"[SKIP] {upn} not in Entra ID (on-prem only).")
                continue
            else:
                add_log(f"[ERROR] Could not check {upn}: HTTP {res.status_code}", "ERROR")
        except Exception as e:
            add_log(f"[ERROR] Exception for {upn}: {str(e)}", "ERROR")
        final_report.append(user)

    licensed_count = sum(1 for u in final_report if u["has_license"])
    add_log(f"=== Sync done. {licensed_count}/{len(final_report)} disabled users have licenses. ===")
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

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
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
            msg = f"FAILED for {upn}: {res.text}"
            add_log(msg, "ERROR")
            return jsonify({"status": "error", "message": msg}), res.status_code
    except Exception as e:
        msg = f"Exception: {str(e)}"
        add_log(msg, "ERROR")
        return jsonify({"status": "error", "message": msg}), 500

@app.route("/api/remove-all-licensed", methods=["POST"])
def remove_all_licensed():
    data = request.json
    users = data.get("users", [])
    token = get_graph_token(
        data["azure_client_id"],
        data["azure_client_secret"],
        data["azure_tenant_id"]
    )
    if not token:
        return jsonify({"status": "error", "message": "Failed to acquire Graph token."}), 401

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    licensed_users = [u for u in users if u.get("has_license")]
    add_log(f"=== Auto-removing licenses from {len(licensed_users)} users ===")

    results = []
    for user in licensed_users:
        upn = user["upn"]
        payload = {"addLicenses": [], "removeLicenses": user["sku_ids"]}
        url = f"https://graph.microsoft.com/v1.0/users/{upn}/assignLicense"
        try:
            res = requests.post(url, headers=headers, json=payload, timeout=15)
            if res.status_code == 200:
                add_log(f"Removed: {upn}")
                results.append({"upn": upn, "status": "success"})
            else:
                add_log(f"Failed: {upn} HTTP {res.status_code}", "ERROR")
                results.append({"upn": upn, "status": "error", "detail": res.text})
        except Exception as e:
            add_log(f"Exception for {upn}: {str(e)}", "ERROR")
            results.append({"upn": upn, "status": "error", "detail": str(e)})

    add_log(f"=== Auto-remove done. Processed {len(results)} users. ===")
    return jsonify({"status": "done", "results": results})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=False, host="0.0.0.0", port=port)
