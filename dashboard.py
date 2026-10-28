import asyncio, csv, io, os, re, json, time, secrets
from datetime import datetime, timedelta, timezone
from functools import wraps
from urllib.parse import urlencode, urlsplit, urlunsplit, parse_qsl

import aiohttp, asyncpg
from dotenv import load_dotenv
from markupsafe import Markup, escape
from quart import Quart, Response, request, session, redirect, render_template_string, abort

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
CREATE TABLE IF NOT EXISTS panels(id BIGSERIAL PRIMARY KEY, guild_id TEXT, kind TEXT, name TEXT, style TEXT DEFAULT 'button',
  title TEXT, description TEXT, color TEXT DEFAULT '#5865F2', button_label TEXT, placeholder TEXT,
  options TEXT DEFAULT '[]', forms TEXT DEFAULT '[]', channel_id TEXT, message_id TEXT);
ALTER TABLE settings ADD COLUMN IF NOT EXISTS ticket_log TEXT;
ALTER TABLE settings ADD COLUMN IF NOT EXISTS app_log TEXT;
ALTER TABLE panels ADD COLUMN IF NOT EXISTS ticket_category TEXT;
ALTER TABLE panels ADD COLUMN IF NOT EXISTS support_role TEXT;
ALTER TABLE panels ADD COLUMN IF NOT EXISTS ticket_log TEXT;
ALTER TABLE panels ADD COLUMN IF NOT EXISTS welcome TEXT;
ALTER TABLE panels ADD COLUMN IF NOT EXISTS entry_desc TEXT;
ALTER TABLE panels ADD COLUMN IF NOT EXISTS members TEXT DEFAULT '[]';
CREATE TABLE IF NOT EXISTS tickets(id BIGSERIAL PRIMARY KEY, guild_id TEXT, num INT, channel_id TEXT, user_id TEXT, user_name TEXT,
  panel_id BIGINT DEFAULT 0, label TEXT, status TEXT DEFAULT 'open', opened_at TIMESTAMPTZ DEFAULT now(), closed_at TIMESTAMPTZ, closed_by TEXT);
CREATE TABLE IF NOT EXISTS blacklist(guild_id TEXT, user_id TEXT, reason TEXT, PRIMARY KEY(guild_id, user_id));
ALTER TABLE settings ADD COLUMN IF NOT EXISTS transcripts TEXT DEFAULT 'log';
ALTER TABLE forms ADD COLUMN IF NOT EXISTS cooldown_hours INT DEFAULT 0;
ALTER TABLE forms ADD COLUMN IF NOT EXISTS required_role TEXT;
ALTER TABLE forms ADD COLUMN IF NOT EXISTS accept_msg TEXT;
ALTER TABLE forms ADD COLUMN IF NOT EXISTS deny_msg TEXT;
ALTER TABLE apps ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT now();
ALTER TABLE apps ADD COLUMN IF NOT EXISTS user_name TEXT;
ALTER TABLE apps ADD COLUMN IF NOT EXISTS reviewer TEXT;
ALTER TABLE apps ADD COLUMN IF NOT EXISTS reviewed_at TIMESTAMPTZ;
ALTER TABLE panels ADD COLUMN IF NOT EXISTS image_url TEXT;
ALTER TABLE panels ADD COLUMN IF NOT EXISTS footer TEXT;
ALTER TABLE panels ADD COLUMN IF NOT EXISTS button_color TEXT;
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
ICONS, USERS = {}, {}
async def bot_guild_ids():
    if time.time() - _cache["t"] > 60:
        st, d = await dapi("GET", "/users/@me/guilds?limit=200")
        if st == 200:
            _cache.update(t=time.time(), ids={g["id"] for g in d})
            ICONS.update({g["id"]: g.get("icon") for g in d})
    return _cache["ids"]

async def gdata(gid):
    _, ch = await dapi("GET", f"/guilds/{gid}/channels")
    _, ro = await dapi("GET", f"/guilds/{gid}/roles")
    ch, ro = ch if isinstance(ch, list) else [], ro if isinstance(ro, list) else []
    text = [{"id": c["id"], "name": "#" + c["name"]} for c in sorted(ch, key=lambda c: c.get("position", 0)) if c["type"] == 0]
    cats = [{"id": c["id"], "name": c["name"]} for c in ch if c["type"] == 4]
    roles = [{"id": r["id"], "name": r["name"]} for r in sorted(ro, key=lambda r: -r["position"]) if r["id"] != gid and not r.get("managed")]
    return text, cats, roles

def ago(dt):
    if not dt: return ""
    sec = int((datetime.now(timezone.utc) - dt).total_seconds())
    for n, u in ((86400, "d"), (3600, "h"), (60, "m")):
        if sec >= n: return f"{sec // n}{u} ago"
    return "just now"
web.jinja_env.globals.update(ago=ago)

async def uname(uid):
    if uid not in USERS:
        st, d = await dapi("GET", f"/users/{uid}")
        USERS[uid] = (d.get("global_name") or d.get("username")) if st == 200 and isinstance(d, dict) else None
    return USERS[uid]

def icon(path): return Markup(f'<svg viewBox="0 0 24 24" width=22 height=22 fill=none stroke=currentColor stroke-width=1.8 stroke-linecap=round stroke-linejoin=round>{path}</svg>')
ICO = {"ticket": icon('<path d="M4 8a2 2 0 0 0 0 8v2a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-2a2 2 0 0 1 0-8V6a1 1 0 0 0-1-1H5a1 1 0 0 0-1 1z"/><path d="M14 5v14" stroke-dasharray="2 3"/>'),
       "trend": icon('<path d="M3 17l6-6 4 4 8-8"/><path d="M15 7h6v6"/>'), "clock": icon('<circle cx=12 cy=12 r=9 /><path d="M12 7v5l3 2"/>'),
       "inbox": icon('<path d="M4 13l2-7h12l2 7v5H4z"/><path d="M4 13h5l1 2h4l1-2h5"/>')}
SERIES_SQL = """SELECT g::date AS day,
 (SELECT COUNT(*) FROM tickets t WHERE t.guild_id=$1 AND t.opened_at::date=g::date) AS t,
 (SELECT COUNT(*) FROM apps a WHERE a.guild_id=$1 AND a.created_at::date=g::date) AS a
 FROM generate_series(current_date-13, current_date, interval '1 day') g"""

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
:root{--bg:#05060f;--text:#eef0ff;--mute:#9aa3d6;--ac:#7c8cff;--vi:#a97bff;--ok:#3ddbb0;--bad:#ff6b81;--glass:rgba(255,255,255,.05);--edge:rgba(255,255,255,.11);--ease:cubic-bezier(.22,1,.36,1);color-scheme:dark}
*{box-sizing:border-box}html{scroll-behavior:smooth}::selection{background:rgba(124,140,255,.45)}
body{margin:0;min-height:100vh;background:var(--bg);color:var(--text);font:16px/1.55 Figtree,system-ui,sans-serif;overflow-x:hidden}
h1,h2,h3{font-family:'Bricolage Grotesque',Figtree,sans-serif;margin:0;letter-spacing:-.02em;line-height:1.1}
a{color:inherit}option{background:#12142b;color:#fff}
::-webkit-scrollbar{width:10px;height:10px}::-webkit-scrollbar-thumb{background:rgba(255,255,255,.14);border-radius:9px;border:2px solid transparent;background-clip:content-box}::-webkit-scrollbar-thumb:hover{background:rgba(255,255,255,.26);background-clip:content-box;border:2px solid transparent}
.bg{position:fixed;inset:0;z-index:-1;overflow:hidden;pointer-events:none;background:radial-gradient(1200px 700px at 50% -12%,#151b4d,transparent 62%),linear-gradient(180deg,#05060f,#080a1b 55%,#05060f)}
.bg i{position:absolute;border-radius:50%;filter:blur(100px);mix-blend-mode:screen;will-change:transform;animation:drift 30s ease-in-out infinite alternate}
.bg i:nth-child(1){width:54vmax;height:54vmax;left:-18vmax;top:-22vmax;background:radial-gradient(circle,#4a56ff,transparent 66%);opacity:.6}
.bg i:nth-child(2){width:46vmax;height:46vmax;right:-16vmax;top:2vh;background:radial-gradient(circle,#a05cff,transparent 66%);opacity:.52;animation-duration:37s;animation-delay:-12s}
.bg i:nth-child(3){width:42vmax;height:42vmax;left:18vw;bottom:-26vmax;background:radial-gradient(circle,#14c8b2,transparent 66%);opacity:.42;animation-duration:43s;animation-delay:-22s}
.bg::before{content:"";position:absolute;inset:-50%;background:conic-gradient(from 0deg at 50% 38%,transparent,rgba(124,140,255,.13),transparent 30%,rgba(61,219,176,.09),transparent 60%,rgba(169,123,255,.13),transparent);filter:blur(46px);animation:spin 120s linear infinite}
.bg::after{content:"";position:absolute;inset:0;background-image:linear-gradient(rgba(255,255,255,.04) 1px,transparent 1px),linear-gradient(90deg,rgba(255,255,255,.04) 1px,transparent 1px);background-size:56px 56px;-webkit-mask-image:radial-gradient(ellipse 70% 60% at 50% 18%,#000,transparent);mask-image:radial-gradient(ellipse 70% 60% at 50% 18%,#000,transparent);animation:grid 16s linear infinite}
.bg b{position:absolute;inset:0;opacity:.08;mix-blend-mode:overlay;background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='160' height='160'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='.8' numOctaves='2' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E")}
.glow{position:fixed;top:0;left:0;width:560px;height:560px;margin:-280px 0 0 -280px;border-radius:50%;background:radial-gradient(circle,rgba(124,140,255,.17),transparent 65%);pointer-events:none;z-index:-1;opacity:0;transition:opacity .8s;will-change:transform}.glow.on{opacity:1}
.topbar{position:sticky;top:0;z-index:5;background:rgba(5,6,15,.5);-webkit-backdrop-filter:blur(18px) saturate(140%);backdrop-filter:blur(18px) saturate(140%);border-bottom:1px solid rgba(255,255,255,.07)}
.top{display:flex;justify-content:space-between;align-items:center;max-width:1080px;margin:0 auto;padding:14px 24px}
.brand{display:flex;gap:10px;align-items:center;text-decoration:none;font:700 18px 'Bricolage Grotesque',sans-serif}
.brand svg{transition:transform .6s var(--ease)}.brand:hover svg{transform:rotate(-10deg)}
.who{display:flex;gap:12px;align-items:center;color:var(--mute);font-size:14px}
main{max-width:1080px;margin:0 auto;padding:16px 24px 80px}
.btn{position:relative;overflow:hidden;isolation:isolate;display:inline-flex;align-items:center;justify-content:center;gap:8px;border:0;border-radius:12px;padding:11px 22px;color:#fff;font:600 15px Figtree,sans-serif;text-decoration:none;cursor:pointer;
background:linear-gradient(135deg,#6f7dff,#9a6bff);box-shadow:inset 0 1px 0 rgba(255,255,255,.3),0 6px 18px -4px rgba(111,125,255,.55);transition:transform .35s var(--ease),box-shadow .35s var(--ease),background-color .3s}
.btn::before{content:"";position:absolute;inset:0;z-index:-1;border-radius:inherit;background:radial-gradient(130px circle at var(--bx,50%) var(--by,0%),rgba(255,255,255,.34),transparent 70%);opacity:0;transition:opacity .3s}
.btn:hover{transform:translateY(-1px);box-shadow:inset 0 1px 0 rgba(255,255,255,.38),0 12px 28px -6px rgba(140,110,255,.65)}.btn:hover::before{opacity:1}
.btn:active{transform:translateY(0) scale(.98);transition-duration:.12s}
.btn.ghost{background:rgba(255,255,255,.06);box-shadow:inset 0 0 0 1px var(--edge);-webkit-backdrop-filter:blur(10px);backdrop-filter:blur(10px)}.btn.ghost:hover{background:rgba(255,255,255,.1);box-shadow:inset 0 0 0 1px rgba(255,255,255,.22),0 10px 24px -8px rgba(0,0,0,.5)}
.btn.bad{background:rgba(255,107,129,.1);color:var(--bad);box-shadow:inset 0 0 0 1px rgba(255,107,129,.35)}.btn.bad:hover{background:rgba(255,107,129,.18);box-shadow:inset 0 0 0 1px rgba(255,107,129,.6),0 10px 24px -8px rgba(255,107,129,.4)}
.btn.sm{padding:7px 14px;font-size:14px;border-radius:10px}
.btn:disabled{opacity:.6;cursor:progress;transform:none}.btn.busy::after{content:"";width:14px;height:14px;border:2px solid rgba(255,255,255,.35);border-top-color:#fff;border-radius:50%;animation:spin .7s linear infinite}
.rip{position:absolute;z-index:-1;border-radius:50%;background:rgba(255,255,255,.32);transform:scale(0);animation:rip .7s var(--ease) forwards;pointer-events:none}
:focus-visible{outline:2px solid var(--ok);outline-offset:3px}
label{display:block;font-weight:500;font-size:14px;color:var(--mute);margin:16px 0 6px}
input,select,textarea{width:100%;background:rgba(255,255,255,.05);border:1px solid var(--edge);border-radius:12px;padding:12px 14px;color:var(--text);font:inherit;transition:border-color .25s var(--ease),box-shadow .25s var(--ease),background .25s}
textarea::placeholder,input::placeholder{color:rgba(154,163,214,.5)}input:hover,select:hover,textarea:hover{background:rgba(255,255,255,.075)}
input:focus,select:focus,textarea:focus{outline:0;border-color:rgba(124,140,255,.9);background:rgba(255,255,255,.085);box-shadow:0 0 0 4px rgba(124,140,255,.2)}
select{appearance:none;cursor:pointer;background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='8'%3E%3Cpath d='M1 1l5 5 5-5' fill='none' stroke='%239aa3d6' stroke-width='2'/%3E%3C/svg%3E");background-repeat:no-repeat;background-position:right 14px center;padding-right:38px}
textarea{resize:vertical;min-height:96px}input[type=color]{height:46px;padding:5px;cursor:pointer}
.panel,.tile{position:relative;overflow:hidden;background:var(--glass);border:1px solid var(--edge);-webkit-backdrop-filter:blur(22px) saturate(150%);backdrop-filter:blur(22px) saturate(150%);box-shadow:inset 0 1px 0 rgba(255,255,255,.12),0 20px 50px -20px rgba(2,3,18,.7);animation:rise .8s var(--ease) both;animation-delay:calc(var(--i,0)*70ms)}
.panel{border-radius:24px;padding:28px}
.panel::before,.tile::before{content:"";position:absolute;inset:0;pointer-events:none;opacity:0;transition:opacity .4s;background:radial-gradient(420px circle at var(--mx,50%) var(--my,0%),rgba(124,140,255,.13),transparent 60%)}
.panel::after,.tile::after{content:"";position:absolute;inset:0;border-radius:inherit;padding:1px;pointer-events:none;opacity:0;transition:opacity .4s;background:radial-gradient(240px circle at var(--mx,50%) var(--my,50%),rgba(170,180,255,.85),transparent 70%);-webkit-mask:linear-gradient(#000 0 0) content-box,linear-gradient(#000 0 0);-webkit-mask-composite:xor;mask:linear-gradient(#000 0 0) content-box exclude,linear-gradient(#000 0 0)}
.panel:hover::before,.tile:hover::before,.panel:hover::after,.tile:hover::after{opacity:1}
.lead{color:var(--mute);margin:8px 0 0;max-width:56ch}
.toast{position:fixed;right:20px;bottom:20px;z-index:9;background:rgba(18,21,46,.72);-webkit-backdrop-filter:blur(18px);backdrop-filter:blur(18px);border:1px solid rgba(61,219,176,.45);border-left-width:4px;border-radius:14px;padding:14px 20px;box-shadow:0 20px 50px -10px rgba(0,0,0,.6);animation:toast .5s var(--ease) both}
.toast.out{opacity:0;transform:translateY(10px);transition:opacity .5s,transform .5s var(--ease)}
.hero{display:grid;grid-template-columns:1.05fr 1fr;gap:56px;align-items:center;padding:56px 0 24px}
.hero h1{font-size:clamp(38px,5.4vw,64px);background:linear-gradient(110deg,#fff 20%,#b9c2ff 60%,#d9b8ff);-webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent}
.hero .btn{margin-top:30px;padding:15px 30px;font-size:17px;border-radius:14px}
.dc{background:#313338;border:1px solid rgba(255,255,255,.08);border-radius:14px;padding:16px 18px;font:14px/1.4 system-ui,sans-serif;color:#dbdee1}
.dc .m{display:flex;gap:12px;margin-top:14px}.dc .m:first-child{margin-top:0}.dc .a{flex:none;width:38px;height:38px;border-radius:50%;background:var(--ac);display:grid;place-items:center;font-weight:700;color:#fff}
.dc b{color:#fff}.dc .emb{border-left:4px solid var(--c,#5865f2);background:#2b2d31;border-radius:4px;padding:10px 12px;margin-top:6px}
.dc .emb p{margin:2px 0 0}.dbtn{display:inline-block;margin-top:8px;padding:6px 14px;border-radius:4px;background:#5865f2;color:#fff;font-weight:500}.dbtn.g{background:#248046}
.dsel{display:flex;justify-content:space-between;max-width:340px;margin-top:8px;padding:9px 12px;border-radius:4px;background:#1e1f22;color:#949ba4}
.dbtns{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}.dbtns .dbtn{margin:0}
.hero .dc{box-shadow:0 50px 100px -30px rgba(0,0,0,.8),0 0 90px rgba(124,140,255,.16);animation:float 9s ease-in-out infinite}
.hero .m{animation:pop .7s var(--ease) both}.hero .m:nth-child(1){animation-delay:.3s}.hero .m:nth-child(2){animation-delay:1.1s}.hero .m:nth-child(3){animation-delay:1.9s}
.tiles{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:18px;margin-top:30px}
.tile{display:flex;gap:16px;align-items:center;padding:20px;border-radius:20px;text-decoration:none;transition:transform .45s var(--ease),box-shadow .45s var(--ease)}
a.tile:hover{transform:translateY(-4px);box-shadow:inset 0 1px 0 rgba(255,255,255,.18),0 30px 60px -20px rgba(2,3,18,.9),0 0 50px -10px rgba(124,140,255,.35)}
.tile.off{opacity:.72;transition:opacity .3s,transform .45s var(--ease)}.tile.off:hover{opacity:1}.tile span{font-weight:600}.tile small{display:block;color:var(--mute);font-weight:400}
.ava{flex:none;width:50px;height:50px;border-radius:16px;display:grid;place-items:center;font:700 22px 'Bricolage Grotesque',sans-serif;background:linear-gradient(135deg,hsl(var(--h) 75% 58%),hsl(calc(var(--h) + 55) 70% 38%));box-shadow:0 8px 20px -4px hsla(var(--h),70%,50%,.45)}
.head{display:flex;align-items:center;gap:16px;margin:12px 0 26px}.head h1{font-size:clamp(30px,4.4vw,46px)}.back{color:var(--mute);text-decoration:none;font-size:14px;transition:color .25s}.back:hover{color:var(--text)}
.tabs{position:relative;display:inline-flex;gap:2px;padding:5px;border-radius:16px;background:var(--glass);border:1px solid var(--edge);-webkit-backdrop-filter:blur(14px);backdrop-filter:blur(14px);margin-bottom:28px;max-width:100%;overflow-x:auto}
.tabs button{position:relative;z-index:1;background:0;border:0;color:var(--mute);font:600 15px Figtree,sans-serif;padding:9px 20px;border-radius:11px;cursor:pointer;transition:color .3s;white-space:nowrap}
.tabs button[aria-selected=true],.tabs button:hover{color:#fff}
.tabs i{position:absolute;top:5px;bottom:5px;left:0;border-radius:11px;background:linear-gradient(135deg,#6f7dff,#9a6bff);box-shadow:inset 0 1px 0 rgba(255,255,255,.3),0 6px 20px -4px rgba(111,125,255,.6);transition:transform .5s var(--ease),width .5s var(--ease)}
.badge{margin-left:8px;background:rgba(255,255,255,.22);color:#fff;border-radius:99px;padding:1px 9px;font-size:13px}
.pane{animation:fade .35s var(--ease)}.pane[hidden]{display:none}
.cols{display:grid;grid-template-columns:1.1fr 1fr;gap:24px;align-items:start}.cols>.panel+.panel,.stick{position:sticky;top:84px}
.panel h2{font-size:25px;margin-bottom:4px}.stack>.panel{margin-bottom:24px}
.hint{color:var(--mute);font-size:14px;margin:14px 0 0}.hint b{color:var(--text);font-weight:600}
.rows{display:grid;gap:10px;margin:18px 0 0}.row{display:flex;align-items:center;gap:14px;padding:14px 16px;border-radius:16px;background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.08);transition:border-color .3s,background .3s}
.row:hover{border-color:rgba(124,140,255,.4);background:rgba(255,255,255,.065)}
.row .n{flex:1;min-width:0}.row .n b{display:block;overflow:hidden;text-overflow:ellipsis}.row .n small{color:var(--mute)}.row form{margin:0}
.sw{position:relative;width:50px;height:28px;border-radius:99px;border:1px solid var(--edge);background:rgba(255,255,255,.1);cursor:pointer;transition:background .35s var(--ease),box-shadow .35s var(--ease),border-color .35s}
.sw::after{content:"";position:absolute;top:3px;left:3px;width:20px;height:20px;border-radius:50%;background:#fff;box-shadow:0 2px 6px rgba(0,0,0,.4);transition:transform .4s var(--ease)}
.sw.on{background:linear-gradient(135deg,#2fd1a6,#3ddbb0);box-shadow:0 0 20px rgba(61,219,176,.4);border-color:transparent}.sw.on::after{transform:translateX(22px)}
.empty{padding:28px;text-align:center;color:var(--mute);border:1px dashed rgba(255,255,255,.2);border-radius:16px;background:rgba(255,255,255,.02)}
.qrow{display:flex;gap:8px;margin-top:8px;animation:rowin .4s var(--ease)}.qrow.gone{animation:rowout .2s ease-in forwards}.qrow input+input{flex:1}
.qrow .x{flex:none;width:44px;border:1px solid var(--edge);background:rgba(255,255,255,.04);color:var(--mute);border-radius:12px;font-size:20px;cursor:pointer;transition:color .25s,border-color .25s,background .25s}.qrow .x:hover{color:var(--bad);border-color:rgba(255,107,129,.6);background:rgba(255,107,129,.1)}
.split{display:flex;justify-content:space-between;align-items:center;margin-top:10px;color:var(--mute);font-size:14px}
.pair{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.seg{display:flex;flex-wrap:wrap;gap:8px}.seg label{margin:0;color:var(--text);cursor:pointer}.seg input{position:absolute;opacity:0;width:0}
.seg span{display:block;padding:10px 16px;border-radius:12px;border:1px solid var(--edge);background:rgba(255,255,255,.04);transition:background .3s,border-color .3s,box-shadow .35s var(--ease),color .3s;font-weight:500}
.seg span:hover{background:rgba(255,255,255,.09)}.seg input:checked+span{background:linear-gradient(135deg,#6f7dff,#9a6bff);border-color:transparent;box-shadow:inset 0 1px 0 rgba(255,255,255,.3),0 6px 20px -4px rgba(111,125,255,.6);color:#fff}
.seg input:focus-visible+span{outline:2px solid var(--ok);outline-offset:3px}
.chk{display:flex;align-items:center;gap:10px;margin:8px 0;color:var(--text);font-size:15px;cursor:pointer}.chk input{width:18px;height:18px;padding:0;accent-color:#7c8cff}
.me{width:30px;height:30px;border-radius:50%;object-fit:cover;border:1px solid var(--edge)}
body{transition:opacity .2s}body.leaving{opacity:0}
.toast{overflow:hidden}.toast::after{content:"";position:absolute;left:0;bottom:0;height:3px;background:var(--ok);animation:life 4.2s linear forwards}
.search{max-width:380px;margin-top:22px}.tile[hidden]{display:none}
.ava{overflow:hidden}.ava img{width:100%;height:100%;object-fit:cover}.hico{width:52px;height:52px;border-radius:16px;object-fit:cover}
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:16px;margin-bottom:24px}
.panel.stat{padding:20px 22px;display:flex;flex-direction:column;gap:2px}.stat b{font:700 38px 'Bricolage Grotesque',sans-serif;letter-spacing:-.03em;line-height:1.1}.stat span{color:var(--mute);font-size:14px}
.ico{width:42px;height:42px;border-radius:13px;display:grid;place-items:center;margin-bottom:10px;color:#fff;background:linear-gradient(135deg,#6f7dff,#9a6bff);box-shadow:inset 0 1px 0 rgba(255,255,255,.3),0 8px 20px -6px rgba(111,125,255,.6)}
.stat:nth-child(2) .ico{background:linear-gradient(135deg,#2fd1a6,#18a58a)}.stat:nth-child(3) .ico{background:linear-gradient(135deg,#ffb84d,#ff8a4d)}.stat:nth-child(4) .ico{background:linear-gradient(135deg,#ff7aa8,#c05cff)}
.cols2{display:grid;grid-template-columns:1.35fr 1fr;gap:24px;align-items:start}
.setup{margin-bottom:24px}.prog{height:8px;border-radius:9px;background:rgba(255,255,255,.08);overflow:hidden;margin:14px 0 18px}.prog i{display:block;height:100%;background:linear-gradient(90deg,#6f7dff,#3ddbb0);border-radius:inherit;animation:fill 1.2s var(--ease) both}
.steps{display:flex;flex-wrap:wrap;gap:8px}.step{display:inline-flex;gap:8px;align-items:center;padding:8px 14px 8px 10px;border-radius:99px;border:1px solid var(--edge);text-decoration:none;font-size:14px;background:rgba(255,255,255,.03);transition:background .25s,border-color .25s}
.step:hover{background:rgba(255,255,255,.09);border-color:rgba(124,140,255,.5)}.step.done{color:var(--mute)}.step.done span:last-child{text-decoration:line-through}
.tick{width:18px;height:18px;border-radius:50%;border:1.5px solid var(--mute);display:grid;place-items:center;font-size:11px;font-weight:700}.done .tick{background:var(--ok);border-color:var(--ok);color:#06231c}
.chart{display:flex;gap:6px;height:170px;margin-top:22px}.day{flex:1;display:flex;flex-direction:column;align-items:center;gap:6px;height:100%}
.bars{flex:1;width:100%;display:flex;gap:3px;align-items:flex-end;border-bottom:1px solid var(--edge)}
.bars i{flex:1;border-radius:4px 4px 0 0;transform-origin:bottom;animation:grow .9s var(--ease) both;animation-delay:calc(var(--n)*35ms + 250ms)}
.b1{background:linear-gradient(180deg,#8f9bff,#5b6bff)}.b2{background:linear-gradient(180deg,#5fe8c4,#1fb994)}.day small{color:var(--mute);font-size:11px}
.legend{display:flex;gap:14px;align-items:center;color:var(--mute);font-size:13px}.legend i{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:6px}.legend .b1,.legend .b2{border-radius:3px}
.feed{margin-top:8px}.ev{display:flex;gap:12px;align-items:center;padding:11px 0;border-bottom:1px solid rgba(255,255,255,.06)}.ev:last-child{border:0}
.ev div{min-width:0;font-size:15px}.ev small{display:block;color:var(--mute);font-size:13px}.dot{flex:none;width:9px;height:9px;border-radius:50%;background:#7c8cff;box-shadow:0 0 12px #7c8cff}.dot.application{background:var(--ok);box-shadow:0 0 12px var(--ok)}
.chip{margin-left:auto;flex:none;padding:3px 11px;border-radius:99px;font-size:12px;font-weight:600;background:rgba(255,255,255,.08);color:var(--mute);text-transform:capitalize}
.chip.open,.chip.pending{background:rgba(255,200,87,.14);color:#ffc857}.chip.accepted{background:rgba(61,219,176,.14);color:var(--ok)}.chip.denied{background:rgba(255,107,129,.14);color:var(--bad)}
.bar{position:fixed;left:50%;bottom:20px;transform:translateX(-50%);z-index:8;display:flex;gap:10px;align-items:center;padding:10px 12px 10px 20px;border-radius:18px;background:rgba(18,21,46,.74);-webkit-backdrop-filter:blur(20px);backdrop-filter:blur(20px);border:1px solid var(--edge);box-shadow:0 24px 60px -12px rgba(0,0,0,.7);animation:rise .6s var(--ease) both;animation-delay:.2s}
.dirtymsg{color:#ffc857;font-size:14px;opacity:0;transition:opacity .3s;margin-right:6px}.dirty .dirtymsg{opacity:1}
.filters{display:flex;flex-wrap:wrap;gap:10px;margin:20px 0 6px;align-items:end}.filters>*{flex:1;min-width:150px;margin:0}.filters .btn{flex:none;min-width:0}
.app{border:1px solid rgba(255,255,255,.08);border-radius:16px;background:rgba(255,255,255,.04);margin-top:10px;overflow:hidden;transition:border-color .3s,background .3s}
.app:hover{background:rgba(255,255,255,.06)}.app[open]{border-color:rgba(124,140,255,.45)}.app summary{display:flex;align-items:center;gap:12px;padding:14px 16px;cursor:pointer;list-style:none}.app summary::-webkit-details-marker{display:none}
.app summary div{flex:1;min-width:0}.app summary small{display:block;color:var(--mute)}.qa{padding:0 18px 18px;margin:0}.qa dt{color:var(--mute);font-size:13px;margin-top:14px}.qa dd{margin:4px 0 0;white-space:pre-wrap}
.pimg{display:block;max-width:100%;border-radius:4px;margin-top:8px}.pft{display:block;color:#949ba4;margin-top:8px;font-size:12px}.dbtn.b2{background:#4e5058}.dbtn.r{background:#da373c}
.inline{display:flex;gap:10px}.inline>*{margin:0}.inline input:first-child{flex:1}
@keyframes grow{from{transform:scaleY(0)}}@keyframes fill{from{width:0}}@keyframes life{from{width:100%}to{width:0}}
@keyframes drift{to{transform:translate3d(8vw,6vh,0) scale(1.12)}}@keyframes grid{to{background-position:0 56px,56px 0}}
@keyframes rise{from{opacity:0;transform:translateY(14px)}}@keyframes pop{from{opacity:0;transform:translateY(10px)}}@keyframes fade{from{opacity:0}}
@keyframes spin{to{transform:rotate(1turn)}}@keyframes float{50%{transform:translateY(-6px)}}@keyframes rip{to{transform:scale(2.6);opacity:0}}
@keyframes toast{from{opacity:0;transform:translateY(14px)}}@keyframes rowin{from{opacity:0;transform:translateY(-6px)}}@keyframes rowout{to{opacity:0;transform:translateX(14px);height:0;margin:0}}
@media(max-width:820px){.stats{grid-template-columns:1fr 1fr}.cols2{grid-template-columns:1fr}.bar{width:calc(100% - 24px);justify-content:flex-end}.stick{position:static}.hero,.cols,.pair{grid-template-columns:1fr}.hero{padding-top:16px;gap:36px}.cols>.panel+.panel{position:static}.bg i{filter:blur(60px)}.glow{display:none}}
@media(prefers-reduced-motion:reduce){*,::before,::after{animation:none!important;transition:none!important}.glow{display:none}}
</style>
<div class=bg><i></i><i></i><i></i><b></b></div><div class=glow></div>
<header class=topbar><div class=top><a class=brand href="/"><svg width=28 height=28 viewBox="0 0 28 28"><rect x=2 y=7 width=17 height=17 rx=5 fill="#6c7bff"/><rect x=9 y=2 width=17 height=17 rx=5 fill="#3ddbb0" fill-opacity=".9"/></svg>Tickets &amp; Applications</a>
{% if session.uid %}<div class=who>{% if session.avatar %}<img class=me src="https://cdn.discordapp.com/avatars/{{ session.uid }}/{{ session.avatar }}.png?size=64" alt="">{% endif %}{{ session.name }}<a class="btn ghost sm" href="/logout">Log out</a></div>{% endif %}</div></header>
{% if msg %}<div class=toast role=status>{{ msg }}</div>{% endif %}
<main>{{ body }}</main>
<script>
(function(){
var $=function(s,r){return(r||document).querySelector(s)},$$=function(s,r){return[].slice.call((r||document).querySelectorAll(s))};
var t=$('.toast');if(t)setTimeout(function(){t.classList.add('out')},4200);
$$('form').forEach(function(f){f.addEventListener('submit',function(ev){var b=ev.submitter||f.querySelector('button.btn');if(b)setTimeout(function(){if(ev.defaultPrevented)return;b.disabled=true;b.classList.add('busy')},0)})});
var tabs=$$('.tabs button'),panes=$$('.pane'),ind=$('.tabs i');
function show(n){tabs.forEach(function(b){var on=b.dataset.t===n;b.setAttribute('aria-selected',on);if(on){ind.style.width=b.offsetWidth+'px';ind.style.transform='translateX('+b.offsetLeft+'px)'}});panes.forEach(function(p){p.hidden=p.id!=='p-'+n})}
if(tabs.length){tabs.forEach(function(b){b.addEventListener('click',function(){history.replaceState(null,'','#'+b.dataset.t);show(b.dataset.t)})});
var cur=function(){var h=location.hash.slice(1);return['overview','tickets','apps','settings'].indexOf(h)>-1?h:'overview'};show(cur());addEventListener('resize',function(){show(cur())});}
function lab(s,e){var o=s.options[s.selectedIndex];return o&&o.value?o.text:e}
[['cat','pvcat','no category'],['role','pvrole','only the ticket owner'],['rev','pvrev','no channel yet'],['acc','pvacc','no role'],['tlog','pvtlog','no channel'],['alog','pvalog','no channel']].forEach(function(a){var s=$('#'+a[0]),o=$('#'+a[1]);if(s&&o){var u=function(){o.textContent=lab(s,a[2])};s.addEventListener('change',u);u()}});
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
$$('.panel,.tile').forEach(function(e){e.style.setProperty('--i',[].indexOf.call(e.parentNode.children,e))});
document.addEventListener('pointermove',function(e){var t=e.target;if(!t.closest)return;[['.panel,.tile','--mx','--my'],['.btn','--bx','--by']].forEach(function(a){var el=t.closest(a[0]);if(el){var r=el.getBoundingClientRect();el.style.setProperty(a[1],(e.clientX-r.left)+'px');el.style.setProperty(a[2],(e.clientY-r.top)+'px')}})});
document.addEventListener('click',function(e){var b=e.target.closest&&e.target.closest('.btn');if(!b||b.disabled)return;var r=b.getBoundingClientRect(),s=Math.max(r.width,r.height),p=document.createElement('span');p.className='rip';p.style.cssText='width:'+s+'px;height:'+s+'px;left:'+(e.clientX-r.left-s/2)+'px;top:'+(e.clientY-r.top-s/2)+'px';b.appendChild(p);setTimeout(function(){p.remove()},600)});
function el(t,c,x){var e=document.createElement(t);if(c)e.className=c;if(x!=null)e.textContent=x;return e}
function render(){var pv=$('#pv');if(!pv)return;
var kind=pv.dataset.kind,st=($('input[name=style]:checked')||{}).value,title=$('#f-title').value,desc=$('#f-desc').value,ph=($('#f-ph')||{}).value,labels;
$$('[data-for]').forEach(function(e){e.hidden=e.dataset.for.split(',').indexOf(st)<0});
pv.textContent='';var m=el('div','m'),b=el('div'),emb=el('div','emb');emb.style.setProperty('--c',$('#f-color').value);
if(title)emb.appendChild(el('b',0,title));if(desc){var p=el('p',0,desc);p.style.whiteSpace='pre-wrap';emb.appendChild(p)}
var im=$('#f-img').value.trim(),ft=$('#f-foot').value.trim();if(im.indexOf('https://')===0){var pi=el('img','pimg');pi.src=im;pi.alt='';pi.onerror=function(){pi.remove()};emb.appendChild(pi)}if(ft)emb.appendChild(el('small','pft',ft));
b.appendChild(el('b',0,'Support bot'));b.appendChild(emb);
if(kind==='ticket'){labels=$$('input[name=opt_label]').map(function(i){return i.value.trim()}).filter(Boolean);if(!labels.length)labels=['Create ticket'];ph=ph||'Select a ticket type'}
else if(kind==='multi'){labels=$$('input[name=members]:checked').map(function(i){return i.dataset.name});ph=ph||'Choose a topic'}
else if(st==='menu'){labels=[$('#f-btn').value.trim()||'Apply']}
else{var c=$$('input[name=forms]:checked');if(!c.length)c=$$('input[name=forms]').filter(function(i){return i.dataset.open==='1'});labels=c.map(function(i){return i.dataset.name});ph=ph||'Choose a form'}
if(st==='dropdown'){var d=el('div','dsel');d.appendChild(el('span',0,ph));d.appendChild(el('span',0,'\u25be'));b.appendChild(d)}
else{var cm={blurple:'',gray:' b2',green:' g',red:' r'},bc=($('input[name=button_color]:checked')||{}).value||(kind==='apply'?'green':'blurple'),r=el('div','dbtns');labels.forEach(function(l){r.appendChild(el('span','dbtn'+cm[bc],l))});if(!labels.length)r.appendChild(el('span','hint',kind==='multi'?'Pick at least one ticket panel':'No open forms yet'));b.appendChild(r)}
m.appendChild(el('div','a','S'));m.appendChild(b);pv.appendChild(m)}
var os=$('#opts'),ao=$('#addo'),oc=$('#oc');
function ocount(){if(oc)oc.textContent=$$('.qrow:not(.gone)',os).length+' of 25 types'}
function orow(focus){if($$('.qrow:not(.gone)',os).length>=25)return;var d=document.createElement('div');d.className='qrow';
var a=document.createElement('input');a.name='opt_label';a.maxLength=80;a.placeholder='Label';a.setAttribute('aria-label','Label');
var b=document.createElement('input');b.name='opt_desc';b.maxLength=100;b.placeholder='Description (dropdown only)';b.setAttribute('aria-label','Description');
var x=document.createElement('button');x.type='button';x.className='x';x.textContent='\u00d7';x.setAttribute('aria-label','Remove type');
d.appendChild(a);d.appendChild(b);d.appendChild(x);os.appendChild(d);if(focus)a.focus();ocount()}
if(os){ao.addEventListener('click',function(){orow(true);render()});
os.addEventListener('click',function(e){if(e.target.className==='x'){var r=e.target.parentNode;if($$('.qrow:not(.gone)',os).length>1){r.classList.add('gone');setTimeout(function(){r.remove();ocount();render()},200)}}});ocount()}
document.addEventListener('input',render);document.addEventListener('change',render);render();
var gl=$('.glow');
if(gl&&matchMedia('(pointer:fine)').matches&&!matchMedia('(prefers-reduced-motion:reduce)').matches){var tx=innerWidth/2,ty=innerHeight/3,gx=tx,gy=ty;
addEventListener('pointermove',function(e){tx=e.clientX;ty=e.clientY;gl.classList.add('on')});
(function loop(){gx+=(tx-gx)*.08;gy+=(ty-gy)*.08;gl.style.transform='translate3d('+gx+'px,'+gy+'px,0)';requestAnimationFrame(loop)})()}
function flash(m){var t=document.createElement('div');t.className='toast';t.setAttribute('role','status');t.textContent=m;document.body.appendChild(t);setTimeout(function(){t.classList.add('out');setTimeout(function(){t.remove()},600)},4200)}
$$('[data-count]').forEach(function(n){var to=+n.dataset.count||0,t0=null;function f(ts){t0=t0||ts;var p=Math.min((ts-t0)/900,1);n.textContent=Math.round(to*(1-Math.pow(1-p,3)));if(p<1)requestAnimationFrame(f)}requestAnimationFrame(f)});
$$('[data-tab]').forEach(function(a){a.addEventListener('click',function(e){e.preventDefault();history.replaceState(null,'','#'+a.dataset.tab);show(a.dataset.tab)})});
var sr=$('#srch');if(sr){var tl=$$('.tile');sr.addEventListener('input',function(){var v=sr.value.toLowerCase();tl.forEach(function(t){t.hidden=t.dataset.name.indexOf(v)<0})});addEventListener('keydown',function(e){if(e.key==='/'&&document.activeElement!==sr){e.preventDefault();sr.focus()}})}
var pf=$('#pform');if(pf){var dirty=false,mark=function(){dirty=true;pf.classList.add('dirty')};pf.addEventListener('input',mark);pf.addEventListener('change',mark);
pf.addEventListener('click',function(e){if(e.target.closest&&e.target.closest('#addq,#addo,.x'))mark()});
pf.addEventListener('submit',function(e){if(qs&&!$$('input[name=q]',qs).some(function(i){return i.value.trim()})){e.preventDefault();flash('Add at least one question.');var f0=$('input[name=q]',qs);if(f0)f0.focus();return}dirty=false});
addEventListener('beforeunload',function(e){if(dirty){e.preventDefault();e.returnValue=''}})}
document.addEventListener('click',function(e){var a=e.target.closest&&e.target.closest('a[href]');if(!a||e.defaultPrevented||e.button||e.metaKey||e.ctrlKey||e.shiftKey||a.target||a.hasAttribute('data-tab')||a.hasAttribute('download'))return;
var u=new URL(a.href,location.href);if(u.origin!==location.origin||(u.pathname===location.pathname&&u.hash))return;e.preventDefault();document.body.classList.add('leaving');setTimeout(function(){location.href=a.href},180)});
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
    session.update(uid=u["id"], name=u["username"], avatar=u.get("avatar"), csrf=secrets.token_hex(16),
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
    mine = [dict(g, has_bot=g["id"] in ids, icon=ICONS.get(g["id"]) if g["id"] in ids else None, h=int(g["id"]) % 360, ini=(g["name"][:1] or "?").upper(),
                 invite="https://discord.com/oauth2/authorize?" + urlencode({"client_id": CLIENT_ID, "scope": "bot applications.commands", "permissions": 268520464, "guild_id": g["id"]}))
            for g in session.get("guilds", [])]
    mine.sort(key=lambda g: not g["has_bot"])
    return await page("""<h1 style="font-size:clamp(30px,4vw,44px);margin-top:12px">Your servers</h1>
 <p class=lead>Pick a server to manage its tickets and applications. Servers without the bot show an invite link.</p>
 {% if mine|length > 6 %}<input id=srch class=search placeholder="Search servers  (press /)" autocomplete=off>{% endif %}
 <div class=tiles>{% for g in mine %}
  <a class="tile {{ '' if g.has_bot else 'off' }}" data-name="{{ g.name|lower }}" href="{{ ('/g/' ~ g.id) if g.has_bot else g.invite }}"><div class=ava style="--h:{{ g.h }}">{% if g.icon %}<img src="https://cdn.discordapp.com/icons/{{ g.id }}/{{ g.icon }}.png?size=96" alt="">{% else %}{{ g.ini }}{% endif %}</div>
  <span>{{ g.name }}<small>{{ 'Open dashboard' if g.has_bot else 'Invite the bot first' }}</small></span></a>
 {% else %}<div class=empty style="grid-column:1/-1">No servers found. You need the Manage Server permission on a server to see it here.</div>{% endfor %}</div>""", mine=mine)

@web.route("/g/<gid>")
@guild_route
async def guild_page(gid, gname):
    text, cats, roles = await gdata(gid)
    s = dict(await db("SELECT * FROM settings WHERE guild_id=$1", gid, mode="fetchrow") or {})
    forms = [dict(r, n=len(json.loads(r["questions"]))) for r in await db("SELECT * FROM forms WHERE guild_id=$1 ORDER BY id", gid)]
    panels = [dict(r) for r in await db("SELECT * FROM panels WHERE guild_id=$1 ORDER BY id", gid)]
    chn = {c["id"]: c["name"] for c in text}
    ts = dict(await db("""SELECT COUNT(*) FILTER (WHERE status='open') AS open, COUNT(*) FILTER (WHERE opened_at > now() - interval '7 days') AS week
                          FROM tickets WHERE guild_id=$1""", gid, mode="fetchrow"))
    ap = dict(await db("""SELECT COUNT(*) FILTER (WHERE status='pending') AS pending, COUNT(*) FILTER (WHERE created_at > now() - interval '7 days') AS week
                          FROM apps WHERE guild_id=$1""", gid, mode="fetchrow"))
    series = await db(SERIES_SQL, gid)
    mx = max([r["t"] for r in series] + [r["a"] for r in series] + [1])
    days = [dict(day=r["day"].strftime("%d"), tip=f'{r["day"]:%d %b}: {r["t"]} tickets, {r["a"]} applications', th=round(r["t"] * 100 / mx), ah=round(r["a"] * 100 / mx)) for r in series]
    rt = [dict(r) for r in await db("SELECT * FROM tickets WHERE guild_id=$1 ORDER BY opened_at DESC LIMIT 8", gid)]
    ra = [dict(r) for r in await db("""SELECT a.*, COALESCE(f.name,'Application') AS fname FROM apps a LEFT JOIN forms f ON f.id=a.form_id
                                       WHERE a.guild_id=$1 ORDER BY a.created_at DESC LIMIT 8""", gid)]
    ev = sorted([dict(k="ticket", who=r["user_name"], what=r["label"], at=r["opened_at"], st=r["status"]) for r in rt]
                + [dict(k="application", who=r["user_name"], what=r["fname"], at=r["created_at"], st=r["status"]) for r in ra],
                key=lambda e: e["at"], reverse=True)[:8]
    bl = await db("SELECT user_id, reason FROM blacklist WHERE guild_id=$1 LIMIT 50", gid)
    names = await asyncio.gather(*[uname(r["user_id"]) for r in bl])
    blocked = [dict(user_id=r["user_id"], reason=r["reason"], name=n) for r, n in zip(bl, names)]
    steps = [("Set a support role", bool(s.get("support_role") or any(p["support_role"] for p in panels)), "tickets"),
             ("Create a ticket panel", any(p["kind"] == "ticket" for p in panels), "tickets"),
             ("Publish a panel", any(p["message_id"] for p in panels), "tickets"),
             ("Create an application form", bool(forms), "apps"),
             ("Set up a log channel", bool(s.get("ticket_log") or s.get("app_log")), "settings")]
    done = sum(1 for x in steps if x[1])
    cards = [("Open tickets", ts["open"], ICO["ticket"]), ("Tickets this week", ts["week"], ICO["trend"]),
             ("Pending applications", ap["pending"], ICO["clock"]), ("Applications this week", ap["week"], ICO["inbox"])]
    return await page("""{% macro prow(p) %}<div class=row><div class=n><b>{{ p.name }}</b><small>{{ styles[p.style] }} &middot; {{ ('published in ' ~ chn[p.channel_id]) if p.message_id and chn.get(p.channel_id) else 'not published' }}</small></div>
<a class="btn ghost sm" href="/g/{{ gid }}/panel/{{ p.id }}">Edit</a>
<form method=post action="/g/{{ gid }}/panel/{{ p.id }}/duplicate"><input type=hidden name=csrf value="{{ session.csrf }}"><button class="btn ghost sm">Copy</button></form>
<form method=post data-confirm="Delete this panel?" action="/g/{{ gid }}/panel/{{ p.id }}/delete"><input type=hidden name=csrf value="{{ session.csrf }}"><button class="btn bad sm">Delete</button></form></div>{% endmacro %}
<a class=back href="/">&larr; All servers</a>
<div class=head>{% if icon %}<img class=hico src="https://cdn.discordapp.com/icons/{{ gid }}/{{ icon }}.png?size=128" alt="">{% endif %}<h1>{{ gname }}</h1></div>
<nav class=tabs role=tablist><button data-t=overview role=tab>Overview</button><button data-t=tickets role=tab>Tickets</button><button data-t=apps role=tab>Applications{% if ap.pending %}<span class=badge>{{ ap.pending }}</span>{% endif %}</button><button data-t=settings role=tab>Settings</button><i></i></nav>

<section class=pane id=p-overview>
 {% if done < steps|length %}<div class="panel setup"><div class=split style="margin:0"><h2>Get started</h2><span>{{ done }} of {{ steps|length }} done</span></div>
  <div class=prog><i style="width:{{ (done * 100 / steps|length)|round|int }}%"></i></div>
  <div class=steps>{% for label, ok, tab in steps %}<a data-tab="{{ tab }}" href="#{{ tab }}" class="step {{ 'done' if ok }}"><span class=tick>{{ '✓' if ok }}</span><span>{{ label }}</span></a>{% endfor %}</div></div>{% endif %}
 <div class=stats>{% for label, n, ico in cards %}<div class="panel stat"><div class=ico>{{ ico }}</div><b data-count="{{ n }}">0</b><span>{{ label }}</span></div>{% endfor %}</div>
 <div class=cols2>
  <div class=panel><div class=split style="margin:0"><h2>Last 14 days</h2><span class=legend><span><i class=b1></i>Tickets</span><span><i class=b2></i>Applications</span></span></div>
   <div class=chart>{% for d in days %}<div class=day title="{{ d.tip }}"><div class=bars style="--n:{{ loop.index0 }}"><i class=b1 style="height:{{ d.th }}%;--n:{{ loop.index0 }}"></i><i class=b2 style="height:{{ d.ah }}%;--n:{{ loop.index0 }}"></i></div><small>{{ d.day }}</small></div>{% endfor %}</div></div>
  <div class=panel><h2>Recent activity</h2><div class=feed>{% for e in ev %}<div class=ev><span class="dot {{ e.k }}"></span><div><b>{{ e.who or 'Unknown' }}</b> {{ 'opened a ticket' if e.k == 'ticket' else 'applied' }}<small>{{ e.what }} &middot; {{ ago(e.at) }}</small></div><span class="chip {{ e.st }}">{{ e.st }}</span></div>
   {% else %}<div class=empty>Nothing yet. Tickets and applications show up here as they happen.</div>{% endfor %}</div></div>
 </div>
</section>

<section class=pane id=p-tickets hidden><div class=cols>
 <div class=panel><h2>Ticket settings</h2><p class=lead>Defaults for every ticket panel. Each panel can override them in its own settings.</p>
  <form method=post action="/g/{{ gid }}/tickets"><input type=hidden name=csrf value="{{ session.csrf }}">
   <label for=cat>Ticket category</label><select id=cat name=ticket_category>{{ opts(cats, s.ticket_category) }}</select>
   <label for=role>Support role</label><select id=role name=support_role>{{ opts(roles, s.support_role) }}</select>
   <label for=tlog>Log channel</label><select id=tlog name=ticket_log>{{ opts(text, s.ticket_log) }}</select>
   <p class=hint>Tickets open under <b id=pvcat>no category</b> and are visible to <b id=pvrole>only the ticket owner</b>. Opened and closed tickets are logged in <b id=pvtlog>no channel</b>.</p>
   <p style="margin:22px 0 0"><button class=btn>Save settings</button></p></form></div>
 <div class=stack>
 <div class=panel><h2>Ticket panels</h2><p class=lead>Each panel has its own message, ticket types, category, support role and log channel.</p>
  <div class=rows>{% for p in tpanels %}{{ prow(p) }}{% else %}<div class=empty>No panels yet. Create one to let members open tickets.</div>{% endfor %}</div>
  <p style="margin:22px 0 0"><a class=btn href="/g/{{ gid }}/panel/new?kind=ticket">New ticket panel</a></p></div>
 <div class=panel><h2>Multi panels</h2><p class=lead>Combine several ticket panels into one message, for example a dropdown where every entry opens a different kind of ticket.</p>
  <div class=rows>{% for p in mpanels %}{{ prow(p) }}{% else %}<div class=empty>No multi panels yet. Create at least one ticket panel first, then combine them here.</div>{% endfor %}</div>
  <p style="margin:22px 0 0"><a class="btn {{ '' if tpanels else 'ghost' }}" href="/g/{{ gid }}/panel/new?kind=multi">New multi panel</a></p></div>
 <div class=panel><h2>Recent tickets</h2><div class=feed>{% for r in rt %}<div class=ev><span class="dot ticket"></span><div><b>#{{ '%04d'|format(r.num or 0) }}</b> {{ r.user_name or 'Unknown' }}<small>{{ r.label }} &middot; {{ ago(r.opened_at) }}</small></div><span class="chip {{ r.status }}">{{ r.status }}</span></div>
  {% else %}<div class=empty>Tickets show up here once members open them.</div>{% endfor %}</div></div>
 </div></div></section>

<section class=pane id=p-apps hidden><div class=cols>
 <div class=stack>
  <div class=panel><h2>Your forms</h2><p class=lead>Closed forms disappear from the Apply menus right away.</p>
   <div class=rows>{% for f in forms %}<div class=row>
    <div class=n><b>{{ f.name }}</b><small>{{ f.n }} question{{ '' if f.n == 1 else 's' }} &middot; {{ 'accepting applications' if f.open else 'closed' }}</small></div>
    <a class="btn ghost sm" href="/g/{{ gid }}/form/{{ f.id }}">Edit</a>
    <form method=post action="/g/{{ gid }}/form/{{ f.id }}/toggle"><input type=hidden name=csrf value="{{ session.csrf }}"><button class="sw {{ 'on' if f.open }}" aria-label="{{ 'Close form' if f.open else 'Open form' }}" title="{{ 'Close form' if f.open else 'Open form' }}"></button></form>
    <form method=post data-confirm="Delete this form?" action="/g/{{ gid }}/form/{{ f.id }}/delete"><input type=hidden name=csrf value="{{ session.csrf }}"><button class="btn bad sm">Delete</button></form></div>
   {% else %}<div class=empty>No forms yet. Create your first one and it shows up in the Apply menus straight away.</div>{% endfor %}</div>
   <p style="margin:22px 0 0"><a class=btn href="/g/{{ gid }}/form/new">New form</a></p></div>
  <div class=panel><h2>Application panels</h2><p class=lead>Show one button, a button per form, or a dropdown. After you add or rename forms, publish the panel again.</p>
   <div class=rows>{% for p in apanels %}{{ prow(p) }}{% else %}<div class=empty>No panels yet. Create one to let members apply.</div>{% endfor %}</div>
   <p style="margin:22px 0 0"><a class=btn href="/g/{{ gid }}/panel/new?kind=apply">New application panel</a></p></div>
 </div>
 <div class=stack>
  <div class=panel><h2>Inbox</h2><p class=lead>Every submitted application, with all answers.</p>
   <div class=feed>{% for r in ra[:5] %}<div class=ev><span class="dot application"></span><div><b>{{ r.user_name or 'Unknown' }}</b><small>{{ r.fname }} &middot; {{ ago(r.created_at) }}</small></div><span class="chip {{ r.status }}">{{ r.status }}</span></div>
   {% else %}<div class=empty>No applications yet.</div>{% endfor %}</div>
   <p style="margin:22px 0 0;display:flex;gap:10px;flex-wrap:wrap"><a class=btn href="/g/{{ gid }}/applications">Open inbox</a><a class="btn ghost" download href="/g/{{ gid }}/applications.csv">Export CSV</a></p></div>
  <div class=panel><h2>Application log</h2><p class=lead>Get a message whenever someone applies and when an application is accepted or denied.</p>
   <form method=post action="/g/{{ gid }}/applog"><input type=hidden name=csrf value="{{ session.csrf }}">
    <label for=alog>Log channel</label><select id=alog name=app_log>{{ opts(text, s.app_log) }}</select>
    <p class=hint>Events are logged in <b id=pvalog>no channel</b>.</p>
    <p style="margin:22px 0 0"><button class=btn>Save log channel</button></p></form></div>
 </div></div></section>

<section class=pane id=p-settings hidden><div class=cols>
 <div class=panel><h2>Transcripts</h2><p class=lead>When a ticket is closed, the whole conversation is saved as a text file.</p>
  <form method=post action="/g/{{ gid }}/settings"><input type=hidden name=csrf value="{{ session.csrf }}">
   <label for=tm>What to do with transcripts</label><select id=tm name=transcripts>{% for v, l in tmodes %}<option value="{{ v }}" {{ 'selected' if (s.transcripts or 'log') == v }}>{{ l }}</option>{% endfor %}</select>
   <p class=hint>Transcripts are posted in the ticket's log channel, so set one on the Tickets tab first.</p>
   <p style="margin:22px 0 0"><button class=btn>Save</button></p></form></div>
 <div class=panel><h2>Blocked users</h2><p class=lead>Blocked users cannot open tickets or apply. Use their Discord user ID (turn on Developer Mode, then right-click a user and copy the ID).</p>
  <form method=post action="/g/{{ gid }}/blacklist" class=inline style="margin-top:16px"><input type=hidden name=csrf value="{{ session.csrf }}">
   <input name=user_id inputmode=numeric pattern="[0-9]{15,22}" placeholder="User ID" aria-label="User ID" required><input name=reason maxlength=200 placeholder="Reason (optional)" aria-label="Reason"><button class=btn>Block</button></form>
  <div class=rows>{% for b in blocked %}<div class=row><div class=n><b>{{ b.name or 'Unknown user' }}</b><small>{{ b.user_id }}{{ ' &middot; ' ~ b.reason if b.reason else '' }}</small></div>
   <form method=post action="/g/{{ gid }}/blacklist/{{ b.user_id }}/remove"><input type=hidden name=csrf value="{{ session.csrf }}"><button class="btn ghost sm">Unblock</button></form></div>
  {% else %}<div class=empty>Nobody is blocked.</div>{% endfor %}</div></div>
</div></section>""", gname=gname, gid=gid, s=s, forms=forms, text=text, cats=cats, roles=roles, icon=ICONS.get(gid), chn=chn, styles=STYLE_NAMES,
        tpanels=[p for p in panels if p["kind"] == "ticket"], apanels=[p for p in panels if p["kind"] == "apply"], mpanels=[p for p in panels if p["kind"] == "multi"],
        ts=ts, ap=ap, days=days, rt=rt, ra=ra, ev=ev, blocked=blocked, steps=steps, done=done, cards=cards,
        tmodes=[("off", "Off"), ("log", "Send to the log channel"), ("both", "Log channel and DM to the opener")])

@web.post("/g/<gid>/settings")
@guild_route
async def save_settings(gid, gname):
    f = await request.form
    mode = f.get("transcripts") if f.get("transcripts") in ("off", "log", "both") else "log"
    await db("""INSERT INTO settings(guild_id,transcripts) VALUES($1,$2) ON CONFLICT(guild_id) DO UPDATE SET transcripts=EXCLUDED.transcripts""", gid, mode, mode="execute")
    session["msg"] = "Settings saved."
    return redirect(f"/g/{gid}#settings")

@web.post("/g/<gid>/blacklist")
@guild_route
async def blacklist_add(gid, gname):
    f = await request.form
    uid = f.get("user_id", "").strip()
    name = await uname(uid) if re.fullmatch(r"\d{15,22}", uid) else None
    if not name: session["msg"] = "That user ID was not found."
    else:
        await db("""INSERT INTO blacklist(guild_id,user_id,reason) VALUES($1,$2,$3) ON CONFLICT(guild_id,user_id) DO UPDATE SET reason=EXCLUDED.reason""",
                 gid, uid, f.get("reason", "").strip()[:200], mode="execute")
        session["msg"] = f"{name} is now blocked."
    return redirect(f"/g/{gid}#settings")

@web.post("/g/<gid>/blacklist/<uid>/remove")
@guild_route
async def blacklist_remove(gid, gname, uid):
    await db("DELETE FROM blacklist WHERE guild_id=$1 AND user_id=$2", gid, uid, mode="execute")
    session["msg"] = "User unblocked."
    return redirect(f"/g/{gid}#settings")

# ---- application inbox ----
async def inbox_rows(gid, args, limit=100):
    where, a = ["a.guild_id=$1"], [gid]
    if args.get("status") in ("pending", "accepted", "denied"): a.append(args["status"]); where.append(f"a.status=${len(a)}")
    if args.get("form", "").isdigit(): a.append(int(args["form"])); where.append(f"a.form_id=${len(a)}")
    if args.get("q", "").strip(): a.append("%" + args["q"].strip()[:60] + "%"); where.append(f"(a.user_name ILIKE ${len(a)} OR a.user_id ILIKE ${len(a)})")
    sql = f"""SELECT a.*, COALESCE(f.name,'Application') AS fname, COALESCE(f.questions,'[]') AS fq FROM apps a LEFT JOIN forms f ON f.id=a.form_id
              WHERE {' AND '.join(where)} ORDER BY a.created_at DESC LIMIT {int(limit)}"""
    return [dict(r) for r in await db(sql, *a)]

@web.route("/g/<gid>/applications")
@guild_route
async def inbox(gid, gname):
    rows = await inbox_rows(gid, request.args)
    for r in rows:
        qs, ans = json.loads(r["fq"]), json.loads(r["answers"] or "[]")
        r["qa"] = [(qs[n] if n < len(qs) else f"Question {n + 1}", x) for n, x in enumerate(ans)]
    forms = [dict(r) for r in await db("SELECT id,name FROM forms WHERE guild_id=$1 ORDER BY id", gid)]
    return await page("""<a class=back href="/g/{{ gid }}#apps">&larr; Back to {{ gname }}</a>
<div class=head><h1>Application inbox</h1></div>
<div class=panel><form method=get class=filters>
 <select name=status aria-label=Status><option value="">All statuses</option>{% for v in ['pending', 'accepted', 'denied'] %}<option value="{{ v }}" {{ 'selected' if args.get('status') == v }}>{{ v|capitalize }}</option>{% endfor %}</select>
 <select name=form aria-label=Form><option value="">All forms</option>{% for f in forms %}<option value="{{ f.id }}" {{ 'selected' if args.get('form') == f.id|string }}>{{ f.name }}</option>{% endfor %}</select>
 <input name=q value="{{ args.get('q', '') }}" placeholder="Search by name or ID" aria-label=Search>
 <button class=btn>Filter</button><a class="btn ghost" download href="/g/{{ gid }}/applications.csv?{{ qs }}">Export CSV</a></form>
 {% for r in rows %}<details class=app><summary><span class="dot application"></span><div><b>{{ r.user_name or 'Unknown' }}</b><small>{{ r.fname }} &middot; {{ ago(r.created_at) }}{{ ' &middot; reviewed by ' ~ r.reviewer if r.reviewer else '' }}</small></div><span class="chip {{ r.status }}">{{ r.status }}</span></summary>
  <dl class=qa>{% for q, a in r.qa %}<dt>{{ q }}</dt><dd>{{ a }}</dd>{% endfor %}</dl></details>
 {% else %}<div class=empty style="margin-top:18px">No applications match.</div>{% endfor %}</div>""",
        gname=gname, gid=gid, rows=rows, forms=forms, args=request.args, qs=urlencode({k: v for k, v in request.args.items() if k in ("status", "form", "q") and v}))

@web.route("/g/<gid>/applications.csv")
@guild_route
async def inbox_csv(gid, gname):
    rows = await inbox_rows(gid, request.args, limit=5000)
    safe = lambda x: ("'" + str(x)) if str(x or "")[:1] in ("=", "+", "-", "@", "\t", "\r") else str(x if x is not None else "")
    out = io.StringIO(); w = csv.writer(out)
    w.writerow(["id", "form", "user_id", "user_name", "status", "applied_at", "reviewed_by", "answers"])
    for r in rows:
        qs, ans = json.loads(r["fq"]), json.loads(r["answers"] or "[]")
        w.writerow([safe(x) for x in (r["id"], r["fname"], r["user_id"], r["user_name"], r["status"], r["created_at"], r["reviewer"],
                    " | ".join(f"{qs[n] if n < len(qs) else 'Q' + str(n + 1)}: {x}" for n, x in enumerate(ans)))])
    return Response("\ufeff" + out.getvalue(), mimetype="text/csv", headers={"Content-Disposition": f'attachment; filename="applications-{gid}.csv"'})

# ---- form editor ----
@web.route("/g/<gid>/form/<fid>")
@guild_route
async def form_edit(gid, gname, fid):
    text, cats, roles = await gdata(gid)
    if fid == "new":
        f = dict(id=None, name="", questions=[""], review_channel=None, accept_role=None, required_role=None, cooldown_hours=0, accept_msg="", deny_msg="")
    else:
        row = await db("SELECT * FROM forms WHERE id=$1 AND guild_id=$2", int(fid) if fid.isdigit() else 0, gid, mode="fetchrow")
        if not row: abort(404)
        f = dict(row); f["questions"] = json.loads(f["questions"] or "[]") or [""]
    return await page("""<a class=back href="/g/{{ gid }}#apps">&larr; Back to {{ gname }}</a>
<div class=head><h1>{{ 'Edit' if f.id else 'New' }} form</h1></div>
<form method=post action="/g/{{ gid }}/form/save" id=pform><input type=hidden name=csrf value="{{ session.csrf }}"><input type=hidden name=fid value="{{ f.id or '' }}">
<div class=cols><div class=stack>
 <div class=panel><h2>Questions</h2><p class=lead>Applicants answer each question in a DM with the bot.</p>
  <label for=fname>Form name</label><input id=fname name=name maxlength=100 value="{{ f.name }}" placeholder="Moderator" required>
  <label>Questions</label><div id=qs>{% for q in f.questions %}<div class=qrow><input name=q maxlength=250 placeholder="Type a question" aria-label="Question" value="{{ q }}"><button type=button class=x aria-label="Remove question">&times;</button></div>{% endfor %}</div>
  <div class=split><span id=qc></span><button type=button id=addq class="btn ghost sm">Add question</button></div></div>
 <div class=panel><h2>Messages</h2><p class=lead>Optional DMs for the applicant. Write {user} for their name and {form} for the form name.</p>
  <label for=am>Acceptance message</label><textarea id=am name=accept_msg rows=3 maxlength=1000 placeholder="Your application ({form}) was accepted!">{{ f.accept_msg or '' }}</textarea>
  <label for=dm>Denial message</label><textarea id=dm name=deny_msg rows=3 maxlength=1000 placeholder="Unfortunately, your application ({form}) was denied.">{{ f.deny_msg or '' }}</textarea></div>
</div>
<div class="panel stick"><h2>Rules</h2>
 <label for=rev>Review channel</label><select id=rev name=review_channel>{{ opts(text, f.review_channel) }}</select>
 <label for=acc>Role on acceptance</label><select id=acc name=accept_role>{{ opts(roles, f.accept_role) }}</select>
 <label for=rr>Role required to apply</label><select id=rr name=required_role>{{ opts(roles, f.required_role) }}</select>
 <label for=cd>Cooldown between applications (hours)</label><input id=cd type=number min=0 max=720 name=cooldown_hours value="{{ f.cooldown_hours or 0 }}">
 <p class=hint>Submissions go to <b id=pvrev>no channel yet</b>. Accepted applicants receive <b id=pvacc>no role</b>. Members can only have one pending application per form.</p></div>
</div>
<div class=bar><span class=dirtymsg>Unsaved changes</span><button class=btn>Save form</button></div><div style="height:72px"></div></form>""", gname=gname, gid=gid, f=f, text=text, roles=roles)

@web.post("/g/<gid>/form/save")
@guild_route
async def form_save(gid, gname):
    f = await request.form
    text, cats, roles = await gdata(gid)
    qs = [x.strip()[:250] for x in f.getlist("q") if x.strip()][:25]
    fid = int(f["fid"]) if f.get("fid", "").isdigit() else None
    if not qs:
        session["msg"] = "Add at least one question."
        return redirect(f"/g/{gid}/form/{fid or 'new'}")
    pick = lambda key, items: f.get(key) if f.get(key) in {c["id"] for c in items} else None
    hours = min(max(int(f["cooldown_hours"]), 0), 720) if f.get("cooldown_hours", "").isdigit() else 0
    vals = (f.get("name", "").strip()[:100] or "Application", json.dumps(qs), pick("review_channel", text), pick("accept_role", roles), pick("required_role", roles),
            hours, f.get("accept_msg", "").strip()[:1000] or None, f.get("deny_msg", "").strip()[:1000] or None)
    if fid:
        if not await db("SELECT 1 FROM forms WHERE id=$1 AND guild_id=$2", fid, gid, mode="fetchrow"): abort(404)
        await db("""UPDATE forms SET name=$1,questions=$2,review_channel=$3,accept_role=$4,required_role=$5,cooldown_hours=$6,accept_msg=$7,deny_msg=$8
                    WHERE id=$9 AND guild_id=$10""", *vals, fid, gid, mode="execute")
    else:
        await db("""INSERT INTO forms(name,questions,review_channel,accept_role,required_role,cooldown_hours,accept_msg,deny_msg,guild_id)
                    VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9)""", *vals, gid, mode="execute")
    session["msg"] = "Form saved."
    return redirect(f"/g/{gid}#apps")

@web.post("/g/<gid>/tickets")
@guild_route
async def save_tickets(gid, gname):
    f = await request.form
    text, cats, roles = await gdata(gid)
    cat = f.get("ticket_category") if f.get("ticket_category") in {c["id"] for c in cats} else None
    role = f.get("support_role") if f.get("support_role") in {r["id"] for r in roles} else None
    tlog = f.get("ticket_log") if f.get("ticket_log") in {c["id"] for c in text} else None
    await db("""INSERT INTO settings(guild_id,ticket_category,support_role,ticket_log) VALUES($1,$2,$3,$4) ON CONFLICT(guild_id)
                DO UPDATE SET ticket_category=EXCLUDED.ticket_category, support_role=EXCLUDED.support_role, ticket_log=EXCLUDED.ticket_log""",
             gid, cat, role, tlog, mode="execute")
    session["msg"] = "Ticket settings saved."
    return redirect(f"/g/{gid}#tickets")

@web.post("/g/<gid>/applog")
@guild_route
async def save_applog(gid, gname):
    f = await request.form
    text, _, _ = await gdata(gid)
    alog = f.get("app_log") if f.get("app_log") in {c["id"] for c in text} else None
    await db("""INSERT INTO settings(guild_id,app_log) VALUES($1,$2) ON CONFLICT(guild_id)
                DO UPDATE SET app_log=EXCLUDED.app_log""", gid, alog, mode="execute")
    session["msg"] = "Application log saved."
    return redirect(f"/g/{gid}#apps")

@web.post("/g/<gid>/form/<fid>/<action>")
@guild_route
async def form_action(gid, gname, fid, action):
    if action == "toggle": await db("UPDATE forms SET open=NOT open WHERE id=$1 AND guild_id=$2", int(fid), gid, mode="execute")
    elif action == "delete": await db("DELETE FROM forms WHERE id=$1 AND guild_id=$2", int(fid), gid, mode="execute")
    return redirect(f"/g/{gid}#apps")

STYLE_NAMES = {"button": "Buttons", "dropdown": "Dropdown", "menu": "Single button", "buttons": "Button per form"}
BS = {"blurple": 1, "gray": 2, "green": 3, "red": 4}
def bs(p, d): return BS.get(p.get("button_color") or d, BS[d])
STYLES = {"ticket": ["button", "dropdown"], "apply": ["menu", "buttons", "dropdown"], "multi": ["dropdown", "button"]}

def default_panel(kind):
    base = dict(id=None, kind=kind, button_label="", placeholder="", options=[], forms=[], members=[], channel_id=None,
                ticket_category=None, support_role=None, ticket_log=None, welcome="", entry_desc="", image_url="", footer="", button_color=None)
    if kind == "apply":
        return dict(base, name="Application panel", style="menu", title="Applications", description="Click the button below to apply.",
                    color="#57F287", button_label="Apply", placeholder="Choose a form")
    if kind == "multi":
        return dict(base, name="Multi panel", style="dropdown", title="Support", description="Choose the kind of help you need.",
                    color="#5865F2", placeholder="Choose a topic")
    return dict(base, name="Support panel", style="button", title="Support", description="Click the button below to open a ticket.",
                color="#5865F2", placeholder="Select a ticket type", options=[{"label": "Create ticket", "desc": ""}])

def chunk(btns):
    return [{"type": 1, "components": btns[n:n + 5]} for n in range(0, len(btns), 5)]

async def build_payload(gid, p):
    emb = {"color": int((p["color"] or "#5865F2").lstrip("#"), 16)}
    if p["title"]: emb["title"] = p["title"]
    if p["description"]: emb["description"] = p["description"]
    if len(emb) == 1: emb["title"] = "Support" if p["kind"] == "ticket" else "Applications"
    if (p.get("image_url") or "").startswith("https://"): emb["image"] = {"url": p["image_url"]}
    if p.get("footer"): emb["footer"] = {"text": p["footer"]}
    if p["kind"] == "ticket":
        opts = json.loads(p["options"] or "[]") or [{"label": "Create ticket", "desc": ""}]
        if p["style"] == "dropdown":
            comps = [{"type": 1, "components": [{"type": 3, "custom_id": f"tk:s:{p['id']}", "placeholder": p["placeholder"] or "Select a ticket type",
                      "options": [dict(label=o["label"], value=str(n), **({"description": o["desc"]} if o.get("desc") else {})) for n, o in enumerate(opts)]}]}]
        else:
            comps = chunk([{"type": 2, "style": bs(p, "blurple"), "label": o["label"], "custom_id": f"tk:b:{p['id']}:{n}"} for n, o in enumerate(opts)])
    elif p["kind"] == "multi":
        ids = [int(x) for x in json.loads(p["members"] or "[]")]
        rows = [r for r in await db("SELECT id,name,button_label,entry_desc FROM panels WHERE guild_id=$1 AND kind='ticket' ORDER BY id", gid) if r["id"] in ids][:25]
        if not rows: return None
        ents = [(r["id"], (r["button_label"] or r["name"])[:80], (r["entry_desc"] or "")[:100]) for r in rows]
        if p["style"] == "dropdown":
            comps = [{"type": 1, "components": [{"type": 3, "custom_id": f"mp:s:{p['id']}", "placeholder": p["placeholder"] or "Choose a topic",
                      "options": [dict(label=l, value=str(mid), **({"description": d} if d else {})) for mid, l, d in ents]}]}]
        else:
            comps = chunk([{"type": 2, "style": bs(p, "blurple"), "label": l, "custom_id": f"mp:b:{p['id']}:{mid}"} for mid, l, d in ents])
    elif p["style"] == "menu":
        comps = chunk([{"type": 2, "style": bs(p, "green"), "label": p["button_label"] or "Apply", "custom_id": f"ap:m:{p['id']}"}])
    else:
        ids = [int(x) for x in json.loads(p["forms"] or "[]")]
        rows = [r for r in await db("SELECT id,name FROM forms WHERE guild_id=$1 AND open ORDER BY id LIMIT 25", gid) if not ids or r["id"] in ids]
        if not rows: return None
        if p["style"] == "dropdown":
            comps = [{"type": 1, "components": [{"type": 3, "custom_id": f"ap:s:{p['id']}", "placeholder": p["placeholder"] or "Choose a form",
                      "options": [{"label": r["name"][:100], "value": str(r["id"])} for r in rows]}]}]
        else:
            comps = chunk([{"type": 2, "style": bs(p, "green"), "label": r["name"][:80], "custom_id": f"ap:b:{r['id']}"} for r in rows])
    return {"embeds": [emb], "components": comps}

async def publish_panel(gid, pid):
    p = dict(await db("SELECT * FROM panels WHERE id=$1 AND guild_id=$2", pid, gid, mode="fetchrow"))
    if not p["channel_id"]: return "Saved. Choose a channel to publish the panel."
    payload = await build_payload(gid, p)
    if payload is None: return "Saved, but pick at least one ticket panel first." if p["kind"] == "multi" else "Saved, but there are no open forms to show yet. Create a form first."
    st = None
    if p["message_id"]:
        st, _ = await dapi("PATCH", f"/channels/{p['channel_id']}/messages/{p['message_id']}", json=payload)
    if st is None or st == 404:
        st, d = await dapi("POST", f"/channels/{p['channel_id']}/messages", json=payload)
        if st < 300: await db("UPDATE panels SET message_id=$1 WHERE id=$2", d["id"], pid, mode="execute")
        else: return "Saved, but publishing failed. Does the bot have permission to send messages in that channel?"
        return "Panel published."
    return "Panel updated." if st < 300 else "Saved, but updating failed. Does the bot have permission in that channel?"

@web.route("/g/<gid>/panel/<pid>")
@guild_route
async def panel_edit(gid, gname, pid):
    text, cats, roles = await gdata(gid)
    if pid == "new":
        p = default_panel(request.args.get("kind") if request.args.get("kind") in ("apply", "multi") else "ticket")
    else:
        row = await db("SELECT * FROM panels WHERE id=$1 AND guild_id=$2", int(pid) if pid.isdigit() else 0, gid, mode="fetchrow")
        if not row: abort(404)
        p = dict(row)
        p["options"] = json.loads(p["options"] or "[]") or ([{"label": "Create ticket", "desc": ""}] if p["kind"] == "ticket" else [])
        p["forms"] = [int(x) for x in json.loads(p["forms"] or "[]")]
        p["members"] = [int(x) for x in json.loads(p["members"] or "[]")]
    forms = [dict(r) for r in await db("SELECT id,name,open FROM forms WHERE guild_id=$1 ORDER BY id", gid)]
    tickets = [dict(r) for r in await db("SELECT id,name,button_label,entry_desc FROM panels WHERE guild_id=$1 AND kind='ticket' ORDER BY id", gid)]
    return await page("""<a class=back href="/g/{{ gid }}{{ '#apps' if p.kind == 'apply' else '#tickets' }}">&larr; Back to {{ gname }}</a>
<div class=head><h1>{{ 'Edit' if p.id else 'New' }} {{ {'ticket': 'ticket', 'apply': 'application', 'multi': 'multi'}[p.kind] }} panel</h1></div>
<form method=post action="/g/{{ gid }}/panel/save" id=pform><input type=hidden name=csrf value="{{ session.csrf }}"><input type=hidden name=kind value="{{ p.kind }}"><input type=hidden name=pid value="{{ p.id or '' }}">
<div class=cols><div class=stack>
 <div class=panel><h2>Message</h2><p class=lead>The text and color of the message members see.</p>
  <label for=f-name>Panel name (only you see this)</label><input id=f-name name=name maxlength=60 value="{{ p.name }}" required>
  <label for=f-title>Title</label><input id=f-title name=title maxlength=200 value="{{ p.title }}">
  <label for=f-desc>Description</label><textarea id=f-desc name=description rows=4 maxlength=2000>{{ p.description }}</textarea>
  <label for=f-color>Accent color</label><input id=f-color type=color name=color value="{{ p.color }}">
  <label for=f-img>Banner image URL (optional)</label><input id=f-img name=image_url type=url maxlength=500 value="{{ p.image_url or '' }}" placeholder="https://...">
  <label for=f-foot>Footer text (optional)</label><input id=f-foot name=footer maxlength=200 value="{{ p.footer or '' }}"></div>
 <div class=panel><h2>Style</h2><p class=lead>How members {{ 'start an application' if p.kind == 'apply' else 'open a ticket' }}.</p>
  <div class=seg style="margin-top:14px">{% for v in styles_for %}<label><input type=radio name=style value="{{ v }}" {{ 'checked' if p.style == v }}><span>{{ names[v] }}</span></label>{% endfor %}</div>
  <div data-for=button,menu,buttons><label>Button color</label><div class=seg>{% for v, l in bcolors %}<label><input type=radio name=button_color value="{{ v }}" {{ 'checked' if (p.button_color or bc_default) == v }}><span>{{ l }}</span></label>{% endfor %}</div></div>
  {% if p.kind == 'ticket' %}
  <label>Ticket types</label><div id=opts>{% for o in p.options %}<div class=qrow><input name=opt_label maxlength=80 placeholder="Label" aria-label="Label" value="{{ o.label }}"><input name=opt_desc maxlength=100 placeholder="Description (dropdown only)" aria-label="Description" value="{{ o.desc }}"><button type=button class=x aria-label="Remove type">&times;</button></div>{% endfor %}</div>
  <div class=split><span id=oc></span><button type=button id=addo class="btn ghost sm">Add ticket type</button></div>
  <p class=hint>Each type becomes its own button or dropdown entry, and its name is used for the ticket channel.</p>
  {% elif p.kind == 'multi' %}
  <label>Ticket panels to include</label>
  {% for t in tickets %}<label class=chk><input type=checkbox name=members value="{{ t.id }}" data-name="{{ t.button_label or t.name }}" {{ 'checked' if t.id in p.members }}><span>{{ t.button_label or t.name }}{% if t.entry_desc %} <small style="color:var(--mute)">{{ t.entry_desc }}</small>{% endif %}</span></label>
  {% else %}<div class=empty>No ticket panels yet. Create ticket panels first, then combine them here.</div>{% endfor %}
  <p class=hint>Each ticket panel becomes one entry and keeps its own category, support role, log channel and welcome message. After you rename or add ticket panels, publish this panel again.</p>
  {% else %}
  <div data-for=menu><label for=f-btn>Button text</label><input id=f-btn name=button_label maxlength=80 value="{{ p.button_label }}"></div>
  <label>Forms to include</label>
  {% for f in forms %}<label class=chk><input type=checkbox name=forms value="{{ f.id }}" data-name="{{ f.name }}" data-open="{{ 1 if f.open else 0 }}" {{ 'checked' if f.id in p.forms }}><span>{{ f.name }}{{ '' if f.open else ' (closed)' }}</span></label>
  {% else %}<div class=empty>No forms yet. Create a form on the Applications tab first.</div>{% endfor %}
  <p class=hint>Leave all unchecked to include every open form. The button per form uses the form name as its text.</p>
  {% endif %}
  <div data-for=dropdown><label for=f-ph>Dropdown placeholder</label><input id=f-ph name=placeholder maxlength=100 value="{{ p.placeholder }}"></div></div>
 {% if p.kind == 'ticket' %}<div class=panel><h2>Ticket settings</h2><p class=lead>Tickets from this panel use these settings. Fields left on none use the server defaults from the Tickets tab.</p>
  <label for=t-cat>Ticket category</label><select id=t-cat name=ticket_category>{{ opts(cats, p.ticket_category) }}</select>
  <label for=t-role>Support role</label><select id=t-role name=support_role>{{ opts(roles, p.support_role) }}</select>
  <label for=t-log>Log channel</label><select id=t-log name=ticket_log>{{ opts(text, p.ticket_log) }}</select>
  <label for=t-wel>Welcome message</label><textarea id=t-wel name=welcome rows=3 maxlength=1000 placeholder="Describe your issue and we will help. Write {user} to mention the member.">{{ p.welcome or '' }}</textarea>
  <label for=f-entry>Name in multi panels</label><input id=f-entry name=button_label maxlength=80 value="{{ p.button_label or '' }}" placeholder="{{ p.name }}">
  <label for=f-edesc>Description in multi panels</label><input id=f-edesc name=entry_desc maxlength=100 value="{{ p.entry_desc or '' }}" placeholder="Shown under the entry in a dropdown"></div>{% endif %}
 <div class=panel><h2>Publish</h2><p class=lead>Publishing sends the message, or updates it if it is already posted.</p>
  <label for=f-ch>Channel</label><select id=f-ch name=channel>{{ opts(text, p.channel_id) }}</select>
  </div>
</div>
<div class="panel stick"><h2>Preview</h2><p class=lead>Updates as you type.</p><div class=dc id=pv data-kind="{{ p.kind }}" style="margin-top:18px"></div></div>
</div>
<div class=bar><span class=dirtymsg>Unsaved changes</span><button class="btn ghost" name=do value=save>Save</button><button class=btn name=do value=publish>Save and publish</button></div><div style="height:72px"></div></form>""", gname=gname, gid=gid, p=p, forms=forms, text=text, styles_for=STYLES[p["kind"]], names=STYLE_NAMES, cats=cats, roles=roles, tickets=tickets,
                       bcolors=[("blurple", "Blurple"), ("gray", "Gray"), ("green", "Green"), ("red", "Red")], bc_default="green" if p["kind"] == "apply" else "blurple")

@web.post("/g/<gid>/panel/save")
@guild_route
async def panel_save(gid, gname):
    f = await request.form
    text, cats, roles = await gdata(gid)
    kind = f.get("kind") if f.get("kind") in ("apply", "multi") else "ticket"
    style = f.get("style") if f.get("style") in STYLES[kind] else STYLES[kind][0]
    color = f.get("color", "") if re.fullmatch(r"#[0-9a-fA-F]{6}", f.get("color", "")) else "#5865F2"
    labels, descs = f.getlist("opt_label"), f.getlist("opt_desc")
    options = [{"label": l.strip()[:80], "desc": (descs[n] if n < len(descs) else "").strip()[:100]} for n, l in enumerate(labels) if l.strip()][:25] if kind == "ticket" else []
    if kind == "ticket" and not options: options = [{"label": "Create ticket", "desc": ""}]
    valid = {r["id"] for r in await db("SELECT id FROM forms WHERE guild_id=$1", gid)}
    fids = [int(x) for x in f.getlist("forms") if x.isdigit() and int(x) in valid][:25] if kind == "apply" else []
    tids = {r["id"] for r in await db("SELECT id FROM panels WHERE guild_id=$1 AND kind='ticket'", gid)}
    members = [int(x) for x in f.getlist("members") if x.isdigit() and int(x) in tids][:25] if kind == "multi" else []
    pick = lambda key, items: f.get(key) if f.get(key) in {c["id"] for c in items} else None
    channel = pick("channel", text)
    tcat, trole, tlog = (pick("ticket_category", cats), pick("support_role", roles), pick("ticket_log", text)) if kind == "ticket" else (None, None, None)
    img = f.get("image_url", "").strip()[:500]; img = img if img.startswith("https://") else None
    foot = f.get("footer", "").strip()[:200]
    bcol = f.get("button_color") if f.get("button_color") in BS else None
    vals = ((f.get("name") or "").strip()[:60] or "Untitled panel", style, f.get("title", "").strip()[:200], f.get("description", "").strip()[:2000], color,
            f.get("button_label", "").strip()[:80], f.get("placeholder", "").strip()[:100], json.dumps(options), json.dumps(fids), channel,
            tcat, trole, tlog, f.get("welcome", "").strip()[:1000], f.get("entry_desc", "").strip()[:100], json.dumps(members), img, foot, bcol)
    pid = int(f["pid"]) if f.get("pid", "").isdigit() else None
    old = await db("SELECT * FROM panels WHERE id=$1 AND guild_id=$2", pid, gid, mode="fetchrow") if pid else None
    if pid and not old: abort(404)
    if old:
        if old["message_id"] and old["channel_id"] != channel:  # moved to another channel: remove the old message
            await dapi("DELETE", f"/channels/{old['channel_id']}/messages/{old['message_id']}")
            await db("UPDATE panels SET message_id=NULL WHERE id=$1", pid, mode="execute")
        await db("""UPDATE panels SET name=$1,style=$2,title=$3,description=$4,color=$5,button_label=$6,placeholder=$7,options=$8,forms=$9,channel_id=$10,
                    ticket_category=$11,support_role=$12,ticket_log=$13,welcome=$14,entry_desc=$15,members=$16,image_url=$17,footer=$18,button_color=$19 WHERE id=$20 AND guild_id=$21""", *vals, pid, gid, mode="execute")
    else:
        pid = await db("""INSERT INTO panels(name,style,title,description,color,button_label,placeholder,options,forms,channel_id,
                          ticket_category,support_role,ticket_log,welcome,entry_desc,members,image_url,footer,button_color,guild_id,kind)
                          VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,$21) RETURNING id""", *vals, gid, kind, mode="fetchval")
    session["msg"] = await publish_panel(gid, pid) if f.get("do") == "publish" else "Panel saved."
    return redirect(f"/g/{gid}/panel/{pid}")

@web.post("/g/<gid>/panel/<pid>/delete")
@guild_route
async def panel_delete(gid, gname, pid):
    old = await db("SELECT * FROM panels WHERE id=$1 AND guild_id=$2", int(pid) if pid.isdigit() else 0, gid, mode="fetchrow")
    if old:
        if old["message_id"]: await dapi("DELETE", f"/channels/{old['channel_id']}/messages/{old['message_id']}")
        await db("DELETE FROM panels WHERE id=$1", old["id"], mode="execute")
        session["msg"] = "Panel deleted."
    return redirect(f"/g/{gid}" + ("#apps" if old and old["kind"] == "apply" else "#tickets"))

@web.post("/g/<gid>/panel/<pid>/duplicate")
@guild_route
async def panel_duplicate(gid, gname, pid):
    row = await db("SELECT id FROM panels WHERE id=$1 AND guild_id=$2", int(pid) if pid.isdigit() else 0, gid, mode="fetchrow")
    if not row: abort(404)
    new = await db("""INSERT INTO panels(guild_id,kind,name,style,title,description,color,button_label,placeholder,options,forms,ticket_category,support_role,ticket_log,welcome,entry_desc,members,image_url,footer,button_color)
                      SELECT guild_id,kind,LEFT(name,52) || ' (copy)',style,title,description,color,button_label,placeholder,options,forms,ticket_category,support_role,ticket_log,welcome,entry_desc,members,image_url,footer,button_color
                      FROM panels WHERE id=$1 RETURNING id""", row["id"], mode="fetchval")
    session["msg"] = "Panel duplicated. It is not published yet."
    return redirect(f"/g/{gid}/panel/{new}")
