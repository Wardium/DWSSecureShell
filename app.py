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
# APP EXECUTION
# ==========================================
if __name__ == '__main__':
    logging.info("Starting DWS Server Shell backend...")
    
    # --- MODULE 2: Start the Gatekeeper ---
    # Determine the absolute path to gatekeeper.py to ensure it fires reliably
    gatekeeper_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'gatekeeper.py')
    
    if os.path.exists(gatekeeper_script):
        logging.info("Launching independent Gatekeeper process...")
        # subprocess.Popen runs the script in a detached manner and immediately continues
        subprocess.Popen([sys.executable, gatekeeper_script])
    else:
        logging.error(f"gatekeeper.py not found at {gatekeeper_script}. Skipping Gatekeeper launch.")
    # --------------------------------------
    
    socketio.run(app, host='0.0.0.0', port=5000)
