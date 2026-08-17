from flask import Flask, request, render_template, redirect, url_for, make_response, jsonify
import json
import os
from datetime import datetime

app = Flask(__name__)
app.secret_key = 'REPLACE_WITH_A_SECURE_SECRET_KEY'

# Configuration
USER_PASSWORD = "admin"
ADMIN_PASSWORD = "dws13125851313241086670"
DATA_FILE = 'data/access_log.json'

# Ensure data file exists
os.makedirs('data', exist_ok=True)
if not os.path.exists(DATA_FILE):
    with open(DATA_FILE, 'w') as f:
        json.dump({"allowed_ips": {}, "banned_ips": {}, "attempts": {}}, f)

def load_data():
    with open(DATA_FILE, 'r') as f:
        return json.load(f)

def save_data(data):
    with open(DATA_FILE, 'w') as f:
        json.dump(data, f, indent=4)

def get_client_ip():
    # Nginx Proxy Manager passes the real IP here
    return request.headers.get('X-Original-Remote-Addr', request.remote_addr)

@app.route('/auth')
def auth():
    """Nginx Proxy Manager hits this endpoint to check access."""
    ip = get_client_ip()
    data = load_data()
    
    if ip in data['banned_ips']:
        return "Banned", 403
        
    # Check IP whitelist OR valid cookie
    if ip in data['allowed_ips'] or request.cookies.get('dws_auth') == 'granted':
        return "OK", 200
    
    # Log the unauthorized attempt
    if ip not in data['attempts']:
        data['attempts'][ip] = {"time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "count": 1}
    else:
        data['attempts'][ip]['count'] += 1
        data['attempts'][ip]['time'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    save_data(data)
    
    return "Unauthorized", 401

@app.route('/login', methods=['GET', 'POST'])
def login():
    """The minimalist login page."""
    redirect_url = request.args.get('redirect', '')
    
    if request.method == 'POST':
        password = request.form.get('password')
        if password == USER_PASSWORD:
            ip = get_client_ip()
            data = load_data()
            
            # Whitelist IP and remove from attempts
            data['allowed_ips'][ip] = {"name": "Auto-Logged In", "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
            if ip in data['attempts']:
                del data['attempts'][ip]
            save_data(data)
            
            resp = make_response(redirect(redirect_url if redirect_url else '/'))
            resp.set_cookie('dws_auth', 'granted', max_age=60*60*24*365) # 1 year cookie
            return resp
            
    return render_template('login.html', redirect_url=redirect_url)

@app.route('/admin', methods=['GET', 'POST'])
def admin():
    """Admin dashboard."""
    if request.method == 'POST':
        if request.form.get('password') == ADMIN_PASSWORD:
            resp = make_response(redirect(url_for('admin')))
            resp.set_cookie('dws_admin', 'granted', max_age=60*60*24)
            return resp
            
    if request.cookies.get('dws_admin') != 'granted':
        return render_template('login.html', is_admin=True)
        
    return render_template('admin.html', data=load_data())

@app.route('/api/action', methods=['POST'])
def admin_action():
    """Handles allow/ban/revoke actions from the admin panel."""
    if request.cookies.get('dws_admin') != 'granted':
        return jsonify({"error": "Unauthorized"}), 401
        
    action = request.json.get('action')
    ip = request.json.get('ip')
    name = request.json.get('name', 'Manually Added')
    data = load_data()
    
    if action == 'allow':
        data['allowed_ips'][ip] = {"name": name, "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        if ip in data['attempts']: del data['attempts'][ip]
    elif action == 'revoke':
        if ip in data['allowed_ips']: del data['allowed_ips'][ip]
    elif action == 'ban':
        data['banned_ips'][ip] = {"time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        if ip in data['allowed_ips']: del data['allowed_ips'][ip]
        if ip in data['attempts']: del data['attempts'][ip]
    elif action == 'unban':
        if ip in data['banned_ips']: del data['banned_ips'][ip]
        
    save_data(data)
    return jsonify({"status": "success"})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5050)
