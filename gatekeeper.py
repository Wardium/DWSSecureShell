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
    "https://www.teamexist.com",
    "https://cloud.teamexist.com",
    "https://auth.teamexist.com"
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
MAX_EVENTS = 100

TRUSTED_IPS = [
    "127.0.0.1",
    "192.168.2.91",
    "172.18.0.1"
]

# --- HELPER FUNCTIONS FOR GEOLOCATION ---
def get_ip_location(ip, data=None):
    if ip.startswith(('192.168.', '10.', '127.', '172.')):
        return "Local Network"
    if data and ip in data.get('geo_cache', {}):
        return data['geo_cache'][ip]
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Firefox/123.0'}
        geo_resp = requests.get(
            f"http://ip-api.com/json/{ip}?fields=status,country,city",
            headers=headers,
            timeout=2
        ).json()
        if geo_resp.get('status') == 'success':
            loc = f"{geo_resp.get('city')}, {geo_resp.get('country')}"
            if data is not None:
                data.setdefault('geo_cache', {})[ip] = loc
            return loc
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

def default_db_structure():
    return {
        "allowed_ips": {},
        "banned_ips": {},
        "attempts": {},
        "active_sessions": {},
        "pending_logins": {},
        "known_ips": {},
        "geo_cache": {},
        "events": [],
        "last_event_id": 0
    }

def record_event(data, event_type, ip, route, status_code, detail="", location=None, user_agent=None, origin=None, session_id=None):
    """Appends a real network/auth event to the rolling telemetry queue."""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if not user_agent:
        user_agent = request.headers.get('User-Agent', 'Unknown Browser')
    short_ua = user_agent[:48] + "..." if len(user_agent) > 48 else user_agent

    if not location:
        existing_loc = data.get('known_ips', {}).get(ip, {}).get('location')
        location = existing_loc if existing_loc else ("📍 " + get_ip_location(ip, data))

    # Maintain persistent metadata for known external/visitor IPs
    data.setdefault('known_ips', {})[ip] = {
        "ip": ip,
        "location": location,
        "device_info": short_ua,
        "last_route": route,
        "last_seen": now_str,
        "last_status": status_code
    }

    next_id = int(data.get('last_event_id', 0)) + 1
    data['last_event_id'] = next_id

    event_obj = {
        "id": next_id,
        "type": event_type,       # AUTH_OK, REJECT, LOCK_WAIT, LOCK_ABANDON, LOGIN_SUCCESS, LOGIN_FAIL, VERIFY_OK, VERIFY_FAIL, LOGOUT, REVOKE, ADMIN_ALLOW, ADMIN_BAN
        "ip": ip,
        "route": route,
        "status": status_code,
        "detail": detail,
        "location": location,
        "device_info": short_ua,
        "origin": origin or "",
        "session_id": session_id or "",
        "time": now_str
    }

    events = data.setdefault('events', [])
    events.append(event_obj)
    if len(events) > MAX_EVENTS:
        data['events'] = events[-MAX_EVENTS:]

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

    # Expire lock-screen waits if the user closed the tab or went idle > 75 seconds
    expired_pending = []
    for ip, info in list(data.get('pending_logins', {}).items()):
        try:
            wait_Start = datetime.strptime(info['time'], "%Y-%m-%d %H:%M:%S")
            if now - wait_Start > timedelta(seconds=75):
                expired_pending.append((ip, info))
        except (ValueError, KeyError, TypeError):
            expired_pending.append((ip, {}))

    for ip, info in expired_pending:
        data['pending_logins'].pop(ip, None)
        record_event(
            data,
            event_type="LOCK_ABANDON",
            ip=ip,
            route="/login",
            status_code=408,
            detail="Lock screen timed out / closed",
            location=info.get('location'),
            user_agent=info.get('device_info')
        )

    return data

def load_data():
    defaults = default_db_structure()

    if not os.path.exists(DATA_FILE):
        return defaults

    try:
        with open(DATA_FILE, 'r') as f:
            data = json.load(f)
    except Exception as e:
        logging.warning(f"[Gatekeeper] Database corrupted or empty ({e}). Rebuilding clean state...")
        return defaults

    if not isinstance(data, dict):
        return defaults

    for key, default_val in defaults.items():
        if key not in data or not isinstance(data[key], type(default_val)):
            data[key] = default_val

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

def should_emit_pass_event(data, ip, cooldown_sec=4):
    """Prevents static asset bursts (CSS/JS/images) from spamming 30 identical pass events in 1 second."""
    now = datetime.now()
    for ev in reversed(data.get('events', [])):
        if ev.get('ip') == ip and ev.get('type') in ('AUTH_OK', 'VERIFY_OK'):
            try:
                ev_time = datetime.strptime(ev['time'], "%Y-%m-%d %H:%M:%S")
                if (now - ev_time).total_seconds() < cooldown_sec:
                    return False
            except Exception:
                pass
            break
    return True

# --- ROUTES ---

@app.route('/')
def index():
    return redirect(url_for('login'))

@app.route('/watch')
@app.route('/homebase')
def watch_homebase():
    """Zero-options real-time HomeBase visualizer."""
    return render_template('watch.html')

@app.route('/api/telemetry', methods=['GET', 'OPTIONS'])
def api_telemetry():
    """Real-time data feed for the /watch visualizer."""
    origin = request.headers.get('Origin')
    if request.method == 'OPTIONS':
        response = make_response()
        if origin in ALLOWED_ORIGINS:
            response.headers.add("Access-Control-Allow-Origin", origin)
            response.headers.add("Access-Control-Allow-Methods", "GET, OPTIONS")
            response.headers.add("Access-Control-Allow-Credentials", "true")
        return response

    since_id = request.args.get('since', default=0, type=int)
    data = load_data()
    # Save if cleanup_old_attempts expired any pending_logins
    save_data(data)

    new_events = [ev for ev in data.get('events', []) if ev.get('id', 0) > since_id]

    payload = {
        "server_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "last_event_id": data.get('last_event_id', 0),
        "active_sessions": data.get('active_sessions', {}),
        "allowed_ips": data.get('allowed_ips', {}),
        "banned_ips": data.get('banned_ips', {}),
        "attempts": data.get('attempts', {}),
        "pending_logins": data.get('pending_logins', {}),
        "known_ips": data.get('known_ips', {}),
        "events": new_events
    }

    response = jsonify(payload)
    if origin in ALLOWED_ORIGINS:
        response.headers.add("Access-Control-Allow-Origin", origin)
        response.headers.add("Access-Control-Allow-Credentials", "true")
    return response

@app.route('/api/login_leave', methods=['POST'])
def api_login_leave():
    """Triggered when a user closes the /login lock screen without authenticating."""
    ip = get_client_ip()
    data = load_data()
    if ip in data.get('pending_logins', {}):
        info = data['pending_logins'].pop(ip, {})
        record_event(
            data,
            event_type="LOCK_ABANDON",
            ip=ip,
            route="/login",
            status_code=499,
            detail="Closed lock screen tab",
            location=info.get('location'),
            user_agent=info.get('device_info')
        )
        save_data(data)
    return jsonify({"status": "ok"})

@app.route('/auth')
def auth():
    ip = get_client_ip()
    target_url = request.headers.get('X-Original-URI', 'Unknown URL')

    # Do not generate self-traffic when viewing the /watch visualizer
    if any(target_url.startswith(p) for p in ('/watch', '/homebase', '/api/telemetry', '/api/login_leave')):
        return "OK", 200

    data = load_data()

    if ip in TRUSTED_IPS or ip.startswith(('172.', '10.', '192.168.')):
        if should_emit_pass_event(data, ip, cooldown_sec=6):
            record_event(
                data,
                event_type="AUTH_OK",
                ip=ip,
                route="/auth",
                status_code=200,
                detail=f"Trusted LAN -> {target_url}",
                location="📍 Local Network"
            )
            save_data(data)
        return "OK", 200

    if ip in data['banned_ips']:
        record_event(
            data,
            event_type="REJECT",
            ip=ip,
            route="/auth",
            status_code=403,
            detail=f"Banned IP blocked -> {target_url}"
        )
        save_data(data)
        return "Banned", 403

    session_id = request.cookies.get('dws_auth')
    if ip in data['allowed_ips'] or (session_id and session_id in data['active_sessions']):
        if session_id and session_id in data['active_sessions']:
            data['active_sessions'][session_id]['last_seen'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            data['active_sessions'][session_id]['last_uri'] = target_url
        if should_emit_pass_event(data, ip, cooldown_sec=4):
            record_event(
                data,
                event_type="AUTH_OK",
                ip=ip,
                route="/auth",
                status_code=200,
                detail=f"Authorized -> {target_url}",
                session_id=session_id
            )
        save_data(data)
        return "OK", 200

    loc_str = "📍 " + get_ip_location(ip, data)
    if ip not in data['attempts']:
        data['attempts'][ip] = {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "count": 1,
            "location": loc_str
        }
    else:
        data['attempts'][ip]['count'] += 1
        data['attempts'][ip]['time'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    record_event(
        data,
        event_type="REJECT",
        ip=ip,
        route="/auth",
        status_code=401,
        detail=f"Stopped by Gatekeeper -> {target_url}",
        location=loc_str
    )
    save_data(data)
    return "Unauthorized", 401

@app.route('/login', methods=['GET', 'POST'])
def login():
    redirect_url = request.args.get('redirect', '')
    ip = get_client_ip()
    data = load_data()
    user_agent = request.headers.get('User-Agent', 'Unknown Browser')
    short_ua = user_agent[:40] + "..." if len(user_agent) > 40 else user_agent

    session_id = request.cookies.get('dws_auth')
    invalid_cookie = bool(session_id and session_id not in data['active_sessions'])

    if request.method == 'POST':
        password = request.form.get('password')
        if password == USER_PASSWORD:
            new_session_id = str(uuid.uuid4())
            lat = request.form.get('lat')
            lon = request.form.get('lon')

            if lat and lon and lat.strip() and lon.strip():
                location_str = "🎯 " + get_gps_address(lat, lon)
            else:
                location_str = "📍 " + get_ip_location(ip, data)

            data['active_sessions'][new_session_id] = {
                "ip": ip,
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "location": location_str,
                "device_info": short_ua
            }

            data['attempts'].pop(ip, None)
            data['pending_logins'].pop(ip, None)

            record_event(
                data,
                event_type="LOGIN_SUCCESS",
                ip=ip,
                route="/login",
                status_code=200,
                detail="Lock screen password accepted",
                location=location_str,
                user_agent=user_agent,
                session_id=new_session_id
            )
            save_data(data)

            logging.info(f"[Gatekeeper] LOGIN SUCCESS -> Issued Session ID to {ip} ({location_str})")

            resp = make_response(redirect(redirect_url if redirect_url else '/'))
            resp.set_cookie('dws_auth', new_session_id, max_age=60*60*24*365, domain='.teamexist.com', samesite='None', secure=True)
            return resp
        else:
            loc_str = "📍 " + get_ip_location(ip, data)
            if ip not in data['attempts']:
                data['attempts'][ip] = {"time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "count": 1, "location": loc_str}
            else:
                data['attempts'][ip]['count'] += 1
                data['attempts'][ip]['time'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            data['pending_logins'].pop(ip, None)
            record_event(
                data,
                event_type="LOGIN_FAIL",
                ip=ip,
                route="/login",
                status_code=401,
                detail="Wrong password on lock screen",
                location=loc_str,
                user_agent=user_agent
            )
            save_data(data)
            logging.warning(f"[Gatekeeper] LOGIN FAILED -> Bad password from {ip}")

    else:
        # GET /login -> Visitor is currently sitting at the lock screen waiting to enter password
        loc_str = "📍 " + get_ip_location(ip, data)
        data['pending_logins'][ip] = {
            "ip": ip,
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "location": loc_str,
            "device_info": short_ua,
            "redirect_url": redirect_url
        }
        record_event(
            data,
            event_type="LOCK_WAIT",
            ip=ip,
            route="/login",
            status_code=200,
            detail=f"Waiting at lock screen ({redirect_url or 'direct'})",
            location=loc_str,
            user_agent=user_agent
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

    if request.method == 'OPTIONS':
        response = make_response()
        if origin in ALLOWED_ORIGINS:
            response.headers.add("Access-Control-Allow-Origin", origin)
            response.headers.add("Access-Control-Allow-Headers", "Content-Type")
            response.headers.add("Access-Control-Allow-Methods", "POST, OPTIONS")
            response.headers.add("Access-Control-Allow-Credentials", "true")
        return response

    client_ip = get_client_ip()
    print(f"[Gatekeeper] 2. Request from IP: {client_ip} | Origin: {origin}", flush=True)

    session_token = request.cookies.get('dws_auth')
    has_cookie = session_token is not None
    is_valid = check_token(session_token)
    data = load_data()

    if is_valid:
        print("[Gatekeeper] 4. Access GRANTED.", flush=True)
        if session_token in data.get('active_sessions', {}):
            data['active_sessions'][session_token]['last_seen'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        record_event(
            data,
            event_type="VERIFY_OK",
            ip=client_ip,
            route="/api/verify",
            status_code=200,
            detail=f"API Verify OK ({origin or 'direct'})",
            origin=origin,
            session_id=session_token
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
        record_event(
            data,
            event_type="VERIFY_FAIL",
            ip=client_ip,
            route="/api/verify",
            status_code=401,
            detail=reason,
            origin=origin
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

    if origin in ALLOWED_ORIGINS:
        response.headers.add("Access-Control-Allow-Origin", origin)
        response.headers.add("Access-Control-Allow-Credentials", "true")

    return response

# --- API AUTHENTICATION ROUTES ---

@app.route('/api/login', methods=['POST', 'OPTIONS'])
def api_login():
    origin = request.headers.get('Origin')

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
    user_agent = request.headers.get('User-Agent', 'Unknown Browser')

    if password == USER_PASSWORD:
        new_session_id = str(uuid.uuid4())
        lat = req_data.get('lat')
        lon = req_data.get('lon')

        if lat and lon and str(lat).strip() and str(lon).strip():
            location_str = "🎯 " + get_gps_address(str(lat), str(lon))
        else:
            location_str = "📍 " + get_ip_location(ip, data)

        data['active_sessions'][new_session_id] = {
            "ip": ip,
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "location": location_str,
            "device_info": user_agent[:40] + "..." if len(user_agent) > 40 else user_agent
        }

        data['attempts'].pop(ip, None)
        data['pending_logins'].pop(ip, None)

        record_event(
            data,
            event_type="LOGIN_SUCCESS",
            ip=ip,
            route="/api/login",
            status_code=200,
            detail=f"API Login Accepted ({origin or 'direct'})",
            location=location_str,
            user_agent=user_agent,
            origin=origin,
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
        loc_str = "📍 " + get_ip_location(ip, data)
        if ip not in data['attempts']:
            data['attempts'][ip] = {"time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "count": 1, "location": loc_str}
        else:
            data['attempts'][ip]['count'] += 1
            data['attempts'][ip]['time'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        data['pending_logins'].pop(ip, None)
        record_event(
            data,
            event_type="LOGIN_FAIL",
            ip=ip,
            route="/api/login",
            status_code=401,
            detail=f"API Login Failed: Bad password ({origin or 'direct'})",
            location=loc_str,
            user_agent=user_agent,
            origin=origin
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

    ip = get_client_ip()
    session_token = request.cookies.get('dws_auth')
    is_valid = check_token(session_token)
    data = load_data()

    if is_valid:
        if should_emit_pass_event(data, ip, cooldown_sec=4):
            record_event(
                data,
                event_type="VERIFY_OK",
                ip=ip,
                route="/api/status",
                status_code=200,
                detail=f"Status Check: Logged In ({origin or 'direct'})",
                origin=origin,
                session_id=session_token
            )
            save_data(data)
    else:
        record_event(
            data,
            event_type="VERIFY_FAIL",
            ip=ip,
            route="/api/status",
            status_code=401,
            detail=f"Status Check: Not Logged In ({origin or 'direct'})",
            origin=origin
        )
        save_data(data)

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
    data = load_data()

    if session_token and session_token in data['active_sessions']:
        sess = data['active_sessions'].pop(session_token, {})
        record_event(
            data,
            event_type="LOGOUT",
            ip=sess.get('ip', ip),
            route="/api/logout",
            status_code=200,
            detail="User logged out -> Session closed",
            location=sess.get('location'),
            origin=origin,
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
        sess = data['active_sessions'].pop(session_id)
        record_event(
            data,
            event_type="REVOKE",
            ip=sess.get('ip', 'Unknown'),
            route="/api/action",
            status_code=200,
            detail=f"Admin revoked session {session_id[:8]}",
            location=sess.get('location'),
            session_id=session_id
        )
        logging.info(f"[Gatekeeper] Revoked Active Session: {session_id}")

    elif action == 'allow':
        data['allowed_ips'][target_ip] = {"name": name, "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        data['attempts'].pop(target_ip, None)
        data['pending_logins'].pop(target_ip, None)
        record_event(
            data,
            event_type="ADMIN_ALLOW",
            ip=target_ip,
            route="/api/action",
            status_code=200,
            detail=f"Admin whitelisted IP ({name})"
        )

    elif action == 'revoke':
        if target_ip in data['allowed_ips']:
            del data['allowed_ips'][target_ip]
            record_event(
                data,
                event_type="REVOKE",
                ip=target_ip,
                route="/api/action",
                status_code=200,
                detail="Admin removed IP from whitelist"
            )

    elif action == 'ban':
        data['banned_ips'][target_ip] = {"time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        data['allowed_ips'].pop(target_ip, None)
        data['attempts'].pop(target_ip, None)
        data['pending_logins'].pop(target_ip, None)
        record_event(
            data,
            event_type="ADMIN_BAN",
            ip=target_ip,
            route="/api/action",
            status_code=403,
            detail="Admin banned IP address"
        )

    save_data(data)
    return jsonify({"status": "success"})

if __name__ == '__main__':
    logging.info("[Gatekeeper] Gatekeeper Auth Module is ONLINE and listening on port 5050.")
    app.run(host='0.0.0.0', port=5050)
