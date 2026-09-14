from flask import Flask, request, jsonify, render_template, make_response
from flask_cors import CORS
import requests
import sqlite3
import datetime
import uuid
import traceback

app = Flask(__name__)
CORS(app, supports_credentials=True)

# ---------------------------------------------------------
# THE LOCAL AI ADDRESS (Python talks to this, NOT the browser)
OLLAMA_URL = "http://192.168.2.134:11434/api/chat"
# ---------------------------------------------------------

DB_NAME = "aurora.db"

MODEL_MAP = {
    "Aurora": "DWS:Aurora",  # Update these if your local Ollama tags differ
    "Swift": "DWS:Swift",
    "Avani": "DWS:Avani",
    "Optic": "DWS:Optic"
}

def init_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS chats
                 (id TEXT PRIMARY KEY, user_id TEXT, title TEXT, created_at DATETIME, starred INTEGER)''')
    c.execute('''CREATE TABLE IF NOT EXISTS messages
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id TEXT, role TEXT, content TEXT, timestamp DATETIME)''')
    conn.commit()
    conn.close()

def cleanup_old_history():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    seven_days_ago = datetime.datetime.now() - datetime.timedelta(days=7)
    c.execute("SELECT id FROM chats WHERE created_at < ? AND starred = 0", (seven_days_ago,))
    old_chats = [row[0] for row in c.fetchall()]
    for chat_id in old_chats:
        c.execute("DELETE FROM messages WHERE chat_id = ?", (chat_id,))
    c.execute("DELETE FROM chats WHERE created_at < ? AND starred = 0", (seven_days_ago,))
    conn.commit()
    conn.close()

@app.route('/')
def index():
    return render_template('aurora.html')

@app.route('/api/login', methods=['POST'])
def login():
    user_id = request.json.get('user_id', '').strip()
    resp = make_response(jsonify({"success": True, "user_id": user_id}))
    resp.set_cookie('aurora_user_id', user_id, max_age=60*60*24*365)
    return resp

@app.route('/api/logout', methods=['POST'])
def logout():
    resp = make_response(jsonify({"success": True}))
    resp.set_cookie('aurora_user_id', '', expires=0)
    return resp

@app.route('/api/history', methods=['GET'])
def get_history():
    try:
        cleanup_old_history()
        user_id = request.cookies.get('aurora_user_id')
        if not user_id:
            return jsonify([])

        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute("SELECT id, title, created_at, starred FROM chats WHERE user_id = ? ORDER BY created_at DESC", (user_id,))
        chats = [{"id": row[0], "title": row[1], "created_at": row[2], "starred": bool(row[3])} for row in c.fetchall()]
        conn.close()
        return jsonify(chats)
    except Exception as e:
        print(f"History Error: {e}")
        return jsonify([])

@app.route('/api/chat/<chat_id>', methods=['GET'])
def get_chat_messages(chat_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT role, content FROM messages WHERE chat_id = ? ORDER BY timestamp ASC", (chat_id,))
    messages = [{"role": row[0], "content": row[1]} for row in c.fetchall()]
    conn.close()
    return jsonify(messages)

@app.route('/api/generate', methods=['POST'])
def generate():
    print("\n--- INCOMING REQUEST FROM BROWSER ---")
    try:
        user_id = request.cookies.get('aurora_user_id', 'anonymous')
        data = request.json
        chat_id = data.get('chat_id')
        model_choice = data.get('model', 'DWS:Aurora')
        message = data.get('message')
        
        actual_model = MODEL_MAP.get(model_choice, "DWS:Aurora")
        print(f"Targeting Local Model: {actual_model}")

        # 1. DB Operations
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        
        if not chat_id:
            chat_id = str(uuid.uuid4())
            title = message[:30] + "..." if len(message) > 30 else message
            c.execute("INSERT INTO chats (id, user_id, title, created_at, starred) VALUES (?, ?, ?, ?, 0)",
                      (chat_id, user_id, title, datetime.datetime.now()))
        
        c.execute("INSERT INTO messages (chat_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
                  (chat_id, "user", message, datetime.datetime.now()))
        conn.commit()

        c.execute("SELECT role, content FROM messages WHERE chat_id = ? ORDER BY timestamp ASC", (chat_id,))
        history = [{"role": row[0], "content": row[1]} for row in c.fetchall()]

        # 2. Network request to the local AI
        payload = {
            "model": actual_model,
            "messages": history,
            "stream": False
        }
        
        print(f"Attempting to contact local AI at: {OLLAMA_URL}...")
        response = requests.post(OLLAMA_URL, json=payload, timeout=120)
        
        if response.status_code != 200:
            error_msg = f"AI Server rejected request. Status: {response.status_code}, Details: {response.text}"
            print(error_msg)
            conn.close()
            return jsonify({"error": error_msg}), 500
            
        ai_message = response.json().get('message', {}).get('content', '')
        
        if not ai_message:
            print("AI responded, but the message was blank.")
            conn.close()
            return jsonify({"error": "AI returned an empty response."}), 500
        
        # 3. Save and return
        c.execute("INSERT INTO messages (chat_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
                  (chat_id, "assistant", ai_message, datetime.datetime.now()))
        conn.commit()
        conn.close()
        
        print("Successfully generated response. Sending back to browser.")
        return jsonify({"chat_id": chat_id, "response": ai_message})
        
    except requests.exceptions.RequestException as e:
        print(f"\n[NETWORK ERROR] Could not reach the local AI at {OLLAMA_URL}")
        print(f"Details: {str(e)}")
        if 'conn' in locals(): conn.close()
        return jsonify({"error": f"Backend failed to reach local AI: {str(e)}"}), 500
        
    except Exception as e:
        print("\n[CRITICAL ERROR] The Python server crashed!")
        traceback.print_exc()
        if 'conn' in locals(): conn.close()
        return jsonify({"error": f"Python script crash: {str(e)}"}), 500

if __name__ == '__main__':
    init_db()
    # Forces port 5101, disables reloader to prevent background crashes
    app.run(host='0.0.0.0', port=5101, debug=True, use_reloader=False)
