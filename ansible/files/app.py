"""
Notes app: browser UI + JSON API on top of MariaDB (1 primary, N replicas).

  - Writes always go to the primary.
  - Reads go to a random replica (falls back to the other replicas, then the primary).
  - After a write the page re-reads from the primary once, so you see your own change
    immediately even if a replica is a few ms behind.
  - The page shows which app server answered, where the data was read from, and the
    row count on every DB node (so replication is visible in the browser).

Config comes from environment variables (see the names below).
"""
import os
import random
import socket

import pymysql
from flask import Flask, jsonify, redirect, render_template_string, request, url_for
from pymysql.cursors import DictCursor

app = Flask(__name__)


def env(*names, default=None):
    """Return the first environment variable that is set (supports alternative names)."""
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return default


# ---- config: change the names here if your .env uses different ones ----
DB_USER = env("DB_USER", "APP_DB_USER", "MYSQL_USER", default="appuser")
DB_PASS = env("DB_PASSWORD", "DB_PASS", "APP_DB_PASS", "MYSQL_PASSWORD")
DB_NAME = env("DB_NAME", "APP_DB_NAME", "MYSQL_DATABASE", default="crud_db")
DB_PORT = int(env("DB_PORT", default="3306"))
PRIMARY = env("DB_PRIMARY_HOST", "PRIMARY_HOST", "DB_WRITE_HOST", "DB_PRIMARY")
REPLICAS = [
    h.strip()
    for h in (env("DB_REPLICA_HOSTS", "REPLICA_HOSTS", "DB_READ_HOSTS", "DB_REPLICAS", default="")).split(",")
    if h.strip()
]

if not PRIMARY or DB_PASS is None:
    raise SystemExit(
        "Missing DB config: need a primary host and a DB password in the environment "
        "(DB_PRIMARY_HOST / DB_REPLICA_HOSTS / DB_USER / DB_PASSWORD / DB_NAME)."
    )

MESSAGES = {
    "added": ("Note added.", False),
    "updated": ("Note updated.", False),
    "deleted": ("Note deleted.", False),
    "empty": ("A title is required.", True),
    "error": ("The database rejected that change. Try again.", True),
}


# --------------------------------------------------------------------------
# Database helpers
# --------------------------------------------------------------------------
def connect(host):
    return pymysql.connect(
        host=host,
        port=DB_PORT,
        user=DB_USER,
        password=DB_PASS,
        database=DB_NAME,
        cursorclass=DictCursor,
        connect_timeout=3,
        read_timeout=5,
        write_timeout=5,
        autocommit=True,
    )


def read_query(sql, args=(), force_primary=False):
    """Run a SELECT on a random replica (then other replicas, then primary). Returns (rows, host)."""
    if force_primary or not REPLICAS:
        hosts = [PRIMARY]
    else:
        hosts = random.sample(REPLICAS, len(REPLICAS)) + [PRIMARY]
    last_error = None
    for host in hosts:
        try:
            conn = connect(host)
            try:
                with conn.cursor() as cur:
                    cur.execute(sql, args)
                    return cur.fetchall(), host
            finally:
                conn.close()
        except pymysql.MySQLError as exc:
            last_error = exc
    raise last_error


def write_query(sql, args=()):
    """Run an INSERT/UPDATE/DELETE on the primary. Returns (affected_rows, lastrowid)."""
    conn = connect(PRIMARY)
    try:
        with conn.cursor() as cur:
            affected = cur.execute(sql, args)
            return affected, cur.lastrowid
    finally:
        conn.close()


def node_status(host, role):
    try:
        conn = connect(host)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) AS c FROM notes")
                return {"host": host, "role": role, "up": True, "count": cur.fetchone()["c"]}
        finally:
            conn.close()
    except pymysql.MySQLError:
        return {"host": host, "role": role, "up": False, "count": None}


# --------------------------------------------------------------------------
# Browser UI
# --------------------------------------------------------------------------
PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Notes - HA demo</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--text:#1c2430;--muted:#6b7686;--line:#e3e7ee;--accent:#3b6cf6;--ok:#1a9d5c;--bad:#d9453d}
@media (prefers-color-scheme:dark){:root{--bg:#0f1319;--card:#171d26;--text:#e6eaf0;--muted:#8d99aa;--line:#262f3b;--accent:#6c93ff;--ok:#3ecf8e;--bad:#ff6b63}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:900px;margin:0 auto;padding:24px 16px 48px}
header{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:center;gap:10px;margin-bottom:16px}
h1{font-size:22px;margin:0}
.chips{display:flex;flex-wrap:wrap;gap:6px}
.chip{background:var(--card);border:1px solid var(--line);border-radius:999px;padding:2px 10px;font-size:12px;color:var(--muted)}
.chip b{color:var(--text)}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;margin-bottom:16px}
.card h2{font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin:0 0 12px}
.nodes{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:10px}
.node{border:1px solid var(--line);border-radius:10px;padding:10px 12px}
.node .role{font-size:12px;color:var(--muted)}
.node .ip{font-weight:600}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px;background:var(--ok)}
.dot.down{background:var(--bad)}
input,textarea{width:100%;padding:9px 10px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--text);font:inherit}
textarea{min-height:70px;resize:vertical;margin-top:8px}
button{border:0;border-radius:8px;padding:8px 14px;font:inherit;cursor:pointer;background:var(--accent);color:#fff}
button.ghost{background:transparent;color:var(--muted);border:1px solid var(--line)}
button.danger{background:transparent;color:var(--bad);border:1px solid var(--line)}
.row{display:flex;gap:8px;margin-top:10px;flex-wrap:wrap}
.search{display:flex;gap:8px}
.note{border-top:1px solid var(--line);padding:12px 0}
.note:first-of-type{border-top:0}
.note h3{margin:0 0 2px;font-size:16px}
.note p{margin:0;color:var(--muted);white-space:pre-wrap}
.actions{display:flex;gap:8px;margin-top:10px;align-items:flex-start;flex-wrap:wrap}
details summary{cursor:pointer;list-style:none;border:1px solid var(--line);border-radius:8px;padding:8px 14px;color:var(--muted)}
details[open]{flex:1 1 100%}
details[open] summary{display:inline-block;margin-bottom:10px}
.msg{padding:10px 14px;border-radius:8px;margin-bottom:16px;border:1px solid var(--line);background:var(--card)}
.msg.err{color:var(--bad)}
.small{font-size:12px;color:var(--muted)}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>Notes</h1>
    <div class="chips">
      <span class="chip">Served by <b>{{ host }}</b></span>
      {% if read_from %}<span class="chip">Read from <b>{{ read_from }}</b> ({{ 'primary' if read_from == primary else 'replica' }})</span>{% endif %}
      <span class="chip">Writes to <b>{{ primary }}</b></span>
    </div>
  </header>

  {% if flash %}<div class="msg {{ 'err' if flash[1] }}">{{ flash[0] }}</div>{% endif %}
  {% if error %}<div class="msg err">{{ error }}</div>{% endif %}

  <div class="card">
    <h2>Database cluster</h2>
    <div class="nodes">
      {% for n in nodes %}
      <div class="node">
        <div class="role"><span class="dot {{ '' if n.up else 'down' }}"></span>{{ n.role }}</div>
        <div class="ip">{{ n.host }}</div>
        <div class="small">{% if n.up %}{{ n.count }} notes{% else %}unreachable{% endif %}</div>
      </div>
      {% endfor %}
    </div>
    <p class="small" style="margin:10px 0 0">Equal counts mean replication is in sync. Refresh the page: "Served by" rotates across the app servers (load balancer) and "Read from" alternates between the replicas.</p>
  </div>

  <div class="card">
    <h2>New note</h2>
    <form method="post" action="{{ url_for('add') }}">
      <input name="title" maxlength="200" placeholder="Title" required>
      <textarea name="content" placeholder="Content (optional)"></textarea>
      <div class="row"><button type="submit">Add note</button></div>
    </form>
  </div>

  <div class="card">
    <h2>{{ notes|length }} note{{ '' if notes|length == 1 else 's' }}{% if q %} matching "{{ q }}"{% endif %}</h2>
    <form class="search" method="get" action="{{ url_for('index') }}" style="margin-bottom:8px">
      <input name="q" value="{{ q }}" placeholder="Search title or content">
      <button class="ghost" type="submit">Search</button>
    </form>
    {% for n in notes %}
    <div class="note">
      <h3>{{ n.title }}</h3>
      {% if n.content %}<p>{{ n.content }}</p>{% endif %}
      <div class="actions">
        <details>
          <summary>Edit</summary>
          <form method="post" action="{{ url_for('edit', nid=n.id) }}">
            <input name="title" maxlength="200" value="{{ n.title }}" required>
            <textarea name="content">{{ n.content or '' }}</textarea>
            <div class="row"><button type="submit">Save</button></div>
          </form>
        </details>
        <form method="post" action="{{ url_for('delete', nid=n.id) }}" onsubmit="return confirm('Delete this note?')">
          <button class="danger" type="submit">Delete</button>
        </form>
      </div>
    </div>
    {% else %}
    <p class="small">No notes yet. Add one above.</p>
    {% endfor %}
  </div>
</div>
</body>
</html>
"""


@app.route("/")
def index():
    q = request.args.get("q", "").strip()
    force_primary = request.args.get("src") == "primary"
    flash = MESSAGES.get(request.args.get("msg", ""))
    notes, source, error = [], None, None
    try:
        if q:
            like = "%" + q + "%"
            notes, source = read_query(
                "SELECT id, title, content FROM notes WHERE title LIKE %s OR content LIKE %s ORDER BY id DESC LIMIT 200",
                (like, like),
                force_primary,
            )
        else:
            notes, source = read_query(
                "SELECT id, title, content FROM notes ORDER BY id DESC LIMIT 200",
                (),
                force_primary,
            )
    except pymysql.MySQLError as exc:
        error = "Database error: " + str(exc)

    nodes = [node_status(PRIMARY, "primary")] + [node_status(r, "replica") for r in REPLICAS]
    return render_template_string(
        PAGE,
        notes=notes,
        read_from=source,
        error=error,
        flash=flash,
        q=q,
        nodes=[type("N", (), n) for n in nodes],
        host=socket.gethostname(),
        primary=PRIMARY,
    )


@app.route("/add", methods=["POST"])
def add():
    title = request.form.get("title", "").strip()
    content = request.form.get("content", "").strip()
    if not title:
        return redirect(url_for("index", msg="empty"))
    try:
        write_query("INSERT INTO notes (title, content) VALUES (%s, %s)", (title[:200], content))
    except pymysql.MySQLError:
        return redirect(url_for("index", msg="error"))
    return redirect(url_for("index", src="primary", msg="added"))


@app.route("/edit/<int:nid>", methods=["POST"])
def edit(nid):
    title = request.form.get("title", "").strip()
    content = request.form.get("content", "").strip()
    if not title:
        return redirect(url_for("index", msg="empty"))
    try:
        write_query("UPDATE notes SET title=%s, content=%s WHERE id=%s", (title[:200], content, nid))
    except pymysql.MySQLError:
        return redirect(url_for("index", msg="error"))
    return redirect(url_for("index", src="primary", msg="updated"))


@app.route("/delete/<int:nid>", methods=["POST"])
def delete(nid):
    try:
        write_query("DELETE FROM notes WHERE id=%s", (nid,))
    except pymysql.MySQLError:
        return redirect(url_for("index", msg="error"))
    return redirect(url_for("index", src="primary", msg="deleted"))


# --------------------------------------------------------------------------
# Health check (used by the ALB) - intentionally does not touch the database,
# so a DB problem doesn't make the load balancer drop every app server.
# --------------------------------------------------------------------------
@app.route("/health")
def health():
    return jsonify(status="ok", host=socket.gethostname())


# --------------------------------------------------------------------------
# JSON API
# --------------------------------------------------------------------------
def _payload():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        data = request.form
    return (data.get("title") or "").strip(), (data.get("content") or "").strip()


def _tag(resp, source):
    resp.headers["X-Served-By"] = socket.gethostname()
    resp.headers["X-Read-From"] = source
    return resp


@app.route("/notes", methods=["GET", "POST"])
def api_notes():
    if request.method == "POST":
        title, content = _payload()
        if not title:
            return jsonify(error="title is required"), 400
        _, new_id = write_query("INSERT INTO notes (title, content) VALUES (%s, %s)", (title[:200], content))
        return jsonify(id=new_id, title=title, content=content, written_to=PRIMARY), 201
    rows, source = read_query("SELECT id, title, content FROM notes ORDER BY id DESC LIMIT 200")
    return _tag(jsonify(rows), source)


@app.route("/notes/<int:nid>", methods=["GET", "PUT", "DELETE"])
def api_note(nid):
    if request.method == "GET":
        rows, source = read_query("SELECT id, title, content FROM notes WHERE id=%s", (nid,))
        if not rows:
            return jsonify(error="not found"), 404
        return _tag(jsonify(rows[0]), source)
    if request.method == "PUT":
        title, content = _payload()
        if not title:
            return jsonify(error="title is required"), 400
        write_query("UPDATE notes SET title=%s, content=%s WHERE id=%s", (title[:200], content, nid))
        return jsonify(id=nid, title=title, content=content)
    affected, _ = write_query("DELETE FROM notes WHERE id=%s", (nid,))
    if affected == 0:
        return jsonify(error="not found"), 404
    return jsonify(deleted=nid)


@app.errorhandler(pymysql.MySQLError)
def db_error(exc):
    return jsonify(error="database unavailable", detail=str(exc)), 503


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")))

