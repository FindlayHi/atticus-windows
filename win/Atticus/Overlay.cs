// Atticus overlay: hold to talk and release to send, or press to start and press again to send.
// A box in the top corner (ui\ui.html, with the thinking-orbs orb) shows your words live (Whisper, on this PC),
// then Atticus's notes and steps while atticus.py works, then the answer. It never takes focus unless you type in it.
using System.Diagnostics;
using System.Text.Json;
using System.Text.RegularExpressions;
using Microsoft.Web.WebView2.WinForms;

namespace Atticus;

sealed class Overlay : Form
{
    // Moving, resizing, hovering and clicking are read in the page and sent here, like the Mac app's event monitors.
    const string PointerScript = @"
addEventListener('DOMContentLoaded', () => {
  const post = b => chrome.webview.postMessage({ n: 'w', b }), E = 6, C = 14;
  let st = null, hov = false, hot = false, tapping = false;
  const edge = (x, y) => { const w = innerWidth, h = innerHeight;
    const l = x < E || (x < C && y > h - C), r = x > w - E || (x > w - C && y > h - C), b = y > h - E || (y > h - C && (x < C || x > w - C));
    return l || r || b ? { l, r, b } : null; };
  const overX = (x, y) => x > innerWidth - 42 && y < 42;
  const inComp = e => { const c = document.getElementById('comp'); return c && c.classList.contains('open') && c.contains(e.target); };
  const tap = ui.tap; ui.tap = p => { tapping = true; try { tap(p); } finally { tapping = false; } };
  addEventListener('pointermove', e => {
    if (st) {
      const dpr = devicePixelRatio, dx = e.screenX - st.x, dy = e.screenY - st.y;
      if (st.k === 'move') { if (Math.abs(dx) + Math.abs(dy) > 3) st.moved = true; if (st.moved) post({ move: [dx * dpr, dy * dpr] }); }
      else {
        if (st.e.l || st.e.r) post({ width: Math.min(900, Math.max(340, st.w + (st.e.r ? dx : -dx))) * dpr, left: st.e.l });
        if (st.e.b) ui.hold(Math.min(1000, Math.max(120, st.h + dy)));
      }
      return;
    }
    const ed = edge(e.clientX, e.clientY);
    document.documentElement.style.cursor = ed ? (ed.b && (ed.l || ed.r) ? (ed.l ? 'nesw-resize' : 'nwse-resize') : ed.b ? 'ns-resize' : 'ew-resize') : '';
    const x = overX(e.clientX, e.clientY) && !ed; if (x !== hot) { hot = x; post({ xhot: x }); }
    if (!hov) { hov = true; post({ hover: true }); }
  }, true);
  document.documentElement.addEventListener('mouseleave', () => { if (st) return; hov = false; post({ hover: false }); if (hot) { hot = false; post({ xhot: false }); } });
  addEventListener('pointerdown', e => {
    if (e.button !== 0) return;
    if (inComp(e)) { post({ key: 1 }); return; }   // the typing box: a normal click, the box takes the keyboard
    e.preventDefault(); e.stopPropagation();
    const ed = edge(e.clientX, e.clientY);
    if (!ed && overX(e.clientX, e.clientY)) { post({ close: 1 }); return; }
    st = ed ? { k: 'size', e: ed, x: e.screenX, y: e.screenY, w: innerWidth, h: innerHeight } : { k: 'move', x: e.screenX, y: e.screenY, moved: false };
    try { document.documentElement.setPointerCapture(e.pointerId); } catch (_) {}
    post({ grab: st.k });
  }, true);
  addEventListener('pointerup', e => {
    if (!st) return;
    const s = st; st = null;
    if (s.k === 'move' && !s.moved) { post({ key: 1 }); ui.tap([e.clientX, e.clientY]); }   // a click, not a drag: the page decides
    post({ drop: s.k });
  }, true);
  addEventListener('click', e => { if (!tapping && !inComp(e)) { e.preventDefault(); e.stopPropagation(); } }, true);
});";

    WebView2 web;
    readonly Speech speech;
    public string text = "", answer = "", ahead = "", commit = "", askFile = "", consumedText = "";
    public bool busy, fresh, composing, clickTalk, toggleTalk, hovering, pinned, stuck, liveRevised, fastBusy, dragging;
    int gen, stops, consumed, listenID;
    List<string> did = new();
    Process proc, fastProc;
    (Process p, StreamWriter input, string commit)? warm;
    // Selected text or the text cursor in the user's app, read at the shortcut; replacement: what Replace / Insert / Delete puts there.
    TextSelection selection;
    string replacement, mode = "replace", warmSel = "", lastAsk = "";   // mode: replace · delete · draft
    int selectionID;
    bool selectionPending, replacing;
    DateTime began = DateTime.Now;
    public DateTime lastDone = DateTime.MinValue;
    System.Windows.Forms.Timer holdTimer, pauseItem, aheadItem, quiet;
    Point grabOrigin; Rectangle grabFrame;
    MiniScreen mini;
    DateTime guardUntil, allowUntil;   // the focus guard: on while a task runs and 2 s after; off for 3 s after a click on the mini screen
    bool listening => speech.Running;
    public bool Listening => listening;
    double Dpi => DeviceDpi / 96.0;

    public Overlay(Speech s)
    {
        speech = s;
        FormBorderStyle = FormBorderStyle.None; ShowInTaskbar = false; TopMost = true; StartPosition = FormStartPosition.Manual;
        BackColor = Color.Black;
        Size = new Size((int)(Math.Max(340, Prefs.Num("boxW")) * DeviceDpi / 96.0), (int)(76 * DeviceDpi / 96.0));
        Opacity = 0;
    }
    protected override bool ShowWithoutActivation => true;
    protected override CreateParams CreateParams { get { var c = base.CreateParams; c.ExStyle |= Native.WS_EX_TOOLWINDOW | Native.WS_EX_NOACTIVATE | Native.WS_EX_TOPMOST; return c; } }

    public async Task Init()
    {
        CreateHandle();
        Native.Backdrop(Handle);
        if (Environment.GetEnvironmentVariable("ATTICUS_CAPTURABLE") != "1")   // debugging: let screenshots see the box
            Native.SetWindowDisplayAffinity(Handle, 0x11);   // never in screen shares, recordings, or Atticus's own screenshots
        web = await Web.Load(this, "ui.html", OnMessage, true, PointerScript);
        mini = new MiniScreen(this) { Publish = v => js("mini", v) };
        Hooks.Guard = Guard;
        Hooks.EscWanted = () => Visible && Opacity > 0;
        Hooks.Escape = Escape;
        Hooks.Shortcut = Shortcut;
        Hooks.ClickAt = (x, y) =>   // click anywhere else closes the box once the answer is up
        {
            if (!Visible || busy || listening || stuck || Bounds.Contains(x, y)) return;
            Close(false);
        };
        SystemEvents_DisplayChanged();
    }

    void SystemEvents_DisplayChanged() => Microsoft.Win32.SystemEvents.DisplaySettingsChanged += (_, _) => BeginInvoke(() => Frame(Bounds));

    // ---- small helpers ----
    public void js(string call, object arg = null) => Web.Js(web, arg == null ? $"ui.{call}()" : $"ui.{call}(...{Web.Json(new[] { arg })})");
    static void After(double secs, Action a) { var t = new System.Windows.Forms.Timer { Interval = Math.Max(1, (int)(secs * 1000)) }; t.Tick += (_, _) => { t.Dispose(); a(); }; t.Start(); }
    static System.Windows.Forms.Timer Later(double secs, Action a) { var t = new System.Windows.Forms.Timer { Interval = Math.Max(1, (int)(secs * 1000)) }; t.Tick += (_, _) => { t.Stop(); a(); }; t.Start(); return t; }
    static void Stop(ref System.Windows.Forms.Timer t) { t?.Stop(); t?.Dispose(); t = null; }
    public static string TranscriptKey(string s) => string.Join(" ", s.ToLowerInvariant().Split((char[])null, StringSplitOptions.RemoveEmptyEntries)).Trim(' ', '.', ',', '!', '?', ':', ';');
    double Linger(double s) => s * (Prefs.Str("linger") switch { "short" => 0.5, "long" => 2, _ => 1 });
    string[] Words() => text.Split(' ', StringSplitOptions.RemoveEmptyEntries);
    string Rest() { var w = Words(); return w.Length > consumed ? string.Join(" ", w.Skip(consumed)) : ""; }

    // Keep the close button and composer reachable on every display.
    void Frame(Rectangle r)
    {
        var scr = Screen.AllScreens.OrderByDescending(s => { var i = Rectangle.Intersect(s.WorkingArea, r); return i.Width * i.Height; }).FirstOrDefault() ?? Screen.PrimaryScreen;
        var m = (int)(8 * Dpi);
        var b = Rectangle.Inflate(scr.WorkingArea, -m, -m);
        r.Width = Math.Min(r.Width, b.Width); r.Height = Math.Min(r.Height, b.Height);
        r.X = Math.Min(Math.Max(r.X, b.Left), b.Right - r.Width); r.Y = Math.Min(Math.Max(r.Y, b.Top), b.Bottom - r.Height);
        Bounds = r;
        js("limit", b.Height / Dpi);
    }

    void Activatable(bool on)
    {
        var ex = Native.GetWindowLong(Handle, -20);
        Native.SetWindowLong(Handle, -20, on ? ex & ~Native.WS_EX_NOACTIVATE : ex | Native.WS_EX_NOACTIVATE);
    }
    void TakeKeys()   // the user clicked or is about to type in the box
    {
        pinned = true;
        Activatable(true);
        Native.keybd_event(0xE8, 0, 0, IntPtr.Zero); Native.keybd_event(0xE8, 0, 2, IntPtr.Zero);   // counts as input: Windows lets us come forward
        Activate(); Native.SetForegroundWindow(Handle);
        web?.Focus();
    }

    void OnMessage(string name, JsonElement b)
    {
        if (name == "w") { Pointer(b); return; }
        if (name == "h")   // the page reports its height; the box grows down from the top
        {
            if (b.ValueKind != JsonValueKind.Number) return;
            var h = (int)Math.Round(b.GetDouble() * Dpi);
            if (h <= 0 || h == Height) return;
            Frame(new Rectangle(Left, Top, Width, h));
            return;
        }
        if (name != "c" || b.ValueKind != JsonValueKind.Object) return;
        if (b.TryGetProperty("boxH", out var bh)) Prefs.D["boxH"] = bh.GetDouble();
        if (b.TryGetProperty("miniRect", out var mr)) mini.Layout(mr);
        if (b.TryGetProperty("miniOpen", out _))   // a click on the mini screen: the user wants that window
        {
            allowUntil = DateTime.Now.AddSeconds(3); mini.Open();
            if (!busy) { mini.Reset(); pinned = false; composing = false; Hide(3); }
        }
        if (b.TryGetProperty("miniClear", out _)) mini.Reset();
        if (b.TryGetProperty("focus", out var f))
        {
            composing = f.GetBoolean();
            if (!composing && !hovering && !busy) Hide(3);
        }
        if (b.TryGetProperty("type", out _)) { fresh = false; if (listening) Hush(); }   // clicked or typed in the box while listening: stop the mic quietly
        if (b.TryGetProperty("copy", out var cp) && cp.GetString() is { Length: > 0 } a) { try { Clipboard.SetText(a); } catch { } js("copied"); }
        if (b.TryGetProperty("send", out var sd)) Typed(sd.GetString(), true);
        if (b.TryGetProperty("replace", out _) && !busy) ApplyReplacement();
        if (b.TryGetProperty("retry", out _) && !busy && !listening && lastAsk != "") Typed(lastAsk, true);
        if (b.TryGetProperty("mic", out _))
        {
            fresh = false;
            if (!busy && !listening) { clickTalk = true; Start(); if (!listening) clickTalk = false; }
        }
        if (b.TryGetProperty("ready", out _))
        {
            js("size", Prefs.Num("boxH"));
            Web.Js(web, "document.documentElement.style.background='rgba(18,18,20,.5)'");   // the Mac app tints natively now; the acrylic here needs the page's tint
        }
        if (b.TryGetProperty("safe", out _)) { Prefs.Set("safe", true); js("mode", true); Program.Settings.Push(); }
        if (b.TryGetProperty("pick", out var pk) && askFile != "") { Answer(pk.GetString() ?? ""); }
        if (b.TryGetProperty("allow", out var al) && askFile != "") { Answer(al.GetBoolean() ? "y" : "n"); }
        if (b.TryGetProperty("off", out var of) && of.GetString() is { Length: > 0 } k && k != "safe" && Prefs.Defaults.GetValueOrDefault(k) is bool)
        { Prefs.Set(k, false); Program.Settings.Push(); }   // "Turn off tips", "Don't ask again" on a card
        if (b.TryGetProperty("improve", out var im)) { Prefs.Set("improve", im.GetBoolean()); Prefs.Set("improveAsked", true); Program.Settings.Push(); }
        if (b.TryGetProperty("comp", out _)) { }
    }

    // Focus guard: while a task runs (and 2 s after), a window that comes forward by itself (a launch, a browser tab, a player)
    // goes straight back behind the user's app. Not when the user asked to see it (atticus.py's "focus" file, for the ask),
    // a helper raised it on purpose ("raise", 3 s), the user just clicked or typed, or clicked the mini screen.
    bool Guard(IntPtr h)
    {
        var now = DateTime.Now; var mine = Hooks.MineHwnd;
        if (!(busy || now < guardUntil) || now < allowUntil || (now - Hooks.LastInput).TotalSeconds < 0.6 || mine == IntPtr.Zero || mine == h || !Native.IsWindow(mine)) return false;
        try
        {
            if (File.Exists(Path.Combine(Paths.Config, "focus")) || (DateTime.UtcNow - File.GetLastWriteTimeUtc(Path.Combine(Paths.Config, "raise"))).TotalSeconds < 3) return false;
        }
        catch { }
        Native.GetWindowThreadProcessId(h, out var a); Native.GetWindowThreadProcessId(mine, out var b);
        if (a == b) return false;   // the user's own app opened a window (a dialog, a second window)
        BeginInvoke(() => Native.Bring(mine));
        return true;
    }

    void Answer(string s)   // whole, or the waiting side reads it half-written
    {
        try { File.WriteAllText(askFile + ".tmp", s); File.Move(askFile + ".tmp", askFile, true); } catch { }
        askFile = "";
    }

    void Pointer(JsonElement b)
    {
        if (b.TryGetProperty("hover", out var hv))
        {
            hovering = hv.GetBoolean(); js("hover", hovering);
            if (!hovering && !busy) Hide(3);   // mouse gone for a few seconds = close
        }
        if (b.TryGetProperty("xhot", out var xh)) js("xhot", xh.GetBoolean());
        if (b.TryGetProperty("close", out _)) Close(true);
        if (b.TryGetProperty("key", out _)) TakeKeys();
        if (b.TryGetProperty("grab", out _)) { pinned = true; dragging = true; grabOrigin = Location; grabFrame = Bounds; }
        if (b.TryGetProperty("move", out var mv))
        {
            stuck = true;   // dragged: only ✕ closes it
            Frame(new Rectangle(grabOrigin.X + (int)mv[0].GetDouble(), grabOrigin.Y + (int)mv[1].GetDouble(), Width, Height));
        }
        if (b.TryGetProperty("width", out var wd))
        {
            var w = (int)wd.GetDouble();
            var left = b.TryGetProperty("left", out var l) && l.GetBoolean();
            Frame(new Rectangle(left ? grabFrame.Right - w : grabFrame.Left, Top, w, Height));
            Prefs.D["boxW"] = w / Dpi;
        }
        if (b.TryGetProperty("drop", out var dr)) { dragging = false; if (dr.GetString() == "size") Prefs.Set("boxH", Prefs.Num("boxH")); }
    }

    // ---- showing and hiding ----
    public void ShowBox()
    {
        gen++; js("visible", true); js("mode", Prefs.Bool("safe"));
        if (Visible && Opacity > 0) { Fade(1, 0.18); return; }   // still up: stay where the user dragged it
        var scr = Hooks.MineHwnd != IntPtr.Zero ? Screen.FromHandle(Hooks.MineHwnd) : Screen.FromPoint(Cursor.Position);
        var vf = scr.WorkingArea;
        var w = (int)(Math.Max(340, Prefs.Num("boxW")) * Dpi);
        var x = Prefs.Str("corner") == "top-right" ? vf.Right - w - (int)(14 * Dpi) : vf.Left + (int)(14 * Dpi);
        Opacity = 0;
        if (!Visible) Show();
        Frame(new Rectangle(x, vf.Top + (int)(10 * Dpi), w, Height));
        Fade(1, 0.18);
    }

    System.Windows.Forms.Timer fader;
    void Fade(double to, double secs, Action done = null)
    {
        fader?.Dispose();
        var from = Opacity; var t0 = DateTime.Now;
        fader = new System.Windows.Forms.Timer { Interval = 15 };
        fader.Tick += (_, _) =>
        {
            var k = Math.Min(1, (DateTime.Now - t0).TotalSeconds / secs);
            Opacity = from + (to - from) * k;
            if (k >= 1) { fader.Dispose(); fader = null; done?.Invoke(); }
        };
        fader.Start();
    }

    public void Hide(double s)
    {
        var g = gen;
        After(s, () =>
        {
            if (g != gen || busy || listening || hovering || pinned || composing || Voice.Speaking || mini?.Holds == true) return;
            Fade(0, 0.3, () => { if (g == gen) Gone(); });
        });
    }

    void Gone()
    {
        mini?.Reset();
        Hide(); fresh = false; pinned = false; stuck = false; composing = false; js("compose", false); js("visible", false);
        Activatable(false);
    }

    // Esc: stop listening, stop the answer, or close the box.
    void Escape()
    {
        if (Voice.Shush()) return;
        if (listening) { Hush(); Sound.Cue("soundStop"); js("stopped", "Stopped listening"); Hide(1.5); }
        else if (busy) Cancel();
        else if (Visible) Close(false);
    }

    public void Hush()   // stop listening, drop what was heard, say nothing
    {
        mini?.Reset();
        speech.Cancel(); Stop(ref pauseItem); Stop(ref holdTimer); Unwarm();
        clickTalk = false; toggleTalk = false; Stop(ref quiet); stops++;
        listenID++; selectionID++; selectionPending = false; replacing = false;
        CancelFast();
        Stop(ref aheadItem); DropAhead();
        Py.Stop(proc); proc = null; askFile = "";
        busy = false;
    }

    void Dismiss()   // the shortcut again before anything happened: say so, then go
    {
        if (listening) Hush();
        Sound.Cue("soundStop"); js("stopped", "Answer stopped");
        var g = gen; After(0.5, () => { if (g == gen) Close(false); });
    }

    void Cancel()
    {
        Hush(); Voice.Shush();
        Sound.Cue("soundStop"); js("stopped", "Answer stopped"); Hide(1.5);
    }

    // ✕ also ends the conversation; clicking away or timing out keeps it for 5 minutes.
    public void Close(bool clear)
    {
        Voice.Shush(); Hush();
        if (clear) { try { File.Delete(Paths.Recent); } catch { } lastDone = DateTime.MinValue; }
        gen++;
        var g = gen;
        Fade(0, 0.2, () => { if (g == gen) Gone(); });
    }

    bool shortcutDown;
    void Shortcut(bool pressed)
    {
        if (!pressed)
        {
            shortcutDown = false;
            if (listening && !toggleTalk && !clickTalk) StopListening();
            return;
        }
        if (shortcutDown) return;   // one action per physical press
        shortcutDown = true;
        if (Prefs.Bool("paused")) return;
        if (Program.Meeting.Running) { Program.Meeting.Focus(); return; }   // in a meeting the shortcut opens its panel (its own ask box)
        if (Voice.Shush()) return;
        if (listening) { if (toggleTalk) StopListening(); else Dismiss(); return; }
        if (busy) { Cancel(); return; }
        Start();
    }

    void Review(string message)
    {
        Hush(); js("msg", message);
        TakeKeys(); js("draft", text);
    }

    // ---- listening ----
    public void Start()
    {
        if (Prefs.Bool("paused")) return;
        Voice.Shush();
        if (listening) { Dismiss(); return; }
        if (busy) { Cancel(); return; }
        if (fresh && Visible && Opacity > 0) { Dismiss(); return; }
        CancelFast();
        CaptureSelection(); replacement = null;
        text = ""; ahead = ""; consumed = 0; consumedText = ""; liveRevised = false; did = new();
        if ((!Visible || Opacity == 0) && (DateTime.Now - lastDone).TotalSeconds > 30) { js("reset"); try { File.Delete(Paths.Recent); } catch { } }
        Program.Onboarding.Tried();
        if (Prefs.Str("input") == "type" && !clickTalk)   // Settings: the shortcut opens the box ready to type
        {
            if (composing && Visible) { Dismiss(); return; }
            var id0 = selectionID;
            void open() { if (selectionID != id0) return; ShowBox(); TakeKeys(); js("compose", true); }
            if (selectionPending) Selected = open; else open();   // the box takes the keyboard only once the selection is read
            return;
        }
        toggleTalk = !clickTalk && Prefs.Str("talkMode") == "toggle";
        Sound.Cue("soundStart");
        Unwarm(); if (!selectionPending) warm = Spawn();   // else once the selection is read, so the worker has it
        hovering = false;
        js("listen");
        js("shortcut", toggleTalk ? "Press again to send" : clickTalk ? "" : "Release keys to send");
        ShowBox();
        if (!speech.Ready)
        {
            Hush(); js("msg", File.Exists(Paths.Model) ? "Speech is still loading. Try again in a moment." : "Atticus needs its speech model. Setup is open to get it.");
            if (!File.Exists(Paths.Model)) Program.Onboarding.Open();
            Hide(6); return;
        }
        speech.Prompt = Hints();
        var id = ++listenID;
        var err = speech.Start(v => { if (listenID == id) js("level", v); }, w => { if (listenID == id) Heard(w); });
        if (err != "") { Hush(); js("msg", err); Hide(6); return; }
        began = DateTime.Now;
        holdTimer = new System.Windows.Forms.Timer { Interval = 150 };
        holdTimer.Tick += (_, _) =>
        {
            if (!listening) { Stop(ref holdTimer); return; }
            var held = (DateTime.Now - began).TotalSeconds;
            if (clickTalk && text.Length == 0 && held > 8) { Stop(ref holdTimer); Hush(); js("stopped", ""); Hide(3); return; }   // nothing said: stop the mic quietly
            if (held > 60) { Stop(ref holdTimer); RecordingLimit(); return; }
            if (!toggleTalk && !clickTalk && !Hooks.ModsHeld()) { Stop(ref holdTimer); StopListening(); }
        };
        holdTimer.Start();
    }

    // Words Whisper should expect: the user's app names plus a few of the user's own (free, on this PC).
    static string Hints()
    {
        var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        var words = new List<string> { "Atticus", "Chrome", "VS Code", "Spotify", "Notepad" };
        try
        {
            var me = JsonDocument.Parse(File.ReadAllText(Path.Combine(Paths.Config, "me.json"))).RootElement;
            if (me.TryGetProperty("name", out var n) && n.GetString() is { Length: > 0 } nm) words.Add(nm);
        }
        catch { }
        try { foreach (var p in JsonDocument.Parse(File.ReadAllText(Path.Combine(Paths.Config, "skills.json"))).RootElement.EnumerateObject()) words.Add(p.Name); } catch { }
        var list = words.Where(w => w.Length > 0 && seen.Add(w)).Take(40);
        return "Open " + string.Join(", ", list) + ".";
    }

    void RecordingLimit()
    {
        if (toggleTalk) Review("Recording paused after 60 seconds. Review your words and press Enter to send.");
        else StopListening();
    }

    void Heard(string now)
    {
        if (!listening && !busy) return;
        text = now; if (now.Trim().Length > 0) fresh = false;
        js("words", text);
        if (consumed > 0 && TranscriptKey(string.Join(" ", Words().Take(consumed))) != consumedText) liveRevised = true;
        if (Prefs.Bool("live"))   // instant commands (open, move, search, play) run mid-sentence; the AI still waits for confirmed words
        {
            TryFast(); Stop(ref pauseItem);
            pauseItem = Later(0.55, TryFast);
        }
        if (clickTalk) { Stop(ref quiet); quiet = Later(1.6, StopListening); }   // no keys to let go of: a pause ends it
        if (ahead != "" && TranscriptKey(Rest()) != TranscriptKey(ahead)) DropAhead();   // the user kept talking: that start was for half a sentence
        Stop(ref aheadItem);   // a longer pause = the user is probably done: start the AI now, keys still held
        if (!Prefs.Bool("ahead")) return;
        aheadItem = Later(Prefs.Num("aheadDelay"), StartAhead);
    }

    static readonly string[] Commands = { "open", "launch", "start", "pull up", "bring up", "focus", "switch to", "go to", "snap", "move", "put", "throw", "send", "make", "play", "search", "google", "look up" };

    void StartAhead()
    {
        var r = Rest();
        if (!listening || fastBusy || selectionPending || liveRevised || r == "" || TranscriptKey(r) == TranscriptKey(ahead)) return;
        var core = Regex.Replace(r.ToLowerInvariant(), @"^(?:(?:hey|ok|okay|atticus|please|can you|could you|would you|also|and|now|then)[,\s]+)+", "");
        if (Commands.Any(c => core.StartsWith(c + " "))) return;
        DropAhead();
        ahead = r;
        Run(r, true);
        warm = Spawn();   // ready again in case the user keeps talking
    }

    // Runs `atticus.py --fast` on the words not yet used; exit 0 means it was a simple command and it is done.
    void CancelFast() { Py.Stop(fastProc); fastProc = null; fastBusy = false; }

    void TryFast()
    {
        var w = Words();
        if (!Prefs.Bool("live") || !Prefs.Bool("instant") || !listening || fastBusy || liveRevised || w.Length <= consumed) return;
        var seg = string.Join(" ", w.Skip(consumed)); var n = w.Length; var prefix = TranscriptKey(string.Join(" ", w));
        Process p;
        try { p = Py.Start(new[] { "--fast", seg }, new() { ["ATTICUS_FRONT"] = Hooks.Mine, ["ATTICUS_LIVE"] = "1" }); } catch { return; }
        fastProc = p; fastBusy = true;
        Task.Run(() =>
        {
            var line = p.StandardOutput.ReadToEnd().Trim();
            p.WaitForExit();
            BeginInvoke(() =>
            {
                if (fastProc != p) return;
                fastProc = null; fastBusy = false;
                if (p.ExitCode == 0 && line.StartsWith("✓"))
                {
                    consumed = n; consumedText = prefix;
                    if (TranscriptKey(string.Join(" ", Words().Take(n))) != prefix) liveRevised = true;
                    did.Add(line[2..]);
                    js("line", "▸ " + line[2..]);
                }
            });
        });
    }

    void DropAhead()
    {
        if (ahead != "") { Py.Stop(proc); proc = null; ahead = ""; if (listening) busy = false; }
    }

    void StopListening()
    {
        if (!listening || (clickTalk && text.Length == 0)) return;
        Stop(ref holdTimer);
        clickTalk = false; toggleTalk = false; Stop(ref quiet);
        busy = true;
        js("think");   // keys are up: stop saying Listening now, not when the last words land
        stops++;
        var g = stops;
        void once(string final)
        {
            if (g != stops) return;
            stops++; listenID++; Stop(ref pauseItem);
            if (final == null) { Review("Couldn't confirm your words. Check them and press Enter."); return; }
            if (final.Length > 0 && final != text)
            {
                text = final; js("words", text);
                if (consumed > 0 && TranscriptKey(string.Join(" ", Words().Take(consumed))) != consumedText) liveRevised = true;
            }
            Finish();
        }
        var t = speech.Stop();
        t.ContinueWith(r => BeginInvoke(() => once(r.IsCompletedSuccessfully ? r.Result : null)));
        After(8, () => { if (g == stops) { speech.Cancel(); once(null); } });   // a final pass that hangs: offer the words for review
    }

    void Typed(string t, bool keep = false)   // the typing box (keep: it stays open for the next one)
    {
        if (busy || listening || string.IsNullOrWhiteSpace(t)) return;
        if (selectionPending) { var id = selectionID; After(0.05, () => { if (selectionID == id) Typed(t, keep); }); return; }
        replacement = null;
        fresh = false; js("listen", keep); ShowSelected(); ShowBox(); js("words", t); Run(t);
    }

    void Finish()
    {
        if (!busy) return;   // cancelled while the last words landed
        if (fastBusy || selectionPending) { var g = stops; After(0.05, () => { if (g == stops) Finish(); }); return; }
        Stop(ref aheadItem);
        if (liveRevised)
        {
            Hush(); js("msg", "Words changed after an instant command. Review before sending another request.");
            TakeKeys(); js("draft", text); return;
        }
        var rest = Rest();
        if (rest != "" && TranscriptKey(rest) == TranscriptKey(ahead) && proc is { HasExited: false })   // already started on exactly this while the user talked
        {
            ahead = ""; Unwarm();
            try { File.WriteAllText(commit, "go"); } catch { Cancel(); js("msg", "Could not confirm the request. Please try again."); }
            return;
        }
        DropAhead();
        if (rest != "") { Run(rest); return; }
        busy = false; Unwarm();   // nothing left for the AI
        if (did.Count == 0) { js("msg", "Didn't catch that."); Hide(1.2); return; }
        answer = "✓ " + string.Join(" ", did);
        Deliver(string.Join(" ", did));
        js("line", answer); lastDone = DateTime.Now; Sound.Cue("soundDone"); Hide(Linger(5));
    }

    // ---- the worker ----
    // atticus.py --wait, started at key-down so Claude is up by the time the user lets go; Run() hands it the words.
    (Process p, StreamWriter input, string commit)? Spawn()
    {
        var c = Path.Combine(Path.GetTempPath(), "atticus-commit-" + Guid.NewGuid());
        Process p;
        var sel = SelKey;
        try
        {
            p = Py.Start(new[] { "--wait" }, new() { ["ATTICUS_UI"] = "1", ["ATTICUS_FRONT"] = Hooks.Mine, ["ATTICUS_COMMIT"] = c, ["ATTICUS_FOCUS_GUARD"] = "1",
                ["ATTICUS_SELECTION"] = sel == "\u0001" ? "" : sel, ["ATTICUS_CARET"] = sel == "\u0001" ? "1" : "" }, input: true);
        }
        catch { return null; }
        warmSel = sel;
        p.OutputDataReceived += (_, e) => { if (e.Data != null) BeginInvoke(() => OnLine(p, e.Data)); };
        p.ErrorDataReceived += (_, e) => { if (e.Data != null) Program.Err(e.Data); };
        p.BeginOutputReadLine(); p.BeginErrorReadLine();
        p.EnableRaisingEvents = true;
        p.Exited += (_, _) =>
        {
            p.WaitForExit();   // the answer may still be in the pipe
            BeginInvoke(() =>
            {
                try { File.Delete(c); } catch { }
                if (proc != p) return;   // a cancelled run ending late must not touch the next one
                if (ahead != "") { proc = null; ahead = ""; busy = listening; return; }
                busy = false; proc = null; guardUntil = DateTime.Now.AddSeconds(2); mini.Finish(); js("finished");   // a player can jump forward ~0.5 s after it returns
                if (answer != "") { lastDone = DateTime.Now; Sound.Cue("soundDone"); }
                Hide(Linger(Math.Max(6, Math.Min(30, answer.Length / 12.0))));   // longer answers stay longer
            });
        };
        return (p, p.StandardInput, c);
    }

    void OnLine(Process p, string line)
    {
        if (proc != p) return;   // buffered output from a cancelled request stays cancelled
        if (line == "! setup") { Program.Onboarding.Open(); return; }   // Claude not installed, signed out or no plan
        if (line.StartsWith("▣ ")) { if (Prefs.Bool("miniScreen")) mini.Receive(line[2..]); return; }   // the window Atticus works in: the mini screen
        if (line.StartsWith("@ ")) { if (mini.HasTarget) js("line", line); else Ring.At(line); return; }   // where it clicked in the mini screen, or experimental "show me where"
        if (line == "! improve") { ShowBox(); js("improve"); return; }   // once, after a task that worked: Help improve Atticus?
        if (line.StartsWith("? "))   // safe mode: "? <question>\t<command>\t<answer file>[\t<setting it can turn off>[\tretry]]"
        {
            var q = line[2..].Split('\t');
            if (q.Length is < 3 or > 5) return;
            askFile = q[2]; ShowBox(); js("ask", q.Length == 3 ? new[] { q[0], q[1] } : new[] { q[0], q[1], q[3], q.Length == 5 ? q[4] : "" }); return;
        }
        if (line.StartsWith("⚙ "))   // "⚙ <key> <true|false|option>": the AI changes one of Atticus's settings; never pause, and Safe mode only on
        {
            var q = line.Split(' ');
            if (q.Length != 3 || q[1] == "paused") return;
            var def = Prefs.Defaults.GetValueOrDefault(q[1]);
            if (def is bool && (q[1] != "safe" || q[2] == "true")) Program.Settings.Apply(q[1], q[2] == "true");
            else if (def is string) Program.Settings.Apply(q[1], q[2]);   // a choice setting: "⚙ model sonnet"
            else return;
            Program.Settings.Push();
            return;
        }
        if (line.StartsWith("◇ "))   // "which one?": the choices card
        {
            try
            {
                using var d = JsonDocument.Parse(line[2..]);
                askFile = d.RootElement.GetProperty("a").GetString() ?? "";
                ShowBox(); Web.Js(web, $"ui.pick({line[2..]})");
            }
            catch { }
            return;
        }
        if (line.StartsWith("↺ "))   // "↺ {t, mode}": new text for the selection (replace · delete) or a draft for the cursor (draft · insert)
        {
            try
            {
                using var d = JsonDocument.Parse(line[2..]);
                var draft = d.RootElement.GetProperty("t").GetString() ?? "";
                var m = d.RootElement.TryGetProperty("mode", out var md) ? md.GetString() : "replace";
                var now = m == "insert";   // the user asked to put it in: no Insert click needed
                replacement = draft; mode = now ? "draft" : m; pinned = true;
                var target = selection != null && (selection.Editable || selection.Pastable) && (mode != "draft" || selection.Caret);
                js("rewrite", new object[] { draft, mode, target });
                if (mode == "draft" && target && (now || Prefs.Str("draftInsert") == "auto")) ApplyReplacement();
            }
            catch { }
            return;
        }
        if (line.StartsWith("✓")) { answer = line; mini.Finish(); Deliver(replacement is { Length: > 0 } r ? r : line.Length > 2 ? line[2..] : ""); }
        if (line.StartsWith("! ")) js("msg", line[2..]);
        else js("line", line);
    }

    void Unwarm() { if (warm is { } w) Py.Stop(w.p); warm = null; }

    void Run(string t, bool speculative = false)
    {
        mini.Reset(); busy = true; answer = "";
        try { File.Delete(Path.Combine(Paths.Config, "focus")); } catch { }   // a new ask: "show it" from the last one no longer counts
        if (!speculative) js("think");
        if (warm is { } w0 && w0.p.HasExited) warm = null;
        if (warm != null && warmSel != SelKey) Unwarm();   // started before the selection changed
        if (!speculative) lastAsk = t;
        var w = warm ?? Spawn();
        if (w == null) { busy = false; js("msg", "Could not start atticus.py. Run setup again."); Hide(3); return; }
        warm = null; proc = w.Value.p; commit = w.Value.commit;
        if (!speculative)
        {
            try { File.WriteAllText(commit, "go"); }
            catch { Cancel(); js("msg", "Could not confirm the request. Please try again."); return; }
        }
        try { w.Value.input.Write(Hooks.Mine + "\t" + t.Replace("\n", " ") + "\n"); w.Value.input.Close(); }
        catch { Cancel(); js("msg", "The request worker stopped. Please try again."); }
    }

    // ---- selected text ----
    Action Selected;   // waiting for the selection (typing mode opens the box after it)
    string SelKey => Prefs.Bool("selection") && selection != null ? selection.Caret ? "\u0001" : selection.Text : "";   // \u0001: no highlight, the cursor sits in a text box
    void ShowSelected() { if (selection is { Caret: false } s) js("selected", s.Text); }

    void CaptureSelection()   // at the shortcut, before Atticus can take the focus
    {
        var id = ++selectionID;
        selection = null; Selected = null; selectionPending = Prefs.Bool("selection");
        if (!selectionPending) return;
        TextSelection.CaptureAsync(Native.GetForegroundWindow()).ContinueWith(t => BeginInvoke(() =>
        {
            if (selectionID != id) return;   // cancelled, or a newer press
            selectionPending = false; selection = Prefs.Bool("selection") ? t.Result : null;
            if (listening) ShowSelected();   // typing: Typed shows it with the request
            if (warm != null && warmSel != SelKey) Unwarm();
            if (warm == null && listening) warm = Spawn();
            var then = Selected; Selected = null; then?.Invoke();
        }));
    }

    // Replace / Delete / Insert: the text goes to the saved selection, or for a draft to the saved cursor.
    async void ApplyReplacement()
    {
        if (replacement is not { } draft || replacing) return;
        if (!Prefs.Bool("selection") || selection is not { } sel || (mode == "draft" && !sel.Caret)) { js("replaced", false); return; }
        replacing = true; var id = selectionID;
        bool ok;
        try { ok = await sel.Apply(mode == "delete" ? "" : draft, () => selectionID == id && replacing && Prefs.Bool("selection") && !Prefs.Bool("paused"), Handle); }
        catch { ok = false; }
        if (selectionID != id) return;
        replacing = false;
        if (ok) { replacement = null; selection = null; }
        js("replaced", ok);
    }

    void Deliver(string a)   // Settings: read answers aloud, copy them
    {
        if (Prefs.Bool("autoCopy")) { try { Clipboard.SetText(a); } catch { } }
        if (Prefs.Bool("speak")) Voice.Say(a, () => Hide(2));
    }

    // Test hook: ATTICUS_DEMO="<request>" runs it once without the mic.
    public void Demo(string t) { js("listen"); ShowBox(); js("words", t); Run(t); }
}
