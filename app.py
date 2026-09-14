# THESE TWO LINES MUST BE AT THE VERY TOP
import eventlet
eventlet.monkey_patch()

from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO
import paramiko
import logging
import sys
import requests
import time
import subprocess
import os

logging.basicConfig(
    stream=sys.stdout, 
    level=logging.INFO, 
    format='[%(asctime)s] %(levelname)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')

# --- GLOBAL VARIABLES ---
# Dictionary to track active SSH sessions
active_sessions = {}
# Dictionary to track what state we are forcing devices into
enforced_devices = {}

# ==========================================
# MODULE 1: DWS Server Shell
# ==========================================
SERVERS = {
    "1": {"host": "192.168.2.111", "user": "dylan", "password": "weqr1234"},
    "2": {"host": "192.168.2.91", "user": "dylanwardstudios", "password": "weqr1234"},
    "3": {"host": "192.168.2.134", "user": "dylan", "password": "sIjkew-1qixwe-sogcog"}
}

@app.route('/shell/<server_id>')
def shell(server_id):
    if server_id not in SERVERS:
        logging.warning(f"404: Invalid Server ID accessed: {server_id}")
        return "Server not found", 404
    return render_template('shell.html', server_id=server_id)

@socketio.on('connect_ssh')
def handle_ssh_connection(data):
    server_id = data.get('server_id')
    if server_id not in SERVERS:
        return

    server = SERVERS[server_id]
    client_sid = request.sid  
    
    logging.info(f"Attempting SSH connection to {server['host']}...")
    
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    try:
        ssh.connect(server['host'], username=server['user'], password=server['password'], timeout=5)
        channel = ssh.invoke_shell()
        
        active_sessions[client_sid] = ssh
        
        logging.info(f"SUCCESS: SSH session established for {server['host']}")

        def listen_to_ssh():
            while not channel.closed:
                try:
                    output = channel.recv(1024).decode('utf-8')
                    if output:
                        socketio.emit('ssh_output', {'output': output}, to=client_sid)
                except Exception as e:
                    logging.error(f"SSH listener error: {e}")
                    break
            logging.info(f"Stopped listening to SSH on {server['host']}")

        socketio.start_background_task(listen_to_ssh)

        @socketio.on('ssh_input')
        def handle_input(input_data):
            if not channel.closed:
                channel.send(input_data['input'])

    except Exception as e:
        logging.error(f"FAILED: SSH connection failed. Reason: {str(e)}")
        socketio.emit('ssh_output', {'output': f'\r\n[!] Connection failed: {str(e)}\r\n'}, to=client_sid)

@socketio.on('disconnect')
def handle_disconnect():
    session_id = request.sid
    if session_id in active_sessions:
        logging.info(f"Tab closed. Terminating SSH session for {session_id}")
        active_sessions[session_id].close()
        del active_sessions[session_id]

# ==========================================
# MODULE 4: Local Port Forwarding Proxy
# ==========================================
from flask import Response

@app.route('/port/<int:target_port>', methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'OPTIONS'])
@app.route('/port/<int:target_port>/<path:subpath>', methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'OPTIONS'])
def local_port_proxy(target_port, subpath=""):
    # Route traffic to localhost on the requested port
    target_url = f"http://127.0.0.1:{target_port}/{subpath}"
    
    # Forward query parameters if they exist
    if request.query_string:
        target_url = f"{target_url}?{request.query_string.decode('utf-8')}"
        
    try:
        # Strip the original Host header so the request appears local to the target app
        req_headers = {key: value for key, value in request.headers if key.lower() != 'host'}
        
        # Forward the request to the local service (added a 15-second timeout to prevent hanging)
        proxied_response = requests.request(
            method=request.method,
            url=target_url,
            headers=req_headers,
            data=request.get_data(),
            cookies=request.cookies,
            allow_redirects=False,
            stream=True,
            timeout=15
        )
        
        # Exclude hop-by-hop headers that shouldn't be forwarded to the client
        excluded_headers = ['content-encoding', 'content-length', 'transfer-encoding', 'connection']
        resp_headers = []
        
        for key, value in proxied_response.raw.headers.items():
            if key.lower() not in excluded_headers:
                # Rewrite absolute redirects so you aren't forced back to a localhost URL
                if key.lower() == 'location':
                    host_replacement = f"{request.scheme}://{request.host}/port/{target_port}"
                    value = value.replace(f"http://127.0.0.1:{target_port}", host_replacement)
                    value = value.replace(f"http://localhost:{target_port}", host_replacement)
                resp_headers.append((key, value))
        
        # Stream the response back to the client
        return Response(
            proxied_response.iter_content(chunk_size=10*1024), 
            proxied_response.status_code, 
            resp_headers
        )

    except requests.exceptions.ConnectionError:
        logging.error(f"Proxy module: Connection Refused to port {target_port}")
        # We use 503 here instead of 502 so Cloudflare doesn't intercept the page
        return jsonify({
            "error": "Connection Refused", 
            "details": f"Port {target_port} is either not running, or it is not listening on 127.0.0.1."
        }), 503
        
    except requests.exceptions.Timeout:
        logging.error(f"Proxy module: Timeout connecting to port {target_port}")
        return jsonify({"error": "Gateway Timeout", "details": f"Port {target_port} took too long to respond."}), 504
        
    except Exception as e:
        logging.error(f"Proxy module error connecting to local port {target_port}: {str(e)}")
        return jsonify({"error": "Internal Proxy Error", "details": str(e)}), 500

# ==========================================
# APP EXECUTION
# ==========================================
if __name__ == '__main__':
    logging.info("Starting DWS Server Shell backend...")
    
    # --- MODULE 2: Start the Gatekeeper ---
    # Determine the absolute path to gatekeeper.py to ensure it fires reliably
    gatekeeper_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'gatekeeper.py')
    
    if os.path.exists(gatekeeper_script):
        logging.info("Launching high-performance concurrent Gatekeeper process via Gunicorn...")
        # Spawns Gunicorn with 4 asynchronous gevent workers handling port 5050
        subprocess.Popen(
            ["gunicorn", "-w", "4", "-k", "gevent", "-b", "0.0.0.0:5050", "gatekeeper:app"],
            stdout=sys.stdout,
            stderr=sys.stderr,
            close_fds=True
        )
    else:
        logging.error(f"gatekeeper.py not found at {gatekeeper_script}. Skipping Gatekeeper launch.")
    # --------------------------------------

    # --- MODULE 3: Start Aurora AI ---
    # Determine the absolute path to aurora.py
    aurora_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'aurora.py')
    
    if os.path.exists(aurora_script):
        logging.info("Launching Aurora AI module on port 5101...")
        # Spawns Aurora using the current Python environment (sys.executable)
        subprocess.Popen(
            [sys.executable, "aurora.py"],
            stdout=sys.stdout,
            stderr=sys.stderr,
            close_fds=True
        )
    else:
        logging.error(f"aurora.py not found at {aurora_script}. Skipping Aurora launch.")
    # --------------------------------------
    
    # Start the main SocketIO app (blocking call)
    socketio.run(app, host='0.0.0.0', port=5000)
