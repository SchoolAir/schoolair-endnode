#!/usr/bin/env python3
"""SchoolAir Gatekeeper Registration Wizard — two-step setup flow.

AP mode  (unregistered / fresh device):
  Step 1 – WiFi credentials + registration token  → validate token with server.
  Step 2 – Site name, asset name, environment     → full registration.

WiFi mode (registered device, local-network access):
  Management – Token-first authentication         → update device identity.
"""

import asyncio
import html as _html
import json
import os
import pwd
import random
import re
import secrets
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

from microdot import Microdot, Response
from microdot.websocket import with_websocket

from config import (
    AP_CONNECTION_NAME,
    AP_INTERFACE,
    AP_IP,
    CONFIG_DIR,
    ERROR_FILE,
    HEARTBEAT_TIMEOUT,
    HEARTBEAT_URL,
    NEW_SERVER_BASE_URL,
    NODE_RED_TOKEN_FILE,
    PI_MAIN_ENV_PATH,
    SERVER_PORT,
    STAGING_FILE,
    STATUS_FILE,
    VALIDATE_URL,
)

LEGACY_URL   = "https://data.schoolair.org/node/aqc/register"
TEMP_PROFILE = "school-air-temp"
SAVED_PREFIX = "schoolair-"
IDLE_TIMEOUT = 15 * 60  # seconds idle before auto-shutdown (management mode only)
SESSION_TTL  = 30 * 60  # seconds before an incomplete session expires

app = Microdot()


# ── In-memory state ────────────────────────────────────────────────────────────

# Dict of active sessions: session_token → session data.
# mode:  "setup" (AP flow) | "management" (WiFi-mode flow)
# step:  0 = created/in-progress, 1 = WiFi+token validated, 2 = registered
wifi_sessions: dict = {}

# Single status dict broadcast to all WebSocket clients.
# "redirect" is populated on step1_success so the browser knows where to go.
reg_state: dict = {"state": "idle", "message": "", "redirect": ""}

_last_activity: float = time.time()
_connection_in_progress: bool = False  # guards single wlan0 against concurrent ops


@app.before_request
async def _track_activity(request):
    global _last_activity
    _last_activity = time.time()
    host = (request.headers.get("Host") or "").split(":")[0].lower().strip()
    _FRIENDLY_HOSTS = {"localhost", "schoolair-register.local", "schoolair", "regiwiz"}
    if (host
            and not re.match(r"^\d+\.\d+\.\d+\.\d+$", host)
            and not host.endswith(".local")
            and host not in _FRIENDLY_HOSTS):
        return Response("", status_code=302, headers={"Location": f"http://{AP_IP}/"})


# ── HTML helpers ──────────────────────────────────────────────────────────────

def _render(template: str, raw: dict = None, **kwargs) -> str:
    """Replace [[key]] placeholders. kwargs are HTML-escaped; raw dict is verbatim."""
    for k, v in kwargs.items():
        template = template.replace(f"[[{k}]]", _html.escape(str(v)))
    for k, v in (raw or {}).items():
        template = template.replace(f"[[{k}]]", str(v))
    return template


def _html_response(body: str, status: int = 200) -> Response:
    return Response(body, status_code=status,
                    headers={"Content-Type": "text/html; charset=utf-8"})


def _json_response(data: dict, status: int = 200) -> Response:
    return Response(json.dumps(data), status_code=status,
                    headers={"Content-Type": "application/json"})


def _friendly_error(msg: str) -> str:
    if "Token rejected" in msg or "401" in msg or "403" in msg:
        return "Invalid or outdated token."
    if "Could not reach" in msg or "URLError" in msg:
        return "SchoolAir Cloud is unreachable — check internet connection."
    return msg


# ── Session helpers ────────────────────────────────────────────────────────────

def _new_session(mode: str, token: str = "", ssid: str = "", password: str = "") -> str:
    """Create and register a new wizard session. Returns the session token."""
    sess_tok = secrets.token_urlsafe(16)
    now = time.time()
    wifi_sessions[sess_tok] = {
        "mode":          mode,
        "token":         token,
        "ssid":          ssid,
        "password":      password,
        "site":          "",
        "asset":         "",
        "environment":   "indoor",
        "migrate":       False,
        "created_at":    now,
        "last_activity": now,
        "step":          0,
    }
    return sess_tok


def _get_session(sess_tok: str) -> dict | None:
    sess = wifi_sessions.get(sess_tok)
    if not sess:
        return None
    if time.time() - sess["last_activity"] > SESSION_TTL:
        wifi_sessions.pop(sess_tok, None)
        return None
    sess["last_activity"] = time.time()
    return sess


def _prune_sessions() -> None:
    now = time.time()
    for k in [k for k, v in wifi_sessions.items()
              if now - v["last_activity"] > SESSION_TTL]:
        wifi_sessions.pop(k, None)


def _resolve_session(request) -> tuple:
    """Return (sess_tok, sess) extracted from request, or (None, None) if invalid."""
    tok = (
        request.headers.get("X-Session")
        or (request.json or {}).get("session")
        or (request.form or {}).get("session")
        or (request.args or {}).get("s")
    )
    if not tok:
        return None, None
    sess = _get_session(tok)
    return (tok, sess) if sess else (None, None)


# ── Wizard greetings ──────────────────────────────────────────────────────────

_SKIN_TONES = ["\U0001F3FB", "\U0001F3FC", "\U0001F3FD", "\U0001F3FE", "\U0001F3FF"]
_WIZARD_BASE = "\U0001F9D9"
_ZWJ  = "‍"
_VS16 = "️"
_MALE   = "♂"
_FEMALE = "♀"
_skin_tone_counts = [0] * len(_SKIN_TONES)


def _pick_skin_tone() -> str:
    min_c = min(_skin_tone_counts)
    candidates = [i for i, c in enumerate(_skin_tone_counts) if c == min_c]
    idx = random.choice(candidates)
    _skin_tone_counts[idx] += 1
    return _SKIN_TONES[idx]


def _wizard_emoji(gender: str) -> str:
    tone = _pick_skin_tone()
    if gender == "m":
        return _WIZARD_BASE + tone + _ZWJ + _MALE + _VS16
    if gender == "f":
        return _WIZARD_BASE + tone + _ZWJ + _FEMALE + _VS16
    return _WIZARD_BASE + tone + _VS16


_GANDALF_QUOTE = ("A wizard is never late. Nor is he early. He arrives precisely when he means to.", "Gandalf")
_GLINDA_QUOTE  = ("You've always had the power, my dear. You just had to learn it for yourself.", "Glinda")
_RAINE_QUOTE   = ("Go. You know I can't stand an audience.", "Raine Whispers")


# ── Device landing page (served when Host starts with "schoolair") ─────────────

LANDING_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SchoolAir Device</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
     background:linear-gradient(135deg,#667eea,#764ba2);min-height:100vh;
     display:flex;align-items:center;justify-content:center;padding:1rem}
.card{background:#fff;border-radius:16px;padding:2rem;max-width:400px;
      width:100%;box-shadow:0 8px 32px rgba(0,0,0,.18)}
h1{font-size:1.4rem;color:#1a56db;font-weight:700;text-align:center;margin-bottom:1.5rem}
.row{display:flex;justify-content:space-between;align-items:baseline;
     padding:.6rem 0;border-bottom:1px solid #f0f0f0}
.row:last-of-type{border-bottom:none}
.lbl{color:#666;font-size:.85rem}
.val{font-weight:600;font-size:.95rem;text-align:right;max-width:60%;word-break:break-all}
.badge{display:inline-block;padding:.2rem .6rem;border-radius:999px;font-size:.78rem;font-weight:700}
.badge-ok{background:#d1fae5;color:#065f46}
.badge-warn{background:#fef3c7;color:#92400e}
.btn{display:block;width:100%;margin-top:1.5rem;padding:.75rem;
     background:#1a56db;color:#fff;border:none;border-radius:8px;
     font-size:1rem;font-weight:600;cursor:pointer;text-align:center;text-decoration:none}
.btn:hover{background:#1e429f}
</style>
</head>
<body>
<div class="card">
  <h1>🌬️ SchoolAir Device</h1>
  <div class="row"><span class="lbl">Hostname</span><span class="val">[[hostname]]</span></div>
  <div class="row"><span class="lbl">Registration</span><span class="val">[[reg_badge]]</span></div>
  [[site_row]]
  [[asset_row]]
  <a href="/" class="btn">Open Registration Wizard →</a>
</div>
</body>
</html>"""


# ── Step 1: WiFi + Token ──────────────────────────────────────────────────────

STEP1_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SchoolAir Setup – Step 1</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
     background:linear-gradient(135deg,#667eea,#764ba2);min-height:100vh;
     display:flex;align-items:center;justify-content:center;padding:1rem}
.card{background:#fff;border-radius:16px;padding:2rem;max-width:440px;
      width:100%;box-shadow:0 8px 32px rgba(0,0,0,.18)}
.logo{text-align:center;margin-bottom:1.25rem}
.logo h1{font-size:1.4rem;color:#1a56db;font-weight:700}
.logo p{color:#6b7280;font-size:.8rem;margin-top:.25rem}
.sect{font-size:.72rem;font-weight:600;color:#6b7280;text-transform:uppercase;
      letter-spacing:.05em;margin:1.25rem 0 .4rem}
label{display:block;font-size:.875rem;font-weight:500;color:#374151;
      margin-bottom:.2rem;margin-top:.65rem}
input[type=text],input[type=password]{width:100%;padding:.6rem .75rem;
  border:1.5px solid #d1d5db;border-radius:8px;font-size:1rem;outline:none;
  transition:border-color .2s}
input:focus{border-color:#1a56db}
.pw{position:relative}
.pw input{padding-right:3.5rem}
.pw button{position:absolute;right:.75rem;top:50%;transform:translateY(-50%);
  background:none;border:none;cursor:pointer;color:#6b7280;font-size:.85rem;padding:.2rem}
.notice{padding:.75rem;border-radius:8px;font-size:.875rem;margin:.5rem 0}
.notice-err{background:#fef2f2;color:#dc2626;border:1.5px solid #fca5a5}
.notice-quote{background:#eef2ff;color:#3730a3;border:1.5px solid #a5b4fc;
  padding:.65rem .85rem;border-radius:8px;font-size:.82rem;font-style:italic;margin-bottom:.75rem}
.quote-author{font-size:.72rem;font-weight:600;font-style:normal;display:block;margin-top:.2rem}
.btn{width:100%;margin-top:1.25rem;padding:.8rem;border:none;border-radius:10px;
     font-size:.95rem;font-weight:600;cursor:pointer;transition:background .2s}
.btn-blue{background:#1a56db;color:#fff}
.btn-blue:hover{background:#1649c0}
.btn-scan{width:100%;margin-top:.75rem;padding:.65rem;border:1.5px solid #1a56db;
  border-radius:10px;background:#eff6ff;color:#1a56db;font-size:.9rem;font-weight:600;
  cursor:pointer;transition:background .2s}
.btn-scan:hover:not(:disabled){background:#dbeafe}
.btn-scan:disabled{opacity:.5;cursor:not-allowed}
.scan-list{margin-top:.4rem}
.scan-item{display:flex;align-items:center;justify-content:space-between;
  padding:.5rem .75rem;border:1.5px solid #e5e7eb;border-radius:8px;
  margin-top:.35rem;cursor:pointer;transition:border-color .15s,background .15s}
.scan-item:hover{border-color:#1a56db;background:#eff6ff}
.scan-ssid{font-size:.875rem;color:#374151;font-weight:500;word-break:break-all}
.scan-meta{font-size:.75rem;color:#6b7280;white-space:nowrap;margin-left:.5rem}
.empty-msg{font-size:.85rem;color:#9ca3af;padding:.5rem 0}
</style>
</head>
<body>
<div class="card">
  <div class="logo">
    <h1>[[wizard_emoji]] SchoolAir Setup</h1>
    <p>Step 1 of 2 — Network &amp; Authorization</p>
  </div>
  <div class="notice-quote"><em>"[[quote]]"</em><span class="quote-author">— [[quote_author]]</span></div>
  <div id="notice" class="notice notice-err" style="display:none"></div>

  <div class="sect">Registration Token</div>
  <input type="text" id="token" placeholder="8-character code, e.g. aB3xQr7Z"
         autocomplete="on" value="[[prefill_token]]">

  <div class="sect">Wi-Fi Network</div>
  <button type="button" id="scan-btn" class="btn-scan" onclick="doScan()">🔍 Scan for Networks</button>
  <div id="scan-list" class="scan-list" onclick="handleScanClick(event)"></div>
  <label for="ssid">Network Name (SSID)</label>
  <input type="text" id="ssid" placeholder="School Wi-Fi name (or scan above)"
         value="[[prefill_ssid]]">
  <label for="password">Password</label>
  <div class="pw">
    <input type="password" id="password" placeholder="Leave blank for open networks">
    <button type="button" onclick="tpw()">Show</button>
  </div>

  <button type="button" class="btn btn-blue" onclick="doConnect()">Connect &amp; Verify →</button>

  <form id="sf" method="POST" action="/step1/connect" style="display:none">
    <input type="hidden" name="token"    id="f-token">
    <input type="hidden" name="ssid"     id="f-ssid">
    <input type="hidden" name="password" id="f-password">
  </form>
</div>
<script>
function tpw(){
  const f=document.getElementById('password'),b=f.nextElementSibling;
  if(f.type==='password'){f.type='text';b.textContent='Hide';}
  else{f.type='password';b.textContent='Show';}
}
function showErr(msg){
  const el=document.getElementById('notice');
  el.textContent=msg;el.style.display='';
}
function signalBars(s){
  if(s>=75)return'||||';if(s>=50)return'||| ';if(s>=25)return'||  ';return'|   ';
}
function handleScanClick(e){
  const item=e.target.closest('.scan-item');if(!item)return;
  document.getElementById('ssid').value=item.dataset.ssid;
  if(item.dataset.secured==='1')document.getElementById('password').focus();
  else document.getElementById('ssid').focus();
}
async function doScan(){
  const btn=document.getElementById('scan-btn');
  const list=document.getElementById('scan-list');
  btn.disabled=true;btn.textContent='Scanning…';list.innerHTML='';
  try{
    const r=await fetch('/wifi/scan',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
    const d=await r.json();
    if(d.error){showErr(d.error);return;}
    const nets=d.networks||[];
    if(!nets.length){list.innerHTML='<p class="empty-msg">No networks found.</p>';return;}
    list.innerHTML=nets.map(n=>{
      const safe=n.ssid.replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
      const disp=n.ssid.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
      return`<div class="scan-item" data-ssid="${safe}" data-secured="${n.secured?'1':'0'}">
        <span class="scan-ssid">${disp}</span>
        <span class="scan-meta">${signalBars(n.signal)} ${n.secured?'🔒':'🔓'}</span>
      </div>`;
    }).join('');
  }catch{showErr('Scan failed — try again.');}
  btn.disabled=false;btn.textContent='🔍 Scan for Networks';
}
function doConnect(){
  const token=document.getElementById('token').value.trim();
  const ssid=document.getElementById('ssid').value.trim();
  if(!token){showErr('Registration token is required.');return;}
  if(!ssid){showErr('Network name (SSID) is required.');return;}
  document.getElementById('f-token').value=token;
  document.getElementById('f-ssid').value=ssid;
  document.getElementById('f-password').value=document.getElementById('password').value;
  document.getElementById('sf').submit();
}
</script>
</body>
</html>"""


# ── Step 2: Site + Asset ──────────────────────────────────────────────────────

STEP2_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SchoolAir Setup – Step 2</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
     background:linear-gradient(135deg,#667eea,#764ba2);min-height:100vh;
     display:flex;align-items:center;justify-content:center;padding:1rem}
.card{background:#fff;border-radius:16px;padding:2rem;max-width:440px;
      width:100%;box-shadow:0 8px 32px rgba(0,0,0,.18)}
.logo{text-align:center;margin-bottom:1.25rem}
.logo h1{font-size:1.4rem;color:#1a56db;font-weight:700}
.logo p{color:#6b7280;font-size:.8rem;margin-top:.25rem}
.badge{display:inline-block;background:#ecfdf5;color:#065f46;border:1.5px solid #6ee7b7;
       border-radius:20px;padding:.2rem .75rem;font-size:.78rem;font-weight:600;margin-top:.4rem}
.sect{font-size:.72rem;font-weight:600;color:#6b7280;text-transform:uppercase;
      letter-spacing:.05em;margin:1.25rem 0 .4rem}
label{display:block;font-size:.875rem;font-weight:500;color:#374151;
      margin-bottom:.2rem;margin-top:.65rem}
.field-row{display:flex;gap:.4rem;align-items:flex-end}
.field-row input{flex:1;min-width:0}
input[type=text]{width:100%;padding:.6rem .75rem;border:1.5px solid #d1d5db;
  border-radius:8px;font-size:1rem;outline:none;transition:border-color .2s;background:#fff}
input:focus{border-color:#1a56db}
input:disabled{background:#f3f4f6;color:#374151;cursor:default}
.lock-btn{padding:.55rem .7rem;border:1.5px solid #d1d5db;border-radius:8px;
  background:#f9fafb;cursor:pointer;font-size:.95rem;line-height:1;
  flex-shrink:0;transition:border-color .2s}
.lock-btn:hover{border-color:#1a56db}
.tog{display:flex;gap:.5rem;margin-top:.5rem}
.tog input[type=radio]{display:none}
.tog label{flex:1;text-align:center;padding:.6rem;border:1.5px solid #d1d5db;
  border-radius:8px;cursor:pointer;font-weight:500;color:#6b7280;
  transition:all .2s;margin:0}
.tog input[type=radio]:checked+label{background:#1a56db;color:#fff;border-color:#1a56db}
.migrate-row{display:flex;align-items:center;gap:.6rem;margin:.75rem 0 .25rem}
.migrate-row input[type=checkbox]{width:1.1rem;height:1.1rem;accent-color:#1a56db;
  flex-shrink:0;cursor:pointer}
.migrate-lbl{font-size:.9rem;color:#374151;cursor:pointer;display:flex;align-items:center;gap:.4rem}
.tip{position:relative;display:inline-flex;align-items:center;justify-content:center;
  width:1.1rem;height:1.1rem;border-radius:50%;background:#d1d5db;color:#374151;
  font-size:.7rem;font-weight:700;cursor:help;flex-shrink:0}
.tip-body{display:none;position:absolute;left:1.4rem;top:50%;transform:translateY(-50%);
  background:#1f2937;color:#f9fafb;font-size:.78rem;line-height:1.5;font-weight:400;
  padding:.6rem .75rem;border-radius:8px;width:220px;z-index:10;pointer-events:none;
  box-shadow:0 4px 12px rgba(0,0,0,.3)}
.tip:hover .tip-body,.tip:focus .tip-body{display:block}
.notice{padding:.75rem;border-radius:8px;font-size:.875rem;margin:.5rem 0}
.notice-err{background:#fef2f2;color:#dc2626;border:1.5px solid #fca5a5}
.notice-quote{background:#eef2ff;color:#3730a3;border:1.5px solid #a5b4fc;
  padding:.65rem .85rem;border-radius:8px;font-size:.82rem;font-style:italic;margin-bottom:.75rem}
.quote-author{font-size:.72rem;font-weight:600;font-style:normal;display:block;margin-top:.2rem}
.btn{width:100%;margin-top:1.25rem;padding:.8rem;border:none;border-radius:10px;
     font-size:.95rem;font-weight:600;cursor:pointer;transition:background .2s}
.btn-blue{background:#1a56db;color:#fff}
.btn-blue:hover:not(:disabled){background:#1649c0}
.btn-blue:disabled{background:#93c5fd;cursor:not-allowed}
.btn-ghost{background:none;color:#6b7280;font-size:.85rem;margin-top:.75rem;padding:.4rem;
  text-decoration:underline;cursor:pointer;border:none}
</style>
</head>
<body>
<div class="card">
  <div class="logo">
    <h1>[[wizard_emoji]] SchoolAir Setup</h1>
    <p>Step 2 of 2 — Device Details</p>
    <div class="badge">Wi-Fi: [[ssid]]</div>
  </div>
  <div class="notice-quote"><em>"[[quote]]"</em><span class="quote-author">— [[quote_author]]</span></div>
  <div id="notice" class="notice notice-err" style="display:none"></div>

  <div class="sect">Location</div>
  <label for="site">Site Name</label>
  <div class="field-row">
    <input type="text" id="site" placeholder="e.g. Lincoln Elementary" oninput="update()">
    <button type="button" id="site-lock-btn" class="lock-btn"
            onclick="toggleLock('site')" style="display:none" title="Edit / Lock">✏️</button>
  </div>
  <label for="asset">Asset Name</label>
  <div class="field-row">
    <input type="text" id="asset" placeholder="e.g. Room 302" oninput="update()">
    <button type="button" id="asset-lock-btn" class="lock-btn"
            onclick="toggleLock('asset')" style="display:none" title="Edit / Lock">✏️</button>
  </div>

  <div class="migrate-row">
    <input type="checkbox" id="migrate">
    <label for="migrate" class="migrate-lbl">
      New monitoring location
      <span class="tip" tabindex="0" aria-label="What does this mean?">?
        <span class="tip-body">Check this when moving the sensor to a different physical
          location and wanting to keep old data separate. Leave unchecked to rename.</span>
      </span>
    </label>
  </div>

  <div class="sect">Environment</div>
  <div class="tog">
    <input type="radio" id="ev_in"  name="environment" value="indoor"  [[indoor_checked]]>
    <label for="ev_in">Indoor</label>
    <input type="radio" id="ev_out" name="environment" value="outdoor" [[outdoor_checked]]>
    <label for="ev_out">Outdoor</label>
  </div>

  <form id="sf" method="POST" action="/step2/register" style="display:none">
    <input type="hidden" name="session"     value="[[session_token]]">
    <input type="hidden" name="site"        id="f-site">
    <input type="hidden" name="asset_name"  id="f-asset">
    <input type="hidden" name="environment" id="f-env">
    <input type="hidden" name="migrate"     id="f-migrate">
  </form>

  <button type="button" id="reg-btn" class="btn btn-blue" onclick="doRegister()" disabled>
    Complete Registration
  </button>
  <button type="button" class="btn btn-ghost" onclick="location.href='/'">← Start Over</button>
</div>
<script>
const INIT = [[init_json]];
const locked = {site: INIT.siteLocked, asset: INIT.assetLocked};

function applyLock(field){
  const el=document.getElementById(field);
  const btn=document.getElementById(field+'-lock-btn');
  el.disabled=locked[field];btn.textContent=locked[field]?'✏️':'🔒';
}
function init(){
  document.getElementById('site').value=INIT.site;
  document.getElementById('asset').value=INIT.asset;
  if(INIT.site){document.getElementById('site-lock-btn').style.display='';}
  if(INIT.asset){document.getElementById('asset-lock-btn').style.display='';}
  applyLock('site');applyLock('asset');update();
}
function toggleLock(field){
  const el=document.getElementById(field);
  const initVal=field==='site'?INIT.site:INIT.asset;
  if(locked[field]){locked[field]=false;applyLock(field);el.focus();}
  else{
    if(!initVal){showErr('No saved value to revert to.');return;}
    locked[field]=true;el.value=initVal;applyLock(field);
  }
  update();
}
function update(){
  const site=document.getElementById('site').value.trim();
  const asset=document.getElementById('asset').value.trim();
  document.getElementById('reg-btn').disabled=!(site&&asset);
}
function showErr(msg){
  const el=document.getElementById('notice');el.textContent=msg;el.style.display='';
}
function doRegister(){
  const site=document.getElementById('site').value.trim();
  const asset=document.getElementById('asset').value.trim();
  if(!site||!asset){showErr('Site and Asset are required.');return;}
  const env=document.querySelector('input[name="environment"]:checked')?.value||'indoor';
  const migrate=document.getElementById('migrate').checked;
  document.getElementById('f-site').value=site;
  document.getElementById('f-asset').value=asset;
  document.getElementById('f-env').value=env;
  document.getElementById('f-migrate').value=migrate?'1':'';
  document.getElementById('sf').submit();
}
init();
</script>
</body>
</html>"""


# ── Connecting page (shown during background WiFi/registration tasks) ──────────

CONNECTING_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SchoolAir – Connecting</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
     background:linear-gradient(135deg,#667eea,#764ba2);min-height:100vh;
     display:flex;align-items:center;justify-content:center;padding:1rem}
.card{background:#fff;border-radius:16px;padding:2rem 1.5rem;max-width:420px;
      width:100%;text-align:center;box-shadow:0 8px 32px rgba(0,0,0,.18)}
h1{font-size:1.4rem;color:#1a56db;margin-bottom:.5rem}
#icon{font-size:3rem;margin:1.5rem 0}
#msg{color:#374151;font-size:1rem;min-height:3rem;line-height:1.5}
#hint{color:#6b7280;font-size:.8rem;margin-top:1rem;padding:.75rem;
      background:#f9fafb;border-radius:8px;display:none;line-height:1.5}
.spin{display:inline-block;width:2.5rem;height:2.5rem;
      border:4px solid #e5e7eb;border-top-color:#1a56db;
      border-radius:50%;animation:sp .8s linear infinite}
@keyframes sp{to{transform:rotate(360deg)}}
#retry{display:none;margin-top:1.5rem;padding:.75rem 1.5rem;
       background:#1a56db;color:#fff;border:none;border-radius:8px;
       font-size:1rem;cursor:pointer}
.quote-block{margin-top:1.25rem;font-size:.82rem;color:#3730a3;
  background:#eef2ff;border:1.5px solid #a5b4fc;border-radius:8px;
  padding:.65rem .85rem;font-style:italic;line-height:1.5;text-align:left}
.q-author{font-size:.72rem;font-weight:600;font-style:normal;color:#3730a3;
  display:block;margin-top:.2rem}
</style>
</head>
<body>
<div class="card">
  <h1>[[wizard_emoji]] SchoolAir Setup</h1>
  <div id="icon"><div class="spin"></div></div>
  <div id="msg">Connecting…</div>
  <div id="hint"></div>
  <div class="quote-block" style="display:none"><em>"[[quote]]"</em><span class="q-author">— [[quote_author]]</span></div>
  <button id="retry" onclick="location.href='[[retry_url]]'">Try Again</button>
</div>
<script>
const STEP=[[step]];
const AP_DROP_1="Testing your Wi-Fi — the setup hotspot briefly dropped. This is normal.";
const AP_DROP_2="The setup hotspot dropped — the device is connecting to the school Wi-Fi. "+
  "If successful, registration is complete. If the hotspot reappears within 60 seconds, "+
  "tap Try Again.";
const MGMT_DROP="Switching networks — this page's connection to the device dropped, which "+
  "is expected. If the new network works, the device will reappear on it. If not, it will "+
  "fall back to its setup hotspot after a couple of minutes.";
function icon(t){const el=document.getElementById('icon');
  if(t==='spin')el.innerHTML='<div class="spin"></div>';else el.textContent=t;}
function hint(t){const h=document.getElementById('hint');h.textContent=t;h.style.display='block';}
function msg(t){document.getElementById('msg').textContent=t;}
function showQuote(){const q=document.querySelector('.quote-block');if(q)q.style.display='';}
let ws,dropped=false,reconnTimer;
function connect(){
  const proto=location.protocol==='https:'?'wss':'ws';
  ws=new WebSocket(proto+'://'+location.host+'/ws/status');
  ws.onmessage=function(e){
    const d=JSON.parse(e.data);
    if(d.state==='ping')return;
    msg(d.message);
    if(d.state==='step1_success'){
      icon('✅');hint('Token verified! Opening the device details form…');
      showQuote();setTimeout(()=>{window.location.href=d.redirect;},2000);
    } else if(d.state==='success'){
      icon('✅');
      if(STEP==='mgmt'){
        hint('Connected and saved.');
        const r=document.getElementById('retry');
        r.textContent='← Back to Device Configuration';r.style.display='inline-block';
      } else {
        hint(STEP===2?'Registration complete. This hotspot will close shortly.':'Done!');
      }
      showQuote();
    } else if(d.state==='error'){
      icon('❌');document.getElementById('retry').style.display='inline-block';dropped=true;ws.close();
    }
  };
  ws.onclose=function(){
    clearTimeout(reconnTimer);
    if(!dropped){
      dropped=true;icon('📶');
      msg(STEP===1?AP_DROP_1:(STEP==='mgmt'?MGMT_DROP:AP_DROP_2));
      hint(STEP==='mgmt'?'':'On Pi Zero hardware the hotspot may drop during connection — this is normal.');
    }
    reconnTimer=setTimeout(connect,3000);
  };
  ws.onerror=function(){ws.close();};
}
connect();
</script>
</body>
</html>"""


# ── Management mode: token auth form ─────────────────────────────────────────

MANAGEMENT_AUTH_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SchoolAir Device</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
     background:linear-gradient(135deg,#667eea,#764ba2);min-height:100vh;
     display:flex;align-items:center;justify-content:center;padding:1rem}
.card{background:#fff;border-radius:16px;padding:2rem;max-width:400px;
      width:100%;box-shadow:0 8px 32px rgba(0,0,0,.18)}
.logo{text-align:center;margin-bottom:1.5rem}
.logo h1{font-size:1.4rem;color:#1a56db;font-weight:700}
.logo p{color:#6b7280;font-size:.8rem;margin-top:.25rem}
.row{display:flex;justify-content:space-between;align-items:baseline;
     padding:.55rem 0;border-bottom:1px solid #f0f0f0}
.row:last-of-type{border-bottom:none}
.lbl{color:#666;font-size:.85rem}
.val{font-weight:600;font-size:.9rem;text-align:right;word-break:break-all}
.badge{display:inline-block;padding:.2rem .6rem;border-radius:999px;font-size:.75rem;font-weight:700}
.badge-ok{background:#d1fae5;color:#065f46}
.sect{font-size:.72rem;font-weight:600;color:#6b7280;text-transform:uppercase;
      letter-spacing:.05em;margin:1.25rem 0 .4rem}
label{display:block;font-size:.875rem;font-weight:500;color:#374151;margin-bottom:.2rem}
input[type=text]{width:100%;padding:.6rem .75rem;border:1.5px solid #d1d5db;
  border-radius:8px;font-size:1rem;outline:none;transition:border-color .2s}
input:focus{border-color:#1a56db}
.notice{padding:.75rem;border-radius:8px;font-size:.875rem;margin:.5rem 0}
.notice-err{background:#fef2f2;color:#dc2626;border:1.5px solid #fca5a5}
.btn{width:100%;margin-top:1.25rem;padding:.8rem;border:none;border-radius:10px;
     font-size:.95rem;font-weight:600;cursor:pointer;background:#1a56db;color:#fff}
.btn:hover{background:#1649c0}
</style>
</head>
<body>
<div class="card">
  <div class="logo">
    <h1>🌬️ SchoolAir Device</h1>
    <p>[[hostname]]</p>
  </div>
  <div class="row"><span class="lbl">Registration</span>
    <span class="val">[[reg_badge]]</span></div>
  [[site_row]]
  [[asset_row]]

  <div class="sect">Enter Token to Configure</div>
  <label for="token">Registration Token</label>
  <input type="text" id="token" placeholder="8-character code, e.g. aB3xQr7Z" autocomplete="on">
  <div id="notice" class="notice notice-err" style="display:none"></div>
  <button type="button" class="btn" onclick="doAuth()">Verify →</button>
</div>
<script>
function showErr(msg){
  const el=document.getElementById('notice');el.textContent=msg;el.style.display='';
}
async function doAuth(){
  const token=document.getElementById('token').value.trim();
  if(!token){showErr('Token is required.');return;}
  try{
    const r=await fetch('/management/auth',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({token}),
    });
    const d=await r.json();
    if(d.redirect)window.location.href=d.redirect;
    else showErr(d.error||'Verification failed.');
  }catch{showErr('Could not reach the device.');}
}
</script>
</body>
</html>"""


# ── Management mode: device configuration form ────────────────────────────────

MANAGEMENT_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SchoolAir – Device Configuration</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
     background:linear-gradient(135deg,#667eea,#764ba2);min-height:100vh;
     display:flex;align-items:center;justify-content:center;padding:1rem}
.card{background:#fff;border-radius:16px;padding:2rem;max-width:440px;
      width:100%;box-shadow:0 8px 32px rgba(0,0,0,.18)}
.logo{text-align:center;margin-bottom:1.25rem}
.logo h1{font-size:1.4rem;color:#1a56db;font-weight:700}
.badge{display:inline-block;background:#ecfdf5;color:#065f46;border:1.5px solid #6ee7b7;
       border-radius:20px;padding:.2rem .75rem;font-size:.78rem;font-weight:600;margin-top:.4rem}
.sect{font-size:.72rem;font-weight:600;color:#6b7280;text-transform:uppercase;
      letter-spacing:.05em;margin:1.25rem 0 .4rem}
label{display:block;font-size:.875rem;font-weight:500;color:#374151;
      margin-bottom:.2rem;margin-top:.65rem}
input[type=text]{width:100%;padding:.6rem .75rem;border:1.5px solid #d1d5db;
  border-radius:8px;font-size:1rem;outline:none;transition:border-color .2s}
input:focus{border-color:#1a56db}
.tog{display:flex;gap:.5rem;margin-top:.5rem}
.tog input[type=radio]{display:none}
.tog label{flex:1;text-align:center;padding:.6rem;border:1.5px solid #d1d5db;
  border-radius:8px;cursor:pointer;font-weight:500;color:#6b7280;transition:all .2s;margin:0}
.tog input[type=radio]:checked+label{background:#1a56db;color:#fff;border-color:#1a56db}
.notice{padding:.75rem;border-radius:8px;font-size:.875rem;margin:.5rem 0}
.notice-ok{background:#ecfdf5;color:#065f46;border:1.5px solid #6ee7b7}
.notice-err{background:#fef2f2;color:#dc2626;border:1.5px solid #fca5a5}
.btn{width:100%;margin-top:1.25rem;padding:.8rem;border:none;border-radius:10px;
     font-size:.95rem;font-weight:600;cursor:pointer;transition:background .2s}
.btn-blue{background:#1a56db;color:#fff}
.btn-blue:hover{background:#1649c0}
.btn-danger{background:#fef2f2;color:#dc2626;border:1.5px solid #fca5a5;margin-top:.5rem}
.btn-danger:hover{background:#fee2e2}
.divider{border:none;border-top:1px solid #e5e7eb;margin:1.25rem 0}
.network-row{display:flex;align-items:center;padding:.55rem .75rem;
  border:1.5px solid #e5e7eb;border-radius:8px;margin-top:.4rem}
.net-active{font-size:.75rem;color:#059669;margin-left:.4rem}
.net-name{font-size:.875rem;color:#374151;font-weight:500;word-break:break-all;
  flex:1;min-width:0;margin-right:.5rem}
.net-controls{display:flex;align-items:center;gap:.3rem;flex-shrink:0}
.forget-btn{padding:.3rem .6rem;border:1.5px solid #fca5a5;border-radius:6px;
  background:#fff;color:#dc2626;font-size:.95rem;cursor:pointer}
.forget-btn:hover{background:#fef2f2}
.prio-btn{padding:.3rem .5rem;border:1.5px solid #c7d2fe;border-radius:6px;
  background:#eef2ff;color:#3730a3;font-size:.95rem;cursor:pointer}
.prio-btn:hover{background:#e0e7ff}
.net-priority{font-size:.7rem;color:#6b7280;font-family:monospace;background:#f3f4f6;
  border-radius:4px;padding:.1rem .35rem;white-space:nowrap}
.empty-msg{font-size:.85rem;color:#9ca3af;padding:.5rem 0}
.pw{position:relative}
.pw input{padding-right:3.5rem}
.pw button{position:absolute;right:.75rem;top:50%;transform:translateY(-50%);
  background:none;border:none;cursor:pointer;color:#6b7280;font-size:.85rem;padding:.2rem}
.btn-scan{width:100%;margin-top:.75rem;padding:.65rem;border:1.5px solid #1a56db;
  border-radius:10px;background:#eff6ff;color:#1a56db;font-size:.9rem;font-weight:600;
  cursor:pointer;transition:background .2s}
.btn-scan:hover:not(:disabled){background:#dbeafe}
.btn-scan:disabled{opacity:.5;cursor:not-allowed}
.scan-list{margin-top:.4rem}
.scan-item{display:flex;align-items:center;justify-content:space-between;
  padding:.5rem .75rem;border:1.5px solid #e5e7eb;border-radius:8px;
  margin-top:.35rem;cursor:pointer;transition:border-color .15s,background .15s}
.scan-item:hover{border-color:#1a56db;background:#eff6ff}
.scan-ssid{font-size:.875rem;color:#374151;font-weight:500;word-break:break-all}
.scan-meta{font-size:.75rem;color:#6b7280;white-space:nowrap;margin-left:.5rem}
.tabs{display:flex;gap:.4rem;margin-bottom:.5rem}
.tab-btn{flex:1;padding:.6rem;border:1.5px solid #d1d5db;border-radius:8px;
  background:#f9fafb;color:#6b7280;font-size:.85rem;font-weight:600;cursor:pointer;
  transition:all .2s}
.tab-btn:hover{border-color:#1a56db}
.tab-btn.active{background:#1a56db;color:#fff;border-color:#1a56db}
.tab-panel{display:none}
.tab-panel.active{display:block}
</style>
</head>
<body>
<div class="card">
  <div class="logo">
    <h1>[[wizard_emoji]] Device Configuration</h1>
    <div class="badge">Registered</div>
  </div>
  <div class="tabs">
    <button type="button" class="tab-btn active" id="tab-btn-reg" onclick="showTab('reg')">Registration</button>
    <button type="button" class="tab-btn" id="tab-btn-net" onclick="showTab('net')">Network</button>
  </div>
  <div id="notice" class="notice" style="display:none"></div>

  <div id="tab-reg" class="tab-panel active">
    <div class="sect">Device Identity</div>
    <label for="site">Site Name</label>
    <input type="text" id="site" value="[[site]]">
    <label for="asset">Asset Name</label>
    <input type="text" id="asset" value="[[asset]]">

    <div class="sect">Environment</div>
    <div class="tog">
      <input type="radio" id="ev_in"  name="environment" value="indoor"  [[indoor_checked]]>
      <label for="ev_in">Indoor</label>
      <input type="radio" id="ev_out" name="environment" value="outdoor" [[outdoor_checked]]>
      <label for="ev_out">Outdoor</label>
    </div>

    <button type="button" class="btn btn-blue" onclick="doUpdate()">Save Changes</button>

    <hr class="divider">
    <button type="button" class="btn btn-danger"
            onclick="if(confirm('Reboot the device now?'))doReboot()">↻ Reboot Device</button>
  </div>

  <div id="tab-net" class="tab-panel">
    <div class="sect">Add Network</div>
    <button type="button" id="scan-btn" class="btn-scan" onclick="doScan()">🔍 Scan for Networks</button>
    <div id="scan-list" class="scan-list" onclick="handleScanClick(event)"></div>
    <label for="new-ssid">Network Name (SSID)</label>
    <input type="text" id="new-ssid" placeholder="Network name (or scan above)">
    <label for="new-password">Password</label>
    <div class="pw">
      <input type="password" id="new-password" placeholder="Leave blank for open networks">
      <button type="button" onclick="tpw()">Show</button>
    </div>
    <button type="button" class="btn btn-blue" onclick="doConnect()">Connect &amp; Save →</button>
    <form id="cf" method="POST" action="/management/connect" style="display:none">
      <input type="hidden" name="session"  value="[[session_token]]">
      <input type="hidden" name="ssid"     id="f-ssid">
      <input type="hidden" name="password" id="f-password">
    </form>

    <hr class="divider">
    [[saved_networks_html]]
  </div>
</div>
<script>
const SESSION="[[session_token]]";
function authHdr(){return{'Content-Type':'application/json','X-Session':SESSION};}
function showTab(name){
  for(const t of ['reg','net']){
    document.getElementById('tab-'+t).classList.toggle('active', t===name);
    document.getElementById('tab-btn-'+t).classList.toggle('active', t===name);
  }
  try{sessionStorage.setItem('sa_tab',name);}catch{}
}
try{
  const saved=sessionStorage.getItem('sa_tab');
  if(saved==='net')showTab('net');
}catch{}
function showNotice(msg,type){
  const el=document.getElementById('notice');
  el.className='notice notice-'+type;el.textContent=msg;el.style.display='';
  if(type==='ok')setTimeout(()=>{el.style.display='none';},5000);
}
function tpw(){
  const f=document.getElementById('new-password'),b=f.nextElementSibling;
  if(f.type==='password'){f.type='text';b.textContent='Hide';}
  else{f.type='password';b.textContent='Show';}
}
function signalBars(s){
  if(s>=75)return'||||';if(s>=50)return'||| ';if(s>=25)return'||  ';return'|   ';
}
function handleScanClick(e){
  const item=e.target.closest('.scan-item');if(!item)return;
  document.getElementById('new-ssid').value=item.dataset.ssid;
  if(item.dataset.secured==='1')document.getElementById('new-password').focus();
  else document.getElementById('new-ssid').focus();
}
async function doScan(){
  const btn=document.getElementById('scan-btn');
  const list=document.getElementById('scan-list');
  btn.disabled=true;btn.textContent='Scanning…';list.innerHTML='';
  try{
    const r=await fetch('/wifi/scan',{method:'POST',headers:authHdr(),body:'{}'});
    const d=await r.json();
    if(d.error){showNotice(d.error,'err');return;}
    const nets=d.networks||[];
    if(!nets.length){list.innerHTML='<p class="empty-msg">No networks found.</p>';return;}
    list.innerHTML=nets.map(n=>{
      const safe=n.ssid.replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
      const disp=n.ssid.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
      return`<div class="scan-item" data-ssid="${safe}" data-secured="${n.secured?'1':'0'}">
        <span class="scan-ssid">${disp}</span>
        <span class="scan-meta">${signalBars(n.signal)} ${n.secured?'🔒':'🔓'}</span>
      </div>`;
    }).join('');
  }catch{showNotice('Scan failed — try again.','err');}
  btn.disabled=false;btn.textContent='🔍 Scan for Networks';
}
function doConnect(){
  const ssid=document.getElementById('new-ssid').value.trim();
  if(!ssid){showNotice('Network name (SSID) is required.','err');return;}
  if(!confirm('This device will switch to "'+ssid+'". If it can\\'t reach that '+
      'network, the netwatch service will fall back to the setup hotspot after '+
      'a few minutes. Continue?')) return;
  document.getElementById('f-ssid').value=ssid;
  document.getElementById('f-password').value=document.getElementById('new-password').value;
  document.getElementById('cf').submit();
}
async function doUpdate(){
  const site=document.getElementById('site').value.trim();
  const asset=document.getElementById('asset').value.trim();
  if(!site||!asset){showNotice('Site and Asset are required.','err');return;}
  const env=document.querySelector('input[name="environment"]:checked')?.value||'indoor';
  try{
    const d=await(await fetch('/management/update',{
      method:'POST',headers:authHdr(),
      body:JSON.stringify({site,asset_name:asset,environment:env,session:SESSION}),
    })).json();
    if(d.ok)showNotice('Saved! Service restarting…','ok');
    else showNotice(d.error||'Failed.','err');
  }catch{showNotice('Request failed.','err');}
}
async function doReboot(){
  await fetch('/wifi/reboot',{method:'POST',headers:authHdr()});
  showNotice('Rebooting…','ok');
}
let forgetConfirmed=false;
function doForget(profile,isActive){
  const ssid=profile.replace(/^schoolair-/,'').replace(/_/g,' ');
  if(isActive){
    if(prompt('⚠️ This is the ACTIVE network.\\nForgetting it will disconnect the device.\\n\\nType DELETE to confirm:')!=='DELETE')return;
  }else if(!forgetConfirmed){
    if(prompt('Type DELETE to forget "'+ssid+'":')!=='DELETE')return;
    forgetConfirmed=true;
  }
  fetch('/wifi/forget',{method:'POST',headers:authHdr(),body:JSON.stringify({profile})})
    .then(r=>r.json()).then(d=>{if(d.ok)location.reload();else showNotice(d.error||'Failed.','err');})
    .catch(()=>showNotice('Request failed.','err'));
}
function doPrioritize(profile){
  fetch('/wifi/prioritize',{method:'POST',headers:authHdr(),body:JSON.stringify({profile})})
    .then(r=>r.json()).then(d=>{if(d.ok)location.reload();else showNotice(d.error||'Failed.','err');})
    .catch(()=>showNotice('Request failed.','err'));
}
</script>
</body>
</html>"""


# ── Persistence helpers ───────────────────────────────────────────────────────

try:
    _pw = pwd.getpwnam("admin")
    _ADMIN_UID, _ADMIN_GID = _pw.pw_uid, _pw.pw_gid
except KeyError:
    _ADMIN_UID, _ADMIN_GID = -1, -1


def _fix_owner(path: str) -> None:
    if _ADMIN_UID >= 0:
        try:
            os.chown(path, _ADMIN_UID, _ADMIN_GID)
        except OSError:
            pass


def _has_token() -> bool:
    """Return True if AUTH_TOKEN is present and non-empty in the telemetry .env."""
    try:
        for line in open(PI_MAIN_ENV_PATH).read().splitlines():
            if line.startswith("AUTH_TOKEN="):
                return bool(line[len("AUTH_TOKEN="):].strip())
    except OSError:
        pass
    return False


def _write_env_key(key: str, value: str) -> None:
    content = ""
    if os.path.exists(PI_MAIN_ENV_PATH):
        with open(PI_MAIN_ENV_PATH) as f:
            content = f.read()
    pattern = rf"^{re.escape(key)}=.*$"
    if re.search(pattern, content, re.MULTILINE):
        content = re.sub(pattern, f"{key}={value}", content, flags=re.MULTILINE)
    else:
        content += f"\n{key}={value}\n"
    with open(PI_MAIN_ENV_PATH, "w") as f:
        f.write(content)
    _fix_owner(PI_MAIN_ENV_PATH)


def _write_auth_token(token: str) -> None:
    _write_env_key("AUTH_TOKEN", token)


def _write_new_auth_token(token: str) -> None:
    """Write NEW_AUTH_TOKEN and NEW_SERVER_URL into pi-main's .env."""
    _write_env_key("NEW_AUTH_TOKEN", token)
    _write_env_key("NEW_SERVER_URL", NEW_SERVER_BASE_URL)


def _ensure_dir() -> None:
    created = not os.path.exists(CONFIG_DIR)
    os.makedirs(CONFIG_DIR, exist_ok=True)
    if created:
        _fix_owner(CONFIG_DIR)


def write_staging(data: dict) -> None:
    _ensure_dir()
    tmp = STAGING_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, STAGING_FILE)
    _fix_owner(STAGING_FILE)


def read_staging() -> dict:
    try:
        with open(STAGING_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def write_status(data: dict) -> None:
    _ensure_dir()
    tmp = STATUS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, STATUS_FILE)
    _fix_owner(STATUS_FILE)


def write_error(message: str) -> None:
    _ensure_dir()
    with open(ERROR_FILE, "w") as f:
        f.write(f"{datetime.now(timezone.utc).isoformat()}  {message}\n")
    _fix_owner(ERROR_FILE)


def read_wizard_registration() -> dict:
    try:
        with open(STATUS_FILE) as f:
            data = json.load(f)
        if isinstance(data, dict) and data.get("token"):
            return data
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return {}


def read_node_red_registration() -> dict:
    try:
        with open(NODE_RED_TOKEN_FILE) as f:
            data = json.load(f)
        if isinstance(data, dict) and data.get("token"):
            return data
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return {}


# ── Network helpers ───────────────────────────────────────────────────────────

def _profile_name(ssid: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9._-]", "_", ssid)
    return f"{SAVED_PREFIX}{safe}"


async def _cmd(cmd: str) -> tuple:
    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    return proc.returncode, out.decode().strip(), err.decode().strip()


def _display_ssid(name: str) -> str:
    """Fallback human-readable name when the real SSID can't be read.

    Profiles the wizard created itself (SAVED_PREFIX) have their sanitized
    SSID embedded in the name, so that's recoverable even without asking
    NetworkManager. For anything else this just returns the raw connection
    name — callers should prefer _profile_ssid() when a profile name is
    available, since that reads the actual SSID instead of guessing.
    """
    return name[len(SAVED_PREFIX):].replace("_", " ") if name.startswith(SAVED_PREFIX) else name


async def _profile_ssid(name: str) -> str:
    """The real SSID of a connection profile, read from NetworkManager.

    Falls back to _display_ssid() if the property is empty (e.g. profile
    vanished between listing and lookup) — not for regular wifi profiles,
    which always have this set.
    """
    _, out, _ = await _cmd(f'nmcli -t -f 802-11-wireless.ssid con show "{name}"')
    ssid = out.split(":", 1)[-1].strip() if ":" in out else ""
    return ssid or _display_ssid(name)


async def _current_wifi() -> tuple:
    _, out, _ = await _cmd("nmcli -t -f NAME,TYPE,STATE con show --active")
    for line in out.splitlines():
        parts = line.split(":")
        if len(parts) < 3:
            continue
        name, ctype, state = parts[0], parts[1], parts[2]
        if ctype in ("wifi", "802-11-wireless") and state == "activated" \
                and name != AP_CONNECTION_NAME:
            return name, await _profile_ssid(name)
    return "", ""


async def _list_saved_profiles() -> list:
    """All known WiFi client profiles — not just ones the wizard added.

    This includes networks configured outside the wizard (netplan, nmtui,
    imaging-time setup) so the management page shows the full picture of
    what the device can connect to, not only what it added itself.
    """
    _, out, _ = await _cmd("nmcli -t -f NAME,TYPE con show")
    names = []
    for line in out.splitlines():
        parts = line.split(":")
        if len(parts) < 2:
            continue
        name, ctype = parts[0], parts[1]
        if ctype not in ("wifi", "802-11-wireless"):
            continue
        if name in (AP_CONNECTION_NAME, TEMP_PROFILE):
            continue
        names.append(name)
    profiles = []
    for name in names:
        _, pout, _ = await _cmd(
            f'nmcli -t -f connection.autoconnect-priority con show "{name}"')
        try:
            priority = int(pout.split(":")[-1].strip())
        except (ValueError, IndexError):
            priority = 0
        profiles.append({
            "name":     name,
            "priority": priority,
            "ssid":     await _profile_ssid(name),
        })
    profiles.sort(key=lambda x: -x["priority"])
    return profiles


def _saved_networks_html(profiles: list, current_profile: str) -> str:
    if not profiles:
        return '<div class="sect">Saved Networks</div><p class="empty-msg">No saved networks.</p>'
    rows = []
    for p in profiles:
        profile   = p["name"]
        priority  = p["priority"]
        ssid      = p["ssid"]
        is_active_js = "true" if profile == current_profile else "false"
        active_tag   = '<span class="net-active">● active</span>' if profile == current_profile else ""
        rows.append(
            f'<div class="network-row">'
            f'<span class="net-name">{_html.escape(ssid)}{active_tag}</span>'
            f'<div class="net-controls">'
            f'<button type="button" class="prio-btn" title="Prioritize"'
            f" onclick=\"doPrioritize('{profile}')\">⬆️</button>"
            f'<span class="net-priority">P{priority}</span>'
            f'<button type="button" class="forget-btn" title="Forget"'
            f" onclick=\"doForget('{profile}',{is_active_js})\">🗑️</button>"
            f'</div></div>'
        )
    return '<div class="sect">Saved Networks</div>' + "".join(rows)


async def _scan_networks() -> list:
    await _cmd(f"nmcli dev wifi rescan ifname {AP_INTERFACE} 2>/dev/null; true")
    await asyncio.sleep(3)
    _, out, _ = await _cmd(
        f"nmcli -t -f SSID,SIGNAL,SECURITY dev wifi list ifname {AP_INTERFACE}"
    )
    seen: dict = {}
    for line in out.splitlines():
        parts = line.split(":")
        ssid = parts[0].strip()
        if not ssid:
            continue
        try:
            signal = int(parts[1]) if len(parts) > 1 else 0
        except ValueError:
            signal = 0
        security = parts[2].strip() if len(parts) > 2 else ""
        secured = bool(security and security not in ("--", "none", "None", ""))
        if ssid not in seen or signal > seen[ssid]["signal"]:
            seen[ssid] = {"ssid": ssid, "signal": signal, "secured": secured}
    return sorted(seen.values(), key=lambda x: -x["signal"])


async def _setup_client_profile(ssid: str, password: str, delete_committed: bool = True) -> tuple:
    """Create a temp connection profile for `ssid`.

    delete_committed=False leaves any existing saved profile for this SSID
    in place (used by run_management_connect, which needs a working fallback
    to restore if the new connection doesn't pan out — see that function).
    """
    committed = _profile_name(ssid)
    await _cmd(f'nmcli con delete "{TEMP_PROFILE}" 2>/dev/null; true')
    if delete_committed:
        await _cmd(f'nmcli con delete "{committed}" 2>/dev/null; true')
    if password:
        rc, _, err = await _cmd(
            f'nmcli con add type wifi ifname {AP_INTERFACE} '
            f'con-name "{TEMP_PROFILE}" ssid "{ssid}" '
            f'wifi-sec.key-mgmt wpa-psk wifi-sec.psk "{password}" '
            f'ipv4.method auto ipv6.method ignore'
        )
    else:
        rc, _, err = await _cmd(
            f'nmcli con add type wifi ifname {AP_INTERFACE} '
            f'con-name "{TEMP_PROFILE}" ssid "{ssid}" '
            f'ipv4.method auto ipv6.method ignore'
        )
    if rc != 0:
        return False, f"Could not create connection profile: {err}"
    return True, "ok"


async def _wait_for_ip(timeout: int = 30) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rc, out, _ = await _cmd(f"nmcli -t -f IP4.ADDRESS dev show {AP_INTERFACE}")
        if rc == 0 and out.strip():
            return True
        await asyncio.sleep(2)
    return False


async def _revert_to_ap() -> None:
    await _cmd(f'nmcli con delete "{TEMP_PROFILE}" 2>/dev/null; true')
    rc, _, _ = await _cmd(f'nmcli con up "{AP_CONNECTION_NAME}" 2>/dev/null')
    if rc != 0:
        await _cmd("systemctl restart hostapd 2>/dev/null; true")


def _get_cpu_serial() -> str:
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("Serial"):
                    return line.split(":")[1].strip()
    except OSError:
        pass
    return "unknown"


def _get_mac_address() -> str:
    import pathlib
    for iface in ("eth0", "wlan0"):
        try:
            return pathlib.Path(f"/sys/class/net/{iface}/address").read_text().strip()
        except OSError:
            pass
    for p in pathlib.Path("/sys/class/net").iterdir():
        if p.name == "lo":
            continue
        try:
            return (p / "address").read_text().strip()
        except OSError:
            pass
    return "unknown"


# ── Cloud calls ───────────────────────────────────────────────────────────────

async def _validate_token(token: str) -> tuple[bool, str]:
    """Validate token with the server's step-1 endpoint.

    Returns (ok, error_message). Falls through on 404 (server not yet updated)
    so the existing /register endpoint can catch invalid tokens at step 2.
    """
    body = json.dumps({
        "mac_address": _get_mac_address(),
        "cpu_serial":  _get_cpu_serial(),
    }).encode()

    def _do():
        req = urllib.request.Request(
            VALIDATE_URL, data=body,
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=HEARTBEAT_TIMEOUT) as r:
            return r.status, r.read().decode()

    loop = asyncio.get_running_loop()
    try:
        code, _ = await loop.run_in_executor(None, _do)
        return (code == 200), ("" if code == 200 else f"Server returned HTTP {code}")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return True, ""  # endpoint not yet deployed — defer to step 2
        if exc.code in (401, 403):
            return False, "Token rejected by SchoolAir Cloud"
        return False, f"Server error HTTP {exc.code}"
    except urllib.error.URLError as exc:
        return False, f"Could not reach SchoolAir Cloud: {exc.reason}"
    except Exception as exc:
        return False, f"Token validation failed: {exc}"


async def _post_legacy_registration(org_token: str, nickname: str) -> tuple:
    body = json.dumps({"cpu_serial": _get_cpu_serial(), "nickname": nickname}).encode()

    def _do():
        req = urllib.request.Request(
            LEGACY_URL, data=body,
            headers={"Authorization": f"Bearer {org_token}",
                     "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=HEARTBEAT_TIMEOUT) as r:
            return r.status, r.read().decode()

    loop = asyncio.get_running_loop()
    try:
        code, body_str = await loop.run_in_executor(None, _do)
        if code == 200:
            try:
                resp = json.loads(body_str)
            except (json.JSONDecodeError, ValueError):
                resp = {}
            return True, "Registered successfully", resp
        return False, f"Server returned HTTP {code}", {}
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return False, "Token rejected by SchoolAir Cloud", {}
        return False, f"Server error HTTP {exc.code}", {}
    except urllib.error.URLError as exc:
        return False, f"Could not reach SchoolAir Cloud: {exc.reason}", {}
    except Exception as exc:
        return False, f"Legacy registration failed: {exc}", {}


async def _post_heartbeat(payload: dict) -> tuple[bool, str, str]:
    """POST to the primary server's register endpoint.

    Returns (success, message, device_auth_token). Always authenticates via
    Bearer token — auto device-auth re-registration is intentionally removed.
    """
    token = payload.pop("token", "")
    body  = json.dumps(payload).encode()

    def _do():
        req = urllib.request.Request(
            HEARTBEAT_URL, data=body,
            headers={
                "Content-Type":  "application/json",
                "Accept":        "application/json",
                "Authorization": f"Bearer {token}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=HEARTBEAT_TIMEOUT) as r:
            return r.status, r.read().decode()

    loop = asyncio.get_running_loop()
    try:
        code, resp_body = await loop.run_in_executor(None, _do)
        print(f"[heartbeat] HTTP {code}: {resp_body[:200]}")
        if code == 200:
            try:
                device_auth_token = json.loads(resp_body).get("auth_token", "")
            except Exception:
                device_auth_token = ""
            return True, "Registered successfully", device_auth_token
        return False, f"Server returned HTTP {code}", ""
    except urllib.error.HTTPError as exc:
        body_str = ""
        try:
            body_str = exc.read().decode()[:200]
        except Exception:
            pass
        print(f"[heartbeat] HTTP {exc.code}: {body_str}")
        if exc.code in (401, 403):
            return False, "Token rejected by SchoolAir Cloud", ""
        return False, f"Server error HTTP {exc.code}", ""
    except urllib.error.URLError as exc:
        return False, f"Could not reach SchoolAir Cloud: {exc.reason}", ""
    except Exception as exc:
        return False, f"Heartbeat failed: {exc}", ""


# ── State helper ──────────────────────────────────────────────────────────────

def _set(state: str, message: str, redirect: str = "") -> None:
    reg_state["state"]    = state
    reg_state["message"]  = message
    reg_state["redirect"] = redirect


# ── Network helpers (AP detection) ────────────────────────────────────────────

async def _ap_is_active() -> bool:
    _, out, _ = await _cmd("nmcli -t -f NAME,STATE con show --active")
    return AP_CONNECTION_NAME in out


# ── Registration background tasks ─────────────────────────────────────────────

async def run_step1(sess_tok: str) -> None:
    """Connect to WiFi and validate the registration token. Always reverts to AP."""
    global _connection_in_progress
    sess = wifi_sessions.get(sess_tok)
    if not sess:
        _set("error", "Session expired.")
        _connection_in_progress = False
        return

    ssid     = sess["ssid"]
    password = sess["password"]
    token    = sess["token"]

    _set("connecting", f'Adding profile for "{ssid}"…')
    ok, msg = await _setup_client_profile(ssid, password)
    if not ok:
        _set("error", msg)
        wifi_sessions.pop(sess_tok, None)
        _connection_in_progress = False
        await _revert_to_ap()
        return

    _set("connecting", f'Connecting to "{ssid}"…')
    rc, _, err = await _cmd(f'nmcli con up "{TEMP_PROFILE}"')
    if rc != 0:
        detail = err or "Check SSID and password."
        _set("error", f'Could not connect to "{ssid}": {detail}')
        wifi_sessions.pop(sess_tok, None)
        _connection_in_progress = False
        await _revert_to_ap()
        return

    _set("wifi_up", f'Joined "{ssid}". Waiting for IP address…')
    if not await _wait_for_ip(timeout=30):
        _set("error", f'Joined "{ssid}" but did not receive an IP within 30 s.')
        wifi_sessions.pop(sess_tok, None)
        _connection_in_progress = False
        await _revert_to_ap()
        return

    _set("validating", "Validating registration token…")
    valid, vmsg = await _validate_token(token)

    # Always revert to AP after step 1, regardless of result.
    await _cmd(f'nmcli con delete "{TEMP_PROFILE}" 2>/dev/null; true')
    await _revert_to_ap()
    _connection_in_progress = False

    if valid:
        sess["step"] = 1
        _set("step1_success",
             f'"{ssid}" and token verified. Opening device details form…',
             redirect=f"/step2?s={sess_tok}")
    else:
        wifi_sessions.pop(sess_tok, None)
        _set("error", _friendly_error(vmsg))


async def run_step2(sess_tok: str) -> None:
    """Reconnect to WiFi and complete full registration. Commits WiFi on success."""
    global _connection_in_progress
    sess = wifi_sessions.get(sess_tok)
    if not sess:
        _set("error", "Session expired.")
        _connection_in_progress = False
        return

    ssid        = sess["ssid"]
    password    = sess["password"]
    token       = sess["token"]
    site        = sess["site"]
    asset       = sess["asset"]
    environment = sess["environment"]
    migrate     = sess["migrate"]

    _set("connecting", f'Reconnecting to "{ssid}"…')
    ok, msg = await _setup_client_profile(ssid, password)
    if not ok:
        _set("error", msg)
        wifi_sessions.pop(sess_tok, None)
        _connection_in_progress = False
        await _revert_to_ap()
        return

    rc, _, err = await _cmd(f'nmcli con up "{TEMP_PROFILE}"')
    if rc != 0:
        detail = err or "Check SSID and password."
        _set("error", f'Could not reconnect to "{ssid}": {detail}')
        wifi_sessions.pop(sess_tok, None)
        _connection_in_progress = False
        await _revert_to_ap()
        return

    _set("wifi_up", f'Joined "{ssid}". Waiting for IP…')
    if not await _wait_for_ip(timeout=30):
        _set("error", f'Got IP timeout on "{ssid}".')
        wifi_sessions.pop(sess_tok, None)
        _connection_in_progress = False
        await _revert_to_ap()
        return

    success = True
    hb_msg  = ""
    device_auth_token = ""
    legacy_resp: dict = {}

    if site == "LEGACY":
        _set("heartbeat", "Registering with legacy server…")
        success, hb_msg, legacy_resp = await _post_legacy_registration(token, asset)
    else:
        _set("heartbeat", "Completing registration with SchoolAir Cloud…")
        payload = {
            "token":       token,
            "mac_address": _get_mac_address(),
            "cpu_serial":  _get_cpu_serial(),
            "nickname":    asset,
            "migrate":     migrate,
            "new_asset":   {"nickname": asset, "type": environment,
                            "site_name": site or None},
        }
        success, hb_msg, device_auth_token = await _post_heartbeat(payload)

    if not success:
        await _cmd(f'nmcli con delete "{TEMP_PROFILE}" 2>/dev/null; true')
        wifi_sessions.pop(sess_tok, None)
        _connection_in_progress = False
        _set("error", f"Connected, but registration failed: {_friendly_error(hb_msg)}")
        write_error(hb_msg)
        await _revert_to_ap()
        return

    # Commit WiFi profile permanently with incremental priority.
    committed = _profile_name(ssid)
    await _cmd(f'nmcli con modify "{TEMP_PROFILE}" connection.id "{committed}"')
    all_profiles = await _list_saved_profiles()
    other_max = max((p["priority"] for p in all_profiles if p["name"] != committed), default=0)
    await _cmd(f'nmcli con modify "{committed}" connection.autoconnect-priority {other_max + 1}')

    if site != "LEGACY":
        write_status({
            "token":         token,
            "site":          site,
            "asset_name":    asset,
            "environment":   environment,
            "ssid":          ssid,
            "registered_at": datetime.now(timezone.utc).isoformat(),
        })
        try:
            _write_auth_token(token)
        except Exception as e:
            print(f"[wizard] Warning: could not write AUTH_TOKEN: {e}")
        if device_auth_token:
            try:
                _write_new_auth_token(device_auth_token)
            except Exception as e:
                print(f"[wizard] Warning: could not write NEW_AUTH_TOKEN: {e}")
    else:
        device_token_value = legacy_resp.get("token", token)
        device_token = {
            "token":     device_token_value,
            "device_id": legacy_resp.get("device_id", ""),
            "nickname":  asset,
        }
        with open(NODE_RED_TOKEN_FILE, "w") as _f:
            json.dump(device_token, _f)
        _fix_owner(NODE_RED_TOKEN_FILE)
        try:
            _write_auth_token(device_token_value)
        except Exception as e:
            print(f"[wizard] Warning: could not write AUTH_TOKEN: {e}")

    try:
        os.remove(STAGING_FILE)
    except FileNotFoundError:
        pass

    wifi_sessions.pop(sess_tok, None)
    _connection_in_progress = False
    _set("success", "Registration complete! This hotspot will shut down shortly.")
    asyncio.create_task(_delayed_shutdown())


async def run_management_update(sess_tok: str) -> None:
    """Re-register device with updated identity (device is already on WiFi)."""
    sess = wifi_sessions.get(sess_tok)
    if not sess:
        return

    token       = sess["token"]
    site        = sess["site"]
    asset       = sess["asset"]
    environment = sess["environment"]

    _set("heartbeat", "Updating device registration…")
    payload = {
        "token":       token,
        "mac_address": _get_mac_address(),
        "cpu_serial":  _get_cpu_serial(),
        "nickname":    asset,
        "migrate":     False,
        "new_asset":   {"nickname": asset, "type": environment, "site_name": site or None},
    }
    success, hb_msg, device_auth_token = await _post_heartbeat(payload)

    if not success:
        _set("error", _friendly_error(hb_msg))
        write_error(hb_msg)
        return

    existing = read_wizard_registration()
    write_status({
        "token":         token,
        "site":          site,
        "asset_name":    asset,
        "environment":   environment,
        "ssid":          existing.get("ssid", ""),
        "registered_at": datetime.now(timezone.utc).isoformat(),
    })
    try:
        _write_auth_token(token)
    except Exception as e:
        print(f"[wizard] Warning: could not write AUTH_TOKEN: {e}")
    if device_auth_token:
        try:
            _write_new_auth_token(device_auth_token)
        except Exception as e:
            print(f"[wizard] Warning: could not write NEW_AUTH_TOKEN: {e}")

    wifi_sessions.pop(sess_tok, None)
    _set("success", "Device updated successfully.")
    asyncio.create_task(_delayed_management_shutdown())


async def run_management_connect(ssid: str, password: str) -> None:
    """Add + connect a new WiFi network from management mode.

    The device is already on WiFi when this runs, so unlike run_step1/run_step2
    there is no AP to fall back to on failure — instead we try to bring the
    previously-active saved profile back up. netwatch.sh is the final safety
    net: if the device ends up with no working uplink at all, it reverts to
    the setup hotspot on its own after its grace period.
    """
    global _connection_in_progress
    prev_profile, _ = await _current_wifi()

    _set("connecting", f'Adding profile for "{ssid}"…')
    # delete_committed=False: if ssid is the network we're already on (e.g. a
    # resubmitted form, or refreshing a saved password), don't delete its
    # profile until the replacement is confirmed working — otherwise the
    # fallback-to-prev_profile below would have nothing left to restore.
    ok, msg = await _setup_client_profile(ssid, password, delete_committed=False)
    if not ok:
        _set("error", msg)
        _connection_in_progress = False
        return

    _set("connecting", f'Connecting to "{ssid}"…')
    rc, _, err = await _cmd(f'nmcli con up "{TEMP_PROFILE}"')
    if rc != 0:
        detail = err or "Check SSID and password."
        _set("error", f'Could not connect to "{ssid}": {detail}')
        await _cmd(f'nmcli con delete "{TEMP_PROFILE}" 2>/dev/null; true')
        if prev_profile:
            await _cmd(f'nmcli con up "{prev_profile}" 2>/dev/null; true')
        _connection_in_progress = False
        return

    _set("wifi_up", f'Joined "{ssid}". Waiting for IP address…')
    if not await _wait_for_ip(timeout=30):
        _set("error", f'Joined "{ssid}" but did not receive an IP within 30 s.')
        await _cmd(f'nmcli con delete "{TEMP_PROFILE}" 2>/dev/null; true')
        if prev_profile:
            await _cmd(f'nmcli con up "{prev_profile}" 2>/dev/null; true')
        _connection_in_progress = False
        return

    # New connection is confirmed working — now it's safe to drop any old
    # profile of the same name and commit the temp one permanently in its place.
    committed = _profile_name(ssid)
    await _cmd(f'nmcli con delete "{committed}" 2>/dev/null; true')
    await _cmd(f'nmcli con modify "{TEMP_PROFILE}" connection.id "{committed}"')
    all_profiles = await _list_saved_profiles()
    other_max = max((p["priority"] for p in all_profiles if p["name"] != committed), default=0)
    await _cmd(f'nmcli con modify "{committed}" connection.autoconnect-priority {other_max + 1}')

    _connection_in_progress = False
    _set("success", f'Connected to "{ssid}" and saved.')


async def _delayed_shutdown() -> None:
    """After successful setup registration: tear down AP, start ingest, stop wizard."""
    await asyncio.sleep(6)
    await _cmd("iptables -t nat -D PREROUTING -i wlan0 -p tcp --dport 80  -j REDIRECT --to-port 80  2>/dev/null; true")
    await _cmd("iptables -t nat -D PREROUTING -i wlan0 -p tcp --dport 443 -j REDIRECT --to-port 443 2>/dev/null; true")
    await _cmd(f'nmcli con down "{AP_CONNECTION_NAME}" 2>/dev/null; true')
    await _cmd("systemctl stop hostapd 2>/dev/null; true")
    await _cmd("systemctl disable hostapd 2>/dev/null; true")
    await _cmd("systemctl restart schoolair 2>/dev/null; true")
    await _cmd("systemctl stop schoolair-wizard 2>/dev/null; true")


async def _delayed_management_shutdown() -> None:
    """After a management update: restart ingest and hand port 80 back to nginx."""
    await asyncio.sleep(6)
    await _cmd("systemctl restart schoolair 2>/dev/null; true")
    await _cmd("systemctl stop schoolair-wizard 2>/dev/null; true")


async def _session_pruner() -> None:
    """Background task: remove expired sessions every 5 minutes."""
    while True:
        await asyncio.sleep(300)
        _prune_sessions()


async def _idle_watchdog() -> None:
    """Auto-shutdown in management (WiFi) mode after IDLE_TIMEOUT of inactivity.

    In AP (setup) mode the wizard runs until the user completes registration.
    In management mode it shuts down if nobody is using it.
    """
    await asyncio.sleep(5)
    if await _ap_is_active():
        return  # setup mode — wizard shuts down after successful registration
    if not _has_token():
        return  # not yet registered — don't idle-timeout during initial registration
    while True:
        await asyncio.sleep(60)
        if time.time() - _last_activity > IDLE_TIMEOUT:
            print(f"[schoolair-wizard] No activity for {IDLE_TIMEOUT}s — shutting down.")
            await _cmd("systemctl stop schoolair-wizard 2>/dev/null; true")
            break


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/", methods=["GET"])
async def index(request):
    host = (request.headers.get("Host") or "").split(":")[0].lower().strip()

    # Device landing page (shown when accessed via schoolair.local)
    if host.startswith("schoolair") and host not in ("schoolair-register.local",):
        wiz      = read_wizard_registration()
        hostname = os.uname().nodename
        if wiz:
            badge     = '<span class="badge badge-ok">Registered</span>'
            site_row  = (f'<div class="row"><span class="lbl">Site</span>'
                         f'<span class="val">{_html.escape(wiz.get("site",""))}</span></div>')
            asset_row = (f'<div class="row"><span class="lbl">Asset</span>'
                         f'<span class="val">{_html.escape(wiz.get("asset_name",""))}</span></div>')
        else:
            badge     = '<span class="badge badge-warn">Not registered</span>'
            site_row  = ""
            asset_row = ""
        body = (LANDING_HTML
                .replace("[[hostname]]", _html.escape(hostname))
                .replace("[[reg_badge]]", badge)
                .replace("[[site_row]]",  site_row)
                .replace("[[asset_row]]", asset_row))
        return _html_response(body)

    quote, author = _GANDALF_QUOTE

    if await _ap_is_active():
        # Setup mode: show Step 1 form.
        prefill = read_staging()
        body = _render(STEP1_HTML, raw={
            "wizard_emoji":   _wizard_emoji("m"),
            "quote":          quote,
            "quote_author":   author,
            "prefill_token":  "",
            "prefill_ssid":   prefill.get("ssid", ""),
        })
        return _html_response(body)

    # Management mode: device is on WiFi — show token-auth form.
    wiz      = read_wizard_registration()
    hostname = os.uname().nodename
    if wiz:
        badge     = '<span class="badge badge-ok">Registered</span>'
        site_row  = (f'<div class="row"><span class="lbl">Site</span>'
                     f'<span class="val">{_html.escape(wiz.get("site",""))}</span></div>')
        asset_row = (f'<div class="row"><span class="lbl">Asset</span>'
                     f'<span class="val">{_html.escape(wiz.get("asset_name",""))}</span></div>')
    else:
        badge = '<span class="badge badge-warn">Not registered</span>'
        site_row = asset_row = ""

    body = _render(MANAGEMENT_AUTH_HTML, raw={
        "reg_badge":  badge,
        "site_row":   site_row,
        "asset_row":  asset_row,
    }, hostname=hostname)
    return _html_response(body)


@app.route("/step1/connect", methods=["POST"])
async def step1_connect(request):
    global _connection_in_progress
    f        = request.form or {}
    token    = (f.get("token")    or "").strip()
    ssid     = (f.get("ssid")     or "").strip()
    password = (f.get("password") or "").strip()

    quote, author = _GANDALF_QUOTE

    def _err_page(msg: str) -> Response:
        body = _render(STEP1_HTML, raw={
            "wizard_emoji":   _wizard_emoji("m"),
            "quote":          quote,
            "quote_author":   author,
            "prefill_token":  _html.escape(token),
            "prefill_ssid":   _html.escape(ssid),
        })
        # Inject the error notice inline via a minimal JS snippet
        inject = (f'<script>document.addEventListener("DOMContentLoaded",()=>'
                  f'{{const n=document.getElementById("notice");'
                  f'n.textContent={json.dumps(msg)};n.style.display="";}})'
                  f'</script></body>')
        return _html_response(body.replace("</body>", inject))

    if not token:
        return _err_page("Registration token is required.")
    if not ssid:
        return _err_page("Network name (SSID) is required.")
    if _connection_in_progress:
        return _err_page("A connection attempt is already in progress. Please wait.")

    sess_tok = _new_session("setup", token=token, ssid=ssid, password=password)
    _connection_in_progress = True
    _set("connecting", "Starting connection…")
    asyncio.create_task(run_step1(sess_tok))

    quote2, author2 = _RAINE_QUOTE
    return _html_response(_render(CONNECTING_HTML, raw={
        "retry_url":    "/",
        "step":         "1",
        "wizard_emoji": _wizard_emoji("n"),
        "quote":        quote2,
        "quote_author": author2,
    }))


@app.route("/step2", methods=["GET"])
async def step2_page(request):
    sess_tok, sess = _resolve_session(request)
    if not sess or sess.get("step", 0) < 1 or sess.get("mode") != "setup":
        return Response("", status_code=302, headers={"Location": "/"})

    wiz = read_wizard_registration()
    init_site    = wiz.get("site", "")
    init_asset   = wiz.get("asset_name", "")
    site_locked  = bool(init_site)
    asset_locked = bool(init_asset)
    env          = sess.get("environment", "indoor")
    init_json    = json.dumps({
        "site":        init_site,
        "asset":       init_asset,
        "siteLocked":  site_locked,
        "assetLocked": asset_locked,
    })
    quote, author = _GLINDA_QUOTE
    body = _render(STEP2_HTML, raw={
        "init_json":       init_json,
        "session_token":   sess_tok,
        "indoor_checked":  "checked" if env == "indoor"  else "",
        "outdoor_checked": "checked" if env == "outdoor" else "",
        "wizard_emoji":    _wizard_emoji("f"),
        "quote":           quote,
        "quote_author":    author,
    }, ssid=sess.get("ssid", ""))
    return _html_response(body)


@app.route("/step2/register", methods=["POST"])
async def step2_register(request):
    global _connection_in_progress
    sess_tok, sess = _resolve_session(request)
    if not sess or sess.get("step", 0) < 1 or sess.get("mode") != "setup":
        return Response("", status_code=302, headers={"Location": "/"})

    f           = request.form or {}
    site        = (f.get("site")        or "").strip()
    asset       = (f.get("asset_name")  or "").strip()
    environment = (f.get("environment") or "indoor").strip()
    migrate     = bool(f.get("migrate"))

    if not site or not asset:
        return Response("", status_code=302,
                        headers={"Location": f"/step2?s={sess_tok}"})
    if _connection_in_progress:
        return Response("", status_code=302,
                        headers={"Location": f"/step2?s={sess_tok}"})

    sess["site"]        = site
    sess["asset"]       = asset
    sess["environment"] = environment
    sess["migrate"]     = migrate

    _connection_in_progress = True
    write_staging({"ssid": sess["ssid"], "environment": environment})
    _set("connecting", "Preparing to register…")
    asyncio.create_task(run_step2(sess_tok))

    quote, author = _RAINE_QUOTE
    return _html_response(_render(CONNECTING_HTML, raw={
        "retry_url":    f"/step2?s={sess_tok}",
        "step":         "2",
        "wizard_emoji": _wizard_emoji("n"),
        "quote":        quote,
        "quote_author": author,
    }))


@app.route("/management/auth", methods=["POST"])
async def management_auth(request):
    data  = request.json or {}
    token = (data.get("token") or "").strip()
    if not token:
        return _json_response({"error": "Token is required."}, 400)

    valid, vmsg = await _validate_token(token)
    if not valid:
        return _json_response({"error": _friendly_error(vmsg)})

    sess_tok = _new_session("management", token=token)
    wiz = read_wizard_registration()
    sess = wifi_sessions[sess_tok]
    sess["site"]        = wiz.get("site", "")
    sess["asset"]       = wiz.get("asset_name", "")
    sess["environment"] = wiz.get("environment", "indoor")
    sess["step"]        = 1

    return _json_response({"redirect": f"/management?s={sess_tok}"})


@app.route("/management", methods=["GET"])
async def management_page(request):
    sess_tok, sess = _resolve_session(request)
    if not sess or sess.get("mode") != "management" or sess.get("step", 0) < 1:
        return Response("", status_code=302, headers={"Location": "/"})

    profiles = await _list_saved_profiles()
    current_profile, _ = await _current_wifi()

    body = _render(MANAGEMENT_HTML, raw={
        "saved_networks_html": _saved_networks_html(profiles, current_profile),
        "session_token":       sess_tok,
        "wizard_emoji":        _wizard_emoji("m"),
        "indoor_checked":      "checked" if sess.get("environment") == "indoor"  else "",
        "outdoor_checked":     "checked" if sess.get("environment") == "outdoor" else "",
    }, site=sess.get("site", ""), asset=sess.get("asset", ""))
    return _html_response(body)


@app.route("/management/update", methods=["POST"])
async def management_update(request):
    sess_tok, sess = _resolve_session(request)
    if not sess or sess.get("mode") != "management" or sess.get("step", 0) < 1:
        return _json_response({"error": "Not authorized."}, 403)

    data        = request.json or {}
    site        = (data.get("site")        or "").strip()
    asset       = (data.get("asset_name")  or "").strip()
    environment = (data.get("environment") or "indoor").strip()

    if not site or not asset:
        return _json_response({"error": "Site and Asset are required."}, 400)

    sess["site"]        = site
    sess["asset"]       = asset
    sess["environment"] = environment

    asyncio.create_task(run_management_update(sess_tok))
    return _json_response({"ok": True})


@app.route("/management/connect", methods=["POST"])
async def management_connect(request):
    global _connection_in_progress
    sess_tok, sess = _resolve_session(request)
    if not sess or sess.get("mode") != "management" or sess.get("step", 0) < 1:
        return Response("", status_code=302, headers={"Location": "/"})

    f        = request.form or {}
    ssid     = (f.get("ssid")     or "").strip()
    password = (f.get("password") or "").strip()

    back = Response("", status_code=302, headers={"Location": f"/management?s={sess_tok}"})
    if not ssid or _connection_in_progress:
        return back

    _connection_in_progress = True
    _set("connecting", "Starting connection…")
    asyncio.create_task(run_management_connect(ssid, password))

    quote, author = _RAINE_QUOTE
    return _html_response(_render(CONNECTING_HTML, raw={
        "retry_url":    f"/management?s={sess_tok}",
        "step":         "'mgmt'",
        "wizard_emoji": _wizard_emoji("n"),
        "quote":        quote,
        "quote_author": author,
    }))


@app.route("/wifi/scan", methods=["POST"])
async def wifi_scan(request):
    # Allow scan from Step 1 form (no session yet) when the AP is active.
    # Require session otherwise (management mode).
    if not await _ap_is_active():
        _, sess = _resolve_session(request)
        if not sess:
            return _json_response({"error": "Not authorized."}, 403)
    networks = await _scan_networks()
    return _json_response({"networks": networks})


@app.route("/wifi/forget", methods=["POST"])
async def wifi_forget(request):
    _, sess = _resolve_session(request)
    if not sess:
        return _json_response({"error": "Not authorized."}, 403)
    data    = request.json or {}
    profile = data.get("profile", "").strip()
    known   = {p["name"] for p in await _list_saved_profiles()}
    if not profile or profile not in known:
        return _json_response({"error": "Invalid profile name."}, 400)
    await _cmd(f'nmcli con delete "{profile}" 2>/dev/null; true')
    return _json_response({"ok": True})


@app.route("/wifi/forget-all", methods=["POST"])
async def wifi_forget_all(request):
    _, sess = _resolve_session(request)
    if not sess:
        return _json_response({"error": "Not authorized."}, 403)
    profiles = await _list_saved_profiles()
    for p in profiles:
        await _cmd(f'nmcli con delete "{p["name"]}" 2>/dev/null; true')
    return _json_response({"ok": True})


@app.route("/wifi/reboot", methods=["POST"])
async def wifi_reboot(request):
    _, sess = _resolve_session(request)
    if not sess:
        return _json_response({"error": "Not authorized."}, 403)
    asyncio.get_event_loop().call_later(1, lambda: os.system("sudo reboot"))
    return _json_response({"ok": True})


@app.route("/wifi/prioritize", methods=["POST"])
async def wifi_prioritize(request):
    _, sess = _resolve_session(request)
    if not sess:
        return _json_response({"error": "Not authorized."}, 403)
    data      = request.json or {}
    profile   = data.get("profile", "").strip()
    profiles  = await _list_saved_profiles()
    if not profile or profile not in {p["name"] for p in profiles}:
        return _json_response({"error": "Invalid profile."}, 400)
    other_max = max((p["priority"] for p in profiles if p["name"] != profile), default=0)
    new_priority = other_max + 1
    await _cmd(f'nmcli con modify "{profile}" connection.autoconnect-priority {new_priority}')
    return _json_response({"ok": True, "priority": new_priority})


@app.route("/ws/status")
@with_websocket
async def ws_status(request, ws):
    last = {}
    while True:
        current = dict(reg_state)
        if current != last:
            await ws.send(json.dumps(current))
            last = dict(current)
            if current["state"] in ("success", "error", "step1_success"):
                await asyncio.sleep(1)
                break
        await asyncio.sleep(0.4)


@app.route("/status.json", methods=["GET"])
async def status_endpoint(request):
    return _json_response(reg_state)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import ssl as _ssl

    _SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
    _CERT = os.path.join(_SCRIPT_DIR, "cert.pem")
    _KEY  = os.path.join(_SCRIPT_DIR, "key.pem")

    async def _main():
        asyncio.create_task(_idle_watchdog())
        asyncio.create_task(_session_pruner())
        tasks = [app.start_server(host="0.0.0.0", port=SERVER_PORT, debug=False)]
        if os.path.exists(_CERT) and os.path.exists(_KEY):
            _ctx = _ssl.SSLContext(_ssl.PROTOCOL_TLS_SERVER)
            _ctx.load_cert_chain(_CERT, _KEY)
            tasks.append(app.start_server(host="0.0.0.0", port=443, debug=False, ssl=_ctx))
            print("[schoolair-wizard] HTTPS on port 443")
        else:
            print("[schoolair-wizard] cert.pem/key.pem not found — HTTP only")
        print(f"[schoolair-wizard] HTTP on port {SERVER_PORT}")
        await asyncio.gather(*tasks)

    asyncio.run(_main())
