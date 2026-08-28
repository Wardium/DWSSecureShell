from flask import Flask, request, render_template, redirect, url_for, make_response, jsonify
import json
import os
import logging
import sys
import uuid
import requests
from datetime import datetime, timedelta

# Define your allowed whitelist
ALLOWED_ORIGINS = [
    "https://teamexist.com",
    "https://www.teamexist.com"
]


# --- LOGGING SETUP ---
logging.basicConfig(
    stream=sys.stdout, 
    level=logging.INFO, 
    format='[%(asctime)s] %(levelname)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

app = Flask(__name__)
app.secret_key = 'weqr1234'

# --- CONFIGURATION ---
USER_PASSWORD = "weqr1234"
ADMIN_PASSWORD = "dws13125851313241086670"
DATA_FILE = 'data/access_log.json'

TRUSTED_IPS = [
    "127.0.0.1",      
    "192.168.2.91",   
    "172.18.0.1"      
]

# --- HELPER FUNCTIONS FOR GEOLOCATION ---
def get_ip_location(ip):
    if ip.startswith(('192.168.', '10.', '127.', '172.')):
        return "Local Network"
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Firefox/123.0'}
        geo_resp = requests.get(f"http://ip-api.com/json/{ip}?fields=status,country,city", headers=headers, timeout=2).json()
        if geo_resp.get('status') == 'success':
            return f"{geo_resp.get('city')}, {geo_resp.get('country')}"
    except Exception:
        pass
    return "Unknown Location"

def get_gps_address(lat, lon):
    try:
        headers = {'User-Agent': 'DWSGatekeeperAuth/1.0'}
        url = f"https://nominatim.openstreetmap.org/reverse?format=json&lat={lat}&lon={lon}&zoom=18&addressdetails=1"
        res = requests.get(url, headers=headers, timeout=3).json()
        
        address = res.get('address', {})
        road = address.get('road', '')
        house_num = address.get('house_number', '')
        city = address.get('city') or address.get('town') or address.get('village', '')
        state = address.get('state', '')
        
        formatted = f"{house_num} {road}, {city}, {state}".strip().strip(',')
        return formatted if formatted else res.get('display_name', f"GPS: {lat}, {lon}")
    except Exception as e:
        logging.error(f"[Gatekeeper] Reverse Geocode error: {e}")
        return f"GPS: {lat[:7]}, {lon[:7]}"

# --- DATABASE MANAGEMENT (CRASH-PROOF) ---
os.makedirs('data', exist_ok=True)

def cleanup_old_attempts(data):
    now = datetime.now()
    expired_ips = []
    
    for ip, info in list(data.get('attempts', {}).items()):
        try:
            last_attempt = datetime.strptime(info['time'], "%Y-%m-%d %H:%M:%S")
            if now - last_attempt > timedelta(minutes=5):
                expired_ips.append(ip)
        except (ValueError, KeyError, TypeError):
            expired_ips.append(ip)
            
    for ip in expired_ips:
        data['attempts'].pop(ip, None)
        
    return data

def load_data():
    default_structure = {"allowed_ips": {}, "banned_ips": {}, "attempts": {}, "active_sessions": {}}
    
    if not os.path.exists(DATA_FILE):
        return default_structure
        
    try:
        with open(DATA_FILE, 'r') as f:
            data = json.load(f)
    except Exception as e:
        logging.warning(f"[Gatekeeper] Database corrupted or empty ({e}). Rebuilding clean state...")
        return default_structure
        
    if not isinstance(data, dict):
        return default_structure
        
    for key in default_structure:
        if key not in data:
            data[key] = {}
            
    return cleanup_old_attempts(data)

def save_data(data):
    data = cleanup_old_attempts(data)
    # Generate a unique temp file name so concurrent workers don't collide
    temp_file = f"{DATA_FILE}.{uuid.uuid4().hex}.tmp"
    try:
        with open(temp_file, 'w') as f:
            json.dump(data, f, indent=4)
        # Atomically replace the main file
        os.replace(temp_file, DATA_FILE)
    except Exception as e:
        logging.error(f"[Gatekeeper] Failed to save database atomically: {e}")
        # Clean up the dangling temp file if something went wrong
        if os.path.exists(temp_file):
            os.remove(temp_file)

def get_client_ip():
    forwarded_ip = request.headers.get('X-Original-Remote-Addr')
    if not forwarded_ip:
        forwarded_ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    return forwarded_ip.split(',')[0].strip()

# --- ROUTES ---

@app.route('/')
def index():
    return redirect(url_for('login'))

@app.route('/auth')
def auth():
    ip = get_client_ip()
    target_url = request.headers.get('X-Original-URI', 'Unknown URL')
    
    if ip in TRUSTED_IPS or ip.startswith(('172.', '10.', '192.168.')):
        return "OK", 200

    data = load_data()
    
    if ip in data['banned_ips']:
        return "Banned", 403
        
    session_id = request.cookies.get('dws_auth')
    if ip in data['allowed_ips'] or (session_id and session_id in data['active_sessions']):
        return "OK", 200
    
    if ip not in data['attempts']:
        data['attempts'][ip] = {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), 
            "count": 1,
            "location": "📍 " + get_ip_location(ip)
        }
    else:
        data['attempts'][ip]['count'] += 1
        data['attempts'][ip]['time'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
    save_data(data)
    return "Unauthorized", 401

@app.route('/login', methods=['GET', 'POST'])
def login():
    redirect_url = request.args.get('redirect', '')
    ip = get_client_ip()
    data = load_data()
    
    session_id = request.cookies.get('dws_auth')
    invalid_cookie = bool(session_id and session_id not in data['active_sessions'])

    if request.method == 'POST':
        password = request.form.get('password')
        if password == USER_PASSWORD:
            new_session_id = str(uuid.uuid4())
            user_agent = request.headers.get('User-Agent', 'Unknown Browser')
            
            lat = request.form.get('lat')
            lon = request.form.get('lon')
            
            if lat and lon and lat.strip() and lon.strip():
                location_str = "🎯 " + get_gps_address(lat, lon)
            else:
                location_str = "📍 " + get_ip_location(ip)

            data['active_sessions'][new_session_id] = {
                "ip": ip,
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "location": location_str,
                "device_info": user_agent[:40] + "..." if len(user_agent) > 40 else user_agent
            }
            
            if ip in data['attempts']:
                del data['attempts'][ip]
            save_data(data)
            
            logging.info(f"[Gatekeeper] LOGIN SUCCESS -> Issued Session ID to {ip} ({location_str})")
            
            resp = make_response(redirect(redirect_url if redirect_url else '/'))
            resp.set_cookie('dws_auth', new_session_id, max_age=60*60*24*365, domain='.teamexist.com', samesite='None', secure=True)
            return resp
        else:
            logging.warning(f"[Gatekeeper] LOGIN FAILED -> Bad password from {ip}")

    resp = make_response(render_template('login.html', redirect_url=redirect_url))
    
    if invalid_cookie:
        resp.set_cookie('dws_auth', '', expires=0, domain='.teamexist.com', samesite='None', secure=True)
        logging.info(f"[Gatekeeper] Deleted revoked cookie from browser at IP: {ip}")
        
    return resp

def check_token(token):
    print(f"[Gatekeeper] 3. Checking token string: {token}", flush=True)
    
    if not token:
        print("[Gatekeeper] -> Result: FAILED. No token was provided by the browser.", flush=True)
        return False
        
    log_path = os.path.join("data", "access_log.json")
    
    try:
        with open(log_path, "r") as file:
            log_data = json.load(file)
            
        active_sessions = log_data.get("active_sessions", {})
        
        if token in active_sessions:
            # We can even pull data from the JSON file to confirm who it matched!
            session_info = active_sessions[token]
            matched_ip = session_info.get('ip', 'Unknown IP')
            print(f"[Gatekeeper] -> Result: SUCCESS. Token matched active session for IP: {matched_ip}", flush=True)
            return True
        else:
            print("[Gatekeeper] -> Result: FAILED. Token not found in active_sessions dictionary.", flush=True)
            
    except FileNotFoundError:
        print(f"[Gatekeeper] -> Error: Could not find {log_path}", flush=True)
    except json.JSONDecodeError:
        print(f"[Gatekeeper] -> Error: {log_path} contains invalid JSON", flush=True)
        
    return False

@app.route('/api/verify', methods=['POST', 'OPTIONS'])
def verify_access():
    print(f"\n[Gatekeeper] 1. --- New Authorization Request ---", flush=True)
    
    origin = request.headers.get('Origin')
    
    # --- 1. Handle the Browser's Preflight Check ---
    if request.method == 'OPTIONS':
        response = make_response()
        if origin in ALLOWED_ORIGINS:
            response.headers.add("Access-Control-Allow-Origin", origin)
            response.headers.add("Access-Control-Allow-Headers", "Content-Type")
            response.headers.add("Access-Control-Allow-Methods", "POST, OPTIONS")
            response.headers.add("Access-Control-Allow-Credentials", "true")
        return response
        
    # --- 2. Standard POST Processing ---
    client_ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    print(f"[Gatekeeper] 2. Request from IP: {client_ip} | Origin: {origin}", flush=True)

    session_token = request.cookies.get('dws_auth_token')
    is_valid = check_token(session_token) 

    if is_valid:
        print("[Gatekeeper] 4. Access GRANTED.", flush=True)
        # Remember to update this to your actual dashboard URL
        response = jsonify({
            "authorized": True,
            "dashboard_url": "https://dashboard-rfdtq2xvdwq.teamexist.com" 
        })
    else:
        print("[Gatekeeper] 4. Access DENIED.", flush=True)
        response = jsonify({
            "authorized": False
        })
        
    # --- 3. Force the Headers on the Final Output ---
    if origin in ALLOWED_ORIGINS:
        response.headers.add("Access-Control-Allow-Origin", origin)
        response.headers.add("Access-Control-Allow-Credentials", "true")
        
    return response


@app.route('/admin', methods=['GET', 'POST'])
def admin():
    if request.method == 'POST':
        if request.form.get('password') == ADMIN_PASSWORD:
            resp = make_response(redirect(url_for('admin')))
            resp.set_cookie('dws_admin', 'granted', max_age=60*60*24, domain='.teamexist.com', samesite='None', secure=True)
            return resp
            
    if request.cookies.get('dws_admin') != 'granted':
        return render_template('login.html', is_admin=True)
        
    return render_template('admin.html', data=load_data())

@app.route('/api/action', methods=['POST'])
def admin_action():
    if request.cookies.get('dws_admin') != 'granted':
        return jsonify({"error": "Unauthorized"}), 401
        
    action = request.json.get('action')
    target_ip = request.json.get('ip')
    session_id = request.json.get('session_id')
    name = request.json.get('name', 'Manually Added')
    
    data = load_data()
    
    if action == 'revoke_session' and session_id in data['active_sessions']:
        del data['active_sessions'][session_id]
        logging.info(f"[Gatekeeper] Revoked Active Session: {session_id}")
        
    elif action == 'allow':
        data['allowed_ips'][target_ip] = {"name": name, "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        if target_ip in data['attempts']: del data['attempts'][target_ip]
    elif action == 'revoke':
        if target_ip in data['allowed_ips']: del data['allowed_ips'][target_ip]
    elif action == 'ban':
        data['banned_ips'][target_ip] = {"time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        if target_ip in data['allowed_ips']: del data['allowed_ips'][target_ip]
        if target_ip in data['attempts']: del data['attempts'][target_ip]
        
    save_data(data)
    return jsonify({"status": "success"})

if __name__ == '__main__':
    logging.info("[Gatekeeper] Gatekeeper Auth Module is ONLINE and listening on port 5050.")
    app.run(host='0.0.0.0', port=5050)
