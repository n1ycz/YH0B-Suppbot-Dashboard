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

LAYOUT = """<!doctype html><html lang=en><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Tickets & Applications</title>
<link rel=preconnect href="https://fonts.googleapis.com"><link rel=preconnect href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,500;12..96,700&family=Figtree:wght@400;500;600&display=swap" rel=stylesheet>
<style>
:root{--ink:#0d1020;--raise:#151933;--sunk:#0a0c19;--line:#262c55;--text:#e9ecff;--mute:#8e96c8;--ac:#6c7bff;--ok:#3ddbb0;--bad:#ff6b81;color-scheme:dark}
*{box-sizing:border-box}body{margin:0;min-height:100vh;background:var(--ink);color:var(--text);font:16px/1.55 Figtree,system-ui,sans-serif}
h1,h2,h3{font-family:'Bricolage Grotesque',Figtree,sans-serif;margin:0;letter-spacing:-.02em;line-height:1.1}
a{color:inherit}.top{display:flex;justify-content:space-between;align-items:center;max-width:1080px;margin:0 auto;padding:20px 24px}
.brand{display:flex;gap:10px;align-items:center;text-decoration:none;font:700 18px 'Bricolage Grotesque',sans-serif}
.who{display:flex;gap:12px;align-items:center;color:var(--mute);font-size:14px}
main{max-width:1080px;margin:0 auto;padding:8px 24px 72px}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:8px;border:0;border-radius:12px;padding:11px 20px;background:var(--ac);color:#fff;font:600 15px Figtree,sans-serif;text-decoration:none;cursor:pointer;transition:transform .15s,filter .15s,background .15s}
.btn:hover{filter:brightness(1.12);transform:translateY(-1px)}.btn:active{transform:scale(.97)}
.btn.ghost{background:transparent;border:1px solid var(--line);color:var(--text)}.btn.ghost:hover{background:var(--raise)}
.btn.bad{background:#3a1d2d;color:var(--bad)}.btn.sm{padding:7px 13px;font-size:14px;border-radius:10px}
.btn:disabled{opacity:.55;cursor:progress;transform:none}.btn.busy::after{content:"";width:14px;height:14px;border:2px solid #fff6;border-top-color:#fff;border-radius:50%;animation:spin .7s linear infinite}
:focus-visible{outline:2px solid var(--ok);outline-offset:3px}
label{display:block;font-weight:500;font-size:14px;color:var(--mute);margin:16px 0 6px}
input,select{width:100%;background:var(--sunk);border:1px solid var(--line);border-radius:10px;padding:11px 13px;color:var(--text);font:inherit;transition:border-color .15s,box-shadow .15s}
input:focus,select:focus{outline:0;border-color:var(--ac);box-shadow:0 0 0 3px #6c7bff33}
select{appearance:none;background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='8'%3E%3Cpath d='M1 1l5 5 5-5' fill='none' stroke='%238e96c8' stroke-width='2'/%3E%3C/svg%3E");background-repeat:no-repeat;background-position:right 14px center;padding-right:38px}
.panel{background:var(--raise);border:1px solid var(--line);border-radius:20px;padding:26px}
.lead{color:var(--mute);margin:8px 0 0;max-width:56ch}
.toast{position:fixed;right:20px;bottom:20px;z-index:9;background:var(--raise);border:1px solid var(--ok);border-left-width:5px;border-radius:12px;padding:13px 18px;box-shadow:0 12px 40px #0008;animation:toast .35s cubic-bezier(.2,.9,.3,1.2)}
.toast.out{opacity:0;transform:translateY(12px);transition:.4s}
.hero{display:grid;grid-template-columns:1.05fr 1fr;gap:48px;align-items:center;padding:48px 0}
.hero h1{font-size:clamp(36px,5.2vw,60px)}.hero .btn{margin-top:28px;padding:14px 26px;font-size:17px}
.dc{background:#313338;border-radius:10px;padding:14px 16px;font:14px/1.4 system-ui,sans-serif;color:#dbdee1}
.dc .m{display:flex;gap:12px;margin-top:14px}.dc .m:first-child{margin-top:0}.dc .a{flex:none;width:38px;height:38px;border-radius:50%;background:var(--ac);display:grid;place-items:center;font-weight:700;color:#fff}
.dc b{color:#fff}.dc .emb{border-left:4px solid var(--c,#5865f2);background:#2b2d31;border-radius:4px;padding:10px 12px;margin-top:6px}
.dc .emb p{margin:2px 0 0}.dbtn{display:inline-block;margin-top:8px;padding:6px 14px;border-radius:4px;background:#5865f2;color:#fff;font-weight:500}.dbtn.g{background:#248046}
.hero .dc{box-shadow:0 30px 80px #0007}.hero .m{animation:pop .5s cubic-bezier(.2,.9,.3,1.1) both}.hero .m:nth-child(1){animation-delay:.25s}.hero .m:nth-child(2){animation-delay:1s}.hero .m:nth-child(3){animation-delay:1.8s}
.tiles{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:16px;margin-top:28px}
.tile{display:flex;gap:14px;align-items:center;padding:18px;border-radius:16px;background:var(--raise);border:1px solid var(--line);text-decoration:none;transition:transform .2s,border-color .2s}
a.tile:hover{transform:translateY(-3px);border-color:var(--ac)}.tile.off{opacity:.8}.tile span{font-weight:600}.tile small{display:block;color:var(--mute);font-weight:400}
.ava{flex:none;width:46px;height:46px;border-radius:14px;display:grid;place-items:center;font:700 20px 'Bricolage Grotesque',sans-serif;background:hsl(var(--h) 55% 42%)}
.head{display:flex;align-items:center;gap:16px;margin:12px 0 26px}.head h1{font-size:clamp(28px,4vw,40px)}.back{color:var(--mute);text-decoration:none;font-size:14px}.back:hover{color:var(--text)}
.tabs{position:relative;display:flex;gap:6px;border-bottom:1px solid var(--line);margin-bottom:28px;overflow-x:auto}
.tabs button{background:0;border:0;color:var(--mute);font:600 16px Figtree,sans-serif;padding:12px 16px;cursor:pointer;transition:color .15s;white-space:nowrap}
.tabs button[aria-selected=true],.tabs button:hover{color:var(--text)}.tabs i{position:absolute;bottom:-1px;left:0;height:3px;border-radius:3px;background:var(--ac);transition:transform .3s cubic-bezier(.3,.8,.3,1),width .3s}
.badge{margin-left:8px;background:var(--ac);color:#fff;border-radius:99px;padding:1px 9px;font-size:13px}
.pane{animation:fade .25s}.pane[hidden]{display:none}
.cols{display:grid;grid-template-columns:1.1fr 1fr;gap:24px;align-items:start}.cols>.panel+.panel{position:sticky;top:20px}
.panel h2{font-size:24px;margin-bottom:4px}.panel+.panel{margin-top:0}.stack>.panel{margin-bottom:24px}
.hint{color:var(--mute);font-size:14px;margin:14px 0 0}.hint b{color:var(--text);font-weight:600}
.rows{display:grid;gap:10px;margin:18px 0 0}.row{display:flex;align-items:center;gap:14px;padding:14px 16px;border-radius:14px;background:var(--sunk);border:1px solid var(--line)}
.row .n{flex:1;min-width:0}.row .n b{display:block;overflow:hidden;text-overflow:ellipsis}.row .n small{color:var(--mute)}.row form{margin:0}
.sw{position:relative;width:48px;height:28px;border-radius:99px;border:0;background:#2b3163;cursor:pointer;transition:background .25s}
.sw::after{content:"";position:absolute;top:3px;left:3px;width:22px;height:22px;border-radius:50%;background:#fff;transition:transform .25s cubic-bezier(.3,1.4,.5,1)}
.sw.on{background:var(--ok)}.sw.on::after{transform:translateX(20px)}
.empty{padding:26px;text-align:center;color:var(--mute);border:1px dashed var(--line);border-radius:14px}
.qrow{display:flex;gap:8px;margin-top:8px;animation:rowin .25s ease-out}.qrow.gone{animation:rowout .2s ease-in forwards}
.qrow .x{flex:none;width:42px;border:1px solid var(--line);background:0;color:var(--mute);border-radius:10px;font-size:20px;cursor:pointer;transition:.15s}.qrow .x:hover{color:var(--bad);border-color:var(--bad)}
.split{display:flex;justify-content:space-between;align-items:center;margin-top:10px;color:var(--mute);font-size:14px}
.pair{display:grid;grid-template-columns:1fr 1fr;gap:14px}
@keyframes pop{from{opacity:0;transform:translateY(14px) scale(.97)}}@keyframes fade{from{opacity:0}}@keyframes spin{to{transform:rotate(1turn)}}
@keyframes toast{from{opacity:0;transform:translateY(20px)}}@keyframes rowin{from{opacity:0;transform:translateX(-10px)}}@keyframes rowout{to{opacity:0;transform:translateX(16px);height:0;margin:0}}
@media(max-width:820px){.hero,.cols,.pair{grid-template-columns:1fr}.hero{padding-top:12px;gap:32px}.cols>.panel+.panel{position:static}}
@media(prefers-reduced-motion:reduce){*,::before,::after{animation:none!important;transition:none!important}}
</style>
<header class=top><a class=brand href="/"><svg width=28 height=28 viewBox="0 0 28 28"><rect x=2 y=7 width=17 height=17 rx=5 fill="#6c7bff"/><rect x=9 y=2 width=17 height=17 rx=5 fill="#3ddbb0" fill-opacity=".9"/></svg>Tickets &amp; Applications</a>
{% if session.uid %}<div class=who>{{ session.name }}<a class="btn ghost sm" href="/logout">Log out</a></div>{% endif %}</header>
{% if msg %}<div class=toast role=status>{{ msg }}</div>{% endif %}
<main>{{ body }}</main>
<script>
(function(){
var $=function(s,r){return(r||document).querySelector(s)},$$=function(s,r){return[].slice.call((r||document).querySelectorAll(s))};
var t=$('.toast');if(t)setTimeout(function(){t.classList.add('out')},4200);
$$('form').forEach(function(f){f.addEventListener('submit',function(){var b=f.querySelector('button.btn');if(b)setTimeout(function(){b.disabled=true;b.classList.add('busy')},0)})});
var tabs=$$('.tabs button'),panes=$$('.pane'),ind=$('.tabs i');
function show(n){tabs.forEach(function(b){var on=b.dataset.t===n;b.setAttribute('aria-selected',on);if(on){ind.style.width=b.offsetWidth+'px';ind.style.transform='translateX('+b.offsetLeft+'px)'}});panes.forEach(function(p){p.hidden=p.id!=='p-'+n})}
if(tabs.length){tabs.forEach(function(b){b.addEventListener('click',function(){history.replaceState(null,'','#'+b.dataset.t);show(b.dataset.t)})});
var cur=function(){return location.hash==='#apps'?'apps':'tickets'};show(cur());addEventListener('resize',function(){show(cur())});}
function lab(s,e){var o=s.options[s.selectedIndex];return o&&o.value?o.text:e}
[['cat','pvcat','no category'],['role','pvrole','only the ticket owner'],['rev','pvrev','no channel yet'],['acc','pvacc','no role']].forEach(function(a){var s=$('#'+a[0]),o=$('#'+a[1]);if(s&&o){var u=function(){o.textContent=lab(s,a[2])};s.addEventListener('change',u);u()}});
var qs=$('#qs'),add=$('#addq'),qc=$('#qc');
function count(){if(qc)qc.textContent=$$('.qrow:not(.gone)',qs).length+' of 25 questions'}
function row(focus){if($$('.qrow:not(.gone)',qs).length>=25)return;var d=document.createElement('div');d.className='qrow';
var i=document.createElement('input');i.name='q';i.maxLength=250;i.placeholder='Type a question';i.setAttribute('aria-label','Question');
var x=document.createElement('button');x.type='button';x.className='x';x.textContent='\u00d7';x.setAttribute('aria-label','Remove question');
d.appendChild(i);d.appendChild(x);qs.appendChild(d);if(focus)i.focus();count()}
if(qs){add.addEventListener('click',function(){row(true)});
qs.addEventListener('click',function(e){if(e.target.className==='x'){var r=e.target.parentNode;if($$('.qrow:not(.gone)',qs).length>1){r.classList.add('gone');setTimeout(function(){r.remove();count()},200)}else r.firstChild.value=''}});
qs.addEventListener('keydown',function(e){if(e.key==='Enter'&&e.target.name==='q'){e.preventDefault();row(true)}});
if(!$$('.qrow',qs).length)row(false);count()}
$$('form[data-confirm]').forEach(function(f){f.addEventListener('submit',function(e){if(!confirm(f.dataset.confirm))e.preventDefault()})});
})();
</script>"""

def opts(items, sel=None):
    out = '<option value="">– none –</option>'
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
        return await page("""<section class=hero><div>
  <h1>Tickets and applications for your server, ready in minutes.</h1>
  <p class=lead>Log in with Discord, pick a server and send your panels. Build as many application forms as you like, each with its own questions and review channel.</p>
  <a class=btn href="/login">Log in with Discord</a></div>
 <div class=dc aria-hidden=true>
  <div class=m><div class=a>S</div><div><b>Support bot</b><div class=emb><b>Support</b><p>Click the button below to open a ticket.</p></div><span class=dbtn>Create ticket</span></div></div>
  <div class=m><div class=a style="background:#248046">M</div><div><b>Moderator form</b><div>Why do you want to join the team?</div></div></div>
  <div class=m><div class=a>S</div><div><b>Support bot</b><div class=emb style="--c:#3ddbb0"><b>Application accepted</b><p>Welcome to the team!</p></div></div></div>
 </div></section>""")
    ids = await bot_guild_ids()
    mine = [dict(g, has_bot=g["id"] in ids, h=int(g["id"]) % 360, ini=(g["name"][:1] or "?").upper(), invite="https://discord.com/oauth2/authorize?" + urlencode({
        "client_id": CLIENT_ID, "scope": "bot applications.commands", "permissions": 268520464, "guild_id": g["id"]}))
        for g in session.get("guilds", [])]
    return await page("""<h1 style="font-size:clamp(30px,4vw,44px);margin-top:12px">Your servers</h1>
 <p class=lead>Pick a server to manage its tickets and applications. Servers without the bot show an invite link.</p>
 <div class=tiles>{% for g in mine %}
  {% if g.has_bot %}<a class=tile href="/g/{{ g.id }}"><div class=ava style="--h:{{ g.h }}">{{ g.ini }}</div><span>{{ g.name }}<small>Open dashboard</small></span></a>
  {% else %}<a class="tile off" href="{{ g.invite }}"><div class=ava style="--h:{{ g.h }}">{{ g.ini }}</div><span>{{ g.name }}<small>Invite the bot first</small></span></a>{% endif %}
 {% else %}<div class=empty style="grid-column:1/-1">No servers found. You need the Manage Server permission on a server to see it here.</div>{% endfor %}</div>""", mine=mine)

@web.route("/g/<gid>")
@guild_route
async def guild_page(gid, gname):
    text, cats, roles = await gdata(gid)
    s = dict(await db("SELECT * FROM settings WHERE guild_id=$1", gid, mode="fetchrow") or {})
    forms = [dict(r, n=len(json.loads(r["questions"]))) for r in await db("SELECT * FROM forms WHERE guild_id=$1 ORDER BY id", gid)]
    pending = await db("SELECT COUNT(*) FROM apps WHERE guild_id=$1 AND status='pending'", gid, mode="fetchval")
    return await page("""<a class=back href="/">&larr; All servers</a>
<div class=head><h1>{{ gname }}</h1></div>
<nav class=tabs role=tablist><button data-t=tickets role=tab>Tickets</button><button data-t=apps role=tab>Applications{% if pending %}<span class=badge>{{ pending }}</span>{% endif %}</button><i></i></nav>

<section class=pane id=p-tickets><div class=cols>
 <div class=panel><h2>Ticket settings</h2><p class=lead>Choose where new tickets appear and who can see them.</p>
  <form method=post action="/g/{{ gid }}/tickets"><input type=hidden name=csrf value="{{ session.csrf }}">
   <label for=cat>Ticket category</label><select id=cat name=ticket_category>{{ opts(cats, s.ticket_category) }}</select>
   <label for=role>Support role</label><select id=role name=support_role>{{ opts(roles, s.support_role) }}</select>
   <p style="margin:22px 0 0"><button class=btn>Save settings</button></p></form></div>
 <div class=panel><h2>Ticket panel</h2><p class=lead>This is what members will see in Discord.</p>
  <div class=dc style="margin-top:18px"><div class=m><div class=a>S</div><div><b>Support bot</b><div class=emb><b>Support</b><p>Click the button below to open a ticket.</p></div><span class=dbtn>Create ticket</span></div></div></div>
  <p class=hint>Tickets open under <b id=pvcat>no category</b> and are visible to <b id=pvrole>only the ticket owner</b>.</p>
  <form method=post action="/g/{{ gid }}/panel/ticket"><input type=hidden name=csrf value="{{ session.csrf }}">
   <label for=tch>Send the panel to</label><select id=tch name=channel>{{ opts(text) }}</select>
   <p style="margin:22px 0 0"><button class=btn>Send ticket panel</button></p></form></div>
</div></section>

<section class=pane id=p-apps hidden><div class=stack>
 <div class=panel><h2>Your forms</h2><p class=lead>Closed forms disappear from the Apply menu right away. Pending applications: <b>{{ pending }}</b>.</p>
  <div class=rows>{% for f in forms %}<div class=row>
   <div class=n><b>{{ f.name }}</b><small>{{ f.n }} question{{ '' if f.n == 1 else 's' }} &middot; {{ 'accepting applications' if f.open else 'closed' }}</small></div>
   <form method=post action="/g/{{ gid }}/form/{{ f.id }}/toggle"><input type=hidden name=csrf value="{{ session.csrf }}"><button class="sw {{ 'on' if f.open }}" aria-label="{{ 'Close form' if f.open else 'Open form' }}" title="{{ 'Close form' if f.open else 'Open form' }}"></button></form>
   <form method=post data-confirm="Delete this form?" action="/g/{{ gid }}/form/{{ f.id }}/delete"><input type=hidden name=csrf value="{{ session.csrf }}"><button class="btn bad sm">Delete</button></form></div>
  {% else %}<div class=empty>No forms yet. Create your first one below and it shows up in the Apply menu straight away.</div>{% endfor %}</div></div>

 <div class=panel><h2>New form</h2><p class=lead>Applicants answer each question in a DM with the bot.</p>
  <form method=post action="/g/{{ gid }}/form"><input type=hidden name=csrf value="{{ session.csrf }}">
   <label for=fname>Form name</label><input id=fname name=name maxlength=100 placeholder="Moderator" required>
   <label>Questions</label><div id=qs></div>
   <div class=split><span id=qc></span><button type=button id=addq class="btn ghost sm">Add question</button></div>
   <div class=pair><div><label for=rev>Review channel</label><select id=rev name=review_channel>{{ opts(text) }}</select></div>
   <div><label for=acc>Role on acceptance</label><select id=acc name=accept_role>{{ opts(roles) }}</select></div></div>
   <p class=hint>Submissions go to <b id=pvrev>no channel yet</b>. Accepted applicants receive <b id=pvacc>no role</b>.</p>
   <p style="margin:22px 0 0"><button class=btn>Create form</button></p></form></div>

 <div class=panel><h2>Application panel</h2><p class=lead>One button that lists all open forms.</p>
  <div class=dc style="margin-top:18px"><div class=m><div class=a>S</div><div><b>Support bot</b><div class=emb style="--c:#57f287"><b>Applications</b><p>Click the button below to apply.</p></div><span class="dbtn g">Apply</span></div></div></div>
  <form method=post action="/g/{{ gid }}/panel/apply"><input type=hidden name=csrf value="{{ session.csrf }}">
   <label for=ach>Send the panel to</label><select id=ach name=channel>{{ opts(text) }}</select>
   <p style="margin:22px 0 0"><button class=btn>Send application panel</button></p></form></div>
</div></section>""", gname=gname, gid=gid, s=s, forms=forms, pending=pending, text=text, cats=cats, roles=roles)

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
    session["msg"] = "Ticket settings saved."
    return redirect(f"/g/{gid}")

@web.post("/g/<gid>/form")
@guild_route
async def add_form(gid, gname):
    f = await request.form
    text, cats, roles = await gdata(gid)
    qs = [x.strip()[:250] for x in f.getlist("q") if x.strip()][:25]
    if not qs:
        session["msg"] = "Add at least one question."
        return redirect(f"/g/{gid}#apps")
    rc = f.get("review_channel") if f.get("review_channel") in {c["id"] for c in text} else None
    ar = f.get("accept_role") if f.get("accept_role") in {r["id"] for r in roles} else None
    await db("INSERT INTO forms(guild_id,name,questions,review_channel,accept_role) VALUES($1,$2,$3,$4,$5)",
             gid, f["name"][:100], json.dumps(qs), rc, ar, mode="execute")
    session["msg"] = "Form created."
    return redirect(f"/g/{gid}#apps")

@web.post("/g/<gid>/form/<fid>/<action>")
@guild_route
async def form_action(gid, gname, fid, action):
    if action == "toggle": await db("UPDATE forms SET open=NOT open WHERE id=$1 AND guild_id=$2", int(fid), gid, mode="execute")
    elif action == "delete": await db("DELETE FROM forms WHERE id=$1 AND guild_id=$2", int(fid), gid, mode="execute")
    return redirect(f"/g/{gid}#apps")

@web.post("/g/<gid>/panel/<kind>")
@guild_route
async def send_panel(gid, gname, kind):
    f = await request.form
    back = f"/g/{gid}" + ("#apps" if kind == "apply" else "")
    text, _, _ = await gdata(gid)
    ch = f.get("channel")
    if ch not in {c["id"] for c in text}:
        session["msg"] = "Please choose a channel."
        return redirect(back)
    if kind == "ticket":
        payload = {"embeds": [{"title": "Support", "description": "Click the button below to open a ticket.", "color": 0x5865F2}],
                   "components": [{"type": 1, "components": [{"type": 2, "style": 1, "label": "Create ticket", "custom_id": "ticket_open"}]}]}
    else:
        payload = {"embeds": [{"title": "Applications", "description": "Click the button below to apply.", "color": 0x57F287}],
                   "components": [{"type": 1, "components": [{"type": 2, "style": 3, "label": "Apply", "custom_id": "apply_open"}]}]}
    st, _ = await dapi("POST", f"/channels/{ch}/messages", json=payload)
    session["msg"] = "Panel sent." if st < 300 else "Sending failed – does the bot have permission to send messages in that channel?"
    return redirect(back)
