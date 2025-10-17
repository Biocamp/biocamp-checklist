from flask import (
    Flask, render_template, request, redirect, url_for, session, flash,
    abort, g
)
from datetime import datetime
import sqlite3, os, uuid
from typing import Optional, Iterable
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.secret_key = "altere_esta_chave"

BASE_DIR = os.path.dirname(__file__)
DB_PATH = os.getenv("DB_PATH", os.path.join(BASE_DIR, "local.db"))

# -------- Uploads --------
UPLOAD_FOLDER = os.path.join(BASE_DIR, "static", "uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024  # 8MB
ALLOWED_EXTS = {"png", "jpg", "jpeg", "webp", "gif"}

def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTS

# ------------------------------ DB helpers ------------------------------
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def rows_to_list(cur): return [dict(r) for r in cur.fetchall()]

def row_or_none(cur):
    r = cur.fetchone()
    return dict(r) if r else None

def now_iso(): return datetime.utcnow().isoformat(timespec="seconds")

def ensure_schema():
    with get_conn() as c:
        c.execute("""
        CREATE TABLE IF NOT EXISTS ships (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            number TEXT,
            status TEXT,
            sent_at TEXT,
            received_at TEXT,
            viewed_at TEXT,
            description TEXT,
            token TEXT UNIQUE NOT NULL,
            responsible_email TEXT
        )""")
        c.execute("""
        CREATE TABLE IF NOT EXISTS items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ship_id INTEGER NOT NULL,
            label TEXT NOT NULL,
            external_url TEXT,
            confirmed_at TEXT,
            confirmed_by TEXT,
            viewed_at TEXT,
            viewed_by TEXT,
            image_path TEXT,
            FOREIGN KEY(ship_id) REFERENCES ships(id) ON DELETE CASCADE
        )""")
        c.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ship_id INTEGER NOT NULL,
            ts TEXT NOT NULL,
            actor TEXT,
            type TEXT,
            ip TEXT,
            user_agent TEXT,
            FOREIGN KEY(ship_id) REFERENCES ships(id) ON DELETE CASCADE
        )""")
        c.commit()

def ensure_column(table: str, column: str, coltype: str):
    with get_conn() as c:
        cols = [r["name"] for r in c.execute(f"PRAGMA table_info({table})")]
        if column not in cols:
            c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
            c.commit()

ensure_schema()
# migração leve para imagem
ensure_column("items", "image_path", "TEXT")

# ------------------------------ Model ------------------------------
class Ship:
    def __init__(self, d: dict):
        self.id = d["id"]
        self.title = d.get("title")
        self.number = d.get("number")
        self.status = d.get("status")
        self.sent_at = d.get("sent_at")
        self.received_at = d.get("received_at")
        self.viewed_at = d.get("viewed_at")
        self.description = d.get("description")
        self.token = d.get("token")
        self.responsible_email = d.get("responsible_email")

    def open_url(self) -> str:
        return url_for("open_public", token=self.token)

def dict_to_ship(d: dict) -> Ship: return Ship(d)

# ------------------------------ Jinja filters ------------------------------
@app.template_filter("dt")
def fmt_dt(value):
    if not value: return "-"
    try:
        if isinstance(value, str): value = datetime.fromisoformat(value)
        return value.strftime("%d/%m/%Y %H:%M")
    except Exception:
        return value

# ------------------------------ User context (navbar) ----------------------
@app.before_request
def load_user_into_g():
    g.viewer_name = session.get("viewer_name")
    g.viewer_email = session.get("viewer_email")

def current_user(): return session.get("viewer_email")

def current_actor():
    name = session.get("viewer_name")
    email = session.get("viewer_email")
    if email and name:
        return f"{name} <{email}>"
    if email:
        return email
    return "Convidado"

# ------------------------------ Auth ------------------------------
@app.get("/login")
def login_get():
    next_url = request.args.get("next") or url_for("dashboard")
    return render_template("login.html", next_url=next_url)

@app.post("/login")
def login_post():
    name = (request.form.get("name") or "").strip()
    email = (request.form.get("email") or "").strip().lower()
    next_url = request.form.get("next") or url_for("dashboard")
    if not email:
        flash("Informe seu e-mail.", "error")
        return redirect(url_for("login_get", next=next_url))
    session["viewer_name"] = name
    session["viewer_email"] = email
    flash("Identificação confirmada.", "success")
    return redirect(next_url)

@app.get("/logout")
def logout():
    session.pop("viewer_email", None)
    session.pop("viewer_name", None)
    flash("Você saiu da identificação.", "success")
    return redirect(url_for("login_get"))

# ------------------------------ Public (open) ------------------------------
@app.route("/open/<token>", methods=["GET", "POST"])
def open_public(token):
    ship = get_ship_by_token(token)
    if not ship: return "Checklist não encontrada", 404

    can_edit = (not ship.responsible_email) or (current_user() == ship.responsible_email)
    if ship.responsible_email and not current_user():
        return redirect(url_for("login_get", next=url_for("open_public", token=token)))
    actor = current_actor() or "Convidado"

    if request.method == "POST":
        if not can_edit:
            flash(f"Apenas o responsável ({ship.responsible_email}) pode confirmar.", "error")
            return redirect(url_for("open_public", token=token))

        if request.form.get("confirm_all") == "1":
            confirm_all_items(ship.id, actor)
            add_event(ship.id, actor, "CONFIRM_ALL", request.remote_addr, request.user_agent.string)
            set_ship_received_if_done(ship.id)
            flash("Todos os itens foram confirmados.", "success")
            return redirect(url_for("open_public", token=token))

        # confirmar selecionados (apenas os não confirmados)
        ids = [int(x) for x in request.form.getlist("items") if x.isdigit()]
        if ids:
            confirm_selected_items(ship.id, ids, actor)
            add_event(ship.id, actor, "CONFIRM_SELECTED", request.remote_addr, request.user_agent.string)
            set_ship_received_if_done(ship.id)
            flash("Itens selecionados confirmados.", "success")
        else:
            flash("Nenhum item selecionado.", "error")
        return redirect(url_for("open_public", token=token))

    # GET: marca visualizado na primeira visita
    flag_key = f"viewed_flag_ship_{ship.id}"
    if not session.get(flag_key, False):
        items_once = list_items_for_ship(ship.id)
        not_viewed_ids = [it["id"] for it in items_once if not it.get("viewed_at")]
        if not_viewed_ids:
            mark_items_viewed(ship.id, not_viewed_ids, actor)
            add_event(ship.id, actor, "AUTO_VIEWED_ITEMS", request.remote_addr, request.user_agent.string)
            set_ship_viewed_if_first_time(ship.id)
        session[flag_key] = True

    items = list_items_for_ship(ship.id)
    add_event(ship.id, actor, "VIEW_OPEN", request.remote_addr, request.user_agent.string)
    return render_template("open.html", ship=ship, items=items, actor=actor, can_edit=can_edit)

# desconfirmação bloqueada
@app.post("/open/<token>/unconfirm/<int:item_id>")
def unconfirm_item_route(token, item_id):
    ship = get_ship_by_token(token)
    if not ship: abort(404)
    flash("Desconfirmar não é permitido neste checklist.", "error")
    session[f"viewed_flag_ship_{ship.id}"] = True
    return redirect(url_for("open_public", token=token))

# ------------------------------ Internal screens ------------------------------
@app.get("/")
def dashboard():
    ships = list_ships()
    return render_template("dashboard.html", ships=ships)

@app.route("/new", methods=["GET", "POST"])
def new_shipment():
    if request.method == "GET":
        return render_template("new.html")
    title = (request.form.get("title") or "").strip() or "Checklist"
    number = (request.form.get("number") or "").strip() or None
    responsible = (request.form.get("responsible_email") or "").strip().lower() or None
    token = uuid.uuid4().hex
    items = request.form.getlist("items")
    with get_conn() as c:
        c.execute("""INSERT INTO ships (title, number, status, sent_at, token, responsible_email)
                     VALUES (?, ?, ?, ?, ?, ?)""",
                  (title, number, "ENVIADO", now_iso(), token, responsible))
        ship_id = c.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        for label in items:
            if label.strip():
                c.execute("""INSERT INTO items (ship_id, label, external_url) VALUES (?, ?, ?)""",
                          (ship_id, label.strip(), None))
        c.commit()
    flash("Checklist criada com sucesso. Você pode anexar imagens aos itens na tela de Detalhes.", "success")
    return redirect(url_for("detail", ship_id=ship_id))

@app.get("/ship/<int:ship_id>")
def detail(ship_id: int):
    ship = get_ship_by_id(ship_id)
    if not ship: abort(404)
    items = list_items_for_ship(ship.id)
    events = list_events_for_ship(ship.id)
    return render_template("detail.html", ship=ship, items=items, events=events)

@app.post("/ship/<int:ship_id>/add_item")
def add_item(ship_id: int):
    label = (request.form.get("label") or "").strip()
    if not label:
        flash("O item não pode estar vazio.", "error")
        return redirect(url_for("detail", ship_id=ship_id))
    with get_conn() as c:
        c.execute("""INSERT INTO items (ship_id, label, external_url) VALUES (?, ?, ?)""",
                  (ship_id, label, None))
        c.commit()
    flash("Item adicionado com sucesso.", "success")
    return redirect(url_for("detail", ship_id=ship_id))

# --------- Upload/Remoção de Imagem por Item ---------
@app.post("/ship/<int:ship_id>/item/<int:item_id>/upload_image")
def upload_item_image(ship_id: int, item_id: int):
    file = request.files.get("image")
    if not file or file.filename == "":
        flash("Selecione um arquivo de imagem.", "error")
        return redirect(url_for("detail", ship_id=ship_id))
    if not allowed_file(file.filename):
        flash("Formato inválido. Use png, jpg, jpeg, webp ou gif.", "error")
        return redirect(url_for("detail", ship_id=ship_id))

    # nome único
    ext = file.filename.rsplit(".", 1)[1].lower()
    fname = secure_filename(f"{uuid.uuid4().hex}.{ext}")
    save_path = os.path.join(app.config["UPLOAD_FOLDER"], fname)
    file.save(save_path)

    rel_path = f"uploads/{fname}"  # relativo a /static
    with get_conn() as c:
        c.execute("""UPDATE items SET image_path=? WHERE id=? AND ship_id=?""",
                  (rel_path, item_id, ship_id))
        c.commit()
    flash("Imagem anexada ao item.", "success")
    return redirect(url_for("detail", ship_id=ship_id))

@app.post("/ship/<int:ship_id>/item/<int:item_id>/remove_image")
def remove_item_image(ship_id: int, item_id: int):
    with get_conn() as c:
        row = c.execute("""SELECT image_path FROM items WHERE id=? AND ship_id=?""",
                        (item_id, ship_id)).fetchone()
        if row and row["image_path"]:
            fpath = os.path.join(BASE_DIR, "static", row["image_path"])
            try:
                if os.path.exists(fpath): os.remove(fpath)
            except Exception:
                pass
            c.execute("""UPDATE items SET image_path=NULL WHERE id=? AND ship_id=?""",
                      (item_id, ship_id))
            c.commit()
    flash("Imagem removida do item.", "success")
    return redirect(url_for("detail", ship_id=ship_id))

# ------------------------------ DAO ------------------------------
def get_ship_by_token(token: str) -> Optional[Ship]:
    with get_conn() as c:
        cur = c.execute("SELECT * FROM ships WHERE token=? LIMIT 1", (token,))
        row = row_or_none(cur)
    return dict_to_ship(row) if row else None

def get_ship_by_id(ship_id: int) -> Optional[Ship]:
    with get_conn() as c:
        cur = c.execute("SELECT * FROM ships WHERE id=? LIMIT 1", (ship_id,))
        row = row_or_none(cur)
    return dict_to_ship(row) if row else None

def list_ships() -> list[Ship]:
    with get_conn() as c:
        cur = c.execute("SELECT * FROM ships ORDER BY id DESC")
        rows = rows_to_list(cur)
    return [dict_to_ship(r) for r in rows]

def list_items_for_ship(ship_id: int) -> list[dict]:
    with get_conn() as c:
        cur = c.execute("SELECT * FROM items WHERE ship_id=? ORDER BY id ASC", (ship_id,))
        return rows_to_list(cur)

def confirm_all_items(ship_id: int, who: str):
    with get_conn() as c:
        c.execute("UPDATE items SET confirmed_at=?, confirmed_by=? WHERE ship_id=? AND confirmed_at IS NULL",
                  (now_iso(), who, ship_id))
        c.commit()

def confirm_selected_items(ship_id: int, item_ids: Iterable[int], who: str):
    if not item_ids: return
    placeholders = ",".join(["?"] * len(item_ids))
    with get_conn() as c:
        c.execute(f"""UPDATE items SET confirmed_at=?, confirmed_by=?
                      WHERE ship_id=? AND id IN ({placeholders}) AND confirmed_at IS NULL""",
                  (now_iso(), who, ship_id, *item_ids))
        c.commit()

def mark_items_viewed(ship_id: int, item_ids: Iterable[int], who: str):
    if not item_ids: return
    placeholders = ",".join(["?"] * len(item_ids))
    with get_conn() as c:
        c.execute(f"""UPDATE items SET viewed_at=?, viewed_by=?
                      WHERE ship_id=? AND id IN ({placeholders})""",
                  (now_iso(), who, ship_id, *item_ids))
        c.commit()

def set_ship_viewed_if_first_time(ship_id: int):
    with get_conn() as c:
        c.execute("""UPDATE ships SET viewed_at = COALESCE(viewed_at, ?) WHERE id=?""",
                  (now_iso(), ship_id))
        c.commit()

def set_ship_received_if_done(ship_id: int):
    with get_conn() as c:
        remaining = c.execute("""SELECT COUNT(*) AS n FROM items
                                 WHERE ship_id=? AND confirmed_at IS NULL""",
                              (ship_id,)).fetchone()["n"]
        if remaining == 0:
            c.execute("""UPDATE ships SET received_at = COALESCE(received_at, ?) WHERE id=?""",
                      (now_iso(), ship_id))
        c.commit()

def add_event(ship_id: int, actor: str, etype: str, ip: Optional[str], ua: Optional[str]):
    with get_conn() as c:
        c.execute("""INSERT INTO events (ship_id, ts, actor, type, ip, user_agent)
                     VALUES (?, ?, ?, ?, ?, ?)""",
                  (ship_id, now_iso(), actor, etype, ip, ua))
        c.commit()

def list_events_for_ship(ship_id: int) -> list[dict]:
    with get_conn() as c:
        cur = c.execute("SELECT * FROM events WHERE ship_id=? ORDER BY ts DESC", (ship_id,))
        return rows_to_list(cur)

# ------------------------------ Main ------------------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
