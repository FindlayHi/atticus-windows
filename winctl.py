"""Windows side of Atticus's hands: displays, windows, apps, clipboard, typing, screenshots, files, toasts.
atticus.py calls these where the Mac version ran osascript, mdfind, screencapture or pbpaste. Pure ctypes plus
mss (screenshots), Pillow (resize) and psutil (process trees); every call returns quickly or gives up quietly."""
import ctypes, json, os, re, subprocess, sys, time
from ctypes import wintypes as W

if sys.platform != 'win32':   # tests import atticus on any OS; the functions below only run on Windows
    raise ImportError('winctl is Windows only')

user32, kernel32, dwmapi, version = ctypes.WinDLL('user32', use_last_error=True), ctypes.WinDLL('kernel32', use_last_error=True), ctypes.WinDLL('dwmapi'), ctypes.WinDLL('version')
try:   # physical pixels everywhere, the same numbers SetWindowPos, mss and the overlay use
    user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
except (AttributeError, OSError):
    pass

HOME = os.path.expanduser('~')
NO_WINDOW = 0x08000000   # CREATE_NO_WINDOW: helpers never flash a console


def ps(script, timeout=20):
    """One PowerShell command -> stdout ('' on failure)."""
    try:
        return subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-Command', script],
                              capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=timeout, creationflags=NO_WINDOW).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ''


def bash():
    """Git for Windows' bash: Claude Code's Bash tool runs there, so learned shortcuts replay there too."""
    for d in (os.environ.get('ProgramFiles', r'C:\Program Files'), os.environ.get('LOCALAPPDATA', '') + r'\Programs'):
        p = os.path.join(d, 'Git', 'bin', 'bash.exe')
        if os.path.exists(p):
            return p
    return 'bash'


# ---------- displays ----------

class RECT(ctypes.Structure):
    _fields_ = [('left', W.LONG), ('top', W.LONG), ('right', W.LONG), ('bottom', W.LONG)]


class MONITORINFOEX(ctypes.Structure):
    _fields_ = [('cbSize', W.DWORD), ('rcMonitor', RECT), ('rcWork', RECT), ('dwFlags', W.DWORD), ('szDevice', W.WCHAR * 32)]


class LUID(ctypes.Structure):
    _fields_ = [('LowPart', W.DWORD), ('HighPart', W.LONG)]


class PATH_INFO(ctypes.Structure):   # DISPLAYCONFIG_PATH_INFO, flattened (72 bytes)
    _fields_ = [('srcAdapter', LUID), ('srcId', ctypes.c_uint32), ('srcMode', ctypes.c_uint32), ('srcFlags', ctypes.c_uint32),
                ('tgtAdapter', LUID), ('tgtId', ctypes.c_uint32), ('tgtMode', ctypes.c_uint32), ('tech', ctypes.c_uint32),
                ('rot', ctypes.c_uint32), ('scale', ctypes.c_uint32), ('rate', ctypes.c_uint32 * 2), ('order', ctypes.c_uint32),
                ('avail', W.BOOL), ('tgtFlags', ctypes.c_uint32), ('flags', ctypes.c_uint32)]


class HEADER(ctypes.Structure):
    _fields_ = [('type', ctypes.c_uint32), ('size', ctypes.c_uint32), ('adapter', LUID), ('id', ctypes.c_uint32)]


class SOURCE_NAME(ctypes.Structure):
    _fields_ = [('h', HEADER), ('gdi', W.WCHAR * 32)]


class TARGET_NAME(ctypes.Structure):
    _fields_ = [('h', HEADER), ('flags', ctypes.c_uint32), ('tech', ctypes.c_uint32), ('mfr', ctypes.c_uint16), ('product', ctypes.c_uint16),
                ('connector', ctypes.c_uint32), ('friendly', W.WCHAR * 64), ('path', W.WCHAR * 128)]


INTERNAL = {6, 11, 13, 0x80000000}   # LVDS, embedded DisplayPort, UDI embedded, "internal": a laptop's own panel


def _panels():
    """GDI device name (\\\\.\\DISPLAY1) -> (friendly monitor name, built in?), from the display configuration."""
    out, n_path, n_mode = {}, ctypes.c_uint32(), ctypes.c_uint32()
    try:
        if user32.GetDisplayConfigBufferSizes(2, ctypes.byref(n_path), ctypes.byref(n_mode)):
            return out
        paths, modes = (PATH_INFO * n_path.value)(), (ctypes.c_byte * (64 * max(1, n_mode.value)))()
        if user32.QueryDisplayConfig(2, ctypes.byref(n_path), paths, ctypes.byref(n_mode), modes, None):
            return out
        for p in paths[:n_path.value]:
            s = SOURCE_NAME(HEADER(1, ctypes.sizeof(SOURCE_NAME), p.srcAdapter, p.srcId))
            t = TARGET_NAME(HEADER(2, ctypes.sizeof(TARGET_NAME), p.tgtAdapter, p.tgtId))
            if user32.DisplayConfigGetDeviceInfo(ctypes.byref(s)) == 0:
                user32.DisplayConfigGetDeviceInfo(ctypes.byref(t))
                out[s.gdi] = (t.friendly or '', p.tech in INTERNAL)
    except (AttributeError, OSError):
        pass
    return out


def monitors():
    """[{name, x, y, w, h, work: (x, y, w, h), built_in, primary}], the primary one first. Physical pixels, top-left origin."""
    found = []
    PROC = ctypes.WINFUNCTYPE(W.BOOL, W.HMONITOR, W.HDC, ctypes.POINTER(RECT), W.LPARAM)

    def each(h, _dc, _r, _l):
        mi = MONITORINFOEX(); mi.cbSize = ctypes.sizeof(MONITORINFOEX)
        if user32.GetMonitorInfoW(h, ctypes.byref(mi)):
            found.append(mi)
        return True
    user32.EnumDisplayMonitors(None, None, PROC(each), 0)
    names, out, seen = _panels(), [], {}
    for mi in found:
        friendly, built_in = names.get(mi.szDevice, ('', False))
        name = friendly or ('Built-in Display' if built_in else 'Display')
        seen[name] = seen.get(name, 0) + 1
        m, w = mi.rcMonitor, mi.rcWork
        out.append({'name': name if seen[name] == 1 else f'{name} {seen[name]}', 'x': m.left, 'y': m.top, 'w': m.right - m.left, 'h': m.bottom - m.top,
                    'work': (w.left, w.top, w.right - w.left, w.bottom - w.top), 'built_in': built_in, 'primary': bool(mi.dwFlags & 1)})
    out.sort(key=lambda d: not d['primary'])
    if out and not any(d['built_in'] for d in out) and len(out) > 1 and laptop():   # no EDID answer: a laptop's panel is usually the primary
        out[0]['built_in'] = True
    return out


def laptop():
    class SPS(ctypes.Structure):
        _fields_ = [('ac', ctypes.c_byte), ('flag', ctypes.c_byte), ('pct', ctypes.c_byte), ('saver', ctypes.c_byte), ('life', W.DWORD), ('full', W.DWORD)]
    s = SPS()
    return bool(kernel32.GetSystemPowerStatus(ctypes.byref(s))) and s.flag != -128 and (s.flag & 128) == 0   # 128 = no battery


# ---------- processes and windows ----------

user32.GetForegroundWindow.restype = W.HWND
user32.GetWindowThreadProcessId.argtypes = [W.HWND, ctypes.POINTER(W.DWORD)]
user32.SetWindowPos.argtypes = [W.HWND, W.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
user32.GetWindowTextW.argtypes = [W.HWND, W.LPWSTR, ctypes.c_int]
_desc = {}


def exe_of(pid):
    h = kernel32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ''
    try:
        buf, n = ctypes.create_unicode_buffer(1024), W.DWORD(1024)
        return buf.value if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)) else ''
    finally:
        kernel32.CloseHandle(h)


def describe_exe(path):
    """The app's own name from its file version info ("Google Chrome" for chrome.exe), else the file name."""
    if path in _desc:
        return _desc[path]
    name = os.path.splitext(os.path.basename(path))[0]
    try:
        size = version.GetFileVersionInfoSizeW(path, None)
        if size:
            data = ctypes.create_string_buffer(size)
            version.GetFileVersionInfoW(path, 0, size, data)
            ptr, ln = ctypes.c_void_p(), ctypes.c_uint()
            if version.VerQueryValueW(data, r'\VarFileInfo\Translation', ctypes.byref(ptr), ctypes.byref(ln)) and ln.value >= 4:
                lang, cp = ctypes.cast(ptr, ctypes.POINTER(ctypes.c_uint16))[0:2]
                if version.VerQueryValueW(data, f'\\StringFileInfo\\{lang:04x}{cp:04x}\\FileDescription', ctypes.byref(ptr), ctypes.byref(ln)) and ln.value > 1:
                    d = ctypes.wstring_at(ptr, ln.value - 1).strip()
                    if d and len(d) < 40 and not re.search(r'\b(host|launcher|helper|runtime|setup)\b', d, re.I):
                        name = d
    except OSError:
        pass
    _desc[path] = name
    return name


def title(hwnd):
    buf = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(hwnd, buf, 512)
    return buf.value


def pid_of(hwnd):
    pid = W.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def real_pid(hwnd):
    """UWP apps (Calculator, Settings…) draw inside ApplicationFrameHost: the app is the child window's process."""
    pid = pid_of(hwnd)
    if not exe_of(pid).lower().endswith('applicationframehost.exe'):
        return pid
    hit = []
    PROC = ctypes.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)
    user32.EnumChildWindows(hwnd, PROC(lambda c, _: (hit.append(pid_of(c)) if pid_of(c) != pid else None) or True), 0)
    return hit[0] if hit else pid


def cloaked(hwnd):
    v = ctypes.c_int()
    dwmapi.DwmGetWindowAttribute(W.HWND(hwnd), 14, ctypes.byref(v), ctypes.sizeof(v))
    return bool(v.value)


def windows():
    """Top-level app windows a person can see, front to back: [{hwnd, title, exe, proc, app}]."""
    hs = []
    PROC = ctypes.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)
    user32.EnumWindows(PROC(lambda h, _: hs.append(h) or True), 0)
    out = []
    for h in hs:
        if not user32.IsWindowVisible(h) or user32.GetWindow(h, 4) or cloaked(h):   # 4 = GW_OWNER: dialogs and popups belong to another window
            continue
        ex = user32.GetWindowLongW(h, -20)
        t = title(h)
        if not t or ex & 0x80 and not ex & 0x40000:   # WS_EX_TOOLWINDOW without WS_EX_APPWINDOW
            continue
        exe = exe_of(real_pid(h))
        if not exe or os.path.basename(exe).lower() in ('textinputhost.exe', 'atticus.exe', 'searchhost.exe', 'startmenuexperiencehost.exe'):
            continue
        out.append({'hwnd': h, 'title': t, 'exe': exe, 'proc': os.path.splitext(os.path.basename(exe))[0], 'app': describe_exe(exe)})
    return out


def key(s):
    return re.sub(r'[^a-z0-9]', '', s.lower().removesuffix('.exe'))


def matches(w, name):
    """A window belongs to `name` when it names the process ("chrome", "Code"), the app ("Google Chrome") or ends its title ("… - Notepad")."""
    k = key(name)
    return bool(k) and (k in (key(w['proc']), key(w['app'])) or key(w['title'].rsplit(' - ', 1)[-1]) == k or
                        any(k == key(a.get('exe', '')) for a in apps() if key(a['name']) == key(w['app'])))


def find(name, words=''):
    words = words.lower()
    return [w for w in windows() if matches(w, name) and (not words or words in w['title'].lower())]


def front():
    """(app name, window title, hwnd) of the window in front."""
    h = user32.GetForegroundWindow()
    if not h:
        return '', '', None
    exe = exe_of(real_pid(h))
    return (describe_exe(exe) if exe else ''), title(h), h


def frame(hwnd):
    """The visible frame (no invisible resize borders): (x, y, w, h)."""
    r = RECT()
    if dwmapi.DwmGetWindowAttribute(W.HWND(hwnd), 9, ctypes.byref(r), ctypes.sizeof(r)):   # DWMWA_EXTENDED_FRAME_BOUNDS
        user32.GetWindowRect(W.HWND(hwnd), ctypes.byref(r))
    return r.left, r.top, r.right - r.left, r.bottom - r.top


def set_frame(hwnd, x, y, w, h):
    """Move so the visible frame lands exactly there: the window rect is a few pixels bigger on each side."""
    r = RECT()
    user32.GetWindowRect(W.HWND(hwnd), ctypes.byref(r))
    fx, fy, fw, fh = frame(hwnd)
    dl, dt, dr, db = fx - r.left, fy - r.top, r.right - (fx + fw), r.bottom - (fy + fh)
    user32.SetWindowPos(hwnd, None, x - dl, y - dt, w + dl + dr, h + dt + db, 0x0014)   # SWP_NOZORDER | SWP_NOACTIVATE


ON_BRING = None   # set by atticus.py: tells Atticus.exe's focus guard this raise is on purpose


def bring(hwnd):
    """Bring a window forward. Windows only lets the app with the last input do that, so borrow the front window's input queue."""
    if ON_BRING:
        ON_BRING()
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)   # SW_RESTORE
    fg = user32.GetForegroundWindow()
    me, them = kernel32.GetCurrentThreadId(), user32.GetWindowThreadProcessId(fg, None) if fg else 0
    attached = them and them != me and user32.AttachThreadInput(me, them, True)
    user32.keybd_event(0xE8, 0, 0, 0); user32.keybd_event(0xE8, 0, 2, 0)   # an unassigned key: counts as input, does nothing
    user32.BringWindowToTop(hwnd)
    ok = user32.SetForegroundWindow(hwnd)
    if attached:
        user32.AttachThreadInput(me, them, False)
    return bool(ok)


def focus_app(name, words=''):
    ws = find(name, words)
    return bool(ws) and bring(ws[0]['hwnd'])


def close(hwnd):
    user32.PostMessageW(hwnd, 0x0010, 0, 0)   # WM_CLOSE: the app can still ask to save


def rect_for(where, area, size):
    """Where a window goes in a work area (x, y, w, h): left/right half, full, or centered at its own size (capped)."""
    x, y, w, h = area
    cw, ch = min(size[0], w), min(size[1], h)
    return {'left': (x, y, w // 2, h), 'right': (x + w // 2, y, w - w // 2, h), 'full': (x, y, w, h),
            'center': (x + (w - cw) // 2, y + (h - ch) // 2, cw, ch)}[where]


def place(name, where='full', screen='current', words=''):
    """Move a window: where = left|right|full|center, screen = laptop|monitor|current|a display's name."""
    if where not in ('left', 'right', 'full', 'center'):
        return 'where must be left, right, full or center'
    words = re.sub(r'\.[a-z0-9]{2,4}$', '', words, flags=re.I)   # "notes.txt" also finds "notes - Notepad"
    for tries in range(8):   # a window just opened (--show right before) can take a moment to appear
        ws = find(name, words)
        if ws:
            break
        time.sleep(0.25)
    else:
        return f'no window found for "{name}" {words}'.strip()
    hwnd = ws[0]['hwnd']
    if user32.IsZoomed(hwnd) or user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)   # maximized windows can't be moved: restore first
    fx, fy, fw, fh = frame(hwnd)
    mons = monitors()
    pick = {'laptop': lambda m: m['built_in'], 'monitor': lambda m: not m['built_in']}.get(
        screen, (lambda m: m['name'].lower() == screen.lower()) if screen != 'current' else
        (lambda m: m['x'] <= fx + fw // 2 < m['x'] + m['w'] and m['y'] <= fy + fh // 2 < m['y'] + m['h']))
    m = next((m for m in mons if pick(m)), mons[0])
    x, y, w, h = rect_for(where, m['work'], (fw, fh))
    set_frame(hwnd, x, y, w, h)
    if where == 'full':
        user32.ShowWindow(hwnd, 3)   # SW_MAXIMIZE, on the display it now sits on
    return f"{ws[0]['app']} window -> {where} of {m['name']}"


# ---------- apps ----------

APPS_FILE = None   # set by atticus.py: <DIR>/apps.json
_apps = None
INDEX = r'''
$sh = New-Object -ComObject WScript.Shell
$lnk = @{}
foreach ($d in @("$env:ProgramData\Microsoft\Windows\Start Menu\Programs", "$env:APPDATA\Microsoft\Windows\Start Menu\Programs")) {
  Get-ChildItem $d -Recurse -Filter *.lnk -ErrorAction SilentlyContinue | ForEach-Object {
    try { $t = $sh.CreateShortcut($_.FullName).TargetPath } catch { $t = '' }
    if ($t -like '*.exe') { $lnk[$_.BaseName] = @{ lnk = $_.FullName; exe = $t } } } }
@(Get-StartApps | ForEach-Object { $l = $lnk[$_.Name]
  [pscustomobject]@{ name = $_.Name; id = $_.AppID; lnk = if ($l) { $l.lnk } else { '' }; exe = if ($l) { [IO.Path]::GetFileNameWithoutExtension($l.exe) } elseif ($_.AppID -like '*.exe') { [IO.Path]::GetFileNameWithoutExtension($_.AppID) } else { '' } } }) | ConvertTo-Json -Compress
'''


def apps(refresh=False):
    """The Start menu's apps: [{name, id (AppUserModelID), lnk, exe}]. Built with PowerShell (~1 s), cached a day."""
    global _apps
    if _apps is not None and not refresh:
        return _apps
    if not refresh and APPS_FILE and os.path.exists(APPS_FILE) and time.time() - os.path.getmtime(APPS_FILE) < 86400:
        try:
            _apps = json.load(open(APPS_FILE, encoding='utf-8'))
            return _apps
        except ValueError:
            pass
    try:
        got = json.loads(ps(INDEX, 40) or '[]')
        _apps = [a for a in (got if isinstance(got, list) else [got]) if a.get('name') and not re.search(r'\b(uninstall|readme|help|documentation|website)\b', a['name'], re.I)]
    except ValueError:
        _apps = []
    if APPS_FILE and _apps:
        os.makedirs(os.path.dirname(APPS_FILE), exist_ok=True)
        json.dump(_apps, open(APPS_FILE, 'w', encoding='utf-8'))
    return _apps


def launch(app):
    """Open a Start-menu app by its exact name. False if there is none."""
    a = next((a for a in apps() if a['name'] == app), None)
    if not a:
        return False
    if a.get('lnk') and os.path.exists(a['lnk']):
        os.startfile(a['lnk'])
    else:
        subprocess.Popen(['explorer.exe', 'shell:AppsFolder\\' + a['id']], creationflags=NO_WINDOW)
    return True


def opener(target):
    """The app name that opens a URL or file, for "open it on the other screen"."""
    if re.match(r'^[a-z]+://', target):
        ext, kind = re.match(r'^([a-z]+)', target)[1], 'URL'
    else:
        ext, kind = os.path.splitext(target)[1] or '.txt', 'FILE'
    buf, n = ctypes.create_unicode_buffer(1024), W.DWORD(1024)
    # AssocQueryStringW(ASSOCF_NONE, ASSOCSTR_EXECUTABLE=2, ext or protocol, "open", out, &n)
    if ctypes.WinDLL('shlwapi').AssocQueryStringW(0, 2, ext if kind == 'FILE' else ext, 'open', buf, ctypes.byref(n)) == 0 and buf.value:
        return describe_exe(buf.value) if os.path.exists(buf.value) else ''
    return ''


# ---------- clipboard and typing ----------

kernel32.GlobalAlloc.restype = ctypes.c_void_p
kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
user32.GetClipboardData.restype = ctypes.c_void_p
user32.SetClipboardData.restype = ctypes.c_void_p
user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]


def _open_clip():
    for _ in range(20):   # another app may hold it for a moment
        if user32.OpenClipboard(None):
            return True
        time.sleep(0.03)
    return False


def get_clip():
    if not _open_clip():
        return ''
    try:
        h = user32.GetClipboardData(13)   # CF_UNICODETEXT
        p = h and kernel32.GlobalLock(h)
        if not p:
            return ''
        try:
            return ctypes.wstring_at(p)
        finally:
            kernel32.GlobalUnlock(h)
    finally:
        user32.CloseClipboard()


def set_clip(text):
    data = (text or '').encode('utf-16-le') + b'\0\0'
    if not _open_clip():
        return False
    try:
        user32.EmptyClipboard()
        h = kernel32.GlobalAlloc(0x0002, len(data))   # GMEM_MOVEABLE
        p = kernel32.GlobalLock(h)
        ctypes.memmove(p, data, len(data))
        kernel32.GlobalUnlock(h)
        return bool(user32.SetClipboardData(13, h))
    finally:
        user32.CloseClipboard()


def keys(*vks):
    """Press keys together, then let them go in reverse (e.g. keys(0x11, 0x56) = Ctrl+V)."""
    for v in vks:
        user32.keybd_event(v, 0, 0, 0)
    for v in reversed(vks):
        user32.keybd_event(v, 0, 2, 0)


def paste_into(app, text):
    """Paste text into the focused box of `app` (its front window), then put the user's clipboard back."""
    old = get_clip()
    if not set_clip(text):
        return 'could not use the clipboard'
    fg_app = front()[0]
    if app and key(fg_app) != key(app):
        focus_app(app)
        time.sleep(0.25)
    keys(0x11, 0x56)
    time.sleep(0.4)
    set_clip(old)
    return f'typed into {front()[0] or app}'


MEDIA = {'playpause': 0xB3, 'next': 0xB0, 'previous': 0xB1, 'prev': 0xB1, 'stop': 0xB2, 'mute': 0xAD, 'volumeup': 0xAF, 'volumedown': 0xAE}


def media(what):
    vk = MEDIA.get(re.sub(r'[^a-z]', '', what.lower()))
    if not vk:
        return 'media key must be one of: ' + ', '.join(MEDIA)
    keys(vk)
    return f'pressed {what}'


# ---------- screen ----------

def capture(mon, path, width=1280):
    """One display as a JPEG `width` px wide. The Atticus box is never in it (the app excludes itself from capture)."""
    import mss
    from PIL import Image
    with mss.mss() as s:
        g = s.grab({'left': mon['x'], 'top': mon['y'], 'width': mon['w'], 'height': mon['h']})
    img = Image.frombytes('RGB', g.size, g.bgra, 'raw', 'BGRX')
    if img.width > width:
        img = img.resize((width, round(img.height * width / img.width)), Image.LANCZOS)
    img.save(path, 'JPEG', quality=70)
    return path


# ---------- files ----------

def find_files(words, content=False, where='', most=20):
    """Windows Search's index (like Spotlight): by name, or by what's inside. Falls back to a quick walk of the user's folders."""
    words = ' '.join(words.split())
    if not words:
        return []
    scope = (os.path.abspath(os.path.expanduser(where)) if where else HOME).replace('\\', '/')
    q = words.replace("'", "''")
    cond = f"CONTAINS(*, '\"{q}\"')" if content else ' AND '.join(f"System.FileName LIKE '%{w}%'" for w in q.split())
    sql = f"SELECT TOP {most} System.ItemPathDisplay FROM SYSTEMINDEX WHERE SCOPE='file:{scope}' AND {cond} ORDER BY System.DateModified DESC"
    out = ps("$c = New-Object -ComObject ADODB.Connection; $c.Open(\"Provider=Search.CollatorDSO;Extended Properties='Application=Windows';\"); "
             f"$r = $c.Execute(\"{sql}\"); while (-not $r.EOF) {{ $r.Fields.Item(0).Value; $r.MoveNext() }}", 15)
    hits = [l for l in out.splitlines() if l.strip()]
    if hits or content:
        return hits[:most]
    end, ws = time.time() + 3, [w.lower() for w in words.split()]
    for root, dirs, files in os.walk(scope):
        dirs[:] = [d for d in dirs if not d.startswith(('.', '$')) and d not in ('AppData', 'node_modules', '__pycache__')]
        for f in files + dirs:
            if all(w in f.lower() for w in ws):
                hits.append(os.path.join(root, f))
        if len(hits) >= most or time.time() > end:
            break
    return hits[:most]


# ---------- talking to the person ----------

PIPE = r'\\.\pipe\atticus'


def to_app(line):
    """One line to the running Atticus app (toasts, its own settings). False when it isn't running."""
    try:
        with open(PIPE, 'w', encoding='utf-8') as p:
            p.write(line.replace('\n', ' ') + '\n')
        return True
    except OSError:
        return False


def notify(text, heading='Atticus'):
    if to_app('notify\t' + text):
        return
    t = text.replace("'", "''")
    h = heading.replace("'", "''")
    ps("[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null; "
       "$x = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02); "
       f"$s = $x.GetElementsByTagName('text'); $s.Item(0).InnerText = '{h}'; $s.Item(1).InnerText = '{t}'; "
       "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe').Show([Windows.UI.Notifications.ToastNotification]::new($x))")


def ask_text(prompt='What should I do?'):
    import tkinter as tk
    from tkinter import simpledialog
    r = tk.Tk(); r.withdraw(); r.attributes('-topmost', True)
    try:
        return (simpledialog.askstring('Atticus', prompt, parent=r) or '').strip()
    finally:
        r.destroy()


def ask_yes(heading, text):
    import tkinter as tk
    from tkinter import messagebox
    r = tk.Tk(); r.withdraw(); r.attributes('-topmost', True)
    try:
        return messagebox.askyesno(heading, text + '\n\nAllow?', parent=r)
    finally:
        r.destroy()


def choose(question, options):
    """A small list to pick from when there is no Atticus box. -> index, or None."""
    import tkinter as tk
    r = tk.Tk(); r.title('Atticus'); r.attributes('-topmost', True)
    got = []
    tk.Label(r, text=question, padx=12, pady=8).pack()
    lb = tk.Listbox(r, width=60, height=min(8, len(options)))
    for o in options:
        lb.insert('end', o)
    lb.pack(padx=12)
    lb.bind('<Double-Button-1>', lambda _: (got.extend(lb.curselection()), r.destroy()))
    tk.Button(r, text='Pick', command=lambda: (got.extend(lb.curselection()), r.destroy())).pack(pady=8)
    r.mainloop()
    return got[0] if got else None


# ---------- process trees ----------

def kill_tree(pid, me_too=True):
    """Stop a process and everything it started (Claude Code, its tools, the gate hook)."""
    try:
        import psutil
    except ImportError:
        if me_too:   # no threads here: this also runs while Python shuts down
            subprocess.run(['taskkill', '/F', '/T', '/PID', str(pid)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
        return
    try:
        p = psutil.Process(pid)
        for k in p.children(recursive=True):
            try:
                k.kill()
            except psutil.Error:
                pass
        if me_too:
            p.kill()
    except psutil.Error:   # already gone
        pass


# ---------- small helpers the AI calls ----------

def trash(path):
    """To the Recycle Bin (undoable), never a permanent delete."""
    p = os.path.abspath(os.path.expanduser(path))
    if not os.path.exists(p):
        return f'no such file: {path}'
    kind = 'DeleteDirectory' if os.path.isdir(p) else 'DeleteFile'
    q = p.replace("'", "''")
    ps(f"Add-Type -AssemblyName Microsoft.VisualBasic; [Microsoft.VisualBasic.FileIO.FileSystem]::{kind}('{q}', 'OnlyErrorDialogs', 'SendToRecycleBin')")
    return f'moved to the Recycle Bin: {p}' if not os.path.exists(p) else f'could not move {p} to the Recycle Bin'


NOTES = os.path.join(HOME, 'Documents', 'Atticus Notes.md')


def note(text):
    """Windows' to-do and notes apps can't be scripted: notes go in one Markdown file the user owns."""
    os.makedirs(os.path.dirname(NOTES), exist_ok=True)
    with open(NOTES, 'a', encoding='utf-8') as f:
        f.write(f"\n## {time.strftime('%a %b %d %Y %I:%M %p')}\n{text.strip()}\n")
    return f'noted in {NOTES}'


# ---------- inside apps: UI Automation (what Peekaboo does on the Mac) ----------

UI_FILE = None   # set by atticus.py: <DIR>/ui.json, the elements the last look listed
ACTIONABLE = {'ButtonControl': 'B', 'SplitButtonControl': 'B', 'MenuItemControl': 'M', 'HyperlinkControl': 'L', 'TabItemControl': 'T',
              'CheckBoxControl': 'C', 'RadioButtonControl': 'R', 'ComboBoxControl': 'D', 'EditControl': 'E', 'DocumentControl': 'E',
              'ListItemControl': 'I', 'TreeItemControl': 'I', 'DataItemControl': 'I', 'TextControl': 'X'}   # X: labels, read-only (a result, a status)


def _walk(root, most=1500, secs=2.5):
    import uiautomation as auto
    end, n = time.time() + secs, 0
    for c, _ in auto.WalkControl(root, maxDepth=40):
        n += 1
        if n > most or time.time() > end:
            break
        yield c


def _rid(c):
    try:
        return list(c.GetRuntimeId())
    except Exception:
        return []


def ui_see(app, words='', most=80):
    """The buttons, links, fields, tabs and menu items in `app`'s window, each with an ID for ui_click / ui_set."""
    import uiautomation as auto
    ws = find(app, words) or find(app)
    if not ws:
        return f'no window found for "{app}"'
    root = auto.ControlFromHandle(ws[0]['hwnd'])
    items, lines, count = {}, [], {}
    for c in _walk(root):
        kind = ACTIONABLE.get(c.ControlTypeName)
        if not kind or kind == 'X' and count.get('X', 0) >= 15:
            continue
        try:
            if c.IsOffscreen or not c.IsEnabled:
                continue
            r = c.BoundingRectangle
            if r.width() <= 0 or r.height() <= 0:
                continue
            name = ' '.join((c.Name or '').split())[:60]
            value = ''
            if kind == 'E':
                try:
                    value = ' '.join(c.GetValuePattern().Value.split())[:60]
                except Exception:
                    pass
            if not re.search(r'\w', name + value):   # unnamed, or an icon-font glyph
                continue
        except Exception:
            continue
        count[kind] = count.get(kind, 0) + 1
        i = f'{kind}{count[kind]}'
        items[i] = {'rid': _rid(c), 'name': name, 'type': c.ControlTypeName[:-7], 'rect': [r.left, r.top, r.right, r.bottom]}
        lines.append(f'{i} {items[i]["type"]} "{name}"' + (f' = "{value}"' if value else ''))
        if len(items) >= most:
            break
    if UI_FILE:
        json.dump({'hwnd': ws[0]['hwnd'], 'app': ws[0]['app'], 'items': items}, open(UI_FILE, 'w', encoding='utf-8'))
    return f'{ws[0]["app"]}: {ws[0]["title"]}\n' + ('\n'.join(lines) or 'nothing clickable found')


def ui_item(i):
    """An element from the last ui_see, by ID ("B3") or by its exact name ("Seven")."""
    try:
        items = json.load(open(UI_FILE, encoding='utf-8'))['items']
    except (OSError, ValueError, TypeError, AttributeError):
        return None
    if i in items:
        return items[i]
    return next((it for it in items.values() if it['name'].lower() == i.lower() and it['type'] != 'Text'), None)


def _find_items(ids):
    """[(id, control or None, saved item or None)] for several elements, in one walk of the window."""
    import uiautomation as auto
    saved = json.load(open(UI_FILE, encoding='utf-8'))
    want = [(i, ui_item(i)) for i in ids]
    root = auto.ControlFromHandle(saved['hwnd'])
    found = {}
    if root:
        rids = {tuple(it['rid']): n for n, (_, it) in enumerate(want) if it and it['rid']}
        for c in _walk(root):
            r = tuple(_rid(c))
            if r in rids:
                found.setdefault(rids[r], c)
            else:
                for n, (_, it) in enumerate(want):   # the page redrew: same kind and name
                    if it and n not in found and c.ControlTypeName[:-7] == it['type'] and ' '.join((c.Name or '').split())[:60] == it['name']:
                        found[n] = c
            if len(found) == len(want):
                break
    return [(i, found.get(n), it) for n, (i, it) in enumerate(want)], saved['hwnd']


def _find_item(i):
    (got,), _ = _find_items([i])
    return got[1], got[2]


def _press(c, it, hwnd=None):
    for how in ('GetInvokePattern', 'GetTogglePattern', 'GetSelectionItemPattern', 'GetExpandCollapsePattern'):
        try:
            p = getattr(c, how)()
            if not p:
                continue
            {'GetInvokePattern': lambda: p.Invoke(), 'GetTogglePattern': lambda: p.Toggle(),
             'GetSelectionItemPattern': lambda: p.Select(), 'GetExpandCollapsePattern': lambda: p.Expand()}[how]()
            return f'clicked {it["type"].lower()} "{it["name"]}"'
        except Exception:
            continue
    try:
        if hwnd:
            bring(hwnd)   # a real click needs the window in front; the actions above work behind the user's app
            time.sleep(0.15)
        c.Click(simulateMove=False)
        return f'clicked "{it["name"]}" (with the mouse)'
    except Exception as e:
        return f'could not click "{it["name"]}": {e}'


def ui_click(*ids):
    """Press elements from the last ui_see, in order: through the app's own accessibility actions when it has them (no mouse,
    the window can stay behind the user's app), else a real click. Stops at the first one that is missing."""
    try:
        got, hwnd = _find_items(ids)
    except (OSError, ValueError):
        return 'look first: run --ui see "<app>"'
    out = []
    for i, c, it in got:
        if not it:
            out.append(f'no element {i}: look again'); break
        if not c:
            out.append(f'"{it["name"]}" is gone: look again'); break
        out.append(_press(c, it, hwnd))
        if len(got) > 1:
            time.sleep(0.05)   # the app keeps up
    return '\n'.join(out)


def ui_set(i, text):
    """Fill a field from the last ui_see with text (no clipboard, no typing). Falls back to focus + paste."""
    try:
        c, it = _find_item(i)
    except (OSError, ValueError):
        return 'look first: run --ui see "<app>"'
    if not c:
        return f'no element {i}: look again'
    try:
        c.GetValuePattern().SetValue(text)
        return f'filled "{it["name"]}"'
    except Exception:
        try:
            c.SetFocus()
            time.sleep(0.15)
            old = get_clip(); set_clip(text); keys(0x11, 0x41); keys(0x11, 0x56); time.sleep(0.3); set_clip(old)
            return f'typed into "{it["name"]}"'
        except Exception as e:
            return f'could not fill "{it["name"]}": {e}'


KEYNAMES = {'ctrl': 0x11, 'control': 0x11, 'alt': 0x12, 'shift': 0x10, 'win': 0x5B, 'enter': 0x0D, 'return': 0x0D, 'tab': 0x09, 'esc': 0x1B,
            'escape': 0x1B, 'space': 0x20, 'backspace': 0x08, 'delete': 0x2E, 'del': 0x2E, 'up': 0x26, 'down': 0x28, 'left': 0x25, 'right': 0x27,
            'home': 0x24, 'end': 0x23, 'pageup': 0x21, 'pagedown': 0x22, **{f'f{n}': 0x6F + n for n in range(1, 13)}}


def hotkey(app, combo):
    """Press a key combination ("ctrl+s", "alt+f4", "f5") in `app`'s front window."""
    vks = []
    for k in combo.lower().replace(' ', '').split('+'):
        vk = KEYNAMES.get(k) or (ord(k.upper()) if len(k) == 1 and k.isalnum() else None)
        if vk is None:
            return f'unknown key "{k}"'
        vks.append(vk)
    if app and key(front()[0]) != key(app):
        focus_app(app)
        time.sleep(0.2)
    keys(*vks)
    time.sleep(0.35)   # the app reacts (a switcher opens, a channel loads) before the next step
    return f'pressed {combo}'


BROWSERS = ('Google Chrome', 'Microsoft Edge', 'Brave Browser', 'Firefox', 'Opera', 'Vivaldi', 'Arc')


def browser_url(hwnd):
    """The address in a browser window's address bar ('' if it can't be read quickly)."""
    import uiautomation as auto
    try:
        for c in _walk(auto.ControlFromHandle(hwnd), most=400, secs=0.6):
            if c.ControlTypeName == 'EditControl' and re.search(r'address|search with|enter address', c.Name or '', re.I):
                v = c.GetValuePattern().Value
                return v   # as Chrome shows it: an http site has no scheme in the bar
    except Exception:
        pass
    return ''


# ---------- Joplin: its local API (Tools → Options → Web Clipper), not its window ----------

def _joplin_settings():
    try:
        return json.load(open(os.path.join(HOME, '.config', 'joplin-desktop', 'settings.json'), encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def joplin(method, path, body=None, **query):
    """One call to Joplin's API. The token is read from Joplin's own settings here and never leaves this process."""
    import urllib.request, urllib.parse, urllib.error
    s = _joplin_settings()
    token = s.get('api.token')
    if not token:
        raise RuntimeError('Joplin has no API token: turn on Tools → Options → Web Clipper in Joplin')
    port = s.get('api.port') or 41184
    def up():
        try:
            return urllib.request.urlopen(f'http://127.0.0.1:{port}/ping', timeout=1).read() == b'JoplinClipperServer'
        except OSError:
            return False
    if not up():
        if not find('Joplin'):
            launch('Joplin')
        for _ in range(40):   # Joplin starts its server a few seconds after it opens
            time.sleep(0.25)
            if up():
                break
        else:
            raise RuntimeError('Joplin is not answering: open Joplin and turn on Tools → Options → Web Clipper')
    url = f'http://127.0.0.1:{port}/{path}?' + urllib.parse.urlencode({'token': token, **query})
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={'Content-Type': 'application/json'})
    return json.loads(urllib.request.urlopen(req, timeout=10).read() or b'null')


def _all(path, **q):
    out, page = [], 1
    while page < 20:
        r = joplin('GET', path, page=page, limit=100, **q)
        out += r.get('items', [])
        if not r.get('has_more'):
            return out
        page += 1
    return out


def joplin_notebook(name):
    books = _all('folders', fields='id,title')
    k = key(name)
    return next((b for b in books if key(b['title']) == k), None) or next((b for b in books if k and k in key(b['title'])), None), books


def joplin_note(ref):
    """A note by its id, its exact title, or the best search match: {id, title, parent_id} or None."""
    if re.fullmatch(r'[0-9a-f]{32}', ref):
        return joplin('GET', f'notes/{ref}', fields='id,title,parent_id')
    hits = _all('search', query=ref, type='note', fields='id,title,parent_id')[:20]
    exact = next((n for n in hits if n['title'].lower() == ref.lower()), None)
    if exact:
        return exact
    recent = _titles()   # the search index lags behind new notes: look at titles too
    k = ref.lower()
    return next((n for n in recent if n['title'].lower() == k), None) or (hits[0] if hits else None) or next((n for n in recent if k in n['title'].lower()), None)


def _titles():
    out = []
    for page in range(1, 6):   # the 500 most recently changed notes
        r = joplin('GET', 'notes', fields='id,title,parent_id', order_by='updated_time', order_dir='DESC', limit=100, page=page)
        out += r.get('items', [])
        if not r.get('has_more'):
            break
    return out


def joplin_cmd(args):
    """--joplin new "<title>" "<text>" [--in "<notebook>"] | find "<words>" | read "<note>" | add "<note>" "<text>" | open "<note>" | notebooks"""
    try:
        what, rest = (args[0], args[1:]) if args else ('', [])
        book = None
        if '--in' in rest:
            i = rest.index('--in'); book = rest[i + 1] if i + 1 < len(rest) else ''; rest = rest[:i] + rest[i + 2:]
        if what == 'notebooks':
            return '\n'.join(b['title'] for b in _all('folders', fields='id,title')) or 'no notebooks'
        if what == 'new' and rest:
            body = {'title': rest[0], 'body': '\n'.join(rest[1:])}
            if book:
                b, books = joplin_notebook(book)
                if not b:
                    return f'no notebook called "{book}". Notebooks: ' + ', '.join(x['title'] for x in books)
                body['parent_id'] = b['id']
            n = joplin('POST', 'notes', body)
            return f'created note "{n["title"]}" (id {n["id"]})'
        if what == 'find' and rest:
            books = {b['id']: b['title'] for b in _all('folders', fields='id,title')}
            q = ' '.join(rest)
            hits = _all('search', query=q, type='note', fields='id,title,parent_id')[:15]
            seen = {n['id'] for n in hits}
            hits += [n for n in _titles() if all(w in n['title'].lower() for w in q.lower().split()) and n['id'] not in seen][:15 - len(hits)]
            return '\n'.join(f'{n["title"]} [{books.get(n["parent_id"], "?")}] id {n["id"]}' for n in hits) or 'no notes found'
        n = joplin_note(rest[0]) if rest else None
        if what in ('read', 'add', 'open') and not n:
            return f'no note matching "{rest[0] if rest else ""}"'
        if what == 'read':
            return f'{n["title"]}\n' + joplin('GET', f'notes/{n["id"]}', fields='body')['body'][:4000]
        if what == 'add' and len(rest) > 1:
            old = joplin('GET', f'notes/{n["id"]}', fields='body')['body'].rstrip()
            joplin('PUT', f'notes/{n["id"]}', {'body': (old + '\n\n' if old else '') + '\n'.join(rest[1:])})
            return f'added to "{n["title"]}"'
        if what == 'open':
            os.startfile(f'joplin://x-callback-url/openNote?id={n["id"]}')
            return f'opened "{n["title"]}" in Joplin'
        return joplin_cmd.__doc__
    except Exception as e:
        return f'Joplin: {e}'


# ---------- secrets: Windows Credential Manager (generic credentials, this user only) ----------

class _CRED(ctypes.Structure):
    _fields_ = [('Flags', W.DWORD), ('Type', W.DWORD), ('TargetName', W.LPWSTR), ('Comment', W.LPWSTR), ('LastWritten', W.FILETIME),
                ('CredentialBlobSize', W.DWORD), ('CredentialBlob', ctypes.POINTER(ctypes.c_byte)), ('Persist', W.DWORD),
                ('AttributeCount', W.DWORD), ('Attributes', ctypes.c_void_p), ('TargetAlias', W.LPWSTR), ('UserName', W.LPWSTR)]


_advapi = ctypes.WinDLL('advapi32', use_last_error=True)
_advapi.CredReadW.argtypes = [W.LPCWSTR, W.DWORD, W.DWORD, ctypes.POINTER(ctypes.POINTER(_CRED))]
_advapi.CredWriteW.argtypes = [ctypes.POINTER(_CRED), W.DWORD]
_advapi.CredDeleteW.argtypes = [W.LPCWSTR, W.DWORD, W.DWORD]
_advapi.CredFree.argtypes = [ctypes.c_void_p]


def secret_get(name):
    p = ctypes.POINTER(_CRED)()
    if not _advapi.CredReadW(f'Atticus/{name}', 1, 0, ctypes.byref(p)):
        return ''
    try:
        return ctypes.string_at(p.contents.CredentialBlob, p.contents.CredentialBlobSize).decode('utf-16-le')
    finally:
        _advapi.CredFree(p)


def secret_set(name, value):
    if not value:
        return bool(_advapi.CredDeleteW(f'Atticus/{name}', 1, 0))
    blob = value.encode('utf-16-le')
    buf = (ctypes.c_byte * len(blob)).from_buffer_copy(blob)
    c = _CRED(Type=1, TargetName=f'Atticus/{name}', CredentialBlobSize=len(blob), CredentialBlob=buf, Persist=2, UserName='Atticus')   # Persist 2: this user, this PC
    return bool(_advapi.CredWriteW(ctypes.byref(c), 0))
