import calendar
import ctypes
import ctypes.wintypes as wt
import glob
import json
import os
import re
import struct
import sys
import time
import urllib.request
import uuid

CLIENT_ID = os.environ.get("VOLT_RPC_CLIENT_ID", "1565304926945550417")
LARGE_IMAGE = "https://noic.fun/sChkZuMk.png"
LARGE_TEXT = "Volt"
SHOW_GAME_BUTTON = True
VOLT_BUTTON = True
VOLT_URL = "https://voltbz.net/"
WINDOW_TITLE = "Volt"
PROCESS_NAME = "tauri-app.exe"
ROBLOX_PROCESS = "RobloxPlayerBeta.exe"
POLL_SECONDS = 5
IDLE_AFTER = 300

LOCALAPPDATA = os.environ["LOCALAPPDATA"]
VOLT_STORAGE = os.path.join(LOCALAPPDATA, r"com.volt.editor\EBWebView\Default\Local Storage\leveldb")
ROBLOX_LOGS = os.path.join(LOCALAPPDATA, r"Roblox\logs")

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

TH32CS_SNAPPROCESS = 0x2


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wt.DWORD), ("cntUsage", wt.DWORD), ("th32ProcessID", wt.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wt.DWORD),
        ("cntThreads", wt.DWORD), ("th32ParentProcessID", wt.DWORD),
        ("pcPriClassBase", ctypes.c_long), ("dwFlags", wt.DWORD),
        ("szExeFile", wt.WCHAR * 260),
    ]


kernel32.CreateToolhelp32Snapshot.restype = wt.HANDLE


def pids_named(name):
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    pids = set()
    entry = PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(entry)
    ok = kernel32.Process32FirstW(snap, ctypes.byref(entry))
    while ok:
        if entry.szExeFile.lower() == name.lower():
            pids.add(entry.th32ProcessID)
        ok = kernel32.Process32NextW(snap, ctypes.byref(entry))
    kernel32.CloseHandle(snap)
    return pids


EnumWindowsProc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)


def volt_window():
    pids = pids_named(PROCESS_NAME)
    if not pids:
        return None
    found = None

    def cb(hwnd, _):
        nonlocal found
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in pids:
            buf = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(hwnd, buf, 256)
            if buf.value == WINDOW_TITLE:
                found = hwnd
                return False
        return True

    user32.EnumWindows(EnumWindowsProc(cb), 0)
    return found


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.UINT), ("dwTime", wt.DWORD)]


kernel32.GetTickCount.restype = wt.DWORD


def idle_seconds():
    info = LASTINPUTINFO(ctypes.sizeof(LASTINPUTINFO))
    if not user32.GetLastInputInfo(ctypes.byref(info)):
        return 0
    return ((kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF) / 1000


def volt_status(hwnd):
    idle = idle_seconds()
    if idle >= IDLE_AFTER:
        return "idle", idle
    if user32.IsIconic(hwnd):
        return "minimized", 0
    return "active", 0


LAYOUT_KEY = b"\x00\x01volt.workbenchLayout.v1"


def _varint(b, i):
    r = s = 0
    while True:
        c = b[i]
        i += 1
        r |= (c & 0x7F) << s
        s += 7
        if c < 0x80:
            return r, i


def _log_records(data):
    out, buf, i = [], b"", 0
    while i + 7 <= len(data):
        left = 32768 - (i % 32768)
        if left < 7:
            i += left
            continue
        length, typ = struct.unpack_from("<HB", data, i + 4)
        frag = data[i + 7:i + 7 + length]
        i += 7 + length
        if typ == 1:
            out.append(frag)
        elif typ == 2:
            buf = frag
        elif typ == 3:
            buf += frag
        elif typ == 4:
            out.append(buf + frag)
            buf = b""
        else:
            break
    return out


def _latest_layout():
    latest = None
    for path in sorted(glob.glob(os.path.join(VOLT_STORAGE, "*.log"))):
        with open(path, "rb") as f:
            data = f.read()
        for rec in _log_records(data):
            try:
                count, = struct.unpack_from("<I", rec, 8)
                i = 12
                for _ in range(count):
                    tag = rec[i]
                    i += 1
                    kl, i = _varint(rec, i)
                    key = rec[i:i + kl]
                    i += kl
                    if tag == 1:
                        vl, i = _varint(rec, i)
                        if key.endswith(LAYOUT_KEY):
                            latest = rec[i:i + vl]
                        i += vl
            except (IndexError, struct.error):
                continue
    if not latest:
        return None
    text = latest[1:].decode("latin-1") if latest[0] == 1 else latest[1:].decode("utf-16-le")
    return json.loads(text)


LANGUAGES = {
    "lua": "Lua", "luau": "Luau", "txt": "Text", "json": "JSON", "js": "JavaScript",
    "ts": "TypeScript", "py": "Python", "md": "Markdown", "html": "HTML", "css": "CSS",
}

_layout_cache = {"sig": None, "info": None}
_lines_cache = {}


def volt_active_file():
    try:
        sig = tuple((p, os.path.getmtime(p), os.path.getsize(p))
                    for p in glob.glob(os.path.join(VOLT_STORAGE, "*.log")))
        if sig != _layout_cache["sig"]:
            _layout_cache["sig"] = sig
            _layout_cache["info"] = _active_tab(_latest_layout())
    except (OSError, ValueError):
        _layout_cache["info"] = None
    info = _layout_cache["info"]
    if info and info.get("source"):
        info["lines"] = _line_count(info["source"])
    return info


def _line_count(path):
    try:
        key = (os.path.getmtime(path), os.path.getsize(path))
    except OSError:
        return None
    cached = _lines_cache.get(path)
    if cached and cached[0] == key:
        return cached[1]
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return None
    lines = data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)
    _lines_cache[path] = (key, lines)
    return lines


def _language(tab):
    ext = os.path.splitext(tab.get("path") or tab.get("title") or "")[1].lstrip(".").lower()
    lang = ext if ext in LANGUAGES else (tab.get("language") or ext)
    return LANGUAGES.get(lang, lang.capitalize() if lang else None)


def _active_tab(layout):
    if not layout:
        return None
    groups = []

    def walk(node):
        if "paneGroup" in node:
            groups.append(node["paneGroup"])
        for child in node.get("children", []):
            walk(child)

    walk(layout["root"])
    group = next((g for g in groups if g.get("id") == layout.get("activeGroupId")), groups[0] if groups else None)
    if not group:
        return None
    tabs = [t for g in groups for t in g.get("tabs", []) if t.get("kind") != "welcome"]
    info = {"tabs": len(tabs)}
    tab = next((t for t in group["tabs"] if t.get("id") == group.get("activeTabId")), None)
    if not tab or tab.get("kind") == "welcome":
        return info
    info.update(
        title=tab.get("title"),
        language=_language(tab),
        unsaved=bool(tab.get("isDirty")) and tab.get("kind") == "file",
        source=tab.get("path") or tab.get("untitledTempPath"),
    )
    return info


JOIN_RE = re.compile(r"! Joining game '[^']*' place (\d+)")
UNIVERSE_RE = re.compile(r"universeid:(\d+)")
LEAVE_RE = re.compile(r"Disconnected from server|leaveUGCGameInternal")

_roblox = {"path": None, "offset": 0, "place": None, "universe": None, "joined": None}
_game_info = {}


def _log_time(line):
    try:
        return calendar.timegm(time.strptime(line[:19], "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return time.time()


def _newest_roblox_log():
    logs = [p for p in glob.glob(os.path.join(ROBLOX_LOGS, "*_Player_*.log")) if "CrashHandler" not in p]
    return max(logs, key=os.path.getmtime) if logs else None


def roblox_game():
    if not pids_named(ROBLOX_PROCESS):
        _roblox.update(path=None, offset=0, place=None, universe=None, joined=None)
        return None
    path = _newest_roblox_log()
    if not path:
        return None
    if path != _roblox["path"]:
        _roblox.update(path=path, offset=0, place=None, universe=None, joined=None)
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        f.seek(_roblox["offset"])
        chunk = f.read()
        _roblox["offset"] = f.tell()
    for line in chunk.splitlines():
        m = JOIN_RE.search(line)
        if m:
            _roblox["place"], _roblox["universe"] = m.group(1), None
            _roblox["joined"] = _log_time(line)
            continue
        m = UNIVERSE_RE.search(line)
        if m and _roblox["place"]:
            _roblox["universe"] = m.group(1)
            continue
        if LEAVE_RE.search(line):
            _roblox["place"] = _roblox["universe"] = _roblox["joined"] = None
    if not _roblox["place"]:
        return None
    return _roblox["place"], _roblox["universe"], _roblox["joined"] or time.time()


def _get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "VoltRPC"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.load(r)


GAME_INFO_TTL = 300


def game_info(universe_id):
    cached = _game_info.get(universe_id)
    if cached and time.time() - cached[0] < GAME_INFO_TTL:
        return cached[1]
    info = dict(cached[1]) if cached else {}
    try:
        game = _get_json(f"https://games.roblox.com/v1/games?universeIds={universe_id}")["data"][0]
        info.update(
            name=game.get("name"),
            creator=(game.get("creator") or {}).get("name"),
            playing=game.get("playing"),
        )
        if "icon" not in info:
            info["icon"] = _get_json(
                "https://thumbnails.roblox.com/v1/games/icons?universeIds="
                f"{universe_id}&size=512x512&format=Png&isCircular=false&returnPolicy=PlaceHolder"
            )["data"][0]["imageUrl"]
    except (OSError, ValueError, KeyError, IndexError, TypeError):
        pass
    if info.get("name"):
        _game_info[universe_id] = (time.time(), info)
    return info


def _compact(n):
    for size, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if n >= size:
            return f"{n / size:.1f}".rstrip("0").rstrip(".") + suffix
    return str(n)


def _duration(seconds):
    minutes = int(seconds // 60)
    if minutes < 1:
        return "just joined"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m" if hours else f"{minutes}m"


class DiscordIPC:
    def __init__(self, client_id):
        self.client_id = client_id
        self.pipe = None

    def connect(self):
        for i in range(10):
            try:
                self.pipe = open(rf"\\.\pipe\discord-ipc-{i}", "r+b", buffering=0)
                break
            except OSError:
                continue
        else:
            raise ConnectionError("Discord isn't running (no IPC pipe found)")
        self._send(0, {"v": 1, "client_id": self.client_id})
        op, data = self._recv()
        if data.get("evt") != "READY":
            raise ConnectionError(f"Discord handshake failed: {data}")

    def _send(self, op, payload):
        body = json.dumps(payload).encode()
        self.pipe.write(struct.pack("<II", op, len(body)) + body)

    def _recv(self):
        op, length = struct.unpack("<II", self.pipe.read(8))
        return op, json.loads(self.pipe.read(length))

    def set_activity(self, activity):
        self._send(1, {
            "cmd": "SET_ACTIVITY",
            "args": {"pid": os.getpid(), "activity": activity},
            "nonce": str(uuid.uuid4()),
        })
        op, data = self._recv()
        if data.get("evt") == "ERROR":
            print(f"Discord rejected the presence: {data.get('data')}")
        return data

    def close(self):
        if self.pipe:
            try:
                self.pipe.close()
            except OSError:
                pass
            self.pipe = None


def _join(*parts):
    return " • ".join(p for p in parts if p)


def build_activity(start, tab, game, status=("active", 0)):
    tab = tab or {}
    title = tab.get("title")
    lines = tab.get("lines")
    tabs = tab.get("tabs") or 0
    tab_text = f"{tabs} tab{'s' if tabs != 1 else ''} open" if tabs else None
    mode, idle = status

    if title:
        details = _join(f"Editing {title}", f"{lines:,} line{'s' if lines != 1 else ''}" if lines else None)
        if tab.get("unsaved"):
            details += " (unsaved)"
    else:
        details = "On the home screen"
    large_text = _join(LARGE_TEXT, tab.get("language"), tab_text)
    if mode == "idle":
        large_text = _join(LARGE_TEXT, details)
        details = f"Idling • AFK {_duration(idle)}" if idle >= 60 else "Idling"
    elif mode == "minimized":
        details = f"Minimized • {details}"
    activity = {"details": details[:128], "timestamps": {"start": start}}

    assets = {}
    if LARGE_IMAGE:
        assets.update(large_image=LARGE_IMAGE, large_text=large_text[:128])
    buttons = []
    if game:
        place, universe, joined = game
        info = game_info(universe) if universe else {}
        name = info.get("name")
        activity["state"] = _join(f"Playing {name}" if name else "In a Roblox game", _duration(time.time() - joined))[:128]
        if info.get("icon"):
            playing = info.get("playing")
            assets.update(small_image=info["icon"], small_text=_join(
                name,
                f"by {info['creator']}" if info.get("creator") else None,
                f"{_compact(playing)} playing" if playing else None,
            )[:128])
        if SHOW_GAME_BUTTON:
            buttons.append({"label": "View game", "url": f"https://www.roblox.com/games/{place}"})
    else:
        activity["state"] = _join(tab.get("language"), tab_text) or "Idle"
    if VOLT_BUTTON:
        buttons.append({"label": "Get Volt", "url": VOLT_URL})
    if buttons:
        activity["buttons"] = buttons
    if assets:
        activity["assets"] = assets
    return activity


def main():
    if not CLIENT_ID.isdigit():
        sys.exit("Set CLIENT_ID (or the VOLT_RPC_CLIENT_ID env var) to your Discord application ID.")

    ipc = DiscordIPC(CLIENT_ID)
    current = None
    start = None
    print("Volt RPC running. Ctrl+C to stop.")
    while True:
        try:
            hwnd = volt_window()
            if hwnd:
                start = start or int(time.time())
                activity = build_activity(start, volt_active_file(), roblox_game(), volt_status(hwnd))
                if activity != current:
                    if ipc.pipe is None:
                        ipc.connect()
                    ipc.set_activity(activity)
                    current = activity
                    print(f"Presence: {activity['details']} | {activity.get('state', '-')} | "
                          f"{activity.get('assets', {}).get('large_text', '-')} | "
                          f"{activity.get('assets', {}).get('small_text', '-')}")
            elif current is not None:
                ipc.set_activity(None)
                current = start = None
                print("Volt closed - presence cleared.")
        except (OSError, ConnectionError, struct.error) as e:
            print(f"Discord connection issue: {e} - retrying...")
            ipc.close()
            current = None
        except Exception as e:
            print(f"Unexpected error: {e!r} - retrying...")
            current = None
        sys.stdout.flush()
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    if sys.stdout is None:
        sys.stdout = sys.stderr = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "volt_rpc.log"), "a", buffering=1, encoding="utf-8", errors="replace")
    try:
        main()
    except KeyboardInterrupt:
        pass
