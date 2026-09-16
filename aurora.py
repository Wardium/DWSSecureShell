from flask import Flask, request, jsonify, render_template, make_response, Response, stream_with_context
from flask_cors import CORS
import requests
import sqlite3
import datetime
import uuid
import traceback
import json
from duckduckgo_search import DDGS

app = Flask(__name__)
CORS(app, supports_credentials=True)

# ---------------------------------------------------------
OLLAMA_URL = "http://192.168.2.134:11434/api/chat"
# ---------------------------------------------------------

DB_NAME = "aurora.db"

MODEL_MAP = {
    "DWS:Aurora": "DWS:Aurora",
    "DWS:Swift": "DWS:Swift",
    "DWS:Avani": "DWS:Avani",
    "DWS:Optic": "DWS:Optic"
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

# --- API HELPER FUNCTION ---
def get_current_user():
    """Extracts user_id from Headers, JSON payload, or Cookies (for web)."""
    # 1. API Header check
    api_user = request.headers.get('X-User-Id')
    if api_user:
        return api_user
    
    # 2. JSON payload check
    if request.is_json and request.json and 'user_id' in request.json:
        return request.json['user_id']
        
    # 3. Fallback to web cookie
    return request.cookies.get('aurora_user_id', 'anonymous')

def fetch_internet_context(prompt, model_name):
    """
    Asks the AI if it needs to search the web. If yes, runs a free DuckDuckGo search 
    and returns the live data ALONG WITH the system clock. If no, returns an empty string.
    """
    check_payload = {
        "model": model_name,
        "messages": [
            {
                "role": "system", 
                "content": "You are a web-search decision engine. If the user's prompt requires recent facts, news, live data, or things outside your training data, output ONLY the best short search query. If it does NOT require a search (e.g., coding help, local files, general conversation), output exactly the word 'NO'."
            },
            {"role": "user", "content": prompt}
        ],
        "stream": False
    }
    
    try:
        res = requests.post(OLLAMA_URL, json=check_payload, timeout=20)
        if res.status_code == 200:
            ai_decision = res.json().get('message', {}).get('content', '').strip()
            
            # If the AI decides to search, we grab the time and the web data!
            if ai_decision.upper() != "NO" and len(ai_decision) > 1:
                print(f"[*] Aurora requested web search for: '{ai_decision}'")
                
                # Generate the clock ONLY when searching
                current_time = datetime.datetime.now().strftime("%I:%M %p on %A, %B %d, %Y")
                context = f"[System Note: The exact local server time is {current_time}]\n"
                context += "Here is real-time information from the internet:\n"
                
                try:
                    from duckduckgo_search import DDGS
                    with DDGS() as ddgs:
                        results = list(ddgs.text(ai_decision, max_results=3))
                        
                    if results:
                        for r in results:
                            context += f"- {r.get('title')}: {r.get('body')}\n"
                        return context + "\n"
                    else:
                        return context + "- No internet results found.\n\n"
                except Exception as e:
                    print(f"[*] DuckDuckGo search failed: {e}")
                    return context + "\n"
    except Exception as e:
        print(f"[*] Search decision failed: {e}")
        
    return ""

@app.route('/')
def index():
    return render_template('aurora.html')

# --- API ENDPOINTS ---

@app.route('/api/login', methods=['POST'])
def login():
    user_id = request.json.get('user_id', '').strip()
    if not user_id:
        return jsonify({"error": "user_id is required"}), 400
        
    # Returns the user_id as an API token for external clients
    resp = make_response(jsonify({"success": True, "token": user_id, "user_id": user_id}))
    # Still sets the cookie for the web frontend
    resp.set_cookie('aurora_user_id', user_id, max_age=60*60*24*365)
    return resp

@app.route('/api/logout', methods=['POST'])
def logout():
    resp = make_response(jsonify({"success": True, "message": "Logged out successfully"}))
    resp.set_cookie('aurora_user_id', '', expires=0)
    return resp

@app.route('/api/models', methods=['GET'])
def get_models():
    """API endpoint to fetch available models."""
    return jsonify({"success": True, "models": list(MODEL_MAP.keys())})

@app.route('/api/history', methods=['GET'])
def get_history():
    try:
        cleanup_old_history()
        user_id = get_current_user()
        if user_id == 'anonymous':
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

@app.route('/api/star/<chat_id>', methods=['POST'])
def toggle_star(chat_id):
    try:
        starred = request.json.get('starred', True)
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute("UPDATE chats SET starred = ? WHERE id = ?", (1 if starred else 0, chat_id))
        conn.commit()
        conn.close()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/chat/<chat_id>', methods=['DELETE'])
def delete_chat(chat_id):
    try:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute("DELETE FROM messages WHERE chat_id = ?", (chat_id,))
        c.execute("DELETE FROM chats WHERE id = ?", (chat_id,))
        conn.commit()
        conn.close()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/generate', methods=['POST'])
def generate():
    print("\n--- INCOMING API REQUEST (STREAMING) ---")
    
    user_id = get_current_user()
    data = request.json
    chat_id = data.get('chat_id')
    model_choice = data.get('model', 'DWS:Aurora')
    message = data.get('message')
    
    if not message:
        return jsonify({"error": "Message content is required"}), 400
        
    actual_model = MODEL_MAP.get(model_choice, "DWS:Aurora")
    print(f"User: {user_id} | Targeting Local Model: {actual_model}")

    # 1. DB Operations (Closed before streaming to prevent locks)
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
    conn.close()

    # ---> NEW: SMART INTERNET SEARCH <---
    web_context = fetch_internet_context(message, actual_model)
    if web_context:
        history[-1]['content'] = f"{web_context}User's Prompt: {message}"
    # ------------------------------------

    payload = {
        "model": actual_model,
        "messages": history,
        "stream": True 
    }
    
    # 2. Setup the stream generator
    def generate_stream():
        try:
            yield json.dumps({"type": "start", "chat_id": chat_id}) + "\n"
            
            full_ai_message = ""
            print(f"Attempting to contact local AI at: {OLLAMA_URL}...")
            
            with requests.post(OLLAMA_URL, json=payload, stream=True, timeout=500) as response:
                if response.status_code != 200:
                    yield json.dumps({"type": "error", "content": f"AI Server rejected request. Status: {response.status_code}"}) + "\n"
                    return
                
                # Stream the words directly to the browser
                for line in response.iter_lines():
                    if line:
                        chunk = json.loads(line)
                        content = chunk.get("message", {}).get("content", "")
                        full_ai_message += content
                        yield json.dumps({"type": "chunk", "content": content}) + "\n"
            
            # 3. Save the final message to the database
            save_conn = sqlite3.connect(DB_NAME)
            save_c = save_conn.cursor()
            save_c.execute("INSERT INTO messages (chat_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
                      (chat_id, "assistant", full_ai_message, datetime.datetime.now()))
            save_conn.commit()
            save_conn.close()
            
            print("Successfully finished streaming response.")
            yield json.dumps({"type": "done"}) + "\n"
            
        except requests.exceptions.RequestException as e:
            print(f"\n[NETWORK ERROR] Could not reach local AI: {str(e)}")
            yield json.dumps({"type": "error", "content": "Backend failed to reach local AI."}) + "\n"
        except Exception as e:
            print("\n[CRITICAL ERROR] Python script crash!")
            traceback.print_exc()
            yield json.dumps({"type": "error", "content": f"Python crash: {str(e)}"}) + "\n"

    # Send the generator as a live NDJSON stream to bypass Cloudflare limits
    return Response(stream_with_context(generate_stream()), mimetype='application/x-ndjson')

if __name__ == '__main__':
    init_db()
    app.run(host='0.0.0.0', port=5101, debug=True, use_reloader=False)
