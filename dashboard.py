import os, json, time, secrets
from datetime import timedelta
from functools import wraps
from urllib.parse import urlencode, urlsplit, urlunsplit, parse_qsl

import aiohttp, asyncpg
from dotenv import load_dotenv
from markupsafe import Markup, escape
from quart import Quart, request, session, redirect, render_template_string, abort

load_dotenv()
TOKEN, CLIENT_ID, CLIENT_SECRET = os.environ["DISCORD_TOKEN"], os.environ["CLIENT_ID"], os.environ["CLIENT_SECRET"]
BASE_URL = os.environ["BASE_URL"].rstrip("/")
API = "https://discord.com/api/v10"

def clean_dsn(u):
    p = urlsplit(u)
    return urlunsplit(p._replace(query=urlencode([(k, v) for k, v in parse_qsl(p.query) if k != "channel_binding"])))

SCHEMA = """
CREATE TABLE IF NOT EXISTS forms(id BIGSERIAL PRIMARY KEY, guild_id TEXT, name TEXT, questions TEXT,
  review_channel TEXT, accept_role TEXT, open BOOLEAN DEFAULT TRUE);
CREATE TABLE IF NOT EXISTS settings(guild_id TEXT PRIMARY KEY, ticket_category TEXT, support_role TEXT);
CREATE TABLE IF NOT EXISTS apps(id BIGSERIAL PRIMARY KEY, guild_id TEXT, form_id BIGINT, user_id TEXT,
  answers TEXT, status TEXT DEFAULT 'pending');
"""

web = Quart(__name__)
web.secret_key = os.environ["SESSION_SECRET"]
web.config.update(SESSION_COOKIE_SECURE=BASE_URL.startswith("https"), SESSION_COOKIE_SAMESITE="Lax",
                  PERMANENT_SESSION_LIFETIME=timedelta(days=7))
pool = http = None

@web.before_serving
async def start():
    global pool, http
    pool = await asyncpg.create_pool(clean_dsn(os.environ["DATABASE_URL"]), min_size=0, max_size=3,
                                     statement_cache_size=0, max_inactive_connection_lifetime=30)
    http = aiohttp.ClientSession()
    async with pool.acquire() as c: await c.execute(SCHEMA)

@web.after_serving
async def stop():
    await http.close(); await pool.close()

async def dapi(method, path, **kw):
    async with http.request(method, API + path, headers={"Authorization": f"Bot {TOKEN}"}, **kw) as r:
        text = await r.text()
        try: data = json.loads(text) if text else None
        except ValueError: data = None
        return r.status, data

_cache = {"t": 0, "ids": set()}
async def bot_guild_ids():
    if time.time() - _cache["t"] > 60:
        st, d = await dapi("GET", "/users/@me/guilds?limit=200")
        if st == 200: _cache.update(t=time.time(), ids={g["id"] for g in d})
    return _cache["ids"]

async def gdata(gid):
    _, ch = await dapi("GET", f"/guilds/{gid}/channels")
    _, ro = await dapi("GET", f"/guilds/{gid}/roles")
    ch, ro = ch if isinstance(ch, list) else [], ro if isinstance(ro, list) else []
    text = [{"id": c["id"], "name": "#" + c["name"]} for c in sorted(ch, key=lambda c: c.get("position", 0)) if c["type"] == 0]
    cats = [{"id": c["id"], "name": c["name"]} for c in ch if c["type"] == 4]
    roles = [{"id": r["id"], "name": r["name"]} for r in sorted(ro, key=lambda r: -r["position"]) if r["id"] != gid and not r.get("managed")]
    return text, cats, roles

async def db(sql, *a, mode="fetch"):
    for n in range(2):
        try:
            return await getattr(pool, mode)(sql, *a)
        except (asyncpg.PostgresConnectionError, asyncpg.InterfaceError, ConnectionError, OSError):
            if n: raise

LAYOUT = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Bot Dashboard</title><style>
body{font-family:system-ui;background:#1e1f22;color:#dbdee1;max-width:760px;margin:0 auto;padding:16px}
a{color:#00a8fc}input,select,textarea,button{background:#2b2d31;color:#dbdee1;border:1px solid #3f4147;border-radius:6px;padding:8px;font:inherit;width:100%;box-sizing:border-box;margin:4px 0}
button{background:#5865f2;border:0;cursor:pointer;width:auto;padding:8px 16px}.card{background:#2b2d31;border-radius:8px;padding:16px;margin:12px 0}
.card input,.card select,.card textarea{background:#1e1f22}nav{display:flex;justify-content:space-between}.danger{background:#da373c}form.i{display:inline}
.msg{background:#3a3d44;border-left:4px solid #5865f2;padding:10px;border-radius:6px}
</style><nav><a href="/">Dashboard</a>{% if session.uid %}<span>{{ session.name }} · <a href="/logout">Logout</a></span>{% endif %}</nav>
{% if msg %}<p class=msg>{{ msg }}</p>{% endif %}{{ body }}"""

def opts(items, sel=None):
    out = '<option value="">– keine –</option>'
    for x in items:
        out += f'<option value="{x["id"]}" {"selected" if str(x["id"]) == str(sel) else ""}>{escape(x["name"])}</option>'
    return Markup(out)

async def page(tpl, **ctx):
    body = await render_template_string(tpl, opts=opts, **ctx)
    return await render_template_string(LAYOUT, body=Markup(body), msg=session.pop("msg", None))

def can_manage(g):
    p = int(g["permissions"]); return bool(g.get("owner") or p & 0x20 or p & 0x8)

def guild_route(fn):
    @wraps(fn)
    async def w(gid, **k):
        if "uid" not in session: return redirect("/login")
        g = next((x for x in session.get("guilds", []) if x["id"] == gid), None)
        if not g or gid not in await bot_guild_ids(): abort(403)
        return await fn(gid, g["name"], **k)
    return w

@web.before_request
async def csrf():
    if request.method == "POST":
        f = await request.form
        if not session.get("csrf") or f.get("csrf") != session["csrf"]: abort(403)

@web.route("/login")
async def login():
    return redirect("https://discord.com/oauth2/authorize?" + urlencode({
        "client_id": CLIENT_ID, "response_type": "code", "scope": "identify guilds", "redirect_uri": BASE_URL + "/callback"}))

@web.route("/callback")
async def callback():
    async with http.post("https://discord.com/api/oauth2/token", data={
        "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET, "grant_type": "authorization_code",
        "code": request.args.get("code", ""), "redirect_uri": BASE_URL + "/callback"}) as r:
        t = await r.json()
    if "access_token" not in t: return redirect("/login")
    h = {"Authorization": f"Bearer {t['access_token']}"}
    async with http.get("https://discord.com/api/users/@me", headers=h) as r: u = await r.json()
    async with http.get("https://discord.com/api/users/@me/guilds", headers=h) as r: gs = await r.json()
    session.clear(); session.permanent = True
    session.update(uid=u["id"], name=u["username"], csrf=secrets.token_hex(16),
                   guilds=[{"id": g["id"], "name": g["name"][:30]} for g in gs if can_manage(g)][:30])
    return redirect("/")

@web.route("/logout")
async def logout():
    session.clear(); return redirect("/")

@web.route("/")
async def index():
    if "uid" not in session:
        return await page('<div class=card><h2>Application &amp; Ticket Bot</h2><a href="/login">Mit Discord einloggen</a></div>')
    ids = await bot_guild_ids()
    mine = [dict(g, has_bot=g["id"] in ids, invite="https://discord.com/oauth2/authorize?" + urlencode({
        "client_id": CLIENT_ID, "scope": "bot applications.commands", "permissions": 268520464, "guild_id": g["id"]}))
        for g in session.get("guilds", [])]
    return await page("""<h2>Deine Server</h2>{% for g in mine %}<div class=card>{{ g.name }} —
      {% if g.has_bot %}<a href="/g/{{ g.id }}">Verwalten</a>{% else %}<a href="{{ g.invite }}">Bot einladen</a>{% endif %}</div>
      {% else %}<p>Keine Server mit Verwalten-Rechten gefunden.</p>{% endfor %}""", mine=mine)

@web.route("/g/<gid>")
@guild_route
async def guild_page(gid, gname):
    text, cats, roles = await gdata(gid)
    s = dict(await db("SELECT * FROM settings WHERE guild_id=$1", gid, mode="fetchrow") or {})
    forms = [dict(r, n=len(json.loads(r["questions"]))) for r in await db("SELECT * FROM forms WHERE guild_id=$1 ORDER BY id", gid)]
    pending = await db("SELECT COUNT(*) FROM apps WHERE guild_id=$1 AND status='pending'", gid, mode="fetchval")
    return await page("""<h2>{{ gname }}</h2>
<div class=card><h3>Tickets</h3>
 <form method=post action="/g/{{ gid }}/tickets"><input type=hidden name=csrf value="{{ session.csrf }}">
  Kategorie<select name=ticket_category>{{ opts(cats, s.ticket_category) }}</select>
  Support-Rolle<select name=support_role>{{ opts(roles, s.support_role) }}</select><button>Speichern</button></form>
 <form method=post action="/g/{{ gid }}/panel/ticket"><input type=hidden name=csrf value="{{ session.csrf }}">
  Panel in Channel senden<select name=channel>{{ opts(text) }}</select><button>Ticket-Panel senden</button></form></div>
<div class=card><h3>Applications</h3><p>Offene Bewerbungen: {{ pending }}</p>
 {% for f in forms %}<div class=card><b>{{ f.name }}</b> ({{ 'offen' if f.open else 'geschlossen' }}) — {{ f.n }} Fragen
  <form class=i method=post action="/g/{{ gid }}/form/{{ f.id }}/toggle"><input type=hidden name=csrf value="{{ session.csrf }}"><button>{{ 'Schließen' if f.open else 'Öffnen' }}</button></form>
  <form class=i method=post action="/g/{{ gid }}/form/{{ f.id }}/delete"><input type=hidden name=csrf value="{{ session.csrf }}"><button class=danger>Löschen</button></form></div>{% endfor %}
 <h4>Neues Formular</h4>
 <form method=post action="/g/{{ gid }}/form"><input type=hidden name=csrf value="{{ session.csrf }}">
  <input name=name placeholder="Name (z.B. Moderator)" required>
  <textarea name=questions rows=6 placeholder="Eine Frage pro Zeile (max. 25)" required></textarea>
  Review-Channel<select name=review_channel>{{ opts(text) }}</select>
  Rolle bei Annahme<select name=accept_role>{{ opts(roles) }}</select><button>Erstellen</button></form>
 <form method=post action="/g/{{ gid }}/panel/apply"><input type=hidden name=csrf value="{{ session.csrf }}">
  Panel in Channel senden<select name=channel>{{ opts(text) }}</select><button>Application-Panel senden</button></form></div>""",
        gname=gname, gid=gid, s=s, forms=forms, pending=pending, text=text, cats=cats, roles=roles)

@web.post("/g/<gid>/tickets")
@guild_route
async def save_tickets(gid, gname):
    f = await request.form
    text, cats, roles = await gdata(gid)
    cat = f.get("ticket_category") if f.get("ticket_category") in {c["id"] for c in cats} else None
    role = f.get("support_role") if f.get("support_role") in {r["id"] for r in roles} else None
    await db("""INSERT INTO settings(guild_id,ticket_category,support_role) VALUES($1,$2,$3) ON CONFLICT(guild_id)
                DO UPDATE SET ticket_category=EXCLUDED.ticket_category, support_role=EXCLUDED.support_role""",
             gid, cat, role, mode="execute")
    session["msg"] = "Ticket-Einstellungen gespeichert."
    return redirect(f"/g/{gid}")

@web.post("/g/<gid>/form")
@guild_route
async def add_form(gid, gname):
    f = await request.form
    text, cats, roles = await gdata(gid)
    qs = [x.strip()[:250] for x in f["questions"].splitlines() if x.strip()][:25]
    rc = f.get("review_channel") if f.get("review_channel") in {c["id"] for c in text} else None
    ar = f.get("accept_role") if f.get("accept_role") in {r["id"] for r in roles} else None
    await db("INSERT INTO forms(guild_id,name,questions,review_channel,accept_role) VALUES($1,$2,$3,$4,$5)",
             gid, f["name"][:100], json.dumps(qs), rc, ar, mode="execute")
    session["msg"] = "Formular erstellt."
    return redirect(f"/g/{gid}")

@web.post("/g/<gid>/form/<fid>/<action>")
@guild_route
async def form_action(gid, gname, fid, action):
    if action == "toggle": await db("UPDATE forms SET open=NOT open WHERE id=$1 AND guild_id=$2", int(fid), gid, mode="execute")
    elif action == "delete": await db("DELETE FROM forms WHERE id=$1 AND guild_id=$2", int(fid), gid, mode="execute")
    return redirect(f"/g/{gid}")

@web.post("/g/<gid>/panel/<kind>")
@guild_route
async def send_panel(gid, gname, kind):
    f = await request.form
    text, _, _ = await gdata(gid)
    ch = f.get("channel")
    if ch not in {c["id"] for c in text}:
        session["msg"] = "Bitte einen Channel wählen."
        return redirect(f"/g/{gid}")
    if kind == "ticket":
        payload = {"embeds": [{"title": "Support", "description": "Klicke unten, um ein Ticket zu öffnen.", "color": 0x5865F2}],
                   "components": [{"type": 1, "components": [{"type": 2, "style": 1, "label": "Ticket erstellen", "custom_id": "ticket_open"}]}]}
    else:
        payload = {"embeds": [{"title": "Bewerbungen", "description": "Klicke unten, um dich zu bewerben.", "color": 0x57F287}],
                   "components": [{"type": 1, "components": [{"type": 2, "style": 3, "label": "Bewerben", "custom_id": "apply_open"}]}]}
    st, _ = await dapi("POST", f"/channels/{ch}/messages", json=payload)
    session["msg"] = "Panel gesendet." if st < 300 else "Senden fehlgeschlagen – hat der Bot Schreibrechte in diesem Channel?"
    return redirect(f"/g/{gid}")
