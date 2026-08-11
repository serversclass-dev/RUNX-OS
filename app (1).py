import os, shutil, html, urllib.parse, pty, select, termios, struct, fcntl, signal, threading, json, time, platform, socket, subprocess, mimetypes
from flask import Flask, request, redirect, send_file, Response, jsonify
from werkzeug.middleware.dispatcher import DispatcherMiddleware
from werkzeug.serving import run_simple

app = Flask(__name__)
app.config["APPLICATION_ROOT"] = "/fm"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SECRET_HINTS = ("TOKEN", "KEY", "SECRET", "PASSWORD", "AUTH", "COOKIE")

def is_sensitive(name):
    return any(h in name.upper() for h in SECRET_HINTS)

def mask_secrets(text):
    return text

# ================= PERSISTENT PTY SHELL =================
SCROLLBACK_LIMIT = 200_000

class PtyShell:
    def __init__(self, name):
        self.name = name
        self.created = time.time()
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            os.environ["TERM"] = "xterm-256color"
            os.chdir("/")
            os.execvp("bash", ["bash", "-i"])
        else:
            flags = fcntl.fcntl(self.fd, fcntl.F_GETFL)
            fcntl.fcntl(self.fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
            self._set_size(40, 120)
            self.history = ""
            self.lock = threading.Lock()
            threading.Thread(target=self._reader, daemon=True).start()

    def _set_size(self, rows, cols):
        try:
            fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        except Exception:
            pass

    def resize(self, rows, cols):
        self._set_size(rows, cols)

    def _reader(self):
        while True:
            try:
                r, _, _ = select.select([self.fd], [], [], 0.5)
                if r:
                    data = os.read(self.fd, 65536)
                    if not data:
                        break
                    with self.lock:
                        self.history += data.decode(errors="replace")
                        if len(self.history) > SCROLLBACK_LIMIT:
                            self.history = self.history[-SCROLLBACK_LIMIT:]
            except OSError:
                break

    def write(self, text):
        try:
            os.write(self.fd, text.encode())
        except OSError:
            pass

    def snapshot(self):
        with self.lock:
            return mask_secrets(self.history), len(self.history)

    def read_from(self, cursor):
        with self.lock:
            return mask_secrets(self.history[cursor:]), len(self.history)

    def alive(self):
        try:
            pid, _ = os.waitpid(self.pid, os.WNOHANG)
            return pid == 0
        except OSError:
            return False

    def kill(self):
        try:
            os.kill(self.pid, signal.SIGKILL)
        except OSError:
            pass

_shells = {}
_counter = {"n": 0}
_slock = threading.Lock()

def create_shell():
    with _slock:
        _counter["n"] += 1
        name = f"Terminal {_counter['n']}"
        _shells[name] = PtyShell(name)
        return name

def get_shell(name):
    sh = _shells.get(name)
    if sh and not sh.alive():
        del _shells[name]
        return None
    return sh

def list_shells():
    for n in list(_shells.keys()):
        if not _shells[n].alive():
            del _shells[n]
    return sorted(_shells.keys(), key=lambda n: _shells[n].created)

# ================= SYSTEM STATS =================
_cpu_prev = {"total": 0, "idle": 0}

def read_cpu():
    try:
        with open("/proc/stat") as f:
            parts = f.readline().split()[1:]
        nums = list(map(int, parts))
        idle = nums[3] + (nums[4] if len(nums) > 4 else 0)
        total = sum(nums)
        dt = total - _cpu_prev["total"]; di = idle - _cpu_prev["idle"]
        _cpu_prev["total"], _cpu_prev["idle"] = total, idle
        if dt > 0:
            return round(100 * (dt - di) / dt, 1)
    except Exception:
        pass
    return 0.0

def read_mem():
    try:
        info = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, v = line.split(":")
                info[k.strip()] = int(v.strip().split()[0])
        total = info.get("MemTotal", 0); avail = info.get("MemAvailable", 0)
        used = total - avail
        return {"total": total, "used": used, "pct": round(100 * used / total, 1) if total else 0}
    except Exception:
        return {"total": 0, "used": 0, "pct": 0}

def read_disk():
    try:
        st = os.statvfs("/")
        total = st.f_blocks * st.f_frsize; free = st.f_bavail * st.f_frsize
        used = total - free
        return {"total": total, "used": used, "pct": round(100 * used / total, 1) if total else 0}
    except Exception:
        return {"total": 0, "used": 0, "pct": 0}

def read_uptime():
    try:
        with open("/proc/uptime") as f:
            return float(f.readline().split()[0])
    except Exception:
        return 0

def read_loadavg():
    try:
        return os.getloadavg()
    except Exception:
        return (0, 0, 0)

def list_processes(limit=40):
    procs = []
    try:
        out = subprocess.check_output(
            ["ps", "-eo", "pid,comm,pcpu,pmem", "--sort=-pcpu"],
            stderr=subprocess.STDOUT, timeout=5).decode(errors="replace")
        lines = out.strip().splitlines()[1:limit+1]
        for ln in lines:
            p = ln.split(None, 3)
            if len(p) == 4:
                procs.append({"pid": p[0], "name": p[1], "cpu": p[2], "mem": p[3]})
    except Exception:
        pass
    return procs

# ================= UI: RUN X OS DESKTOP =================
DESKTOP = r"""<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>RUN X OS</title>
<link rel="icon" href="/fm/logo">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/xterm@5.3.0/css/xterm.css"/>
<script src="https://cdn.jsdelivr.net/npm/xterm@5.3.0/lib/xterm.js"></script>
<script src="https://cdn.jsdelivr.net/npm/xterm-addon-fit@0.8.0/lib/xterm-addon-fit.js"></script>
<script src="https://cdn.jsdelivr.net/npm/gsap@3.12.5/dist/gsap.min.js"></script>
<style>
:root{
  --accent:#F46821; --accent2:#29BEFD; --teal:#47E6C1; --red:#F43256; --purple:#A259EA;
  --panel:rgba(16,16,20,.82); --panel2:rgba(24,24,30,.94); --stroke:rgba(255,255,255,.08);
  --txt:#eef1f6; --muted:#8a92a3;
}
*{box-sizing:border-box}
html,body{height:100%;margin:0;overflow:hidden;font-family:'Segoe UI',system-ui,-apple-system,sans-serif;color:var(--txt);background:#000}
svg.i{width:1em;height:1em;stroke:currentColor;fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round;vertical-align:-.15em}

/* ===== Boot / splash video ===== */
#boot{position:fixed;inset:0;background:#000;z-index:99999;display:flex;align-items:center;justify-content:center}
#boot video{max-width:70%;max-height:70%;filter:drop-shadow(0 0 60px rgba(244,104,33,.35))}
#boot .skip{position:absolute;bottom:28px;right:32px;color:var(--muted);font-size:12px;cursor:pointer;
  border:1px solid var(--stroke);padding:8px 16px;border-radius:20px;background:rgba(255,255,255,.04)}
#boot .skip:hover{color:#fff;border-color:var(--accent)}

/* ===== Desktop: black BG + big centered logo ===== */
#desktop{position:fixed;inset:0;overflow:hidden;background:#000}
#bglogo{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;pointer-events:none;z-index:0}
#bglogo img{width:min(60vw,620px);opacity:.10;filter:grayscale(.1) drop-shadow(0 0 80px rgba(244,104,33,.25))}
#bgglow{position:absolute;inset:0;z-index:0;pointer-events:none;
  background:radial-gradient(700px 500px at 50% 45%, rgba(244,104,33,.10) 0%, transparent 60%),
             radial-gradient(600px 600px at 80% 90%, rgba(41,190,253,.06) 0%, transparent 55%);}

/* ===== Desktop icons ===== */
#icons{position:absolute;top:26px;left:26px;display:flex;flex-direction:column;gap:18px;z-index:2}
.dicon{width:96px;text-align:center;cursor:pointer;user-select:none;padding:10px;border-radius:12px;transition:.15s}
.dicon:hover{background:rgba(255,255,255,.07)}
.dicon .bx{width:52px;height:52px;margin:0 auto;border-radius:14px;display:flex;align-items:center;justify-content:center;
  font-size:26px;background:linear-gradient(160deg,rgba(255,255,255,.10),rgba(255,255,255,.02));
  border:1px solid var(--stroke);box-shadow:0 6px 20px rgba(0,0,0,.5)}
.dicon .l{font-size:12px;margin-top:8px;text-shadow:0 1px 4px #000}

/* ===== Windows ===== */
.win{position:absolute;min-width:360px;min-height:240px;background:var(--panel);
  backdrop-filter:blur(22px);-webkit-backdrop-filter:blur(22px);
  border:1px solid var(--stroke);border-radius:14px;box-shadow:0 30px 80px rgba(0,0,0,.7);
  display:flex;flex-direction:column;overflow:hidden;z-index:10}
.win.max{border-radius:0}
.titlebar{height:42px;display:flex;align-items:center;gap:10px;padding:0 8px 0 14px;
  background:var(--panel2);cursor:move;user-select:none;border-bottom:1px solid var(--stroke)}
.titlebar .ico{font-size:16px;color:var(--accent);display:flex}
.titlebar .t{flex:1;font-size:13px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.wbtn{width:34px;height:28px;border:0;background:transparent;color:var(--muted);border-radius:7px;cursor:pointer;
  font-size:14px;display:flex;align-items:center;justify-content:center}
.wbtn:hover{background:rgba(255,255,255,.12);color:#fff}
.wbtn.close:hover{background:var(--red);color:#fff}
.wbody{flex:1;overflow:auto;position:relative}
.resizer{position:absolute;width:18px;height:18px;right:0;bottom:0;cursor:nwse-resize;z-index:5}

/* ===== Taskbar ===== */
#taskbar{position:fixed;left:50%;transform:translateX(-50%);bottom:12px;height:58px;display:flex;align-items:center;
  gap:6px;padding:0 12px;background:rgba(14,14,18,.72);backdrop-filter:blur(26px);-webkit-backdrop-filter:blur(26px);
  border:1px solid var(--stroke);border-radius:16px;z-index:9000;box-shadow:0 18px 50px rgba(0,0,0,.6);max-width:94vw}
#startbtn{display:flex;align-items:center;gap:9px;height:42px;padding:0 12px;border-radius:11px;cursor:pointer}
#startbtn:hover{background:rgba(255,255,255,.1)}
#startbtn img{height:26px;width:26px;border-radius:7px}
#startbtn b{font-size:14px;letter-spacing:.5px}
#startbtn b span{color:var(--accent)}
.sep{width:1px;height:30px;background:var(--stroke);margin:0 4px}
.tasks{display:flex;gap:6px;overflow:hidden}
.taskitem{display:flex;align-items:center;gap:8px;height:42px;max-width:190px;padding:0 12px;border-radius:11px;
  background:rgba(255,255,255,.05);cursor:pointer;font-size:12.5px;white-space:nowrap;overflow:hidden;
  border-bottom:2px solid transparent;transition:.15s}
.taskitem:hover{background:rgba(255,255,255,.12)}
.taskitem.active{border-bottom-color:var(--accent);background:rgba(244,104,33,.16)}
.taskitem .ico{color:var(--accent);display:flex}
#tray{display:flex;align-items:center;gap:14px;padding:0 8px 0 12px;font-size:12px;color:var(--muted);margin-left:auto}
#clock{text-align:right;line-height:1.2;color:var(--txt)}
#clock small{color:var(--muted)}

/* ===== Start menu ===== */
#startmenu{position:fixed;left:50%;transform:translateX(-50%);bottom:82px;width:520px;max-height:72vh;overflow:auto;
  background:var(--panel2);backdrop-filter:blur(30px);border:1px solid var(--stroke);border-radius:18px;
  box-shadow:0 40px 100px rgba(0,0,0,.8);padding:22px;display:none;z-index:9500}
#startmenu.open{display:block}
#startmenu .search{width:100%;background:#000;border:1px solid var(--stroke);color:#eee;padding:11px 14px;
  border-radius:10px;font-size:13px;margin-bottom:18px;outline:none}
#startmenu h4{margin:0 0 12px;color:var(--muted);font-weight:600;font-size:11px;letter-spacing:1.5px}
.apps{display:grid;grid-template-columns:repeat(5,1fr);gap:8px}
.app{display:flex;flex-direction:column;align-items:center;gap:9px;padding:16px 6px;border-radius:12px;cursor:pointer;
  text-align:center;font-size:12px;transition:.15s}
.app:hover{background:rgba(255,255,255,.1)}
.app .bx{width:46px;height:46px;border-radius:13px;display:flex;align-items:center;justify-content:center;font-size:22px;
  background:linear-gradient(160deg,rgba(255,255,255,.10),rgba(255,255,255,.02));border:1px solid var(--stroke)}

/* ===== Shared UI ===== */
.toolbar{display:flex;align-items:center;gap:8px;padding:10px;background:var(--panel2);
  border-bottom:1px solid var(--stroke);flex-wrap:wrap}
.field{flex:1;min-width:150px;background:#000;border:1px solid var(--stroke);color:#eee;padding:8px 11px;
  border-radius:8px;font-family:monospace;font-size:12px;outline:none}
.btn{background:var(--accent);color:#fff;border:0;padding:8px 13px;border-radius:8px;cursor:pointer;font-size:12px;
  display:inline-flex;align-items:center;gap:6px}
.btn.ghost{background:rgba(255,255,255,.08);color:var(--txt)}
.btn:hover{filter:brightness(1.12)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(116px,1fr));gap:10px;padding:16px}
.fitem{padding:14px 6px;border-radius:12px;text-align:center;cursor:pointer;user-select:none;transition:.12s}
.fitem:hover{background:rgba(255,255,255,.08)}
.fitem .bx{width:46px;height:46px;margin:0 auto;border-radius:12px;display:flex;align-items:center;justify-content:center;
  font-size:22px;background:rgba(255,255,255,.05);border:1px solid var(--stroke);color:var(--accent2)}
.fitem .n{font-size:12px;margin-top:8px;word-break:break-word;line-height:1.25}
.fitem .s{font-size:10px;color:var(--muted)}
.ctx{position:fixed;background:var(--panel2);border:1px solid var(--stroke);border-radius:10px;
  box-shadow:0 16px 50px rgba(0,0,0,.7);padding:6px;z-index:20000;display:none;min-width:170px}
.ctx div{padding:9px 13px;border-radius:7px;cursor:pointer;font-size:12.5px;display:flex;align-items:center;gap:9px}
.ctx div:hover{background:var(--accent)}
.ctx div.danger:hover{background:var(--red)}
.mon{padding:20px;font-size:13px}
.gauge{margin:14px 0}
.gauge .lab{display:flex;justify-content:space-between;margin-bottom:7px;font-size:12px;color:var(--muted)}
.bar{height:12px;border-radius:7px;background:rgba(255,255,255,.07);overflow:hidden}
.bar>i{display:block;height:100%;border-radius:7px;transition:width .5s ease}
.kv{display:flex;justify-content:space-between;padding:7px 0;border-bottom:1px solid var(--stroke)}
table.ps{width:100%;border-collapse:collapse;font-size:12px}
table.ps th,table.ps td{padding:7px 10px;text-align:left;border-bottom:1px solid var(--stroke)}
table.ps th{color:var(--muted);font-weight:600;position:sticky;top:0;background:var(--panel2)}
textarea.editor{width:100%;height:100%;background:#0a0a0d;color:var(--teal);border:0;padding:16px;
  font-family:monospace;font-size:13px;resize:none;outline:none;line-height:1.5}
pre.viewer{margin:0;padding:16px;background:#0a0a0d;color:#e6e6e6;white-space:pre-wrap;font-family:monospace;font-size:12.5px;min-height:100%}
.termhost{width:100%;height:100%;background:#000}
.imgview{display:flex;align-items:center;justify-content:center;height:100%;background:#0a0a0d}
.imgview img{max-width:100%;max-height:100%}
.notepad{width:100%;height:100%;border:0;background:#0a0a0d;color:#eef1f6;padding:18px;font-size:14px;outline:none;resize:none;line-height:1.6}
.calc{padding:16px}
.calc .disp{background:#000;border:1px solid var(--stroke);border-radius:10px;padding:16px;text-align:right;
  font-size:26px;font-family:monospace;margin-bottom:12px;min-height:58px;overflow:auto}
.calc .keys{display:grid;grid-template-columns:repeat(4,1fr);gap:8px}
.calc button{padding:16px;border:0;border-radius:10px;background:rgba(255,255,255,.07);color:#fff;font-size:16px;cursor:pointer}
.calc button:hover{background:rgba(255,255,255,.14)}
.calc button.op{background:rgba(244,104,33,.25)}
.calc button.eq{background:var(--accent);grid-column:span 2}
</style></head>
<body>

<!-- ===== Boot splash (run.mp4) ===== -->
<div id="boot">
  <video id="bootvid" autoplay muted playsinline>
    <source src="/fm/video" type="video/mp4">
  </video>
  <div class="skip" onclick="finishBoot()">Skip &rsaquo;</div>
</div>

<!-- ===== Desktop ===== -->
<div id="desktop">
  <div id="bgglow"></div>
  <div id="bglogo"><img src="/fm/logo" alt="RUN X OS"></div>
  <div id="icons"></div>
</div>

<!-- ===== Start menu ===== -->
<div id="startmenu">
  <div style="display:flex;align-items:center;gap:12px;margin-bottom:18px">
    <img src="/fm/logo" style="height:40px;width:40px;border-radius:10px">
    <div><b style="font-size:17px">RUN <span style="color:var(--accent)">X</span> OS</b>
    <div style="font-size:11px;color:var(--muted)" id="host"></div></div>
  </div>
  <input class="search" id="appsearch" placeholder="Search apps...">
  <h4>ALL APPLICATIONS</h4>
  <div class="apps" id="applist"></div>
</div>

<!-- ===== Taskbar ===== -->
<div id="taskbar">
  <div id="startbtn"><img src="/fm/logo"><b>RUN <span>X</span> OS</b></div>
  <div class="sep"></div>
  <div class="tasks" id="tasks"></div>
  <div id="tray"><span id="netinfo"></span><div id="clock"></div></div>
</div>

<div class="ctx" id="ctxmenu"></div>

<!-- ===== Modern SVG icon set (Feather-style) ===== -->
<svg style="display:none">
 <symbol id="ic-files" viewBox="0 0 24 24"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></symbol>
 <symbol id="ic-terminal" viewBox="0 0 24 24"><polyline points="4 17 10 11 4 5"/><line x1="12" y1="19" x2="20" y2="19"/></symbol>
 <symbol id="ic-monitor" viewBox="0 0 24 24"><path d="M3 3v18h18"/><path d="M7 14l3-3 3 3 5-6"/></symbol>
 <symbol id="ic-editor" viewBox="0 0 24 24"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.12 2.12 0 0 1 3 3L12 15l-4 1 1-4z"/></symbol>
 <symbol id="ic-gpu" viewBox="0 0 24 24"><rect x="2" y="6" width="20" height="12" rx="2"/><path d="M6 10h4M6 14h4"/><circle cx="16" cy="12" r="2"/></symbol>
 <symbol id="ic-cpu" viewBox="0 0 24 24"><rect x="4" y="4" width="16" height="16" rx="2"/><rect x="9" y="9" width="6" height="6"/><path d="M9 1v3M15 1v3M9 20v3M15 20v3M1 9h3M1 15h3M20 9h3M20 15h3"/></symbol>
 <symbol id="ic-calc" viewBox="0 0 24 24"><rect x="4" y="2" width="16" height="20" rx="2"/><line x1="8" y1="6" x2="16" y2="6"/><line x1="8" y1="14" x2="8" y2="14"/><line x1="12" y1="14" x2="12" y2="14"/><line x1="16" y1="14" x2="16" y2="14"/><line x1="8" y1="18" x2="8" y2="18"/><line x1="12" y1="18" x2="12" y2="18"/><line x1="16" y1="18" x2="16" y2="18"/></symbol>
 <symbol id="ic-note" viewBox="0 0 24 24"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="8" y1="13" x2="16" y2="13"/><line x1="8" y1="17" x2="16" y2="17"/></symbol>
 <symbol id="ic-net" viewBox="0 0 24 24"><path d="M5 12.55a11 11 0 0 1 14 0M1.42 9a16 16 0 0 1 21.16 0M8.53 16.11a6 6 0 0 1 6.95 0"/><line x1="12" y1="20" x2="12" y2="20"/></symbol>
 <symbol id="ic-info" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12" y2="8"/></symbol>
 <symbol id="ic-folder" viewBox="0 0 24 24"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></symbol>
 <symbol id="ic-file" viewBox="0 0 24 24"><path d="M13 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/><polyline points="13 2 13 9 20 9"/></symbol>
 <symbol id="ic-min" viewBox="0 0 24 24"><line x1="5" y1="12" x2="19" y2="12"/></symbol>
 <symbol id="ic-max" viewBox="0 0 24 24"><rect x="4" y="4" width="16" height="16" rx="1"/></symbol>
 <symbol id="ic-close" viewBox="0 0 24 24"><line x1="6" y1="6" x2="18" y2="18"/><line x1="18" y1="6" x2="6" y2="18"/></symbol>
 <symbol id="ic-up" viewBox="0 0 24 24"><line x1="12" y1="19" x2="12" y2="5"/><polyline points="5 12 12 5 19 12"/></symbol>
 <symbol id="ic-plus" viewBox="0 0 24 24"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></symbol>
 <symbol id="ic-download" viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></symbol>
 <symbol id="ic-trash" viewBox="0 0 24 24"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></symbol>
 <symbol id="ic-save" viewBox="0 0 24 24"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><polyline points="17 21 17 13 7 13 7 21"/><polyline points="7 3 7 8 15 8"/></symbol>
 <symbol id="ic-refresh" viewBox="0 0 24 24"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></symbol>
</svg>

<script>
const ICO = n => `<svg class="i"><use href="#ic-${n}"/></svg>`;
/* ===== App registry ===== */
const APPS = {
  files:    {title:'Files',          icon:'files',    open:openFiles},
  terminal: {title:'Terminal',       icon:'terminal', open:openTerminal},
  monitor:  {title:'System Monitor', icon:'monitor',  open:openMonitor},
  processes:{title:'Processes',      icon:'cpu',      open:openProcesses},
  editor:   {title:'Code Editor',    icon:'editor',   open:()=>openEditor('')},
  notes:    {title:'Notes',          icon:'note',     open:openNotes},
  calc:     {title:'Calculator',     icon:'calc',     open:openCalc},
  gpu:      {title:'GPU Info',       icon:'gpu',      open:openGpu},
  network:  {title:'Network',        icon:'net',      open:openNetwork},
  about:    {title:'About',          icon:'info',     open:openAbout},
};
const DESKTOP_ICONS = ['files','terminal','monitor','processes','editor','calc'];

/* ===== Window manager ===== */
let Z=100, winCount=0; const wins={};
function makeWindow(app,title,iconName,w=780,h=500){
  const id='w'+(++winCount);
  const el=document.createElement('div');
  el.className='win'; el.id=id;
  el.style.width=w+'px'; el.style.height=h+'px';
  el.style.left=(70+(winCount*28)%240)+'px';
  el.style.top=(40+(winCount*26)%150)+'px';
  el.style.zIndex=++Z;
  el.innerHTML=`
    <div class="titlebar">
      <span class="ico">${ICO(iconName)}</span>
      <span class="t">${title}</span>
      <button class="wbtn min">${ICO('min')}</button>
      <button class="wbtn max">${ICO('max')}</button>
      <button class="wbtn close">${ICO('close')}</button>
    </div>
    <div class="wbody"></div>
    <div class="resizer">${ICO('max')}</div>`;
  el.querySelector('.resizer').style.opacity='.25';
  document.getElementById('desktop').appendChild(el);
  wins[id]={el,app,title,icon:iconName,min:false,prev:null};
  el.addEventListener('mousedown',()=>focusWin(id));
  el.querySelector('.close').onclick=e=>{e.stopPropagation();closeWin(id)};
  el.querySelector('.min').onclick=e=>{e.stopPropagation();minWin(id)};
  el.querySelector('.max').onclick=e=>{e.stopPropagation();maxWin(id)};
  el.querySelector('.titlebar').addEventListener('dblclick',()=>maxWin(id));
  dragify(el,el.querySelector('.titlebar'));
  resizify(el,el.querySelector('.resizer'));
  gsap.fromTo(el,{scale:.9,opacity:0,y:14},{scale:1,opacity:1,y:0,duration:.28,ease:'back.out(1.6)'});
  focusWin(id); renderTasks(id);
  return {id, body:el.querySelector('.wbody'), el};
}
function focusWin(id){
  const w=wins[id]; if(!w)return;
  if(w.min){w.min=false; w.el.style.display='flex';
    gsap.fromTo(w.el,{scale:.95,opacity:0},{scale:1,opacity:1,duration:.2});}
  w.el.style.zIndex=++Z; renderTasks(id);
}
function closeWin(id){
  const w=wins[id]; if(!w)return;
  if(w.onClose) try{w.onClose()}catch(e){}
  gsap.to(w.el,{scale:.9,opacity:0,duration:.18,onComplete:()=>{w.el.remove();delete wins[id];renderTasks();}});
}
function minWin(id){
  const w=wins[id]; if(!w)return; w.min=true;
  gsap.to(w.el,{scale:.9,opacity:0,y:20,duration:.18,onComplete:()=>{w.el.style.display='none';renderTasks();}});
}
function maxWin(id){
  const w=wins[id]; if(!w)return;
  if(w.el.classList.contains('max')){
    w.el.classList.remove('max');
    gsap.to(w.el,{...w.prev,duration:.22,onComplete:()=>{if(w.onResize)w.onResize()}});
  }else{
    w.prev={left:w.el.offsetLeft+'px',top:w.el.offsetTop+'px',width:w.el.offsetWidth+'px',height:w.el.offsetHeight+'px'};
    w.el.classList.add('max');
    gsap.to(w.el,{left:0,top:0,width:'100vw',height:'calc(100vh - 82px)',duration:.22,
      onComplete:()=>{if(w.onResize)w.onResize()}});
  }
}
function dragify(el,handle){
  let sx,sy,ox,oy,drag=false;
  handle.addEventListener('mousedown',e=>{
    if(el.classList.contains('max'))return;
    drag=true;sx=e.clientX;sy=e.clientY;ox=el.offsetLeft;oy=el.offsetTop;e.preventDefault();
  });
  document.addEventListener('mousemove',e=>{if(!drag)return;
    el.style.left=Math.max(0,ox+e.clientX-sx)+'px';el.style.top=Math.max(0,oy+e.clientY-sy)+'px';});
  document.addEventListener('mouseup',()=>drag=false);
}
function resizify(el,grip){
  let sx,sy,ow,oh,rz=false;
  grip.addEventListener('mousedown',e=>{rz=true;sx=e.clientX;sy=e.clientY;
    ow=el.offsetWidth;oh=el.offsetHeight;e.preventDefault();e.stopPropagation();});
  document.addEventListener('mousemove',e=>{if(!rz)return;
    el.style.width=Math.max(360,ow+e.clientX-sx)+'px';
    el.style.height=Math.max(240,oh+e.clientY-sy)+'px';
    if(wins[el.id]&&wins[el.id].onResize)wins[el.id].onResize();});
  document.addEventListener('mouseup',()=>rz=false);
}

/* ===== Taskbar ===== */
function renderTasks(activeId){
  const t=document.getElementById('tasks');t.innerHTML='';
  for(const id in wins){
    const w=wins[id];
    const d=document.createElement('div');
    d.className='taskitem'+(id===activeId?' active':'');
    d.innerHTML=`<span class="ico">${ICO(w.icon)}</span><span>${w.title}</span>`;
    d.onclick=()=>{ if(w.min) focusWin(id); else if(id===activeId) minWin(id); else focusWin(id); };
    t.appendChild(d);
  }
}

/* ===== Desktop icons + start menu ===== */
function buildDesktop(){
  const ic=document.getElementById('icons');
  DESKTOP_ICONS.forEach(k=>{const a=APPS[k];
    const d=document.createElement('div');d.className='dicon';
    d.innerHTML=`<div class="bx">${ICO(a.icon)}</div><div class="l">${a.title}</div>`;
    d.ondblclick=()=>a.open();
    ic.appendChild(d);
  });
  gsap.from('.dicon',{opacity:0,x:-20,stagger:.05,duration:.4,delay:.1});
  const al=document.getElementById('applist');
  Object.keys(APPS).forEach(k=>{const a=APPS[k];
    const d=document.createElement('div');d.className='app';d.dataset.name=a.title.toLowerCase();
    d.innerHTML=`<div class="bx">${ICO(a.icon)}</div><div>${a.title}</div>`;
    d.onclick=()=>{a.open();toggleStart(false)};
    al.appendChild(d);
  });
  document.getElementById('appsearch').addEventListener('input',e=>{
    const q=e.target.value.toLowerCase();
    document.querySelectorAll('.app').forEach(a=>a.style.display=a.dataset.name.includes(q)?'':'none');
  });
}
function toggleStart(force){
  const m=document.getElementById('startmenu');
  const open=force!==undefined?force:!m.classList.contains('open');
  if(open){m.classList.add('open');gsap.fromTo(m,{y:20,opacity:0},{y:0,opacity:1,duration:.22,ease:'power2.out'});
    document.getElementById('appsearch').focus();}
  else{gsap.to(m,{y:14,opacity:0,duration:.15,onComplete:()=>m.classList.remove('open')});}
}
document.getElementById('startbtn').onclick=e=>{e.stopPropagation();toggleStart()};
document.addEventListener('click',()=>{toggleStart(false);hideCtx()});

/* ===== Clock ===== */
function tick(){const n=new Date();
  document.getElementById('clock').innerHTML=
    `${n.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'})}<br><small>${n.toLocaleDateString([],{day:'2-digit',month:'short',year:'numeric'})}</small>`;
}
setInterval(tick,1000);tick();

/* ===== Context menu ===== */
function showCtx(x,y,items){
  const c=document.getElementById('ctxmenu');c.innerHTML='';
  items.forEach(it=>{const d=document.createElement('div');
    d.innerHTML=`${it.icon?ICO(it.icon):''}<span>${it.label}</span>`;
    if(it.danger)d.className='danger';
    d.onclick=e=>{e.stopPropagation();hideCtx();it.action()};c.appendChild(d);});
  c.style.display='block';
  c.style.left=Math.min(x,innerWidth-190)+'px';
  c.style.top=Math.min(y,innerHeight-c.offsetHeight-70)+'px';
  gsap.fromTo(c,{opacity:0,scale:.95},{opacity:1,scale:1,duration:.12});
}
function hideCtx(){document.getElementById('ctxmenu').style.display='none'}

/* ===== FILES ===== */
function openFiles(startPath){
  const {id,body}=makeWindow('files','Files','files',840,560);
  let path=startPath||'/';
  body.innerHTML=`
    <div class="toolbar">
      <button class="btn ghost" data-up>${ICO('up')} Up</button>
      <input class="field" data-path value="/">
      <button class="btn" data-go>${ICO('refresh')} Go</button>
      <button class="btn ghost" data-mkdir>${ICO('plus')} Folder</button>
      <button class="btn ghost" data-newfile>${ICO('plus')} File</button>
      <label class="btn ghost" style="position:relative;overflow:hidden">${ICO('download')} Upload
        <input type="file" data-upload style="position:absolute;inset:0;opacity:0;cursor:pointer"></label>
    </div>
    <div class="grid" data-grid></div>`;
  const inp=body.querySelector('[data-path]'), grid=body.querySelector('[data-grid]');
  async function load(p){
    const j=await (await fetch('/fm/api/list?path='+encodeURIComponent(p))).json();
    if(j.error){grid.innerHTML=`<div style="padding:20px;color:var(--red)">${esc(j.error)}</div>`;return;}
    path=j.path;inp.value=path;grid.innerHTML='';
    j.entries.forEach(en=>{
      const d=document.createElement('div');d.className='fitem';
      d.innerHTML=`<div class="bx">${en.dir?ICO('folder'):ICO('file')}</div>
        <div class="n">${esc(en.name)}</div><div class="s">${en.dir?'':fmtSize(en.size)}</div>`;
      d.ondblclick=()=>en.dir?load(en.path):openFileFromFm(en);
      d.oncontextmenu=e=>{e.preventDefault();e.stopPropagation();fileCtx(e,en,load,path);};
      grid.appendChild(d);
    });
    gsap.from(grid.children,{opacity:0,y:8,stagger:.01,duration:.25});
  }
  body.querySelector('[data-go]').onclick=()=>load(inp.value);
  inp.addEventListener('keydown',e=>{if(e.key==='Enter')load(inp.value)});
  body.querySelector('[data-up]').onclick=()=>load(path.replace(/\/+$/,'').split('/').slice(0,-1).join('/')||'/');
  body.querySelector('[data-mkdir]').onclick=async()=>{const n=prompt('New folder name:');if(!n)return;
    await fetch('/fm/api/mkdir',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path,name:n})});load(path);};
  body.querySelector('[data-newfile]').onclick=async()=>{const n=prompt('New file name:');if(!n)return;
    await fetch('/fm/api/touch',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path,name:n})});load(path);};
  body.querySelector('[data-upload]').onchange=async e=>{const f=e.target.files[0];if(!f)return;
    const fd=new FormData();fd.append('file',f);fd.append('path',path);
    await fetch('/fm/upload',{method:'POST',body:fd});load(path);};
  load(path);
}
function fileCtx(e,en,reload,path){
  const items=[];
  if(!en.dir){
    items.push({label:'Open',icon:'file',action:()=>openFileFromFm(en)});
    items.push({label:'Edit',icon:'editor',action:()=>openEditor(en.path)});
    items.push({label:'Download',icon:'download',action:()=>location.href='/fm/download?path='+encodeURIComponent(en.path)});
  }else items.push({label:'Open',icon:'folder',action:()=>openFiles(en.path)});
  items.push({label:'Rename',icon:'editor',action:async()=>{const nn=prompt('Rename to:',en.name);if(!nn)return;
    await fetch('/fm/api/rename',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path:en.path,name:nn})});reload(path);}});
  items.push({label:'Delete',icon:'trash',danger:true,action:async()=>{if(!confirm('Delete '+en.name+'?'))return;
    await fetch('/fm/api/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path:en.path})});reload(path);}});
  showCtx(e.clientX,e.clientY,items);
}
function openFileFromFm(en){
  const ext=(en.name.split('.').pop()||'').toLowerCase();
  if(['png','jpg','jpeg','gif','webp','svg','bmp'].includes(ext)) openImage(en.path,en.name);
  else openViewer(en.path);
}

/* ===== VIEWER ===== */
async function openViewer(p){
  const {body}=makeWindow('files',p.split('/').pop()||'View','file',720,500);
  body.innerHTML=`<div class="toolbar"><button class="btn" data-e>${ICO('editor')} Edit</button>
    <button class="btn ghost" data-d>${ICO('download')} Download</button></div><pre class="viewer">Loading...</pre>`;
  const j=await (await fetch('/fm/api/read?path='+encodeURIComponent(p))).json();
  body.querySelector('.viewer').textContent=j.error?('['+j.error+']'):j.content;
  body.querySelector('[data-e]').onclick=()=>openEditor(p);
  body.querySelector('[data-d]').onclick=()=>location.href='/fm/download?path='+encodeURIComponent(p);
}
function openImage(p,name){
  const {body}=makeWindow('files',name||'Image','file',640,480);
  body.innerHTML=`<div class="imgview"><img src="/fm/raw?path=${encodeURIComponent(p)}"></div>`;
}

/* ===== EDITOR ===== */
async function openEditor(p){
  const title=p?p.split('/').pop():'Untitled';
  const {body}=makeWindow('editor',title+' - Editor','editor',720,540);
  body.style.display='flex';body.style.flexDirection='column';
  body.innerHTML=`<div class="toolbar">
    <input class="field" data-path value="${esc(p||'')}" placeholder="/path/to/file">
    <button class="btn" data-save>${ICO('save')} Save</button>
    <span data-status style="color:var(--muted);font-size:11px"></span>
  </div>
  <textarea class="editor" spellcheck="false"></textarea>`;
  const ta=body.querySelector('.editor'), st=body.querySelector('[data-status]');
  if(p){
    const j=await (await fetch('/fm/api/read?path='+encodeURIComponent(p))).json();
    ta.value=j.error?'':j.content;
  }
  ta.addEventListener('keydown',e=>{
    if(e.key==='Tab'){e.preventDefault();
      const s=ta.selectionStart,en=ta.selectionEnd;
      ta.value=ta.value.slice(0,s)+'  '+ta.value.slice(en);
      ta.selectionStart=ta.selectionEnd=s+2;}
    if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='s'){e.preventDefault();doSave();}
  });
  async function doSave(){
    const path=body.querySelector('[data-path]').value.trim();
    if(!path){st.textContent='Enter a path first';return;}
    const j=await (await fetch('/fm/api/save',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({path,content:ta.value})})).json();
    st.textContent=j.ok?'Saved '+new Date().toLocaleTimeString():('Error: '+(j.error||''));
    st.style.color=j.ok?'var(--teal)':'var(--red)';
  }
  body.querySelector('[data-save]').onclick=doSave;
}

/* ===== TERMINAL (persistent PTY) ===== */
function openTerminal(existingName){
  const {id,body}=makeWindow('terminal','Terminal','terminal',800,480);
  body.innerHTML=`
    <div class="toolbar">
      <select class="field" data-sel style="flex:0 0 210px"></select>
      <button class="btn" data-new>${ICO('plus')} New</button>
      <button class="btn ghost" data-kill>${ICO('trash')} Kill</button>
      <span style="color:var(--muted);font-size:11px">Ctrl/Cmd+V paste &middot; right-click paste</span>
    </div>
    <div class="termhost" data-host></div>`;
  const sel=body.querySelector('[data-sel]'), host=body.querySelector('[data-host]');
  let term,fit,cur=0,poll=false,current=null,pollTimer=null;

  function send(d){ if(!current)return;
    fetch('/fm/term_in',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({name:current,data:d})}); }
  async function doPaste(){ if(!current)return;
    try{const t=await navigator.clipboard.readText();if(t)send(t);}
    catch(e){const t=prompt('Paste:','');if(t)send(t);} }
  function mkTerm(){
    host.innerHTML='';
    term=new Terminal({convertEol:false,cursorBlink:true,fontFamily:'monospace',fontSize:13,
      theme:{background:'#000000',foreground:'#e6e6e6',cursor:'#F46821'}});
    fit=new FitAddon.FitAddon();term.loadAddon(fit);
    term.open(host);fit.fit();syncSize();
    term.onData(d=>send(d));
    term.attachCustomKeyEventHandler(e=>{
      const mod=e.ctrlKey||e.metaKey;
      if(e.type==='keydown'&&mod&&e.key.toLowerCase()==='v'){e.preventDefault();doPaste();return false;}
      if(e.type==='keydown'&&mod&&e.key.toLowerCase()==='c'&&term.hasSelection()){
        e.preventDefault();navigator.clipboard.writeText(term.getSelection()).catch(()=>{});return false;}
      return true;
    });
    host.addEventListener('contextmenu',e=>{e.preventDefault();doPaste();});
    host.addEventListener('paste',e=>{e.preventDefault();
      const t=(e.clipboardData||window.clipboardData).getData('text');if(t)send(t);});
  }
  function syncSize(){ if(!fit||!current)return;
    try{fit.fit();
      fetch('/fm/term_resize',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({name:current,rows:term.rows,cols:term.cols})});
    }catch(e){} }
  wins[id].onResize=()=>syncSize();
  wins[id].onClose=()=>{poll=false;if(pollTimer)clearTimeout(pollTimer);};

  async function refresh(){
    const names=(await (await fetch('/fm/term_list')).json()).sessions;
    sel.innerHTML='';names.forEach(n=>{const o=document.createElement('option');o.value=n;o.textContent=n;sel.appendChild(o);});
    if(current&&names.includes(current))sel.value=current;
    return names;
  }
  async function attach(name){
    current=name;cur=0;mkTerm();
    const j=await (await fetch('/fm/term_snapshot?name='+encodeURIComponent(name))).json();
    if(j.gone){current=null;return;}
    term.write(j.data);cur=j.cursor;startPoll();
  }
  function startPoll(){ if(poll)return;poll=true;
    (async function loop(){ if(!poll)return;
      if(current){
        try{const j=await (await fetch('/fm/term_out?name='+encodeURIComponent(current)+'&cursor='+cur)).json();
          if(j.gone){current=null;await refresh();}
          else{if(j.data)term.write(j.data);cur=j.cursor;}
        }catch(e){}
      }
      pollTimer=setTimeout(loop,200);
    })();
  }
  sel.onchange=()=>attach(sel.value);
  body.querySelector('[data-new]').onclick=async()=>{
    const j=await (await fetch('/fm/term_new',{method:'POST'})).json();
    await refresh();sel.value=j.name;attach(j.name);};
  body.querySelector('[data-kill]').onclick=async()=>{ if(!current)return;
    await fetch('/fm/term_close',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:current})});
    const names=await refresh();current=null;
    if(names.length)attach(names[0]);else host.innerHTML='';};
  (async()=>{
    let names=await refresh();
    if(existingName&&names.includes(existingName))attach(existingName);
    else if(names.length)attach(names[0]);
    else{const j=await (await fetch('/fm/term_new',{method:'POST'})).json();await refresh();sel.value=j.name;attach(j.name);}
  })();
}

/* ===== SYSTEM MONITOR ===== */
function openMonitor(){
  const {id,body}=makeWindow('monitor','System Monitor','monitor',480,560);
  body.innerHTML=`<div class="mon">
    <div class="gauge"><div class="lab"><span>CPU</span><span data-cpu>-</span></div>
      <div class="bar"><i data-cpubar style="width:0;background:var(--accent)"></i></div></div>
    <div class="gauge"><div class="lab"><span>Memory</span><span data-mem>-</span></div>
      <div class="bar"><i data-membar style="width:0;background:var(--accent2)"></i></div></div>
    <div class="gauge"><div class="lab"><span>Disk /</span><span data-disk>-</span></div>
      <div class="bar"><i data-diskbar style="width:0;background:var(--teal)"></i></div></div>
    <div class="kv"><span>Hostname</span><span data-host>-</span></div>
    <div class="kv"><span>OS</span><span data-os>-</span></div>
    <div class="kv"><span>Uptime</span><span data-up>-</span></div>
    <div class="kv"><span>Load (1/5/15m)</span><span data-load>-</span></div>
    <div class="kv"><span>Terminals</span><span data-terms>-</span></div>
  </div>`;
  let live=true; wins[id].onClose=()=>{live=false};
  async function upd(){ if(!live)return;
    try{
      const j=await (await fetch('/fm/api/stats')).json();
      const q=s=>body.querySelector(s);
      q('[data-cpu]').textContent=j.cpu+'%'; gsap.to(q('[data-cpubar]'),{width:Math.min(100,j.cpu)+'%',duration:.5});
      q('[data-mem]').textContent=j.mem.pct+'% ('+fmtSize(j.mem.used*1024)+' / '+fmtSize(j.mem.total*1024)+')';
      gsap.to(q('[data-membar]'),{width:j.mem.pct+'%',duration:.5});
      q('[data-disk]').textContent=j.disk.pct+'% ('+fmtSize(j.disk.used)+' / '+fmtSize(j.disk.total)+')';
      gsap.to(q('[data-diskbar]'),{width:j.disk.pct+'%',duration:.5});
      q('[data-host]').textContent=j.host; q('[data-os]').textContent=j.os;
      q('[data-up]').textContent=fmtUptime(j.uptime);
      q('[data-load]').textContent=j.load.map(x=>x.toFixed(2)).join(' / ');
      q('[data-terms]').textContent=j.terminals;
    }catch(e){}
    if(live)setTimeout(upd,2000);
  }
  upd();
}

/* ===== PROCESSES ===== */
function openProcesses(){
  const {id,body}=makeWindow('processes','Processes','cpu',560,540);
  body.innerHTML=`<div class="toolbar"><button class="btn ghost" data-r>${ICO('refresh')} Refresh</button>
    <span style="color:var(--muted);font-size:11px">Top processes by CPU</span></div>
    <div style="overflow:auto;height:calc(100% - 54px)"><table class="ps">
    <thead><tr><th>PID</th><th>Name</th><th>CPU%</th><th>MEM%</th><th></th></tr></thead>
    <tbody data-body></tbody></table></div>`;
  let live=true; wins[id].onClose=()=>{live=false};
  async function load(){
    const j=await (await fetch('/fm/api/procs')).json();
    const tb=body.querySelector('[data-body]');tb.innerHTML='';
    (j.procs||[]).forEach(p=>{
      const tr=document.createElement('tr');
      tr.innerHTML=`<td>${p.pid}</td><td>${esc(p.name)}</td><td>${p.cpu}</td><td>${p.mem}</td>
        <td><button class="btn ghost" style="padding:3px 9px" data-k="${p.pid}">${ICO('close')}</button></td>`;
      tb.appendChild(tr);
    });
    tb.querySelectorAll('[data-k]').forEach(b=>b.onclick=async()=>{
      if(!confirm('Kill PID '+b.dataset.k+'?'))return;
      await fetch('/fm/api/kill',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({pid:b.dataset.k})});
      load();
    });
  }
  body.querySelector('[data-r]').onclick=load;
  (function loop(){ if(!live)return; load(); setTimeout(loop,4000); })();
}

/* ===== NOTES ===== */
function openNotes(){
  const {body}=makeWindow('notes','Notes','note',420,420);
  const saved=localStorage.getItem('runx_notes')||'';
  body.innerHTML=`<textarea class="notepad" placeholder="Quick notes (saved locally in your browser)...">${esc(saved)}</textarea>`;
  const ta=body.querySelector('.notepad');
  ta.addEventListener('input',()=>localStorage.setItem('runx_notes',ta.value));
}

/* ===== CALCULATOR ===== */
function openCalc(){
  const {body}=makeWindow('calc','Calculator','calc',300,420);
  body.innerHTML=`<div class="calc">
    <div class="disp" data-disp>0</div>
    <div class="keys">
      <button data-k="C" class="op">C</button><button data-k="(" class="op">(</button>
      <button data-k=")" class="op">)</button><button data-k="/" class="op">/</button>
      <button data-k="7">7</button><button data-k="8">8</button><button data-k="9">9</button><button data-k="*" class="op">*</button>
      <button data-k="4">4</button><button data-k="5">5</button><button data-k="6">6</button><button data-k="-" class="op">-</button>
      <button data-k="1">1</button><button data-k="2">2</button><button data-k="3">3</button><button data-k="+" class="op">+</button>
      <button data-k="0">0</button><button data-k=".">.</button>
      <button data-k="=" class="eq">=</button>
    </div></div>`;
  const disp=body.querySelector('[data-disp]');let expr='';
  body.querySelectorAll('.keys button').forEach(b=>b.onclick=()=>{
    const k=b.dataset.k;
    if(k==='C'){expr='';disp.textContent='0';return;}
    if(k==='='){
      try{ if(!/^[0-9+\-*/(). ]+$/.test(expr))throw 0;
        const r=Function('"use strict";return('+expr+')')();
        disp.textContent=r;expr=String(r);
      }catch(e){disp.textContent='Error';expr='';}
      return;
    }
    expr+=k;disp.textContent=expr;
  });
}

/* ===== GPU ===== */
async function openGpu(){
  const {body}=makeWindow('gpu','GPU Info','gpu',700,440);
  body.innerHTML=`<div class="toolbar"><button class="btn ghost" data-r>${ICO('refresh')} Refresh</button></div>
    <pre class="viewer">Loading...</pre>`;
  async function load(){body.querySelector('.viewer').textContent='Loading...';
    const j=await (await fetch('/fm/api/gpu')).json();
    body.querySelector('.viewer').textContent=j.output||'No GPU info available.';}
  body.querySelector('[data-r]').onclick=load;load();
}

/* ===== NETWORK ===== */
async function openNetwork(){
  const {body}=makeWindow('network','Network','net',700,460);
  body.innerHTML=`<div class="toolbar"><button class="btn ghost" data-r>${ICO('refresh')} Refresh</button></div>
    <pre class="viewer">Loading...</pre>`;
  async function load(){body.querySelector('.viewer').textContent='Loading...';
    const j=await (await fetch('/fm/api/net')).json();
    body.querySelector('.viewer').textContent=j.output||'No network info.';}
  body.querySelector('[data-r]').onclick=load;load();
}

/* ===== ABOUT ===== */
function openAbout(){
  const {body}=makeWindow('about','About RUN X OS','info',440,340);
  body.innerHTML=`<div style="padding:28px;text-align:center">
    <img src="/fm/logo" style="height:70px;width:70px;border-radius:16px">
    <h2 style="margin:16px 0 4px">RUN <span style="color:var(--accent)">X</span> OS</h2>
    <p style="color:var(--muted);margin:4px 0 18px">Web desktop for your VPS</p>
    <div style="text-align:left;font-size:13px;line-height:2;max-width:290px;margin:0 auto;color:#cfd6e2">
      &bull; Persistent terminals (survive reload)<br>
      &bull; Full file manager + editor<br>
      &bull; Live system monitor &amp; process manager<br>
      &bull; Notes, calculator, GPU &amp; network tools<br>
    </div></div>`;
  gsap.from(body.querySelector('img'),{scale:0,rotate:-30,duration:.5,ease:'back.out(2)'});
}

/* ===== helpers ===== */
function esc(s){return (s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
function fmtSize(b){b=+b||0;const u=['B','KB','MB','GB','TB'];let i=0;while(b>=1024&&i<u.length-1){b/=1024;i++}return b.toFixed(i?1:0)+' '+u[i];}
function fmtUptime(s){s=+s||0;const d=Math.floor(s/86400),h=Math.floor(s%86400/3600),m=Math.floor(s%3600/60);
  return (d?d+'d ':'')+(h?h+'h ':'')+m+'m';}

/* ===== BOOT ===== */
let booted=false;
function finishBoot(){
  if(booted)return; booted=true;
  const b=document.getElementById('boot');
  gsap.to(b,{opacity:0,duration:.6,onComplete:()=>{b.style.display='none';}});
  gsap.fromTo('#bglogo img',{scale:1.2,opacity:0},{scale:1,opacity:.10,duration:1.4,ease:'power2.out'});
  gsap.fromTo('#taskbar',{y:80,opacity:0},{y:0,opacity:1,duration:.6,delay:.2,ease:'back.out(1.4)'});
}
(function initBoot(){
  const v=document.getElementById('bootvid');
  let done=false;
  const go=()=>{if(!done){done=true;finishBoot();}};
  if(v){
    v.addEventListener('ended',go);
    v.addEventListener('error',go);       // no video file -> skip straight in
    v.play().catch(go);                   // autoplay blocked -> skip
    setTimeout(go,8000);                  // hard cap: never hang on boot
  }else go();
})();

/* ===== BOOT UI ===== */
buildDesktop();
fetch('/fm/api/stats').then(r=>r.json()).then(j=>{
  document.getElementById('host').textContent=j.host+' \u00b7 '+j.os;
  document.getElementById('netinfo').textContent=j.ip||'';
}).catch(()=>{});
</script>
</body></html>"""
# ================= ROUTES: shell / logo / video =================
@app.route("/")
def desktop():
    return Response(DESKTOP)

@app.route("/logo")
def logo():
    for cand in ("run.png", "static/run.png"):
        p = os.path.join(BASE_DIR, cand)
        if os.path.isfile(p):
            return send_file(p)
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="128" height="128">'
           '<rect rx="28" width="128" height="128" fill="#F46821"/>'
           '<text x="64" y="88" font-size="76" font-family="sans-serif" '
           'fill="#fff" text-anchor="middle" font-weight="bold">R</text></svg>')
    return Response(svg, mimetype="image/svg+xml")

@app.route("/video")
def video():
    for cand in ("run.mp4", "static/run.mp4"):
        p = os.path.join(BASE_DIR, cand)
        if os.path.isfile(p):
            return send_file(p, mimetype="video/mp4", conditional=True)
    return Response("", status=404)

# ================= TERMINAL API =================
@app.route("/term_list")
def term_list():
    return jsonify({"sessions": list_shells()})

@app.route("/term_new", methods=["POST"])
def term_new():
    return jsonify({"name": create_shell()})

@app.route("/term_close", methods=["POST"])
def term_close():
    name = request.get_json(force=True).get("name")
    sh = _shells.get(name)
    if sh:
        sh.kill()
        _shells.pop(name, None)
    return jsonify({"ok": True})

@app.route("/term_snapshot")
def term_snapshot():
    sh = get_shell(request.args.get("name", ""))
    if not sh:
        return jsonify({"data": "", "cursor": 0, "gone": True})
    data, cursor = sh.snapshot()
    return jsonify({"data": data, "cursor": cursor})

@app.route("/term_in", methods=["POST"])
def term_in():
    d = request.get_json(force=True)
    sh = get_shell(d.get("name", ""))
    if sh:
        sh.write(d.get("data", ""))
    return jsonify({"ok": bool(sh)})

@app.route("/term_out")
def term_out():
    sh = get_shell(request.args.get("name", ""))
    if not sh:
        return jsonify({"gone": True})
    cursor = int(request.args.get("cursor", 0))
    data, newcur = sh.read_from(cursor)
    return jsonify({"data": data, "cursor": newcur})

@app.route("/term_resize", methods=["POST"])
def term_resize():
    d = request.get_json(force=True)
    sh = get_shell(d.get("name", ""))
    if sh:
        sh.resize(int(d.get("rows", 40)), int(d.get("cols", 120)))
    return jsonify({"ok": bool(sh)})

# ================= FILE MANAGER API =================
@app.route("/api/list")
def api_list():
    path = os.path.abspath(request.args.get("path", "/"))
    if not os.path.exists(path):
        return jsonify({"error": f"Not found: {path}"})
    if os.path.isfile(path):
        path = os.path.dirname(path)
    try:
        names = sorted(os.listdir(path))
    except PermissionError:
        return jsonify({"error": f"Permission denied: {path}"})
    except OSError as e:
        return jsonify({"error": str(e)})
    entries = []
    for name in names:
        full = os.path.join(path, name)
        try:
            isdir = os.path.isdir(full)
            size = 0 if isdir else os.path.getsize(full)
        except OSError:
            isdir, size = False, 0
        entries.append({"name": name, "path": full, "dir": isdir, "size": size})
    entries.sort(key=lambda e: (not e["dir"], e["name"].lower()))
    return jsonify({"path": path, "entries": entries})

@app.route("/api/read")
def api_read():
    path = os.path.abspath(request.args.get("path", ""))
    if not os.path.isfile(path):
        return jsonify({"error": "not a file"})
    if is_sensitive(os.path.basename(path)):
        return jsonify({"content": "*** masked (sensitive filename) ***"})
    try:
        with open(path, "r", errors="replace") as f:
            return jsonify({"content": mask_secrets(f.read(200000))})
    except Exception as e:
        return jsonify({"error": f"cannot read as text: {e}"})

@app.route("/raw")
def raw():
    path = os.path.abspath(request.args.get("path", ""))
    if not os.path.isfile(path):
        return Response("not found", status=404)
    if is_sensitive(os.path.basename(path)):
        return Response("masked", status=403)
    mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
    return send_file(path, mimetype=mime)

@app.route("/api/save", methods=["POST"])
def api_save():
    d = request.get_json(force=True)
    path = os.path.abspath(d.get("path", ""))
    if not path:
        return jsonify({"ok": False, "error": "no path"})
    try:
        with open(path, "w") as f:
            f.write(d.get("content", ""))
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/api/mkdir", methods=["POST"])
def api_mkdir():
    d = request.get_json(force=True)
    base = os.path.abspath(d.get("path", "/"))
    name = (d.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "no name"})
    try:
        os.makedirs(os.path.join(base, name), exist_ok=True)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/api/touch", methods=["POST"])
def api_touch():
    d = request.get_json(force=True)
    base = os.path.abspath(d.get("path", "/"))
    name = (d.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "no name"})
    full = os.path.join(base, name)
    try:
        if not os.path.exists(full):
            open(full, "a").close()
        return jsonify({"ok": True, "path": full})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/api/rename", methods=["POST"])
def api_rename():
    d = request.get_json(force=True)
    path = os.path.abspath(d.get("path", ""))
    newname = (d.get("name") or "").strip()
    if not path or not newname:
        return jsonify({"ok": False, "error": "missing args"})
    target = os.path.join(os.path.dirname(path), newname)
    try:
        os.rename(path, target)
        return jsonify({"ok": True, "path": target})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/api/delete", methods=["POST"])
def api_delete():
    d = request.get_json(force=True)
    path = os.path.abspath(d.get("path", ""))
    try:
        shutil.rmtree(path) if os.path.isdir(path) else os.remove(path)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/upload", methods=["POST"])
def upload():
    base = os.path.abspath(request.form.get("path", "/"))
    f = request.files.get("file")
    if f and f.filename:
        try:
            f.save(os.path.join(base, f.filename))
        except Exception as e:
            return Response(f"upload failed: {e}", status=500)
    return jsonify({"ok": True})

@app.route("/download")
def download():
    path = os.path.abspath(request.args.get("path", ""))
    if is_sensitive(os.path.basename(path)):
        return Response("masked (sensitive filename)", status=403)
    if not os.path.isfile(path):
        return Response("not found", status=404)
    return send_file(path, as_attachment=True)

# ================= SYSTEM API =================
@app.route("/api/stats")
def api_stats():
    host = socket.gethostname()
    try:
        ip = socket.gethostbyname(host)
    except Exception:
        ip = ""
    return jsonify({
        "cpu": read_cpu(), "mem": read_mem(), "disk": read_disk(),
        "uptime": read_uptime(), "load": list(read_loadavg()),
        "host": host, "ip": ip,
        "os": f"{platform.system()} {platform.release()}",
        "terminals": len(list_shells()),
    })

@app.route("/api/procs")
def api_procs():
    return jsonify({"procs": list_processes()})

@app.route("/api/kill", methods=["POST"])
def api_kill():
    pid = request.get_json(force=True).get("pid")
    try:
        os.kill(int(pid), signal.SIGTERM)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/api/gpu")
def api_gpu():
    try:
        out = subprocess.check_output(["nvidia-smi"], stderr=subprocess.STDOUT, timeout=5).decode(errors="replace")
    except Exception as e:
        out = f"nvidia-smi unavailable: {e}"
    return jsonify({"output": out})

@app.route("/api/net")
def api_net():
    parts = []
    for cmd in (["ip", "addr"], ["ss", "-tulnp"]):
        try:
            parts.append("$ " + " ".join(cmd) + "\n" +
                         subprocess.check_output(cmd, stderr=subprocess.STDOUT, timeout=5).decode(errors="replace"))
        except Exception as e:
            parts.append("$ " + " ".join(cmd) + f"\n[unavailable: {e}]")
    return jsonify({"output": "\n\n".join(parts)})

# legacy links -> bounce to desktop
@app.route("/reload")
def reload_route():
    return redirect("/fm/")

@app.route("/gpuinfo")
def gpuinfo():
    return redirect("/fm/")

# ================= ENTRYPOINT =================
if __name__ == "__main__":
    wrapped = DispatcherMiddleware(Flask("empty"), {"/fm": app})
    run_simple("0.0.0.0", 9005, wrapped, threaded=True)