# THESE TWO LINES MUST BE AT THE VERY TOP
import eventlet
eventlet.monkey_patch()

from flask import Flask, render_template, request
from flask_socketio import SocketIO
import paramiko
import logging
import sys

logging.basicConfig(
    stream=sys.stdout, 
    level=logging.INFO, 
    format='[%(asctime)s] %(levelname)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')

# Dictionary to track active SSH sessions so we can close them when a tab is closed
active_sessions = {}

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
    
    # SAVE THE SESSION ID HERE BEFORE THE BACKGROUND TASK STARTS
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
                        # USE THE SAVED SESSION ID
                        socketio.emit('ssh_output', {'output': output}, to=client_sid)
                except Exception as e:
                    # LOG THE ACTUAL ERROR INSTEAD OF FAILING SILENTLY
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
        # USE THE SAVED SESSION ID HERE AS WELL
        socketio.emit('ssh_output', {'output': f'\r\n[!] Connection failed: {str(e)}\r\n'}, to=client_sid)

# Handle tab closures safely
@socketio.on('disconnect')
def handle_disconnect():
    session_id = request.sid
    if session_id in active_sessions:
        logging.info(f"Tab closed. Terminating SSH session for {session_id}")
        active_sessions[session_id].close()
        del active_sessions[session_id]



# ==========================================
# MODULE 2: Device State Enforcer
# ==========================================
import requests
import logging

# --- GOOGLE API CREDENTIALS ---
G_CLIENT_ID = "129761454210-26032tf9jtbc70rt04l4ahjuv85281f9.apps.googleusercontent.com"
G_CLIENT_SECRET = "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"
G_PROJECT_ID = "5054b2ba-0390-4474-85be-efeba8a888fd"
G_REFRESH_TOKEN = "1//06RncHilFbstrCgYIARAAGAYSNwF-L9IrwlfZs6o-yBNX2WOI08OLncZJtvPpONnYNTM8BnjeycNbvew_NAt8ajfMuGNBy9w_Aeg"

class GoogleHomeAPI:
    _access_token = None
    _token_expiry = 0

    @classmethod
    def _get_access_token(cls):
        """Silently fetches a new access token using the refresh token."""
        # Check if we need a new token (adding a small buffer for expiry)
        if time.time() > cls._token_expiry - 60:
            logging.info("Requesting new Google Access Token...")
            url = "https://oauth2.googleapis.com/token"
            payload = {
                "client_id": G_CLIENT_ID,
                "client_secret": G_CLIENT_SECRET,
                "refresh_token": G_REFRESH_TOKEN,
                "grant_type": "refresh_token"
            }
            try:
                response = requests.post(url, data=payload)
                response.raise_for_status()
                data = response.json()
                cls._access_token = data['access_token']
                cls._token_expiry = time.time() + data['expires_in']
            except Exception as e:
                logging.error(f"Failed to refresh Google token: {e}")
                
        return cls._access_token

    @classmethod
    def get_devices(cls):
        """Fetches all devices and filters for those with On/Off switches."""
        token = cls._get_access_token()
        if not token:
            return []

        url = f"https://smartdevicemanagement.googleapis.com/v1/enterprises/{G_PROJECT_ID}/devices"
        headers = {"Authorization": f"Bearer {token}"}
        
        try:
            response = requests.get(url, headers=headers)
            response.raise_for_status()
            data = response.json()
            
            parsed_devices = []
            for device in data.get('devices', []):
                traits = device.get('traits', {})
                
                # We only want devices that support being turned on and off
                if 'sdm.devices.traits.OnOff' in traits:
                    state = "on" if traits['sdm.devices.traits.OnOff'].get('isOn') else "off"
                    
                    # Clean up the long Google ID to get a readable name
                    parent_relations = device.get('parentRelations', [])
                    name = parent_relations[0].get('displayName', 'Unknown Device') if parent_relations else 'Unknown Device'
                    
                    parsed_devices.append({
                        "id": device['name'], # Google uses the full resource path as the ID
                        "name": name,
                        "state": state,
                        "supports_toggle": True
                    })
            return parsed_devices
        except Exception as e:
            logging.error(f"Failed to fetch Google devices: {e}")
            return []

    @classmethod
    def set_device_state(cls, device_id, target_state):
        """Sends the command to force the device on or off."""
        token = cls._get_access_token()
        if not token:
            return

        url = f"https://smartdevicemanagement.googleapis.com/v1/{device_id}:executeCommand"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
        
        # Google API requires strict boolean commands
        command = "sdm.devices.commands.OnOff.TurnOn" if target_state == "on" else "sdm.devices.commands.OnOff.TurnOff"
        payload = {
            "command": command,
            "params": {}
        }
        
        try:
            response = requests.post(url, headers=headers, json=payload)
            response.raise_for_status()
            logging.info(f"Successfully commanded {device_id} to turn {target_state}")
        except Exception as e:
            logging.error(f"Failed to change state for {device_id}: {e}")

if __name__ == '__main__':
    logging.info("Starting DWS Server Shell backend...")
    
    # 1. Start the background loop FIRST
    socketio.start_background_task(device_monitor_loop)
    
    # 2. THEN start the main web server loop
    socketio.run(app, host='0.0.0.0', port=5000)

