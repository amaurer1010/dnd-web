import os
import re
from datetime import date, datetime
from functools import wraps
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, flash, jsonify, redirect, render_template, request, url_for
from flask_login import LoginManager, UserMixin, current_user, login_required, login_user, logout_user
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import check_password_hash, generate_password_hash
import anthropic

load_dotenv()

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ["SECRET_KEY"]
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///dnd_assistant.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message = "Please log in to continue."

DATA_DIR = Path(os.environ.get("DATA_DIR", "./data")).resolve()
PROMPT_PATH = Path(os.environ.get("PROMPT_PATH", "./prompt.md"))

with open(PROMPT_PATH, "r") as f:
    SYSTEM_PROMPT = f.read()

client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

# In-memory conversation history, keyed by user ID.
# Each value is a list of message dicts ready for the Claude API.
conversations: dict[int, list] = {}


# ── Models ────────────────────────────────────────────────────────────────────

class User(UserMixin, db.Model):
    id            = db.Column(db.Integer, primary_key=True)
    username      = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    is_admin      = db.Column(db.Boolean, default=False)
    is_active     = db.Column(db.Boolean, default=True)
    daily_limit       = db.Column(db.Integer, default=50)
    created_at        = db.Column(db.DateTime, default=datetime.utcnow)
    character_context = db.Column(db.Text, nullable=True)
    usage_logs        = db.relationship("UsageLog", backref="user", lazy=True, cascade="all, delete")

    def set_password(self, password: str):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)

    def messages_today(self) -> int:
        log = UsageLog.query.filter_by(user_id=self.id, date=date.today()).first()
        return log.message_count if log else 0

    def total_messages(self) -> int:
        return sum(l.message_count for l in self.usage_logs)

    def last_active(self):
        log = UsageLog.query.filter_by(user_id=self.id).order_by(UsageLog.date.desc()).first()
        return log.date if log else None


class UsageLog(db.Model):
    id            = db.Column(db.Integer, primary_key=True)
    user_id       = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    date          = db.Column(db.Date, nullable=False, default=date.today)
    message_count = db.Column(db.Integer, default=0)
    __table_args__ = (db.UniqueConstraint("user_id", "date"),)


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


# ── Claude Tool Definitions ───────────────────────────────────────────────────

TOOLS = [
    {
        "name": "grep_file",
        "description": "Search a file in the data directory for a regex pattern. Returns matching lines with line numbers.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern":   {"type": "string", "description": "Regex pattern to search for"},
                "file_path": {"type": "string", "description": "Path relative to the data directory, e.g. 'spells/spells-xphb.json'"},
            },
            "required": ["pattern", "file_path"],
        },
    },
    {
        "name": "read_file",
        "description": "Read a portion of a file in the data directory by line number.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Path relative to the data directory"},
                "offset":    {"type": "integer", "description": "Line number to start reading from (1-indexed, default 1)"},
                "limit":     {"type": "integer", "description": "Number of lines to read (default 100)"},
            },
            "required": ["file_path"],
        },
        "cache_control": {"type": "ephemeral"},
    },
]


def _safe_path(file_path: str) -> Path | None:
    """Resolve a user-supplied path and verify it stays inside DATA_DIR."""
    try:
        resolved = (DATA_DIR / file_path).resolve()
        resolved.relative_to(DATA_DIR)  # raises ValueError if outside
        return resolved
    except (ValueError, Exception):
        return None


def tool_grep(pattern: str, file_path: str) -> str:
    path = _safe_path(file_path)
    if path is None:
        return "Error: invalid or disallowed file path."
    if not path.exists():
        return f"File not found: {file_path}"
    matches = []
    try:
        with open(path, encoding="utf-8") as f:
            for i, line in enumerate(f, 1):
                if re.search(pattern, line, re.IGNORECASE):
                    matches.append(f"{i}: {line.rstrip()}")
    except Exception as e:
        return f"Error reading file: {e}"
    if not matches:
        return f"No matches for '{pattern}' in {file_path}."
    return "\n".join(matches[:50])


def tool_read(file_path: str, offset: int = 1, limit: int = 50) -> str:
    path = _safe_path(file_path)
    if path is None:
        return "Error: invalid or disallowed file path."
    if not path.exists():
        return f"File not found: {file_path}"
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
        start = max(0, offset - 1)
        chunk = lines[start : start + limit]
        return "".join(f"{start + i + 1}: {line}" for i, line in enumerate(chunk))
    except Exception as e:
        return f"Error reading file: {e}"


def _dispatch_tool(name: str, inputs: dict) -> str:
    if name == "grep_file":
        return tool_grep(inputs["pattern"], inputs["file_path"])
    if name == "read_file":
        return tool_read(inputs["file_path"], inputs.get("offset", 1), inputs.get("limit", 50))
    return f"Unknown tool: {name}"


# ── Claude Agentic Loop ───────────────────────────────────────────────────────

def run_claude(user_id: int, user_message: str) -> str:
    """Add user_message to the conversation and run the Claude tool-use loop."""
    if user_id not in conversations:
        conversations[user_id] = []

    conversations[user_id].append({"role": "user", "content": user_message})

    # Keep only the last 20 messages (10 exchanges) to limit token usage
    if len(conversations[user_id]) > 20:
        conversations[user_id] = conversations[user_id][-20:]

    messages = conversations[user_id].copy()

    # Build system prompt as a list so the static base can be prompt-cached.
    # The base SYSTEM_PROMPT is large and never changes — cache it.
    # Character context is per-user and small — append it uncached.
    user = db.session.get(User, user_id)
    system = [{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}]
    if user and user.character_context and user.character_context.strip():
        system.append({"type": "text", "text": "\n\n## ACTIVE CHARACTER\n" + user.character_context.strip()})

    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=4096,
            system=system,
            tools=TOOLS,
            messages=messages,
        )

        if response.stop_reason == "tool_use":
            # Build serialisable assistant content and gather tool results
            assistant_content = []
            tool_results = []
            for block in response.content:
                if block.type == "text":
                    assistant_content.append({"type": "text", "text": block.text})
                elif block.type == "tool_use":
                    assistant_content.append({
                        "type": "tool_use",
                        "id":    block.id,
                        "name":  block.name,
                        "input": block.input,
                    })
                    tool_results.append({
                        "type":        "tool_result",
                        "tool_use_id": block.id,
                        "content":     _dispatch_tool(block.name, block.input),
                    })
            messages.append({"role": "assistant", "content": assistant_content})
            messages.append({"role": "user",      "content": tool_results})

        else:
            # Final text response — persist the full exchange to conversation history
            final_text = "".join(
                block.text for block in response.content if hasattr(block, "text")
            )
            conversations[user_id].append({"role": "assistant", "content": final_text})
            return final_text


# ── Usage Tracking ────────────────────────────────────────────────────────────

def _increment_usage(user_id: int):
    log = UsageLog.query.filter_by(user_id=user_id, date=date.today()).first()
    if log:
        log.message_count += 1
    else:
        db.session.add(UsageLog(user_id=user_id, date=date.today(), message_count=1))
    db.session.commit()


# ── Decorators ────────────────────────────────────────────────────────────────

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin:
            return redirect(url_for("chat"))
        return f(*args, **kwargs)
    return decorated


# ── Auth Routes ───────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return redirect(url_for("chat") if current_user.is_authenticated else url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("chat"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = User.query.filter_by(username=username).first()
        if user and user.is_active and user.check_password(password):
            login_user(user)
            return redirect(url_for("chat"))
        flash("Invalid username or password.")
    return render_template("login.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("chat"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        confirm  = request.form.get("confirm", "")
        if not username or not password:
            flash("Username and password are required.")
        elif password != confirm:
            flash("Passwords do not match.")
        elif len(password) < 8:
            flash("Password must be at least 8 characters.")
        elif User.query.filter_by(username=username).first():
            flash("That username is already taken.")
        else:
            user = User(username=username, is_active=False)
            user.set_password(password)
            db.session.add(user)
            db.session.commit()
            flash("Account requested! Ask the admin to activate it.")
            return redirect(url_for("login"))
    return render_template("register.html")


@app.route("/logout")
@login_required
def logout():
    conversations.pop(current_user.id, None)
    logout_user()
    return redirect(url_for("login"))


# ── Chat Routes ───────────────────────────────────────────────────────────────

@app.route("/chat")
@login_required
def chat():
    return render_template("chat.html")


@app.route("/chat/message", methods=["POST"])
@login_required
def chat_message():
    if current_user.messages_today() >= current_user.daily_limit:
        return jsonify({"error": f"You've reached your daily limit of {current_user.daily_limit} messages."}), 429
    user_message = (request.json or {}).get("message", "").strip()
    if not user_message:
        return jsonify({"error": "Message cannot be empty."}), 400
    try:
        reply = run_claude(current_user.id, user_message)
        _increment_usage(current_user.id)
        return jsonify({"response": reply})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/chat/reset", methods=["POST"])
@login_required
def chat_reset():
    conversations.pop(current_user.id, None)
    return jsonify({"status": "ok"})


# ── Profile Routes ────────────────────────────────────────────────────────────

@app.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    if request.method == "POST":
        current_user.character_context = request.form.get("character_context", "").strip() or None
        db.session.commit()
        flash("Character profile saved.")
        return redirect(url_for("chat"))
    return render_template("profile.html")


# ── Admin Routes ──────────────────────────────────────────────────────────────

@app.route("/admin")
@login_required
@admin_required
def admin():
    users   = User.query.order_by(User.created_at).all()
    pending = User.query.filter_by(is_active=False, is_admin=False).count()
    return render_template("admin.html", users=users, pending=pending)


@app.route("/admin/users/add", methods=["POST"])
@login_required
@admin_required
def admin_add_user():
    username    = request.form.get("username", "").strip()
    password    = request.form.get("password", "")
    daily_limit = int(request.form.get("daily_limit", 50))
    is_admin    = request.form.get("is_admin") == "on"

    if not username or not password:
        flash("Username and password are required.")
        return redirect(url_for("admin"))
    if User.query.filter_by(username=username).first():
        flash(f'Username "{username}" is already taken.')
        return redirect(url_for("admin"))

    user = User(username=username, is_admin=is_admin, daily_limit=daily_limit)
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    flash(f'User "{username}" created.')
    return redirect(url_for("admin"))


@app.route("/admin/users/<int:user_id>/delete", methods=["POST"])
@login_required
@admin_required
def admin_delete_user(user_id):
    user = db.session.get(User, user_id)
    if not user:
        flash("User not found.")
        return redirect(url_for("admin"))
    if user.id == current_user.id:
        flash("You cannot delete your own account.")
        return redirect(url_for("admin"))
    db.session.delete(user)
    db.session.commit()
    flash(f'User "{user.username}" deleted.')
    return redirect(url_for("admin"))


@app.route("/admin/users/<int:user_id>/toggle", methods=["POST"])
@login_required
@admin_required
def admin_toggle_user(user_id):
    user = db.session.get(User, user_id)
    if not user:
        flash("User not found.")
        return redirect(url_for("admin"))
    if user.id == current_user.id:
        flash("You cannot deactivate your own account.")
        return redirect(url_for("admin"))
    user.is_active = not user.is_active
    db.session.commit()
    return redirect(url_for("admin"))


@app.route("/admin/users/<int:user_id>/limit", methods=["POST"])
@login_required
@admin_required
def admin_set_limit(user_id):
    user = db.session.get(User, user_id)
    if not user:
        flash("User not found.")
        return redirect(url_for("admin"))
    try:
        user.daily_limit = max(1, int(request.form.get("daily_limit", 50)))
        db.session.commit()
    except ValueError:
        flash("Invalid limit value.")
    return redirect(url_for("admin"))


# ── Startup ───────────────────────────────────────────────────────────────────

def init_db():
    with app.app_context():
        db.create_all()
        if not User.query.first():
            admin_username = os.environ.get("ADMIN_USERNAME", "admin")
            admin_password = os.environ.get("ADMIN_PASSWORD", "changeme")
            admin = User(username=admin_username, is_admin=True, daily_limit=9999)
            admin.set_password(admin_password)
            db.session.add(admin)
            db.session.commit()
            print(f"[init] Admin account created — username: {admin_username}")


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5000, debug=False)
