from flask import Flask, request, render_template, redirect, url_for, make_response, jsonify
import json
import os
import logging
import sys
from datetime import datetime

# --- LOGGING SETUP ---
logging.basicConfig(
    stream=sys.stdout, 
    level=logging.INFO, 
    format='[%(asctime)s] %(levelname)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

# Silence Flask's default noisy HTTP request logs
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

app = Flask(__name__)
app.secret_key = 'REPLACE_WITH_A_SECURE_SECRET_KEY'

# Configuration
USER_PASSWORD = "weqr1234"
ADMIN_PASSWORD = "dws13125851313241086670"
DATA_FILE = 'data/access_log.json'

TRUSTED_IPS = [
    "127.0.0.1",      # Localhost
    "192.168.2.91",   # The Physical Server IP
    "172.18.0.1"      # The Docker Network Gateway
]

# Ensure data file exists
os.makedirs('data', exist_ok=True)
if not os.path.exists(DATA_FILE):
    with open(DATA_FILE, 'w') as f:
        json.dump({"allowed_ips": {}, "banned_ips": {}, "attempts": {}}, f)
    logging.info("[Gatekeeper] Created new access_log.json database.")

def load_data():
    with open(DATA_FILE, 'r') as f:
        return json.load(f)

def save_data(data):
    with open(DATA_FILE, 'w') as f:
        json.dump(data, f, indent=4)

def get_client_ip():
    # Try to get the real IP passed from Nginx Proxy Manager first
    forwarded_ip = request.headers.get('X-Original-Remote-Addr')
    if forwarded_ip:
        return forwarded_ip
    return request.remote_addr

@app.route('/')
def index():
    """If someone visits the bare IP/Domain, auto-redirect to the login screen."""
    return redirect(url_for('login'))

@app.route('/auth')
def auth():
    """Nginx Proxy Manager hits this endpoint to check access."""
    ip = get_client_ip()
    target_url = request.headers.get('X-Original-URI', 'Unknown URL')
    
    # --- NEW: Instant bypass for trusted server IPs ---
    if ip in TRUSTED_IPS or ip.startswith('172.'):
        return "OK", 200

    data = load_data()
    
    if ip in data['banned_ips']:
        logging.warning(f"[Gatekeeper] Access DENIED (Banned IP) -> {ip} attempted to reach {target_url}")
        return "Banned", 403
        
    # Check IP whitelist OR valid cookie
    if ip in data['allowed_ips'] or request.cookies.get('dws_auth') == 'granted':
        # logging.info(f"[Gatekeeper] Access GRANTED -> {ip} accessing {target_url}")
        return "OK", 200
    
    # Log the unauthorized attempt
    if ip not in data['attempts']:
        location = "Unknown Location"
        # Don't look up local internal IPs
        if not ip.startswith('192.168.') and not ip.startswith('10.') and not ip.startswith('127.'):
            try:
                # Quick external lookup using standard requests library
                geo_resp = requests.get(f"http://ip-api.com/json/{ip}?fields=status,country,city", timeout=2).json()
                if geo_resp.get('status') == 'success':
                    location = f"{geo_resp.get('city')}, {geo_resp.get('country')}"
            except Exception:
                location = "Lookup Failed"
        else:
            location = "Local Network"

        data['attempts'][ip] = {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), 
            "count": 1,
            "location": location
        }
    else:
        data['attempts'][ip]['count'] += 1
        data['attempts'][ip]['time'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    save_data(data)
    
    logging.info(f"[Gatekeeper] Access UNAUTHORIZED -> {ip} redirected to login page. (Attempt #{data['attempts'][ip]['count']})")
    return "Unauthorized", 401

@app.route('/login', methods=['GET', 'POST'])
def login():
    """The minimalist login page."""
    redirect_url = request.args.get('redirect', '')
    ip = get_client_ip()
    
    if request.method == 'POST':
        password = request.form.get('password')
        if password == USER_PASSWORD:
            data = load_data()
            
            # Whitelist IP and remove from attempts
            data['allowed_ips'][ip] = {"name": "Auto-Logged In", "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
            if ip in data['attempts']:
                del data['attempts'][ip]
            save_data(data)
            
            logging.info(f"[Gatekeeper] SUCCESSFUL LOGIN -> {ip} entered correct password. Access permanently granted.")
            
            resp = make_response(redirect(redirect_url if redirect_url else '/'))
            resp.set_cookie('dws_auth', 'granted', max_age=60*60*24*365, domain='.teamexist.com')
            return resp
        else:
            logging.warning(f"[Gatekeeper] FAILED LOGIN -> {ip} entered incorrect password.")
            
    return render_template('login.html', redirect_url=redirect_url)

@app.route('/admin', methods=['GET', 'POST'])
def admin():
    """Admin dashboard."""
    ip = get_client_ip()
    
    if request.method == 'POST':
        if request.form.get('password') == ADMIN_PASSWORD:
            logging.info(f"[Gatekeeper] ADMIN ACCESS GRANTED -> {ip} entered correct admin password.")
            resp = make_response(redirect(url_for('admin')))
            resp.set_cookie('dws_admin', 'granted', max_age=60*60*24, domain='.teamexist.com')
            return resp
        else:
            logging.warning(f"[Gatekeeper] FAILED ADMIN LOGIN -> {ip} entered incorrect admin password.")
            
    if request.cookies.get('dws_admin') != 'granted':
        return render_template('login.html', is_admin=True)
        
    return render_template('admin.html', data=load_data())

@app.route('/api/action', methods=['POST'])
def admin_action():
    """Handles allow/ban/revoke actions from the admin panel."""
    admin_ip = get_client_ip()
    if request.cookies.get('dws_admin') != 'granted':
        logging.error(f"[Gatekeeper] UNAUTHORIZED API ACTION -> {admin_ip} attempted to execute an admin command without a valid cookie.")
        return jsonify({"error": "Unauthorized"}), 401
        
    action = request.json.get('action')
    target_ip = request.json.get('ip')
    name = request.json.get('name', 'Manually Added')
    data = load_data()
    
    if action == 'allow':
        logging.info(f"[Gatekeeper] ADMIN ACTION -> {admin_ip} manually ALLOWED {target_ip} ({name}).")
        data['allowed_ips'][target_ip] = {"name": name, "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        if target_ip in data['attempts']: del data['attempts'][target_ip]
    elif action == 'revoke':
        logging.info(f"[Gatekeeper] ADMIN ACTION -> {admin_ip} REVOKED access for {target_ip}.")
        if target_ip in data['allowed_ips']: del data['allowed_ips'][target_ip]
    elif action == 'ban':
        logging.info(f"[Gatekeeper] ADMIN ACTION -> {admin_ip} BANNED {target_ip}.")
        data['banned_ips'][target_ip] = {"time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        if target_ip in data['allowed_ips']: del data['allowed_ips'][target_ip]
        if target_ip in data['attempts']: del data['attempts'][target_ip]
    elif action == 'unban':
        logging.info(f"[Gatekeeper] ADMIN ACTION -> {admin_ip} UNBANNED {target_ip}.")
        if target_ip in data['banned_ips']: del data['banned_ips'][target_ip]
        
    save_data(data)
    return jsonify({"status": "success"})

if __name__ == '__main__':
    logging.info("[Gatekeeper] Gatekeeper Auth Module is ONLINE and listening on port 5050.")
    app.run(host='0.0.0.0', port=5050)
