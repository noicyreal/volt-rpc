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
WINDOW_TITLE = "Volt"
PROCESS_NAME = "tauri-app.exe"
ROBLOX_PROCESS = "RobloxPlayerBeta.exe"
POLL_SECONDS = 5

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


def volt_running():
    pids = pids_named(PROCESS_NAME)
    if not pids:
        return False
    found = False

    def cb(hwnd, _):
        nonlocal found
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in pids:
            buf = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(hwnd, buf, 256)
            if buf.value == WINDOW_TITLE:
                found = True
                return False
        return True

    user32.EnumWindows(EnumWindowsProc(cb), 0)
    return found


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


_layout_cache = {"sig": None, "title": None}


def volt_active_file():
    try:
        sig = tuple((p, os.path.getmtime(p), os.path.getsize(p))
                    for p in glob.glob(os.path.join(VOLT_STORAGE, "*.log")))
        if sig != _layout_cache["sig"]:
            _layout_cache["sig"] = sig
            _layout_cache["title"] = _active_title(_latest_layout())
    except (OSError, ValueError):
        _layout_cache["title"] = None
    return _layout_cache["title"]


def _active_title(layout):
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
    tab = next((t for t in group["tabs"] if t.get("id") == group.get("activeTabId")), None)
    if not tab or tab.get("kind") == "welcome":
        return None
    return tab.get("title")


JOIN_RE = re.compile(r"! Joining game '[^']*' place (\d+)")
UNIVERSE_RE = re.compile(r"universeid:(\d+)")
LEAVE_RE = re.compile(r"Disconnected from server|leaveUGCGameInternal")

_roblox = {"path": None, "offset": 0, "place": None, "universe": None}
_game_info = {}


def _newest_roblox_log():
    logs = [p for p in glob.glob(os.path.join(ROBLOX_LOGS, "*_Player_*.log")) if "CrashHandler" not in p]
    return max(logs, key=os.path.getmtime) if logs else None


def roblox_game():
    if not pids_named(ROBLOX_PROCESS):
        _roblox.update(path=None, offset=0, place=None, universe=None)
        return None
    path = _newest_roblox_log()
    if not path:
        return None
    if path != _roblox["path"]:
        _roblox.update(path=path, offset=0, place=None, universe=None)
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        f.seek(_roblox["offset"])
        chunk = f.read()
        _roblox["offset"] = f.tell()
    for line in chunk.splitlines():
        m = JOIN_RE.search(line)
        if m:
            _roblox["place"], _roblox["universe"] = m.group(1), None
            continue
        m = UNIVERSE_RE.search(line)
        if m and _roblox["place"]:
            _roblox["universe"] = m.group(1)
            continue
        if LEAVE_RE.search(line):
            _roblox["place"] = _roblox["universe"] = None
    if not _roblox["place"]:
        return None
    return _roblox["place"], _roblox["universe"]


def _get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "VoltRPC"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.load(r)


def game_info(universe_id):
    if universe_id in _game_info:
        return _game_info[universe_id]
    name, icon = None, None
    try:
        name = _get_json(f"https://games.roblox.com/v1/games?universeIds={universe_id}")["data"][0]["name"]
        icon = _get_json(
            "https://thumbnails.roblox.com/v1/games/icons?universeIds="
            f"{universe_id}&size=512x512&format=Png&isCircular=false&returnPolicy=PlaceHolder"
        )["data"][0]["imageUrl"]
    except (OSError, ValueError, KeyError, IndexError):
        pass
    if name:
        _game_info[universe_id] = (name, icon)
    return name, icon


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


def build_activity(start, file_title, game):
    activity = {
        "details": f"Editing {file_title}"[:128] if file_title else "Using Volt",
        "timestamps": {"start": start},
    }
    assets = {}
    if LARGE_IMAGE:
        assets.update(large_image=LARGE_IMAGE, large_text=LARGE_TEXT)
    if game:
        place, universe = game
        name, icon = game_info(universe) if universe else (None, None)
        activity["state"] = f"Playing {name}"[:128] if name else "In a Roblox game"
        if icon:
            assets.update(small_image=icon, small_text=name)
        if SHOW_GAME_BUTTON:
            activity["buttons"] = [{"label": "View game", "url": f"https://www.roblox.com/games/{place}"}]
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
            if volt_running():
                start = start or int(time.time())
                activity = build_activity(start, volt_active_file(), roblox_game())
                if activity != current:
                    if ipc.pipe is None:
                        ipc.connect()
                    ipc.set_activity(activity)
                    current = activity
                    print(f"Presence: {activity['details']} | {activity.get('state', '-')}")
            elif current is not None:
                ipc.set_activity(None)
                current = start = None
                print("Volt closed - presence cleared.")
        except (OSError, ConnectionError, struct.error) as e:
            print(f"Discord connection issue: {e} - retrying...")
            ipc.close()
            current = None
        sys.stdout.flush()
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    if sys.stdout is None:
        sys.stdout = sys.stderr = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "volt_rpc.log"), "a", buffering=1)
    try:
        main()
    except KeyboardInterrupt:
        pass
