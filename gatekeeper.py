from flask import Flask, request, render_template, redirect, url_for, make_response, jsonify
import json
import os
import logging
import sys
import uuid
import time
import requests
from datetime import datetime, timedelta

# Define your allowed whitelist
ALLOWED_ORIGINS = [
    "https://teamexist.com",
    "https://www.teamexist.com",
    "https://cloud.teamexist.com"
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

DEFAULT_KEYS = {
    "allowed_ips": {},
    "banned_ips": {},
    "attempts": {},
    "active_sessions": {},
    "waiting_logins": {},
    "trusted_active": {},
    "watch_peers": {},
    "watch_events": []
}

def cleanup_old_attempts(data):
    now = datetime.now()
    now_ts = time.time()

    # 1. Clean up expired attempts (5 minutes)
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

    # 2. Clean up stale lock-screen waiting states (older than 120s -> auto-fly back to cloud)
    for ip, info in list(data.get('waiting_logins', {}).items()):
        if now_ts - info.get('ts', 0) > 120:
            data['waiting_logins'].pop(ip, None)
            data['watch_events'].append({
                "id": uuid.uuid4().hex[:12],
                "ts": now_ts,
                "time": now.strftime("%H:%M:%S"),
                "ip": ip,
                "type": "login_timeout",
                "endpoint": "/login",
                "status": "LOCK SCREEN TIMEOUT",
                "location": info.get("location", "📍 Unknown"),
                "device_info": info.get("device_info", "Unknown Browser"),
                "target": info.get("target", "")
            })

    # 3. Keep only the last 60 real-time events (and prune events older than 120s)
    events = data.get('watch_events', [])
    if isinstance(events, list):
        events = [e for e in events if now_ts - e.get('ts', 0) < 120][-60:]
        data['watch_events'] = events
    else:
        data['watch_events'] = []

    # 4. Cap watch_peers history at 120 known external IPs
    peers = data.get('watch_peers', {})
    if len(peers) > 120:
        sorted_ips = sorted(peers.keys(), key=lambda k: peers[k].get('ts', 0), reverse=True)
        data['watch_peers'] = {k: peers[k] for k in sorted_ips[:120]}

    return data

def load_data():
    if not os.path.exists(DATA_FILE):
        return {k: (list(v) if isinstance(v, list) else dict(v)) for k, v in DEFAULT_KEYS.items()}
        
    try:
        with open(DATA_FILE, 'r') as f:
            data = json.load(f)
    except Exception as e:
        logging.warning(f"[Gatekeeper] Database corrupted or empty ({e}). Rebuilding clean state...")
        return {k: (list(v) if isinstance(v, list) else dict(v)) for k, v in DEFAULT_KEYS.items()}
        
    if not isinstance(data, dict):
        return {k: (list(v) if isinstance(v, list) else dict(v)) for k, v in DEFAULT_KEYS.items()}
        
    for key, default_val in DEFAULT_KEYS.items():
        if key not in data or not isinstance(data[key], type(default_val)):
            data[key] = list(default_val) if isinstance(default_val, list) else dict(default_val)
            
    return cleanup_old_attempts(data)

def save_data(data):
    data = cleanup_old_attempts(data)
    temp_file = f"{DATA_FILE}.{uuid.uuid4().hex}.tmp"
    try:
        with open(temp_file, 'w') as f:
            json.dump(data, f, indent=4)
        os.replace(temp_file, DATA_FILE)
    except Exception as e:
        logging.error(f"[Gatekeeper] Failed to save database atomically: {e}")
        if os.path.exists(temp_file):
            os.remove(temp_file)

def get_client_ip():
    forwarded_ip = request.headers.get('X-Original-Remote-Addr')
    if not forwarded_ip:
        forwarded_ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    return (forwarded_ip or "127.0.0.1").split(',')[0].strip()

def short_ua():
    ua = request.headers.get('User-Agent', 'Unknown Browser')
    return ua[:45] + "..." if len(ua) > 45 else ua

def resolve_cached_location(data, ip):
    """Uses cached location if available so /auth and /api/verify stay fast."""
    if ip in data.get('watch_peers', {}) and data['watch_peers'][ip].get('location'):
        return data['watch_peers'][ip]['location']
    if ip in data.get('attempts', {}) and data['attempts'][ip].get('location'):
        return data['attempts'][ip]['location']
    for s in data.get('active_sessions', {}).values():
        if s.get('ip') == ip and s.get('location'):
            return s['location']
    return "📍 " + get_ip_location(ip)

def record_watch_event(data, ip, event_type, endpoint, status, target="", location=None, session_id=None, throttle_sec=0):
    """Appends a real network event for the /watch visualizer and updates peer metadata."""
    now_ts = time.time()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ua = short_ua()
    loc = location or resolve_cached_location(data, ip)

    peer = data['watch_peers'].get(ip, {})
    last_event_ts = peer.get('last_event_ts', 0)
    last_event_type = peer.get('last_event_type', '')

    # Always update known peer metadata
    data['watch_peers'][ip] = {
        "ip": ip,
        "location": loc,
        "device_info": ua,
        "last_endpoint": endpoint,
        "last_status": status,
        "target": target or peer.get('target', ''),
        "time": now_str,
        "ts": now_ts,
        "hit_count": peer.get('hit_count', 0) + 1,
        "last_event_ts": last_event_ts,
        "last_event_type": last_event_type
    }

    # Throttle burst subrequests (e.g. Nginx auth_request firing 15 times on a single page load)
    if throttle_sec > 0 and last_event_type == event_type and (now_ts - last_event_ts) < throttle_sec:
        return False

    data['watch_peers'][ip]['last_event_ts'] = now_ts
    data['watch_peers'][ip]['last_event_type'] = event_type

    data['watch_events'].append({
        "id": uuid.uuid4().hex[:12],
        "ts": now_ts,
        "time": datetime.now().strftime("%H:%M:%S"),
        "ip": ip,
        "type": event_type,  # 'admitted', 'rejected', 'waiting_login', 'activity', 'evicted', 'login_timeout'
        "endpoint": endpoint,
        "status": status,
        "location": loc,
        "device_info": ua,
        "target": target,
        "session_id": session_id
    })
    return True


# --- ROUTES ---

@app.route('/')
def index():
    return redirect(url_for('login'))

@app.route('/watch')
def watch_dashboard():
    """Zero-option live traffic visualizer."""
    is_admin = (request.cookies.get('dws_admin') == 'granted')
    return render_template('watch.html', is_admin=is_admin)

@app.route('/api/watch/state')
def api_watch_state():
    """Returns 100% real server state and real-time events since the client's last cursor."""
    data = load_data()
    since_ts = request.args.get('since', default=0.0, type=float)

    # 1. Build list of everything currently connected INSIDE HomeBase
    inside_nodes = []
    seen_inside_ips = set()

    # Active authenticated cookie sessions
    for sid, info in data.get('active_sessions', {}).items():
        ip = info.get('ip', 'Unknown')
        seen_inside_ips.add(ip)
        peer = data.get('watch_peers', {}).get(ip, {})
        inside_nodes.append({
            "key": f"session:{sid}",
            "session_id": sid,
            "ip": ip,
            "kind": "SESSION",
            "location": info.get('location', '📍 Unknown'),
            "device_info": info.get('device_info', 'Unknown Browser'),
            "time": info.get('time', ''),
            "last_endpoint": peer.get('last_endpoint', '/login'),
            "target": peer.get('target', 'https://cloud.teamexist.com'),
            "hit_count": peer.get('hit_count', 1)
        })

    # Whitelisted IPs
    for ip, info in data.get('allowed_ips', {}).items():
        if ip in seen_inside_ips:
            continue
        seen_inside_ips.add(ip)
        peer = data.get('watch_peers', {}).get(ip, {})
        inside_nodes.append({
            "key": f"allowed:{ip}",
            "session_id": None,
            "ip": ip,
            "kind": f"WHITELIST ({info.get('name', 'Allowed')})",
            "location": peer.get('location') or ("📍 " + get_ip_location(ip)),
            "device_info": peer.get('device_info', 'Whitelisted Host'),
            "time": info.get('time', ''),
            "last_endpoint": peer.get('last_endpoint', '/auth'),
            "target": peer.get('target', ''),
            "hit_count": peer.get('hit_count', 1)
        })

    # Trusted local/LAN IPs that have actively passed through /auth
    for ip, info in data.get('trusted_active', {}).items():
        if ip in seen_inside_ips:
            continue
        seen_inside_ips.add(ip)
        inside_nodes.append({
            "key": f"trusted:{ip}",
            "session_id": None,
            "ip": ip,
            "kind": "TRUSTED LAN",
            "location": info.get('location', '📍 Local Network'),
            "device_info": info.get('device_info', 'LAN Host'),
            "time": info.get('time', ''),
            "last_endpoint": info.get('endpoint', '/auth'),
            "target": info.get('target', ''),
            "hit_count": info.get('hit_count', 1)
        })

    # 2. Build list of nodes currently parked at the HomeBase perimeter (/login screen)
    waiting_nodes = []
    for ip, info in data.get('waiting_logins', {}).items():
        if ip not in seen_inside_ips:
            waiting_nodes.append({
                "ip": ip,
                "location": info.get('location', '📍 Unknown'),
                "device_info": info.get('device_info', 'Unknown Browser'),
                "time": info.get('time', ''),
                "target": info.get('target', ''),
                "ts": info.get('ts', 0)
            })

    # 3. Build list of real external peers in the outer cloud (attempts, banned, previous visitors)
    outside_peers = {}
    for ip, info in data.get('watch_peers', {}).items():
        if ip not in seen_inside_ips:
            outside_peers[ip] = {
                "ip": ip,
                "state": "BANNED" if ip in data.get('banned_ips', {}) else ("ATTEMPTING" if ip in data.get('attempts', {}) else "OUTSIDE PEER"),
                "location": info.get('location', '📍 Unknown'),
                "device_info": info.get('device_info', 'Unknown Browser'),
                "last_endpoint": info.get('last_endpoint', '/auth'),
                "last_status": info.get('last_status', 'Observed'),
                "target": info.get('target', ''),
                "time": info.get('time', ''),
                "attempts": data.get('attempts', {}).get(ip, {}).get('count', 0),
                "hit_count": info.get('hit_count', 1)
            }

    for ip, info in data.get('attempts', {}).items():
        if ip not in seen_inside_ips and ip not in outside_peers:
            outside_peers[ip] = {
                "ip": ip,
                "state": "ATTEMPTING",
                "location": info.get('location', '📍 Unknown'),
                "device_info": "Unauthorized Client",
                "last_endpoint": "/auth",
                "last_status": "401 Unauthorized",
                "target": "",
                "time": info.get('time', ''),
                "attempts": info.get('count', 1),
                "hit_count": info.get('count', 1)
            }

    for ip, info in data.get('banned_ips', {}).items():
        if ip not in seen_inside_ips and ip not in outside_peers:
            outside_peers[ip] = {
                "ip": ip,
                "state": "BANNED",
                "location": "📍 " + get_ip_location(ip),
                "device_info": "Banned Host",
                "last_endpoint": "/auth",
                "last_status": "403 Banned",
                "target": "",
                "time": info.get('time', ''),
                "attempts": 0,
                "hit_count": 1
            }

    new_events = [e for e in data.get('watch_events', []) if e.get('ts', 0) > since_ts]

    return jsonify({
        "server_ts": time.time(),
        "inside": inside_nodes,
        "waiting": waiting_nodes,
        "outside": list(outside_peers.values()),
        "events": new_events
    })

@app.route('/api/watch/leave', methods=['POST'])
def api_watch_leave():
    """Called via navigator.sendBeacon if someone closes /login without logging in."""
    ip = get_client_ip()
    data = load_data()
    if ip in data.get('waiting_logins', {}):
        info = data['waiting_logins'].pop(ip, {})
        record_watch_event(
            data, ip,
            event_type="rejected",
            endpoint="/login (closed)",
            status="CLOSED LOCK SCREEN",
            target=info.get('target', ''),
            location=info.get('location')
        )
        save_data(data)
    return "", 204

@app.route('/auth')
def auth():
    ip = get_client_ip()
    target_url = request.headers.get('X-Original-URI', 'Unknown URL')

    # Do not trigger self-animations when loading /watch or polling /api/watch
    if target_url.startswith(('/watch', '/api/watch')):
        return "OK", 200

    data = load_data()

    if ip in TRUSTED_IPS or ip.startswith(('172.', '10.', '192.168.')):
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        was_known = ip in data['trusted_active']
        prev_hits = data['trusted_active'].get(ip, {}).get('hit_count', 0)
        data['trusted_active'][ip] = {
            "ip": ip,
            "location": "📍 Local Network",
            "device_info": short_ua(),
            "time": now_str,
            "endpoint": "/auth",
            "target": target_url,
            "hit_count": prev_hits + 1
        }
        changed = record_watch_event(
            data, ip,
            event_type="activity" if was_known else "admitted",
            endpoint="/auth",
            status="TRUSTED IP OK (200)",
            target=target_url,
            location="📍 Local Network",
            throttle_sec=4 if was_known else 0
        )
        if not was_known or changed:
            save_data(data)
        return "OK", 200

    if ip in data['banned_ips']:
        if record_watch_event(data, ip, "rejected", "/auth", "BANNED IP (403)", target=target_url, throttle_sec=2):
            save_data(data)
        return "Banned", 403
        
    session_id = request.cookies.get('dws_auth')
    if ip in data['allowed_ips'] or (session_id and session_id in data['active_sessions']):
        if record_watch_event(data, ip, "activity", "/auth", "AUTHORIZED (200)", target=target_url, session_id=session_id, throttle_sec=4):
            save_data(data)
        return "OK", 200
    
    loc = resolve_cached_location(data, ip)
    if ip not in data['attempts']:
        data['attempts'][ip] = {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), 
            "count": 1,
            "location": loc
        }
    else:
        data['attempts'][ip]['count'] += 1
        data['attempts'][ip]['time'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
    record_watch_event(
        data, ip,
        event_type="rejected",
        endpoint="/auth",
        status=f"STOPPED BY GATEKEEPER (Attempt #{data['attempts'][ip]['count']})",
        target=target_url,
        location=loc,
        throttle_sec=2
    )
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
        data['waiting_logins'].pop(ip, None)

        if password == USER_PASSWORD:
            new_session_id = str(uuid.uuid4())
            user_agent = request.headers.get('User-Agent', 'Unknown Browser')
            
            lat = request.form.get('lat')
            lon = request.form.get('lon')
            
            if lat and lon and lat.strip() and lon.strip():
                location_str = "🎯 " + get_gps_address(lat, lon)
            else:
                location_str = resolve_cached_location(data, ip)

            data['active_sessions'][new_session_id] = {
                "ip": ip,
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "location": location_str,
                "device_info": user_agent[:40] + "..." if len(user_agent) > 40 else user_agent
            }
            
            if ip in data['attempts']:
                del data['attempts'][ip]

            record_watch_event(
                data, ip,
                event_type="admitted",
                endpoint="/login",
                status="PASSWORD ACCEPTED -> SESSION ISSUED",
                target=redirect_url or "/",
                location=location_str,
                session_id=new_session_id
            )
            save_data(data)
            
            logging.info(f"[Gatekeeper] LOGIN SUCCESS -> Issued Session ID to {ip} ({location_str})")
            
            resp = make_response(redirect(redirect_url if redirect_url else '/'))
            resp.set_cookie('dws_auth', new_session_id, max_age=60*60*24*365, domain='.teamexist.com', samesite='None', secure=True)
            return resp
        else:
            loc = resolve_cached_location(data, ip)
            if ip not in data['attempts']:
                data['attempts'][ip] = {"time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "count": 1, "location": loc}
            else:
                data['attempts'][ip]['count'] += 1
                data['attempts'][ip]['time'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            record_watch_event(
                data, ip,
                event_type="rejected",
                endpoint="/login",
                status="WRONG PASSWORD",
                target=redirect_url or "/login",
                location=loc
            )
            save_data(data)
            logging.warning(f"[Gatekeeper] LOGIN FAILED -> Bad password from {ip}")

    else:
        # GET /login -> Visitor is sitting on the lock screen waiting to enter password
        loc = resolve_cached_location(data, ip)
        data['waiting_logins'][ip] = {
            "ip": ip,
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "ts": time.time(),
            "location": loc,
            "device_info": short_ua(),
            "target": redirect_url or "/login"
        }
        record_watch_event(
            data, ip,
            event_type="waiting_login",
            endpoint="/login",
            status="AT LOCK SCREEN (AWAITING PASSWORD)",
            target=redirect_url or "/login",
            location=loc,
            throttle_sec=2
        )
        save_data(data)

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
    client_ip = get_client_ip()
    print(f"[Gatekeeper] 2. Request from IP: {client_ip} | Origin: {origin}", flush=True)

    session_token = request.cookies.get('dws_auth')
    has_cookie = session_token is not None
    is_valid = check_token(session_token) 

    data = load_data()

    if is_valid:
        print("[Gatekeeper] 4. Access GRANTED.", flush=True)
        record_watch_event(
            data, client_ip,
            event_type="activity",
            endpoint="/api/verify",
            status="API VERIFY GRANTED",
            target=origin or "API",
            session_id=session_token,
            throttle_sec=2
        )
        save_data(data)
        response = jsonify({
            "authorized": True,
            "dashboard_url": "https://cloud.teamexist.com",
            "debug": {
                "cookie_received": has_cookie,
                "origin_seen": origin,
                "status": "Valid dws_auth token!"
            }
        })
    else:
        reason = "Cookie 'dws_auth' was received, but check_token() returned False." if has_cookie else "No 'dws_auth' cookie was sent by the browser."
        print(f"[Gatekeeper] 4. Access DENIED. ({reason})", flush=True)
        record_watch_event(
            data, client_ip,
            event_type="rejected",
            endpoint="/api/verify",
            status="API VERIFY DENIED",
            target=origin or "API",
            throttle_sec=1
        )
        save_data(data)
        response = jsonify({
            "authorized": False,
            "debug": {
                "cookie_received": has_cookie,
                "origin_seen": origin,
                "reason": reason
            }
        })
        
    # --- 3. Force the Headers on the Final Output ---
    if origin in ALLOWED_ORIGINS:
        response.headers.add("Access-Control-Allow-Origin", origin)
        response.headers.add("Access-Control-Allow-Credentials", "true")
        
    return response

# --- API AUTHENTICATION ROUTES ---

@app.route('/api/login', methods=['POST', 'OPTIONS'])
def api_login():
    origin = request.headers.get('Origin')
    
    # Handle Preflight Check
    if request.method == 'OPTIONS':
        response = make_response()
        if origin in ALLOWED_ORIGINS:
            response.headers.add("Access-Control-Allow-Origin", origin)
            response.headers.add("Access-Control-Allow-Headers", "Content-Type")
            response.headers.add("Access-Control-Allow-Methods", "POST, OPTIONS")
            response.headers.add("Access-Control-Allow-Credentials", "true")
        return response

    req_data = request.get_json(silent=True) or {}
    password = req_data.get('password')
    ip = get_client_ip()
    data = load_data()
    data['waiting_logins'].pop(ip, None)
    
    if password == USER_PASSWORD:
        new_session_id = str(uuid.uuid4())
        user_agent = request.headers.get('User-Agent', 'Unknown Browser')
        
        lat = req_data.get('lat')
        lon = req_data.get('lon')
        
        if lat and lon and str(lat).strip() and str(lon).strip():
            location_str = "🎯 " + get_gps_address(str(lat), str(lon))
        else:
            location_str = resolve_cached_location(data, ip)

        data['active_sessions'][new_session_id] = {
            "ip": ip,
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "location": location_str,
            "device_info": user_agent[:40] + "..." if len(user_agent) > 40 else user_agent
        }
        
        if ip in data['attempts']:
            del data['attempts'][ip]

        record_watch_event(
            data, ip,
            event_type="admitted",
            endpoint="/api/login",
            status="API LOGIN SUCCESS",
            target=origin or "API",
            location=location_str,
            session_id=new_session_id
        )
        save_data(data)
        
        logging.info(f"[Gatekeeper] API LOGIN SUCCESS -> Issued Session ID to {ip}")
        
        response = jsonify({
            "status": "success", 
            "message": "Authentication successful",
            "token": new_session_id
        })
        response.set_cookie('dws_auth', new_session_id, max_age=60*60*24*365, domain='.teamexist.com', samesite='None', secure=True)
        
    else:
        loc = resolve_cached_location(data, ip)
        if ip not in data['attempts']:
            data['attempts'][ip] = {"time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "count": 1, "location": loc}
        else:
            data['attempts'][ip]['count'] += 1
            data['attempts'][ip]['time'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        record_watch_event(
            data, ip,
            event_type="rejected",
            endpoint="/api/login",
            status="API LOGIN BAD PASSWORD",
            target=origin or "API",
            location=loc
        )
        save_data(data)
        logging.warning(f"[Gatekeeper] API LOGIN FAILED -> Bad password from {ip}")
        response = jsonify({"status": "error", "message": "Invalid password"})
        response.status_code = 401

    if origin in ALLOWED_ORIGINS:
        response.headers.add("Access-Control-Allow-Origin", origin)
        response.headers.add("Access-Control-Allow-Credentials", "true")
        
    return response

@app.route('/api/status', methods=['GET', 'OPTIONS'])
def api_status():
    origin = request.headers.get('Origin')
    
    if request.method == 'OPTIONS':
        response = make_response()
        if origin in ALLOWED_ORIGINS:
            response.headers.add("Access-Control-Allow-Origin", origin)
            response.headers.add("Access-Control-Allow-Methods", "GET, OPTIONS")
            response.headers.add("Access-Control-Allow-Credentials", "true")
        return response

    session_token = request.cookies.get('dws_auth')
    is_valid = check_token(session_token)
    
    response = jsonify({"logged_in": is_valid})
    
    if origin in ALLOWED_ORIGINS:
        response.headers.add("Access-Control-Allow-Origin", origin)
        response.headers.add("Access-Control-Allow-Credentials", "true")
        
    return response

@app.route('/api/logout', methods=['POST', 'OPTIONS'])
def api_logout():
    origin = request.headers.get('Origin')
    
    if request.method == 'OPTIONS':
        response = make_response()
        if origin in ALLOWED_ORIGINS:
            response.headers.add("Access-Control-Allow-Origin", origin)
            response.headers.add("Access-Control-Allow-Methods", "POST, OPTIONS")
            response.headers.add("Access-Control-Allow-Credentials", "true")
        return response

    ip = get_client_ip()
    session_token = request.cookies.get('dws_auth')
    if session_token:
        data = load_data()
        if session_token in data['active_sessions']:
            session_info = data['active_sessions'].pop(session_token)
            record_watch_event(
                data, session_info.get('ip', ip),
                event_type="evicted",
                endpoint="/api/logout",
                status="LOGGED OUT",
                target=origin or "API",
                location=session_info.get('location'),
                session_id=session_token
            )
            save_data(data)
            logging.info(f"[Gatekeeper] API LOGOUT -> Revoked Session ID: {session_token}")
            
    response = jsonify({"status": "success", "message": "Logged out successfully"})
    response.set_cookie('dws_auth', '', expires=0, domain='.teamexist.com', samesite='None', secure=True)
    
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
        info = data['active_sessions'].pop(session_id)
        record_watch_event(
            data, info.get('ip', target_ip or 'Unknown'),
            event_type="evicted",
            endpoint="/api/action",
            status="SESSION REVOKED BY ADMIN",
            location=info.get('location'),
            session_id=session_id
        )
        logging.info(f"[Gatekeeper] Revoked Active Session: {session_id}")
        
    elif action == 'allow' and target_ip:
        data['allowed_ips'][target_ip] = {"name": name, "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        data['attempts'].pop(target_ip, None)
        data['banned_ips'].pop(target_ip, None)
        record_watch_event(
            data, target_ip,
            event_type="admitted",
            endpoint="/api/action",
            status=f"WHITELISTED ({name})"
        )
    elif action == 'revoke' and target_ip:
        data['allowed_ips'].pop(target_ip, None)
        data['trusted_active'].pop(target_ip, None)
        record_watch_event(
            data, target_ip,
            event_type="evicted",
            endpoint="/api/action",
            status="WHITELIST REVOKED"
        )
    elif action == 'ban' and target_ip:
        data['banned_ips'][target_ip] = {"time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        data['allowed_ips'].pop(target_ip, None)
        data['attempts'].pop(target_ip, None)
        data['trusted_active'].pop(target_ip, None)
        for sid in [k for k, v in data['active_sessions'].items() if v.get('ip') == target_ip]:
            data['active_sessions'].pop(sid, None)
        record_watch_event(
            data, target_ip,
            event_type="rejected",
            endpoint="/api/action",
            status="BANNED BY ADMIN"
        )
        
    save_data(data)
    return jsonify({"status": "success"})

if __name__ == '__main__':
    logging.info("[Gatekeeper] Gatekeeper Auth Module is ONLINE and listening on port 5050.")
    app.run(host='0.0.0.0', port=5050)
