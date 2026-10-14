"""Windows side of Atticus's hands: displays, windows, apps, clipboard, typing, screenshots, files, toasts.
atticus.py calls these where the Mac version ran osascript, mdfind, screencapture or pbpaste. Pure ctypes plus
mss (screenshots), Pillow (resize) and psutil (process trees); every call returns quickly or gives up quietly."""
import ctypes, hashlib, json, os, re, subprocess, sys, time
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


def cloud(path):
    """True for a OneDrive-style placeholder or an offline file: reading it would download it, so no preview."""
    a = kernel32.GetFileAttributesW(str(path))
    return a != 0xFFFFFFFF and bool(a & 0x441000)   # OFFLINE | RECALL_ON_OPEN | RECALL_ON_DATA_ACCESS


class _GUID(ctypes.Structure):
    _fields_ = [('a', ctypes.c_uint32), ('b', ctypes.c_uint16), ('c', ctypes.c_uint16), ('d', ctypes.c_ubyte * 8)]


class _BMI(ctypes.Structure):   # BITMAPINFOHEADER
    _fields_ = [('size', ctypes.c_uint32), ('w', ctypes.c_int32), ('h', ctypes.c_int32), ('planes', ctypes.c_uint16), ('bits', ctypes.c_uint16),
                ('comp', ctypes.c_uint32), ('img', ctypes.c_uint32), ('xpm', ctypes.c_int32), ('ypm', ctypes.c_int32), ('used', ctypes.c_uint32), ('imp', ctypes.c_uint32)]


def thumb(path, size=320):
    """The shell's own preview (photos, videos, PDFs, Office files: what Explorer shows) as PNG bytes, b'' if the type has none.
    IShellItemImageFactory through ctypes. Run it in its own process (`python winctl.py --thumb <path>`): a slow or stuck
    preview handler can then be killed."""
    import io
    from PIL import Image
    if cloud(path):
        return b''
    ole32, shell32, gdi32 = ctypes.WinDLL('ole32'), ctypes.WinDLL('shell32'), ctypes.WinDLL('gdi32')
    gdi32.CreateCompatibleDC.restype = ctypes.c_void_p
    gdi32.CreateCompatibleDC.argtypes = gdi32.DeleteDC.argtypes = [ctypes.c_void_p]
    gdi32.GetObjectW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
    gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
    gdi32.GetDIBits.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint]
    ole32.CoInitializeEx(None, 2)
    iid = _GUID(0xbcc18b79, 0xba16, 0x442f, (ctypes.c_ubyte * 8)(0x80, 0xc4, 0x8a, 0x59, 0xc3, 0x0c, 0x46, 0x3b))   # IShellItemImageFactory
    f = ctypes.c_void_p()
    shell32.SHCreateItemFromParsingName.argtypes = [ctypes.c_wchar_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    if shell32.SHCreateItemFromParsingName(os.path.abspath(path), None, ctypes.byref(iid), ctypes.byref(f)) or not f:
        return b''
    vt = ctypes.cast(ctypes.cast(f, ctypes.POINTER(ctypes.c_void_p))[0], ctypes.POINTER(ctypes.c_void_p))
    release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vt[2])
    get = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, W.SIZE, ctypes.c_int, ctypes.POINTER(W.HBITMAP))(vt[3])
    bmp = W.HBITMAP()
    try:
        if get(f, W.SIZE(size, size), 0x8, ctypes.byref(bmp)) or not bmp:   # 0x8 THUMBNAILONLY: a plain icon is not a preview
            return b''
        bm = ctypes.create_string_buffer(32)   # BITMAP: type, width, height, ...
        gdi32.GetObjectW(bmp, 32, bm)
        w, h = ctypes.c_int32.from_buffer(bm, 4).value, ctypes.c_int32.from_buffer(bm, 8).value
        bi = _BMI(ctypes.sizeof(_BMI), w, -h, 1, 32, 0)   # top-down 32-bit BGRA
        px = ctypes.create_string_buffer(w * h * 4)
        dc = gdi32.CreateCompatibleDC(None)
        ok = gdi32.GetDIBits(dc, bmp, 0, h, px, ctypes.byref(bi), 0)
        gdi32.DeleteDC(dc)
        if not ok:
            return b''
        img = Image.frombuffer('RGBA', (w, h), px.raw, 'raw', 'BGRA', 0, 1)
        if img.getchannel('A').getextrema()[1] == 0:   # many handlers leave alpha at 0 for an opaque picture
            img = img.convert('RGB')
        out = io.BytesIO()
        img.save(out, 'PNG')
        return out.getvalue()
    finally:
        if bmp:
            gdi32.DeleteObject(bmp)
        release(f)


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


# ---------- hands: a screen map of one window, then click, menu, scroll, type, press, drag, wait (the Mac's Accessibility hands) ----------
# One cached UI Automation read gets the whole window in ~0.1 s; the map numbers its controls, and every step
# re-reads the window, acts on the live element (patterns first, the mouse only when an app has none) and prints what changed.

class Failure(Exception):
    pass


SECRET = re.compile(r'password|passcode|passphrase|passport|\bpin\b|card|cvv|cvc|security (answer|code)|\bssn\b|social security|api.?key|token|one.?time|'
                    r'verification|auth.*code|2fa|\botp\b|bank|account.?number|routing|iban|tax.?id|recovery|seed|mnemonic', re.I)
RISKY = re.compile(r'\b(buy|purchase|pay|payment|checkout|order|send|delete|remove|trash|submit|publish|post|sign.?out|log.?out|unsubscribe|subscribe|'
                   r'transfer|bid|erase|withdraw|donate|authorize|grant access|allow access|logout|book|reserve|merge|deploy|discard|wipe|format|uninstall|'
                   r'apply(?! filters| changes| settings| coupon| code| promo| discount| theme| style| formatting)|confirm)\b', re.I)
NEUTRAL = re.compile(r'^\s*(ok|yes|continue|allow|accept|agree|complete|finish|grant)\s*$', re.I)   # risky only when the dialog around it is
SEARCHY = re.compile(r'search|find|address|filter|jump|switch|url|go to', re.I)   # Enter there goes somewhere, it sends nothing
SKIP = {'Window', 'Pane', 'Group', 'Custom', 'Image', 'ScrollBar', 'Thumb', 'TitleBar', 'Separator', 'ToolBar', 'StatusBar', 'List', 'Tree', 'Table',
        'DataGrid', 'Tab', 'MenuBar', 'Menu', 'Header', 'AppBar', 'SemanticZoom', 'ToolTip', 'ProgressBar', 'Calendar'}
FIELDS = {'Edit', 'Document', 'ComboBox'}
PROPS = (30000, 30001, 30003, 30005, 30008, 30010, 30019, 30045, 30046, 30055, 30057, 30058)   # runtime id, rect, type, name, focus, enabled, password, value, read-only, scroll %, scrollable
_local = __import__('threading').local()


def _ua():
    """This thread's IUIAutomation (the map is also built on a background thread while the user talks)."""
    if not getattr(_local, 'ua', None):
        import comtypes, comtypes.client
        import uiautomation   # noqa: F401 (generates comtypes.gen.UIAutomationClient)
        from comtypes.gen import UIAutomationClient as C
        try:
            comtypes.CoInitializeEx(comtypes.COINIT_APARTMENTTHREADED)
        except OSError:
            pass
        _local.C, _local.types = C, uiautomation.ControlTypeNames
        _local.ua = comtypes.client.CreateObject(C.CUIAutomation, interface=C.IUIAutomation)
    return _local.ua


def _pattern(e, pid, iface):
    try:
        p = e.GetCurrentPattern(pid)
        return p.QueryInterface(getattr(_local.C, iface)) if p else None
    except Exception:
        return None


def mask(s):
    s = re.sub(r'\b(?:\d[ -]?){13,19}\b|\b\d{3}[- ]\d{2}[- ]\d{4}\b|\b\d{6,8}\b', '[SECRET]', s)
    if re.search(r'(one.?time|verification|security|auth|otp|code)\D{0,20}\d{4,8}', s, re.I):
        s = re.sub(r'\b\d{4,8}\b', '[SECRET]', s)
    return s


def norm(s):
    return ' '.join(s.lower().split()).replace('…', '...').translate({c: '-' for c in range(0x2010, 0x2016)} | {0x2212: '-', 0x2018: "'", 0x2019: "'"})   # "Don’t save" = "Don't save"


def rank(label, text):
    """4 exact, 3 starts with, 2 a whole word, 1 inside, 0 not at all."""
    l, t = norm(label), norm(text)
    if not t: return 0
    if l == t: return 4
    if l.startswith(t): return 3
    if re.search(r'\b' + re.escape(t) + r'\b', l): return 2
    return 1 if t in l else 0


def risky_label(label, m=None):
    if norm(label) == 'cancel': return False
    if RISKY.search(label): return True
    if m is None: return bool(NEUTRAL.match(label))   # no map to prove a bare "OK" is harmless
    return bool(NEUTRAL.match(label)) and bool(RISKY.search(' '.join([m['window'], *m['dialogs'], *m['texts']])))


def _owned(main, pid):
    """The app's own popups and dialogs on top of its window: menus, flyouts, Save As."""
    hs = []
    PROC = ctypes.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)
    user32.EnumWindows(PROC(lambda h, _: hs.append(h) or True), 0)
    out = []
    for h in hs:
        if h == main or not user32.IsWindowVisible(h) or cloaked(h) or pid_of(h) != pid:
            continue
        cls = ctypes.create_unicode_buffer(64); user32.GetClassNameW(h, cls, 64)
        style, owner = user32.GetWindowLongW(h, -16) & 0xFFFFFFFF, user32.GetWindow(h, 4)
        x, y, w, hh = frame(h)
        if w > 4 and hh > 4 and (owner == main or cls.value in ('#32768', 'Xaml_WindowedPopupClass') or style & 0x80000000 and not style & 0x00C00000 and not title(h)):
            out.append(h)
    return out[:4]


def target_window(app=''):
    """The mapped window: the named app's front window, else the user's front app (ATTICUS_FRONT), else the front window."""
    if app:
        ws = find(app) or [w for w in windows() if app.lower() in w['title'].lower()]   # an app, or words from a window title
        if not ws:
            raise Failure(f'No open window for "{app}".')
        return ws[0]
    ws = windows()
    mine = os.environ.get('ATTICUS_FRONT', '')
    fg = user32.GetForegroundWindow()
    hit = (mine and next((w for w in ws if matches(w, mine)), None)) or next((w for w in ws if w['hwnd'] == fg), None) or (ws[0] if ws else None)
    if not hit:
        raise Failure('No front app.')
    return hit


def see_map(w):
    """The window as a compact map: [{id, role, label, value, frame, rid, secret}] plus visible text, focus, scroll and page."""
    ua = _ua()
    cr = ua.CreateCacheRequest()
    for p in PROPS:
        cr.AddProperty(p)
    cr.TreeScope = 7   # the element and everything under it, in one cross-process call
    cr.TreeFilter = ua.CreateAndCondition(ua.ControlViewCondition, ua.CreatePropertyCondition(30022, False))   # on screen only
    hwnd, pid = w['hwnd'], pid_of(w['hwnd'])
    if not user32.IsWindow(hwnd):
        raise Failure('The mapped window closed. Run see again.')
    if user32.IsIconic(hwnd):   # minimized: nothing in it has a place on screen; back up behind the user's window, not in front
        user32.ShowWindow(hwnd, 4)   # SW_SHOWNOACTIVATE
        time.sleep(0.3)
    wf = frame(hwnd)
    m = {'hwnd': hwnd, 'pid': pid, 'app': w['app'], 'window': mask(title(hwnd)), 'windowFrame': list(wf), 'dialogs': [], 'nodes': [], 'texts': [],
         'page': '', 'focused': '', 'focusedRole': '', 'scroll': [], '_el': {}, '_all': []}
    roots = []
    for h in _owned(hwnd, pid):   # popups first: an open menu's items beat a long page
        try:
            roots.append((ua.ElementFromHandleBuildCache(h, cr), frame(h)))
            if title(h) and user32.GetWindowLongW(h, -16) & 0x00C00000: m['dialogs'].append(mask(title(h)))   # a dialog, not a menu
        except Exception:
            pass
    try:
        roots.append((ua.ElementFromHandleBuildCache(hwnd, cr), wf))
    except Exception as e:
        raise Failure(f'Could not read {w["app"]}: {e}')
    later, seen = [], set()
    queue = [(e, (f[0], f[1], f[0] + f[2], f[1] + f[3])) for e, f in roots]
    while queue or later:
        if not queue:
            queue, later = later, []   # web pages last: their links would use up the map before the app's own buttons
        e, clip = queue.pop(0)
        try:
            r = e.CachedBoundingRectangle
            role = _local.types.get(e.CachedControlType, 'Control')[:-7]
            name = ' '.join((e.CachedName or '').split())
        except Exception:
            continue
        box = (max(r.left, clip[0]), max(r.top, clip[1]), min(r.right, clip[2]), min(r.bottom, clip[3]))
        shown = box[2] - box[0] >= 4 and box[3] - box[1] >= 4
        if shown and len(m['_all']) < 4000:
            label = mask(name)[:160]
            cp = e.GetCachedPropertyValue
            secret = bool(e.CachedIsPassword) or bool(SECRET.search(label))
            m['_all'].append((e, role, label, [box[0], box[1], box[2] - box[0], box[3] - box[1]]))
            try:
                value = '' if secret or role not in FIELDS | {'Slider', 'Spinner'} else mask(' '.join(str(cp(30045) or '').split()))[:400]
            except Exception:
                value = ''
            if role == 'Edit' and SEARCHY.search(name) and re.match(r'(https?://)?[\w-]+(\.[\w-]+)+(/|$)', value) and not m['page']:
                m['page'] = value
            if e.CachedHasKeyboardFocus:
                m['focused'], m['focusedRole'] = (label or role), role
            if cp(30058) and len(m['scroll']) < 3 and cp(30055) >= 0:
                m['scroll'].append(round(cp(30055) / 100, 3))
            if role in ('Text', 'Header'):
                if label and label not in m['texts'] and len(m['texts']) < 40: m['texts'].append(label)
            elif role not in SKIP and (label or role in FIELDS) and len(m['nodes']) < 70:
                key = (role, label, tuple(m['_all'][-1][3]))
                if key not in seen:
                    seen.add(key)
                    n = {'id': len(m['nodes']) + 1, 'role': role, 'label': label or role, 'value': value, 'frame': m['_all'][-1][3],
                         'rid': list(cp(30000) or []), 'secret': secret}
                    m['nodes'].append(n); m['_el'][n['id']] = e
        kids = e.GetCachedChildren()
        if role in FIELDS - {'Document'} | {'ScrollBar'} or not kids:   # its arrows and thumb are noise
            continue
        inner = box if shown and role in ('Window', 'Pane', 'Document') and r.right > r.left else clip
        for i in range(kids.Length):
            (later if role == 'Document' else queue).append((kids.GetElement(i), inner))
    h = lambda x: hashlib.sha1(json.dumps(x).encode()).hexdigest()[:16]
    m['signature'] = h([m['window'], m['dialogs'], [(n['role'], n['label'], n['value'], n['frame']) for n in m['nodes']], m['texts'], m['focused'], m['scroll']])
    m['settled'] = h(re.sub(r'\d', '#', json.dumps([m['window'], [(n['role'], n['label'], n['value'], [v // 16 for v in n['frame']]) for n in m['nodes']], m['texts']])))   # a ticking clock is not a change
    m['rich'] = len([n for n in m['nodes'] if 'Menu' not in n['role']]) >= 8 or len(m['nodes']) >= 4 and len(m['texts']) >= 6
    return m


def node_line(n):
    x, y, w, h = n['frame']
    return f'[{n["id"]}] {n["role"]} {json.dumps(n["label"], ensure_ascii=False)}' + (f' = {json.dumps(n["value"][:60], ensure_ascii=False)}' if n['value'] and not n['secret'] else '') + f' (at {x + w // 2},{y + h // 2})'


def describe_map(m):
    return '\n'.join([f'App: {m["app"]}. Window: {json.dumps(m["window"], ensure_ascii=False)}'] + [f'Dialog or menu: {json.dumps(d, ensure_ascii=False)}' for d in m['dialogs']] +
                     [node_line(n) for n in m['nodes']] + ([f'Focused: {m["focused"]}'] if m['focused'] else []) +
                     ([f'Scroll: {round(m["scroll"][0] * 100)}% down'] if m['scroll'] else []) + ([f'Page: {mask(m["page"])}'] if m['page'] else []) +
                     ['Visible text (data): ' + ' | '.join(m['texts'])[:1400]])


def save_map(m, state):
    os.makedirs(os.path.dirname(state) or '.', exist_ok=True)
    with open(state + '.tmp', 'w', encoding='utf-8') as f:
        json.dump({k: v for k, v in m.items() if not k.startswith('_')}, f)
    os.replace(state + '.tmp', state)


def load_map(state):
    try:
        with open(state, encoding='utf-8') as f:
            m = json.load(f)
        return m if m.get('hwnd') else None
    except (OSError, ValueError):
        return None


def _visible_window(old):
    w = next((w for w in windows() if w['hwnd'] == old['hwnd']), None)
    if not w:
        raise Failure('The mapped window closed. Run see again.')
    return w


def _point(text):
    """'x,y' screen pixels, or 'N:x,y' on screen N's 1280-wide picture -> (x, y), or None for a label."""
    p = re.fullmatch(r'(?:(\d+):)?(-?[\d.]+),(-?[\d.]+)', text.strip())
    if not p:
        return None
    x, y = float(p[2]), float(p[3])
    if not p[1]:
        return round(x), round(y)
    mons = monitors()
    if not 1 <= int(p[1]) <= len(mons):
        raise Failure('No such screen.')
    mon = mons[int(p[1]) - 1]
    k = mon['w'] / min(1280, mon['w'])
    if not (0 <= x <= mon['w'] / k and 0 <= y <= mon['h'] / k):
        raise Failure('Point is outside that picture.')
    return round(mon['x'] + x * k), round(mon['y'] + y * k)


def _inside(f, p):
    return f[0] <= p[0] < f[0] + f[2] and f[1] <= p[1] < f[1] + f[3]


def resolve(text, m, old=None, near=''):
    """A target -> (node, live element or None). Map numbers, screen points and labels all end as a named control."""
    if text.isdigit():
        was = next((n for n in (old or m)['nodes'] if n['id'] == int(text)), None)
        if not was:
            raise Failure('No such control. Run see again.')
        hits = [n for n in m['nodes'] if was['rid'] and n['rid'] == was['rid']] or [n for n in m['nodes'] if (n['role'], n['label']) == (was['role'], was['label'])]
        if len(hits) != 1:
            raise Failure('The screen changed. Run see again.')
        return hits[0], m['_el'].get(hits[0]['id'])
    p = _point(text)
    if p:
        inside = [n for n in m['nodes'] if _inside(n['frame'], p)]
        if inside:
            n = min(inside, key=lambda n: n['frame'][2] * n['frame'][3])
            return {**n, 'frame': [p[0], p[1], 1, 1]}, m['_el'].get(n['id'])   # named by its control, acted at the very point
        if not _inside(m['windowFrame'], p) and not any(_inside(a[3], p) for a in m['_all']):
            raise Failure('That point is outside the mapped window. Run see again.')
        hit = [a for a in m['_all'] if _inside(a[3], p) and a[2]]
        if hit:
            e, role, label, f = min(hit, key=lambda a: a[3][2] * a[3][3])
            return {'id': 0, 'role': role, 'label': label, 'value': '', 'frame': [p[0], p[1], 1, 1], 'secret': bool(SECRET.search(label))}, e
        return {'id': 0, 'role': 'Unknown', 'label': f'Unidentified screen point {p[0]},{p[1]}', 'value': '', 'frame': [p[0], p[1], 1, 1], 'secret': False}, None
    spot = lambda f: (f[0] + f[2] / 2, f[1] + f[3] / 2)
    anchor = None
    if near:
        anchor = next((n['frame'] for n in m['nodes'] if rank(n['label'], near) == 4), None) or next((a[3] for a in m['_all'] if rank(a[2], near) == 4), None)
        if anchor is None:
            raise Failure(f'No nearby anchor named {json.dumps(near)}.')
    dist = lambda n: ((spot(n['frame'])[0] - spot(anchor)[0]) ** 2 + (spot(n['frame'])[1] - spot(anchor)[1]) ** 2) ** .5 if anchor else n['id']
    hits = sorted((n for n in m['nodes'] if rank(n['label'], text) > 0), key=lambda n: (-rank(n['label'], text), dist(n)))
    best = rank(hits[0]['label'], text) if hits else 0
    if not near and best < 4:   # a list row or a link the map left out: its text
        more = sorted((a for a in m['_all'] if rank(a[2], text) > best and a[1] not in FIELDS), key=lambda a: -rank(a[2], text))
        if more:
            e, role, label, f = more[0]
            return {'id': 0, 'role': role, 'label': label, 'value': '', 'frame': f, 'secret': bool(SECRET.search(label))}, e
    if not hits:
        raise Failure(f'No visible control named {json.dumps(mask(text))}. Use scroll-to for offscreen text.')
    if not near and len(hits) > 1 and rank(hits[1]['label'], text) == best:
        raise Failure(f'More than one control named {json.dumps(text)}; use near.')
    return hits[0], m['_el'].get(hits[0]['id'])


def press_keys(combo, hwnd=None, times=1):
    """'ctrl+shift+t' -> keys down, up, `times` times, in `hwnd` (brought forward first)."""
    vks = []
    for k in combo.lower().replace(' ', '').replace('++', '+plus').split('+'):
        vk = KEYNAMES.get(k) or {'plus': 0xBB, 'minus': 0xBD, 'insert': 0x2D, 'cmd': 0x11, 'command': 0x11, 'option': 0x12, 'windows': 0x5B,
                                 'menu': 0x5D, 'apps': 0x5D, 'printscreen': 0x2C, 'capslock': 0x14}.get(k)
        if vk is None and len(k) == 1:
            r = user32.VkKeyScanW(ord(k))
            vk = r & 0xFF if r != -1 and r & 0xFF != 0xFF else None
        if vk is None:
            raise Failure(f'Unknown key: {k}')
        vks.append(vk)
    if not 1 <= times <= 50:
        raise Failure('Invalid key repeat count.')
    if hwnd:
        _forward(hwnd)
    for _ in range(times):
        keys(*vks)
        time.sleep(0.02)
    return vks


def risky_key(combo, focused_role=''):
    c = combo.lower().replace(' ', '').replace('cmd', 'ctrl').replace('control', 'ctrl')
    mods, _, k = c.rpartition('+')
    mods = set(mods.split('+')) - {''}
    return bool(k == 'f4' and mods & {'alt', 'ctrl'} or k in ('w', 'q') and 'ctrl' in mods or k in ('enter', 'return') and mods & {'ctrl', 'alt'} or
                k == 's' and mods == {'alt'} or k in ('delete', 'del') and (mods or focused_role not in FIELDS))   # close, quit, send, delete


def _forward(hwnd):
    if user32.GetForegroundWindow() != hwnd:
        bring(hwnd)
        time.sleep(0.12)


class _Back:
    """Mouse work borrows the window and the pointer; both go back to the user afterwards."""
    def __init__(self, hwnd):
        self.hwnd = hwnd

    def __enter__(self):
        self.prev, self.pt = user32.GetForegroundWindow(), W.POINT()
        user32.GetCursorPos(ctypes.byref(self.pt))
        _forward(self.hwnd)
        return self

    def __exit__(self, *_):
        user32.mouse_event(0x0004, 0, 0, 0, 0)   # a held button never stays down
        user32.SetCursorPos(self.pt.x, self.pt.y)
        if self.prev and self.prev != self.hwnd and user32.IsWindow(self.prev):
            time.sleep(0.05)
            bring(self.prev)


def mouse(hwnd, at, double=False, right=False, to=None):
    down, up = (0x0008, 0x0010) if right else (0x0002, 0x0004)
    with _Back(hwnd):
        for i in range(2 if double else 1):
            user32.SetCursorPos(*at)
            user32.mouse_event(down, 0, 0, 0, 0)
            if to:
                for s in range(1, 21):
                    user32.SetCursorPos(round(at[0] + (to[0] - at[0]) * s / 20), round(at[1] + (to[1] - at[1]) * s / 20)); time.sleep(0.01)
            user32.mouse_event(up, 0, 0, 0, 0)
            time.sleep(0.05)


def wheel(hwnd, at, direction, notches=5):
    """The wheel goes to whatever is under the pointer, so the window comes forward first when something covers that spot."""
    with _Back(hwnd) if user32.GetAncestor(user32.WindowFromPoint(W.POINT(*at)), 2) != hwnd else _NoBack():
        pt = W.POINT(); user32.GetCursorPos(ctypes.byref(pt))
        user32.SetCursorPos(*at)
        user32.mouse_event(0x0800 if direction in ('down', 'up') else 0x1000, 0, 0, (-120 if direction in ('down', 'left') else 120) * notches, 0)
        time.sleep(0.05)
        user32.SetCursorPos(pt.x, pt.y)


class _NoBack:
    def __enter__(self): return self
    def __exit__(self, *_): pass


def press(e, n, hwnd):
    """Click a control the way the app offers (invoke, toggle, select, expand), else with the mouse."""
    if e is not None:
        ways = [(10000, 'IUIAutomationInvokePattern', lambda p: p.Invoke()), (10015, 'IUIAutomationTogglePattern', lambda p: p.Toggle()),
                (10010, 'IUIAutomationSelectionItemPattern', lambda p: p.Select())]
        if n['role'] in ('ListItem', 'TreeItem', 'DataItem', 'TabItem'):   # one click on a row selects it; invoking a file opens it
            ways = ways[2:] + ways[:2]
        for pid, iface, act in (*ways,
                                (10005, 'IUIAutomationExpandCollapsePattern', lambda p: p.Collapse() if p.CurrentExpandCollapseState == 1 else p.Expand()),
                                (10018, 'IUIAutomationLegacyIAccessiblePattern', lambda p: p.DoDefaultAction())):
            p = _pattern(e, pid, iface)
            if p:
                try:
                    act(p); return
                except Exception:
                    continue
    x, y, w, h = n['frame']
    mouse(hwnd, (x + w // 2, y + h // 2))


def menu(path, w, act):
    """'File > Export > PDF' -> the last item's label. Each level opens (expand or invoke) and the next is looked for in the window and its popups."""
    parts = [p.strip() for p in path.split('>')]
    if len(parts) < 2 or not all(parts):
        raise Failure('Invalid menu path.')
    if not act:
        return mask(parts[-1])
    _forward(w['hwnd'])   # menus close when their window loses focus
    for i, part in enumerate(parts):
        for _ in range(15):
            m = see_map(w)
            hit = [a for a in m['_all'] if rank(a[2], part) == 4 and a[1] in ('MenuItem', 'Button', 'SplitButton', 'TabItem', 'ListItem', 'TreeItem', 'Hyperlink', 'CheckBox', 'RadioButton')]
            hit = [a for a in hit if a[1] == 'MenuItem'] or hit
            if hit:
                break
            time.sleep(0.1)
        else:
            raise Failure(f'No menu item named {json.dumps(part)}.')
        e, role, label, f = hit[-1] if i else hit[0]   # popups come first in the map, so a submenu's item wins over the bar
        n = {'frame': f, 'label': label}
        if i < len(parts) - 1:
            p = _pattern(e, 10005, 'IUIAutomationExpandCollapsePattern')
            try:
                p.Expand() if p else press(e, n, w['hwnd'])
            except Exception:
                press(e, n, w['hwnd'])
            time.sleep(0.15)
        else:
            press(e, n, w['hwnd'])
            return label


def _scroller(m, at=None):
    """The scroll area to move: the smallest one under the target, else the biggest in the window."""
    areas = []
    for e, role, label, f in m['_all']:
        try:
            if e.GetCachedPropertyValue(30058) or e.GetCachedPropertyValue(30057):
                areas.append((e, f))
        except Exception:
            pass
    if at:
        under = [a for a in areas if _inside(a[1], at)]
        return min(under, key=lambda a: a[1][2] * a[1][3]) if under else (None, None)
    return max(areas, key=lambda a: a[1][2] * a[1][3]) if areas else (None, None)


def _moved(before, after):
    texts = len(set(before['texts']) - set(after['texts'])) >= 3 and len(set(after['texts']) - set(before['texts'])) >= 3
    return texts or before['scroll'] != after['scroll'] or any(
        len(hits) == 1 and abs(hits[0]['frame'][1] - n['frame'][1]) + abs(hits[0]['frame'][0] - n['frame'][0]) >= 6
        for n in before['nodes'] for hits in [[a for a in after['nodes'] if (a['role'], a['label']) == (n['role'], n['label'])]])


def scroll(w, direction, pages=1.0, everything=False, at=None):
    if direction not in ('down', 'up', 'left', 'right') or not 0.1 <= pages <= 50:
        raise Failure('Use scroll down|up|left|right [pages|all] [target].')
    vertical, ahead = direction in ('down', 'up'), direction in ('down', 'right')
    done = 0
    for _ in range(50 if everything else max(1, int(-(-pages // 1)))):
        before = see_map(w)
        e, f = _scroller(before, at)
        sp = _pattern(e, 10004, 'IUIAutomationScrollPattern') if e is not None else None
        pct = lambda: (sp.CurrentVerticalScrollPercent if vertical else sp.CurrentHorizontalScrollPercent) if sp else None
        start = pct()
        if start is not None and start >= 0 and abs(start - (100 if ahead else 0)) < 0.1:
            return f'Already at the {dict(down="bottom", up="top").get(direction, direction + " end")}' if not done else f'Scrolled {direction} {done} page{"s" * (done != 1)} (now {round(start)}%)'
        moved = False
        if sp:
            try:
                if everything:
                    sp.SetScrollPercent(-1 if vertical else (100 if ahead else 0), (100 if ahead else 0) if vertical else -1)
                else:
                    amount = (3 if ahead else 0) if pages >= 1 else (4 if ahead else 1)
                    for _ in range(1 if pages >= 1 else max(1, round(pages * 4))):
                        sp.Scroll(2, amount) if vertical else sp.Scroll(amount, 2)
                for _ in range(5):   # Chromium reports the new place a moment later
                    time.sleep(0.08)
                    moved = pct() != start
                    if moved: break
            except Exception:
                pass
        if not moved:   # no scroll pattern (many web pages and Electron apps): the wheel over that spot
            box = f or before['windowFrame']
            wheel(w['hwnd'], at or (box[0] + box[2] // 2, box[1] + box[3] // 2), direction, 5 if pages >= 1 else max(1, round(pages * 5)))
            time.sleep(0.15)
            moved = _moved(before, see_map(w))
        if not moved:
            break
        done += 1
        if everything and pct() is not None and abs(pct() - (100 if ahead else 0)) < 0.1:
            return f'Scrolled to the {dict(down="bottom", up="top").get(direction, direction + " end")}'
    now = pct() if done else None
    return (f'Scrolled {direction} ' + ('a bit' if pages < 1 else f'{done} page{"s" * (done != 1)}') + (f' (now {round(now)}%)' if now is not None and now >= 0 else '')) if done else 'No scroll movement detected'


def scroll_to(w, text):
    shown = lambda m: any(rank(t, text) == 4 for t in m['texts']) or any(rank(n['label'], text) == 4 for n in m['nodes'])
    if shown(see_map(w)):
        return f'Found {json.dumps(mask(text))}'
    m = see_map(w)
    for e in [m['_el'][n['id']] for n in m['nodes'] if n['role'] in ('Document', 'Edit')]:   # a text editor's own lines
        tp = _pattern(e, 10014, 'IUIAutomationTextPattern')
        try:
            r = tp and tp.DocumentRange.FindText(text, False, True)
            if r:
                r.ScrollIntoView(True); time.sleep(0.1)
                return f'Revealed {json.dumps(mask(text))}'
        except Exception:
            pass
    ua = _ua()
    try:   # offscreen too: a list or page the app can scroll to by itself
        hit = ua.ElementFromHandle(w['hwnd']).FindFirst(4, ua.CreatePropertyConditionEx(30005, text, 2))   # descendants, ignore case
    except Exception:
        hit = None
    p = _pattern(hit, 10017, 'IUIAutomationScrollItemPattern') if hit else None
    if p:
        try:
            p.ScrollIntoView(); time.sleep(0.15)
            if shown(see_map(w)):
                return f'Revealed {json.dumps(mask(text))}'
        except Exception:
            pass
    direction, turned = 'down', False
    for _ in range(15):
        r = scroll(w, direction)
        if shown(see_map(w)):
            return f'Found {json.dumps(mask(text))}'
        if r.startswith(('Already', 'No scroll')) or '(now 100%)' in r:
            if turned: break
            direction, turned = 'up', True
    raise Failure(f'Could not find {json.dumps(mask(text))} within 15 pages.')


def type_into(e, n, w, text, append=False):
    """Set a field's text through its value (no clipboard, no focus), else focus it and paste; either way the text must be there after."""
    if n['role'] not in FIELDS:
        raise Failure('Target is not a text field.')
    vp = _pattern(e, 10002, 'IUIAutomationValuePattern')
    lines = lambda s: (s or '').replace('\r\n', '\n').replace('\r', '\n')
    read = lambda: lines(vp.CurrentValue if vp else _text_of(e))
    old = read()
    want = lines(old + text if append else text)
    if vp and not vp.CurrentIsReadOnly:
        try:
            vp.SetValue(want)
            time.sleep(0.05)
            if n['secret'] or read().rstrip('\n') == want.rstrip('\n'):
                return
            if read() != old:
                raise Failure('The field changed, but not to the text given. Run see and check it.')   # never paste a second copy
        except Failure:
            raise
        except Exception:
            pass
    _forward(w['hwnd'])
    try:
        e.SetFocus()
    except Exception:
        x, y, fw, fh = n['frame']; mouse(w['hwnd'], (x + fw // 2, y + fh // 2))
    time.sleep(0.1)
    saved = get_clip()
    if not set_clip(text):
        raise Failure('Could not prepare paste.')
    keys(0x11, 0x23) if append else keys(0x11, 0x41)   # Ctrl+End or Ctrl+A
    keys(0x11, 0x56); time.sleep(0.3)
    set_clip(saved)
    if not n['secret'] and want.strip() not in read():
        raise Failure('Text did not land in the field.')


def _text_of(e):
    tp = _pattern(e, 10014, 'IUIAutomationTextPattern')
    try:
        return tp.DocumentRange.GetText(-1) if tp else ''
    except Exception:
        return ''


def settle(w, secs):
    end, last, since = time.time() + secs, see_map(w)['settled'], time.time()
    while time.time() < end:
        time.sleep(0.1)
        now = see_map(w)['settled']
        if now != last:
            last, since = now, time.time()
        elif time.time() - since >= 0.3:
            return True
    return False


def hands_valid(args):
    """The verbs and their words; no flags, ever (nothing can skip the user's Allow)."""
    if not args:
        return False
    verb, rest = args[0], args[1:]
    if any(a.startswith('--') for a in rest): return False
    if verb == 'see': return len(rest) <= 1
    if verb == 'menu': return len(rest) == 2 and bool(rest[0]) and len(rest[1].split('>')) > 1 and all(p.strip() for p in rest[1].split('>'))
    if verb == 'click':
        if not rest: return False
        i, seen = 1, set()
        while i < len(rest):
            if rest[i] not in ('near', 'double', 'right') or rest[i] in seen or rest[i] == 'near' and i + 1 >= len(rest): return False
            seen.add(rest[i]); i += 2 if rest[i] == 'near' else 1
        return '>' not in rest[0] or len(rest) == 1
    if verb == 'type': return 2 <= len(rest) <= 4 and all(a in ('submit', 'append') for a in rest[2:]) and len(set(rest[2:])) == len(rest[2:])
    if verb == 'drag': return len(rest) == 2
    if verb == 'scroll-to': return len(rest) == 1
    if verb == 'scroll':
        if not 1 <= len(rest) <= 3 or rest[0] not in ('down', 'up', 'left', 'right'): return False
        amount = rest[1] if len(rest) > 1 else '1'
        try:
            return amount == 'all' or 0.1 <= float(amount) <= 50
        except ValueError:
            return len(rest) == 2   # scroll down <target>
    if verb == 'press': return 1 <= len(rest) <= 2 and (len(rest) == 1 or rest[1].isdigit() and 1 <= int(rest[1]) <= 50)
    if verb == 'wait':
        try:
            return not rest or len(rest) == 1 and 0 < float(rest[0]) <= 30
        except ValueError:
            return False
    return False


def _scroll_args(args):
    amount = args[2] if len(args) > 2 else '1'
    try:
        pages = float(amount)
        target = args[3] if len(args) > 3 else ''
    except ValueError:
        pages, target = 1.0, '' if amount == 'all' else amount
        target = args[3] if len(args) > 3 else target
    return pages, amount == 'all', target


def check(args, m, old=None):
    """What a step would touch, by name, and whether the user must allow it. The act re-checks and must get the same answer."""
    verb, labels, secure = args[0], [], False
    if verb == 'click' and '>' in args[1]:
        labels = [mask(p.strip()) for p in args[1].split('>')]
    elif verb in ('click', 'drag', 'type'):
        near = args[args.index('near') + 1] if verb == 'click' and 'near' in args[2:] else ''
        for t in (args[1:3] if verb == 'drag' else args[1:2]):
            n, _ = resolve(t, m, old, near)
            labels.append(n['label']); secure = secure or n['secret']
    elif verb == 'scroll' and _scroll_args(args)[2]:
        labels = [resolve(_scroll_args(args)[2], m, old)[0]['label']]
    elif verb == 'press':
        labels = [m['focused'], args[1]]
    risk = any(l.startswith('Unidentified screen point ') or risky_label(l, m) and verb != 'scroll' or verb == 'drag' and re.search(r'\b(bin|recycle)\b', l, re.I) for l in labels)
    risk = risk or verb == 'press' and risky_key(args[1], m['focusedRole']) or verb == 'type' and 'submit' in args[3:] and not SEARCHY.search(labels[0])
    return {'args': list(args), 'labels': labels, 'secure': secure, 'risky': bool(risk), 'pid': m['pid'], 'hwnd': m['hwnd'], 'window': m['window'], 'app': m['app']}


def diff(before, after):
    """What a step changed, as map lines: the whole map when the window itself changed."""
    if after['hwnd'] != before['hwnd'] or after['dialogs'] != before['dialogs']:
        return describe_map(after)
    key = lambda n: (n['role'], n['label'], n['value'], tuple(n['frame']))
    old = {key(n) for n in before['nodes']}
    names = {(n['role'], n['label']) for n in after['nodes']}
    gone = [f'Removed: {json.dumps(n["label"], ensure_ascii=False)}' for n in before['nodes'] if (n['role'], n['label']) not in names][:15]
    new = [node_line(n) for n in after['nodes'] if key(n) not in old]
    texts = ' | '.join(t for t in after['texts'] if t not in before['texts'])[:1400]
    return '\n'.join([f'Now: {after["window"]}'] + gone + new + [f'Focused: {after["focused"]}'] + ([f'Scroll: {round(after["scroll"][0] * 100)}% down'] if after['scroll'] else []) +
                     ([f'Page: {mask(after["page"])}'] if after['page'] != before['page'] else []) + ([texts] if texts else []))


def hands_cli(args, state, receipt=None, safe=True):
    """-> (exit code, text). 'see [app]' maps; 'label <step>' checks a step; '<step>' with the check as receipt does it."""
    try:
        label = args[:1] == ['label']
        args = args[1:] if label else args
        if not hands_valid(args):
            raise Failure('Use see|click|menu|scroll|scroll-to|press|drag|type|wait; no flags.')
        if args[0] == 'see':
            m = see_map(target_window(args[1] if len(args) > 1 else ''))
            save_map(m, state)
            return 0, describe_map(m)
        if args[0] == 'menu':   # a learned menu path, bound to its app
            w = target_window(args[1])
            if safe and (risky_label(args[2]) or any(risky_label(p.strip()) for p in args[2].split('>'))):
                raise Failure("This menu action needs the user's Allow card. Use the normal hands click path.")
            got = menu(args[2], w, True)
            time.sleep(0.15)
            save_map(see_map(w), state)
            return 0, f'Clicked {json.dumps(got, ensure_ascii=False)}'
        old = load_map(state)
        if not old:
            raise Failure('Run see first.')
        w = _visible_window(old)
        before = see_map(w)
        c = check(args, before, old)
        if label:
            return 0, json.dumps(c)
        if receipt is None:
            raise Failure('Use atticus --hands so the user can allow this action.')
        if any(receipt.get(k) != c[k] for k in ('args', 'labels', 'secure', 'risky', 'pid', 'hwnd')):
            raise Failure('The screen changed while checking permission. Run see again.')
        verb, spot = args[0], None
        if verb == 'click' and '>' in args[1]:
            result = f'Clicked {json.dumps(menu(args[1], w, True), ensure_ascii=False)}'
        elif verb == 'click':
            near = args[args.index('near') + 1] if 'near' in args[2:] else ''
            n, e = resolve(args[1], before, old, near)
            x, y, fw, fh = n['frame']; spot = (x + fw // 2, y + fh // 2)
            if 'double' in args[2:] or 'right' in args[2:] or e is None:
                mouse(w['hwnd'], spot, double='double' in args[2:], right='right' in args[2:])
            else:
                press(e, n, w['hwnd'])
            result = f'Clicked {json.dumps(n["label"], ensure_ascii=False)}'
        elif verb == 'scroll':
            pages, everything, target = _scroll_args(args)
            if target:
                f = resolve(target, before, old)[0]['frame']; spot = (f[0] + f[2] // 2, f[1] + f[3] // 2)
            result = scroll(w, args[1], pages, everything, spot)
        elif verb == 'scroll-to':
            result = scroll_to(w, args[1])
        elif verb == 'press':
            press_keys(args[1], w['hwnd'], int(args[2]) if len(args) > 2 else 1)
            result = f'Pressed {json.dumps(args[1])} in {w["app"]}'
        elif verb == 'drag':
            a, b = resolve(args[1], before, old)[0], resolve(args[2], before, old)[0]
            mid = lambda f: (f[0] + f[2] // 2, f[1] + f[3] // 2)
            spot = mid(b['frame'])
            mouse(w['hwnd'], mid(a['frame']), to=spot)
            result = f'Dragged {json.dumps(a["label"], ensure_ascii=False)} to {json.dumps(b["label"], ensure_ascii=False)}'
        elif verb == 'type':
            n, e = resolve(args[1], before, old)
            if e is None:
                raise Failure('No editable field.')
            x, y, fw, fh = n['frame']; spot = (x + fw // 2, y + fh // 2)
            type_into(e, n, w, args[2], 'append' in args[3:])
            if 'submit' in args[3:]:
                press_keys('enter', w['hwnd'])
            result = f'Typed into {json.dumps(n["label"], ensure_ascii=False)}'
        else:
            result = 'Window settled' if settle(w, float(args[1]) if len(args) > 1 else 5) else 'Window did not settle before the timeout'
        if verb != 'wait':
            time.sleep(0.12)
        if not any(x['hwnd'] == w['hwnd'] for x in windows()):   # it closed the window: done, and nothing left to map
            os.remove(state)
            return 0, result + (f'\n@ {spot[0]} {spot[1]}' if spot else '') + f'\nThe {w["app"]} window closed.'
        after = see_map(w)
        for _ in range(3 if verb == 'click' else 0):   # a menu or dialog can take a moment to open
            if after['signature'] != before['signature']: break
            time.sleep(0.15); after = see_map(w)
        if verb == 'click' and '>' not in args[1] and after['signature'] == before['signature']:
            result = f'Nothing changed after clicking {json.dumps(c["labels"][-1] if c["labels"] else args[1], ensure_ascii=False)}'
        save_map(after, state)
        return 0, result + (f'\n@ {spot[0]} {spot[1]}' if spot else '') + '\n' + diff(before, after)
    except Failure as e:
        return 1, str(e)
    except Exception as e:
        return 1, f'Hands could not finish: {e}'


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


if __name__ == '__main__' and sys.argv[1:2] == ['--thumb'] and len(sys.argv) == 3:   # atticus.py thumb(): PNG base64 on stdout
    import base64
    sys.stdout.write(base64.b64encode(thumb(sys.argv[2])).decode())
