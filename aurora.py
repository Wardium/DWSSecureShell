from flask import Flask, request, jsonify, render_template, make_response, Response, stream_with_context
from flask_cors import CORS
import requests
import sqlite3
import datetime
import uuid
import traceback
import json
from duckduckgo_search import DDGS
from zoneinfo import ZoneInfo

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
    # ---> NEW: Memory Compaction Table <---
    c.execute('''CREATE TABLE IF NOT EXISTS chat_summaries
                 (chat_id TEXT PRIMARY KEY, summary TEXT, last_summarized_id INTEGER)''')
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

def compact_chat_memory(chat_id):
    """
    Uses DWS:Swift to fold older messages into a running summary.
    Leaves the most recent 6 messages verbatim.
    """
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    
    # 1. Fetch all messages in order
    c.execute("SELECT id, role, content FROM messages WHERE chat_id = ? ORDER BY id ASC", (chat_id,))
    all_msgs = c.fetchall()
    
    KEEP_RECENT = 6
    if len(all_msgs) <= KEEP_RECENT:
        conn.close()
        return

    msgs_to_summarize = all_msgs[:-KEEP_RECENT]
    
    # 2. Check what has already been summarized
    c.execute("SELECT summary, last_summarized_id FROM chat_summaries WHERE chat_id = ?", (chat_id,))
    row = c.fetchone()
    current_summary = row[0] if row else ""
    last_id = row[1] if row else 0
    
    new_to_summarize = [m for m in msgs_to_summarize if m[0] > last_id]
    if not new_to_summarize:
        conn.close()
        return

    # 3. Format the chunk for DWS:Swift
    convo_chunk = "\n".join([f"{role.capitalize()}: {content}" for _, role, content in new_to_summarize])
    
    prompt = (
        "You are a memory condenser. Update the existing memory with the new conversation below. "
        "Output ONLY a dense 2-3 sentence summary retaining key facts, user preferences, and decisions.\n\n"
    )
    if current_summary:
        prompt += f"Existing Memory:\n{current_summary}\n\n"
    prompt += f"New Conversation:\n{convo_chunk}"

    try:
        res = requests.post(OLLAMA_URL, json={
            "model": MODEL_MAP.get("DWS:Swift", "DWS:Swift"),
            "messages": [{"role": "user", "content": prompt}],
            "stream": False
        }, timeout=15)
        
        if res.status_code == 200:
            updated_summary = res.json().get('message', {}).get('content', '').strip()
            newest_id = new_to_summarize[-1][0]
            
            c.execute("""
                INSERT INTO chat_summaries (chat_id, summary, last_summarized_id)
                VALUES (?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    summary = excluded.summary,
                    last_summarized_id = excluded.last_summarized_id
            """, (chat_id, updated_summary, newest_id))
            conn.commit()
            print(f"[*] Memory compacted for chat {chat_id}")
    except Exception as e:
        print(f"[*] Memory compaction failed: {e}")
    finally:
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
    Aggressive search engine that looks up almost everything.
    """
    check_payload = {
        "model": model_name,
        "messages": [
            {
                "role": "system", 
                "content": (
                    "You are an aggressive web-search decision engine. You must trigger a search for ANY factual, "
                    "real-world, informational, non-inventive, or non-imaginative question. "
                    "If the user asks for the time, dates, definitions, facts, news, specs, or general knowledge, "
                    "output ONLY the best short search query. "
                    "ONLY output exactly the word 'NO' if the user's prompt is purely imaginative (creative writing), "
                    "strictly code generation, or a casual personal greeting."
                    "If asked what time it is, please provide time, date, facts, info, ANYTHING. ANYTHING that NEEDS REAL WORLD INFORMATION"
                    "DO NOT UNDER ANY CIRCUMSTANCE rely on internal information, look up everything."
                )
            },
            {"role": "user", "content": prompt}
        ],
        "stream": False
    }
    
    try:
        res = requests.post(OLLAMA_URL, json=check_payload, timeout=20)
        if res.status_code == 200:
            ai_decision = res.json().get('message', {}).get('content', '').strip()
            
            # If the AI decides to search (meaning it didn't output 'NO')
            if ai_decision.upper() != "NO" and len(ai_decision) > 1:
                print(f"[*] Aurora requested web search for: '{ai_decision}'")
                
                # ---> INJECT THE PACIFIC TIME CLOCK HERE <---
                from zoneinfo import ZoneInfo
                current_time = datetime.datetime.now(ZoneInfo('America/Vancouver')).strftime("%I:%M %p on %A, %B %d, %Y")
                context = f"[System Note: Your internal clock is {current_time}. Always use 12-hour AM/PM format.]\n\n"
                context += "Here is real-time information from the internet:\n"
                
                try:
                    from duckduckgo_search import DDGS
                    with DDGS() as ddgs:
                        # You can increase max_results=5 or 10 since you have no token limits!
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
    data = request.get_json(silent=True) or {}

    chat_id = data.get('chat_id')
    model_choice = data.get('model', 'DWS:Aurora')
    message = data.get('message', '').strip()

    # Accept either a single image or an array of images
    image = data.get('image')
    images = data.get('images', [])

    if image and not images:
        images = [image]

    if not message and not images:
        return jsonify({"error": "Message content or attachment is required"}), 400

    # Make sure images is actually a list
    if not isinstance(images, list):
        return jsonify({"error": "images must be an array"}), 400

    actual_model = MODEL_MAP.get(model_choice, "DWS:Aurora")

    print(
        f"User: {user_id} | "
        f"Targeting Local Model: {actual_model} | "
        f"Images: {len(images)}"
    )

    # ---------------------------------------------------------
    # 1. DATABASE OPERATIONS
    # ---------------------------------------------------------

    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()

    if not chat_id:
        chat_id = str(uuid.uuid4())

        title_source = message if message else "Image"
        title = (
            title_source[:30] + "..."
            if len(title_source) > 30
            else title_source
        )

        c.execute(
            """
            INSERT INTO chats
            (id, user_id, title, created_at, starred)
            VALUES (?, ?, ?, ?, 0)
            """,
            (
                chat_id,
                user_id,
                title,
                datetime.datetime.now()
            )
        )

    # Save the text message normally
    c.execute(
        """
        INSERT INTO messages
        (chat_id, role, content, timestamp)
        VALUES (?, ?, ?, ?)
        """,
        (
            chat_id,
            "user",
            message,
            datetime.datetime.now()
        )
    )

    conn.commit()

    # ---------------------------------------------------------
    # 2. MEMORY COMPACTION
    # ---------------------------------------------------------

    compact_chat_memory(chat_id)

    # Fetch active summary
    c.execute(
        "SELECT summary FROM chat_summaries WHERE chat_id = ?",
        (chat_id,)
    )

    summary_row = c.fetchone()
    active_summary = summary_row[0] if summary_row else None

    # Fetch recent messages
    c.execute(
        """
        SELECT role, content
        FROM (
            SELECT id, role, content
            FROM messages
            WHERE chat_id = ?
            ORDER BY id DESC
            LIMIT 6
        )
        ORDER BY id ASC
        """,
        (chat_id,)
    )

    history = [
        {
            "role": row[0],
            "content": row[1]
        }
        for row in c.fetchall()
    ]

    conn.close()

    # ---------------------------------------------------------
    # 3. SYSTEM MESSAGE
    # ---------------------------------------------------------

    current_time = datetime.datetime.now(
        ZoneInfo('America/Vancouver')
    ).strftime("%I:%M %p on %A, %B %d, %Y")

    system_content = (
        f"The exact local time is {current_time}. "
        f"You must always use this exact time and date. "
        f"Never use UTC or military time."
    )

    if active_summary:
        system_content += (
            f"\n\n[Memory of earlier conversation: {active_summary}]"
        )

    history.insert(
        0,
        {
            "role": "system",
            "content": system_content
        }
    )

    # ---------------------------------------------------------
    # 4. ADD IMAGE(S) TO THE CURRENT USER MESSAGE
    # ---------------------------------------------------------

    # The latest message is the user's message because we just
    # inserted the system message at index 0.
    if history:
        latest_user_message = history[-1]

        if latest_user_message["role"] == "user" and images:
            latest_user_message["images"] = images

    # ---------------------------------------------------------
    # 5. WEB SEARCH
    # ---------------------------------------------------------

    web_context = fetch_internet_context(message, "DWS:Swift")

    if web_context:
        latest_user_message = history[-1]

        existing_content = latest_user_message.get("content", "")

        latest_user_message["content"] = (
            f"{web_context}\n\n"
            f"User's Prompt: {existing_content}"
        )

        # IMPORTANT:
        # Don't overwrite the whole message object here,
        # because that would delete the "images" field.

    # ---------------------------------------------------------
    # 6. OLLAMA PAYLOAD
    # ---------------------------------------------------------

    payload = {
        "model": actual_model,
        "messages": history,
        "stream": True
    }

    print("Sending payload to Ollama...")
    print(f"Message count: {len(history)}")
    print(f"Image count: {len(images)}")

    # ---------------------------------------------------------
    # 7. STREAM GENERATOR
    # ---------------------------------------------------------

    def generate_stream():
        try:
            yield json.dumps({
                "type": "start",
                "chat_id": chat_id
            }) + "\n"

            full_ai_message = ""

            print(
                f"Attempting to contact local AI at: "
                f"{OLLAMA_URL}..."
            )

            with requests.post(
                OLLAMA_URL,
                json=payload,
                stream=True,
                timeout=500
            ) as response:

                if response.status_code != 200:
                    error_text = response.text

                    print(
                        f"Ollama returned {response.status_code}: "
                        f"{error_text}"
                    )

                    yield json.dumps({
                        "type": "error",
                        "content": (
                            f"AI Server rejected request. "
                            f"Status: {response.status_code}"
                        )
                    }) + "\n"

                    return

                for line in response.iter_lines():
                    if not line:
                        continue

                    try:
                        chunk = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    content = chunk.get(
                        "message", {}
                    ).get("content", "")

                    full_ai_message += content

                    yield json.dumps({
                        "type": "chunk",
                        "content": content
                    }) + "\n"

            # -------------------------------------------------
            # 8. SAVE AI RESPONSE
            # -------------------------------------------------

            save_conn = sqlite3.connect(DB_NAME)
            save_c = save_conn.cursor()

            save_c.execute(
                """
                INSERT INTO messages
                (chat_id, role, content, timestamp)
                VALUES (?, ?, ?, ?)
                """,
                (
                    chat_id,
                    "assistant",
                    full_ai_message,
                    datetime.datetime.now()
                )
            )

            save_conn.commit()
            save_conn.close()

            print(
                "Successfully finished streaming response."
            )

            yield json.dumps({
                "type": "done"
            }) + "\n"

        except requests.exceptions.RequestException as e:
            print(
                f"\n[NETWORK ERROR] "
                f"Could not reach local AI: {str(e)}"
            )

            yield json.dumps({
                "type": "error",
                "content": "Backend failed to reach local AI."
            }) + "\n"

        except Exception as e:
            print(
                "\n[CRITICAL ERROR] Python script crash!"
            )

            traceback.print_exc()

            yield json.dumps({
                "type": "error",
                "content": f"Python crash: {str(e)}"
            }) + "\n"

    return Response(
        stream_with_context(generate_stream()),
        mimetype='application/x-ndjson'
    )

if __name__ == '__main__':
    init_db()
    app.run(host='0.0.0.0', port=5101, debug=True, use_reloader=False)
