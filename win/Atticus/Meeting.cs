// Meeting Copilot: a private side panel (ui\meeting.html) that hears the call (whatever plays on this PC, through WASAPI
// loopback) and your microphone, transcribes both with Whisper on this PC, spots questions and suggests answers through
// atticus.py (one tool-free Haiku call on your Claude plan). Capture starts only from Start listening; audio is never saved
// or sent. With saving on, text goes to %APPDATA%\Atticus\meetings\<id>\ (metadata.json + append-only events.jsonl).
using System.Diagnostics;
using System.Drawing.Imaging;
using System.IO.Compression;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;
using Microsoft.Web.WebView2.WinForms;
using NAudio.CoreAudioApi;
using NAudio.Wave;

namespace Atticus;

// The journal: one folder per meeting, each event one line, flushed through to disk (atticus.py reads, searches and recaps it).
sealed class MeetingArchive
{
    public readonly string Id, Folder;
    public static string Root => Path.Combine(Paths.Config, "meetings");
    public MeetingArchive(string title, string app, DateTime started, string root = null)
    {
        root ??= Root;
        if (Directory.Exists(root) && new DirectoryInfo(root).LinkTarget != null) throw new IOException("The meetings folder is a link");
        Id = Guid.NewGuid().ToString().ToUpperInvariant();
        Folder = Path.Combine(root, Id);
        Directory.CreateDirectory(Folder);
        var meta = new SortedDictionary<string, object> { ["app"] = app, ["started"] = Unix(started), ["title"] = title, ["version"] = 1 };
        File.WriteAllText(Path.Combine(Folder, "metadata.json"), JsonSerializer.Serialize(meta), new UTF8Encoding(false));
        using (new FileStream(Path.Combine(Folder, "events.jsonl"), FileMode.CreateNew)) { }
    }
    public static double Unix(DateTime t) => Math.Round((t.ToUniversalTime() - DateTime.UnixEpoch).TotalSeconds, 3);
    // Opened per line at the end of the file as it is then, so a recap atticus.py appended in between is never overwritten.
    public void Append(object e)
    {
        var b = Encoding.UTF8.GetBytes(JsonSerializer.Serialize(e) + "\n");
        using var f = new FileStream(Path.Combine(Folder, "events.jsonl"), FileMode.Append, FileAccess.Write, FileShare.ReadWrite | FileShare.Delete, 4096, FileOptions.WriteThrough);
        f.Write(b); f.Flush(true);
    }
    public static void Export(string text, string path)   // whole or not at all
    {
        var tmp = Path.Combine(Path.GetDirectoryName(Path.GetFullPath(path))!, ".atticus-" + Guid.NewGuid().ToString("N"));
        try { File.WriteAllText(tmp, text, new UTF8Encoding(false)); File.Move(tmp, path, true); }
        finally { try { File.Delete(tmp); } catch { } }
    }
}

// 16 kHz mono for Whisper from any capture format: channels averaged, then each output sample is the mean of its source span.
sealed class Downsampler
{
    readonly double step; long seen, made; double sum; int n; float last;
    public Downsampler(int rate) => step = rate / 16000.0;
    public float[] Push(ReadOnlySpan<float> mono)
    {
        var o = new List<float>((int)(mono.Length / step) + 2);
        foreach (var s in mono)
        {
            var at = (long)(seen++ / step);
            while (at > made) { if (n > 0) last = (float)(sum / n); o.Add(last); made++; sum = 0; n = 0; }
            sum += s; n++;
        }
        return o.ToArray();
    }
    public static float[] Mono(byte[] buf, int bytes, WaveFormat f)
    {
        int ch = Math.Max(1, f.Channels), size = f.BitsPerSample / 8, frames = bytes / (size * ch);
        var o = new float[frames];
        bool flt = f.Encoding == WaveFormatEncoding.IeeeFloat || f.Encoding == WaveFormatEncoding.Extensible && f.BitsPerSample == 32;   // WASAPI's shared mix is float
        for (int i = 0, p = 0; i < frames; i++)
        {
            float s = 0;
            for (int c = 0; c < ch; c++, p += size)
                s += flt ? BitConverter.ToSingle(buf, p) : size == 2 ? BitConverter.ToInt16(buf, p) / 32768f
                    : size == 3 ? ((buf[p] << 8 | buf[p + 1] << 16 | buf[p + 2] << 24) >> 8) / 8388608f : BitConverter.ToInt32(buf, p) / 2147483648f;
            o[i] = s / ch;
        }
        return o;
    }
}

// Cuts one stream into utterances for Whisper: speech starts above the room's noise floor and ends after 0.8 s of quiet (or at 15 s).
sealed class Segmenter
{
    public const int Rate = 16000, Frame = 480;   // 30 ms
    readonly List<float> cur = new(), pend = new();
    int quiet, voiced; float noise = 0.004f;
    public float Level;
    public int Cuts;   // finished utterances so far: a partial read of an older one is stale
    public bool Speaking => voiced > 0;
    public float[] Current() => Speaking ? cur.ToArray() : null;
    public List<float[]> Push(ReadOnlySpan<float> s)
    {
        var done = new List<float[]>();
        foreach (var x in s) { pend.Add(x); if (pend.Count == Frame) { Step(done); pend.Clear(); } }
        return done;
    }
    void Step(List<float[]> done)
    {
        double e = 0; foreach (var x in pend) e += x * x;
        var rms = (float)Math.Sqrt(e / Frame); Level = rms;
        noise = rms < noise ? rms : noise + (rms - noise) * 0.002f;   // falls fast, rises slowly: a steady hum stops counting as speech
        var loud = rms > Math.Max(0.01f, noise * 3);
        cur.AddRange(pend);
        if (!Speaking)
        {
            if (loud) { voiced = 1; quiet = 0; }
            else if (cur.Count > Rate * 3 / 10) cur.RemoveRange(0, cur.Count - Rate * 3 / 10);   // keep 0.3 s before the first word
            return;
        }
        if (loud) { voiced++; quiet = 0; } else quiet++;
        if (quiet * Frame >= Rate * 8 / 10 || cur.Count >= Rate * 15) { var a = Cut(); if (a != null) done.Add(a); }
    }
    public float[] Cut()   // what was said so far, or null if it was only a click or a breath
    {
        var a = voiced * Frame >= Rate / 4 ? cur.ToArray() : null;
        cur.Clear(); voiced = 0; quiet = 0; if (a != null) Cuts++;
        return a;
    }
}

#pragma warning disable CS0618   // WasapiLoopbackCapture: NAudio 3 still ships it; system-wide loopback is all this needs
// The two streams: the call (loopback of the default speakers or headset) and the mic. 16 kHz mono floats out, on capture threads.
sealed class MeetingAudio : IDisposable
{
    public Action<float[], bool> OnSamples;   // samples, mic?
    public Action<string> OnError;
    WasapiLoopbackCapture loop; WaveIn mic; Downsampler down;
    System.Threading.Timer pad; DateTime lastLoop = DateTime.Now;
    bool done;
    public string Start(bool withMic)
    {
        try
        {
            loop = new WasapiLoopbackCapture();
            down = new Downsampler(loop.WaveFormat.SampleRate);
            loop.DataAvailable += (_, e) => { lastLoop = DateTime.Now; if (e.BytesRecorded > 0) OnSamples?.Invoke(down.Push(Downsampler.Mono(e.Buffer, e.BytesRecorded, loop.WaveFormat)), false); };
            loop.RecordingStopped += (_, e) => { if (!done && e.Exception != null) OnError?.Invoke("Call audio stopped (the speakers or headset changed). Resume to try again."); };
            loop.StartRecording();
        }
        catch (Exception e) { Dispose(); return "Could not hear this PC's sound: " + e.Message; }
        // Loopback sends nothing while the PC is silent: quiet is fed in, so the last words of a sentence still end it.
        pad = new System.Threading.Timer(_ => { var gap = DateTime.Now - lastLoop; if (gap.TotalMilliseconds > 300) { lastLoop = DateTime.Now; OnSamples?.Invoke(new float[(int)Math.Min(16000, gap.TotalSeconds * 16000)], false); } }, null, 250, 250);
        if (!withMic) return "";
        if (WaveIn.DeviceCount == 0) { Dispose(); return "No microphone is available. Connect one and try again."; }
        try
        {
            mic = new WaveIn { WaveFormat = new WaveFormat(16000, 16, 1), BufferMilliseconds = 60 };
            mic.DataAvailable += (_, e) => OnSamples?.Invoke(Downsampler.Mono(e.Buffer, e.BytesRecorded, mic.WaveFormat), true);
            mic.RecordingStopped += (_, e) => { if (!done && e.Exception != null) OnError?.Invoke("The microphone stopped. Resume to try again."); };
            mic.StartRecording();
        }
        catch (Exception e) { Dispose(); return Speech.MicAllowed() ? "Mic error: " + e.Message : "Allow microphone access in Settings → Privacy & security → Microphone (Let desktop apps access your microphone)."; }
        return "";
    }
    public void Dispose()
    {
        done = true;
        pad?.Dispose(); pad = null;
        try { loop?.StopRecording(); } catch { }
        try { mic?.StopRecording(); } catch { }
        loop?.Dispose(); mic?.Dispose(); loop = null; mic = null;
    }
}

// "Notice when you're in a call": Windows records which apps hold the mic (Privacy → Microphone). A call app holding it = a call.
static class Calls
{
    const string Store = @"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone";
    static readonly (string match, string name)[] Apps =
    {
        ("zoom.exe", "Zoom"), ("ms-teams.exe", "Teams"), ("teams.exe", "Teams"), ("msteams_", "Teams"), ("microsoftteams_", "Teams"),
        ("slack.exe", "Slack"), ("discord.exe", "Discord"), ("whatsapp.exe", "WhatsApp"), ("5319275a.whatsappdesktop_", "WhatsApp"),
        ("webex.exe", "Webex"), ("ciscocollabhost.exe", "Webex"), ("atmgr.exe", "Webex"), ("skype.exe", "Skype"), ("microsoft.skypeapp_", "Skype"),
        ("signal.exe", "Signal"), ("chrome.exe", "Chrome"), ("msedge.exe", "Edge"), ("firefox.exe", "Firefox"), ("brave.exe", "Brave"), ("opera.exe", "Opera"),
    };
    // A ConsentStore key (a package family, or a program path with # for \) -> the call app's name, or null.
    public static string Name(string key)
    {
        var k = key.ToLowerInvariant(); var file = k.Contains('#') ? k[(k.LastIndexOf('#') + 1)..] : k;
        foreach (var (m, n) in Apps) if (m.EndsWith('_') ? file.StartsWith(m) : file == m) return n;
        return null;
    }
    public static HashSet<string> OnMic()
    {
        var on = new HashSet<string>();
        try
        {
            using var root = Microsoft.Win32.Registry.CurrentUser.OpenSubKey(Store);
            if (root == null) return on;
            void Look(Microsoft.Win32.RegistryKey parent, string name, bool program)
            {
                using var k = parent.OpenSubKey(name);
                if (k == null || Name(name) is not { } app) return;
                if (k.GetValue("LastUsedTimeStop") is not long stop || stop != 0 || k.GetValue("LastUsedTimeStart") is not long start || start <= 0) return;
                if (program && Process.GetProcessesByName(Path.GetFileNameWithoutExtension(name[(name.LastIndexOf('#') + 1)..])).Length == 0) return;   // crashed mid-call: Windows never wrote Stop
                on.Add(app);
            }
            foreach (var n in root.GetSubKeyNames()) if (n != "NonPackaged") Look(root, n, false);
            using var desk = root.OpenSubKey("NonPackaged");
            if (desk != null) foreach (var n in desk.GetSubKeyNames()) Look(desk, n, true);
        }
        catch { }
        return on;
    }
}

sealed class MeetingPanel : Form
{
    public MeetingPanel() { FormBorderStyle = FormBorderStyle.None; ShowInTaskbar = false; TopMost = true; StartPosition = FormStartPosition.Manual; BackColor = Color.Black; Text = "Meeting Copilot"; }
    protected override bool ShowWithoutActivation => true;   // a "Meeting detected" card never steals focus from the call
    protected override CreateParams CreateParams { get { var c = base.CreateParams; c.ExStyle |= Native.WS_EX_TOOLWINDOW | Native.WS_EX_TOPMOST; return c; } }
    public double Dpi => DeviceDpi / 96.0;
}

sealed class MeetingCopilot
{
    // The page drags by native mouse tracking on the Mac; here the page reports pointer moves (like the overlay) and hovers.
    const string PanelScript = @"(() => {
  let st = null, down = null, eat = false;
  const send = m => chrome.webview.postMessage(m);
  addEventListener('pointerdown', e => { down = { x: e.screenX, y: e.screenY, id: e.pointerId }; }, true);
  window.webkit = { messageHandlers: new Proxy({}, { get: (_, n) => ({ postMessage: b => {
    if (b && b.do === 'drag' && down) { st = { x: down.x, y: down.y, moved: false }; try { document.documentElement.setPointerCapture(down.id); } catch (_) {} }
    send({ n, b }); } }) }) };
  addEventListener('pointermove', e => {
    if (st) { const dx = e.screenX - st.x, dy = e.screenY - st.y; if (Math.abs(dx) + Math.abs(dy) > 3) st.moved = true;
      if (st.moved) send({ n: 'meeting', b: { do: 'move', dx: dx * devicePixelRatio, dy: dy * devicePixelRatio } }); return; }
    window.meeting && window.meeting.hover([e.clientX, e.clientY]);
  }, true);
  addEventListener('pointerup', () => { if (st && st.moved) { eat = true; send({ n: 'meeting', b: { do: 'rememberFrame' } }); setTimeout(() => eat = false, 50); } st = null; }, true);
  addEventListener('click', e => { if (eat) { e.preventDefault(); e.stopPropagation(); eat = false; } }, true);
  addEventListener('DOMContentLoaded', () => document.documentElement.addEventListener('mouseleave', () => window.meeting && window.meeting.hover([])));
})();";

    // Greetings and tag questions ("How are you?", "…, right?") are the user's to answer, not the AI's (same rule as atticus.py).
    const string SmallTalk = @"^(hi|hey|hello|so|oh|well)?\W*(how are (you|things|we)( doing| going| all)?|how('s| is) (it going|everyone|everybody|life|your day)|how have you been|what'?s up|can (you|everyone|everybody) (hear|see) (me|my screen)|is (everyone|everybody) here|are you there)\W*(\w+\W*)?$|(^|[\s,])(right|okay|ok|yeah|you know|make sense|sound good)\?$";
    public static bool Question(string text, string name)
    {
        var line = text.Trim(); var first = name.Split((char[])null, StringSplitOptions.RemoveEmptyEntries).FirstOrDefault() ?? "";
        if (line == "" || Regex.IsMatch(line, SmallTalk, RegexOptions.IgnoreCase)) return false;
        return line.EndsWith('?') || Regex.IsMatch(line, @"^(what|why|how|when|where|who|which|can|could|should|would|do|does|did|is|are|will)\b", RegexOptions.IgnoreCase)
            || first != "" && Regex.IsMatch(line, @"(?<!\w)" + Regex.Escape(first) + @"(?!\w)", RegexOptions.IgnoreCase);
    }

    // With speakers instead of a headset the mic hears the call too: a mic line that repeats what the call just said is dropped.
    public static bool Echo(string mine, IEnumerable<string> theirs)
    {
        static HashSet<string> W(string s) => Regex.Matches(s.ToLowerInvariant(), @"[\p{L}\p{N}']+").Select(m => m.Value).ToHashSet();
        var w = W(mine);
        return w.Count >= 3 && theirs.Any(t => { var o = W(t); return w.Count(o.Contains) >= 0.6 * w.Count; });
    }

    MeetingPanel panel; WebView2 web;
    public bool Running; public int Generation; public JsonObject Nudge;
    bool loaded, demo, briefEdited, minimized, voice, archiveEnded;
    string brief = "", name = "", meetingTitle = "", callApp = "Call", voiceText = "", voicePartial = "", themPartial = "", youPartial = "", pendingBrief, lastPartial = "";
    DateTime began = DateTime.Now, voiceTail = DateTime.MinValue, levelAt = DateTime.MinValue; double elapsed; int serial; int? answering;
    readonly List<Dictionary<string, object>> transcript = new();
    readonly Queue<JsonObject> queue = new();
    Process process, demoProcess, finishing;
    MeetingArchive archive;
    MeetingAudio audio; Segmenter callSeg = new(), micSeg = new();
    readonly SemaphoreSlim whisper = new(1, 1);
    System.Windows.Forms.Timer clock; int ticks;
    Size expanded = new(340, 650);
    Point dragFrom;
    bool Room => callApp == "In person";   // no call: the mic hears everyone, so its questions get answers too
    public bool Open => panel != null;

    void Js(string call, object value = null) => Web.Js(web, $"meeting.{call}(...{Web.Json(new[] { value ?? new Dictionary<string, object>() })})");
    static double Now => MeetingArchive.Unix(DateTime.Now);
    int Seconds => (int)(DateTime.Now - began).TotalSeconds;

    public async void Show(bool demo = false, JsonObject nudge = null)
    {
        if (!Prefs.Bool("meeting") || Prefs.Bool("paused")) return;
        if (panel != null)
        {
            if (nudge != null) return;
            if (Nudge != null) { Nudge = null; if (loaded) Js("prep"); }   // asked for while the call card shows: open prep
            Focus(); return;
        }
        this.demo = demo; Nudge = nudge; minimized = false; loaded = false; Generation++; transcript.Clear(); brief = ""; themPartial = youPartial = ""; elapsed = 0; serial = 0;
        archive = null; archiveEnded = false; meetingTitle = nudge?["title"]?.ToString() ?? ""; callApp = nudge?["app"]?.ToString() ?? "Call";
        try { name = JsonNode.Parse(File.ReadAllText(Path.Combine(Paths.Config, "me.json")))?["name"]?.ToString() ?? ""; } catch { name = ""; }
        var p = panel = new MeetingPanel();
        if (Prefs.Str("meetingSize") is { Length: > 0 } sz && sz.Split(',') is [var sw, var sh] && double.TryParse(sw, out var w) && double.TryParse(sh, out var h))
            expanded = new Size((int)Math.Max(280, w), (int)Math.Max(380, h));
        p.CreateControl();
        var scr = Screen.FromPoint(Cursor.Position).WorkingArea;
        var size = new Size((int)(340 * p.Dpi), (int)(140 * p.Dpi));
        var at = new Point(scr.Right - size.Width - (int)(12 * p.Dpi), scr.Top + (int)(12 * p.Dpi));   // a side panel: top right, clear of the Atticus box
        if (Prefs.Str("meetingFrame") is { Length: > 0 } f && f.Split(',') is [var fx, var fy] && int.TryParse(fx, out var x) && int.TryParse(fy, out var y)
            && Screen.AllScreens.Any(s => s.WorkingArea.Contains(x, y))) at = new Point(x, y);
        p.Bounds = Clamp(new Rectangle(at, size));
        Native.Backdrop(p.Handle);
        if (Environment.GetEnvironmentVariable("ATTICUS_CAPTURABLE") != "1") Native.SetWindowDisplayAffinity(p.Handle, 0x11);   // never in screen shares or recordings
        p.FormClosing += (_, e) => { if (e.CloseReason == CloseReason.UserClosing && panel == p) { e.Cancel = true; Program.Post(Discard); } };   // Alt+F4
        p.Show();
        var g = Generation;
        WebView2 v = null;
        try { v = await Web.Load(p, "meeting.html", (n, b) => { if (n == "meeting" && panel == p) OnMessage(b); }, true, PanelScript); } catch { }
        if (panel != p || g != Generation || v == null) { if (!p.IsDisposed) { p.Close(); p.Dispose(); } if (panel == p) panel = null; return; }
        web = v; v.AllowExternalDrop = false;   // a dropped file would replace the page; "Add files" reads them instead
    }

    Rectangle Clamp(Rectangle r)
    {
        var scr = Screen.AllScreens.OrderByDescending(s => { var i = Rectangle.Intersect(s.WorkingArea, r); return i.Width * i.Height; }).FirstOrDefault() ?? Screen.PrimaryScreen;
        var m = (int)(12 * (panel?.Dpi ?? 1));
        var b = Rectangle.Inflate(scr.WorkingArea, -m, -m);
        r.Width = Math.Min(r.Width, b.Width); r.Height = Math.Min(r.Height, b.Height);
        r.X = Math.Min(Math.Max(r.X, b.Left), b.Right - r.Width); r.Y = Math.Min(Math.Max(r.Y, b.Top), b.Bottom - r.Height);
        return r;
    }

    void Ready()
    {
        if (loaded) return; loaded = true;
        Js("motion", !SystemInformation.UIEffectsEnabled);
        Js("privacy", Prefs.Bool("meetingSave"));
        Js("width", Math.Min(340, (Screen.FromControl(panel).WorkingArea.Width / panel.Dpi) - 24));
        if (demo) Worker("--meeting-demo-brief", new JsonObject(), r => { if (r["error"] != null) { Js("error", "Could not load demo brief"); Js("prep"); } else Js("demoPrep", r); Focus(); });
        else if (Nudge != null) Js("nudge", Nudge);   // shown, never steals focus
        else { Js("mode", Calls.OnMic().Count == 0 ? "room" : "call"); Js("prep"); Focus(); }
    }

    void RememberFrame()
    {
        if (panel == null) return;
        Prefs.Set("meetingFrame", $"{panel.Left},{panel.Top}");
        Prefs.Set("meetingSize", $"{expanded.Width},{expanded.Height}");
    }
    void ResetFrame()
    {
        if (panel == null) return;
        var scr = Screen.FromControl(panel).WorkingArea; var m = (int)(12 * panel.Dpi);
        panel.Bounds = Clamp(new Rectangle(scr.Right - panel.Width - m, scr.Top + m, panel.Width, panel.Height)); RememberFrame();
    }
    // The page asks for its height (0 = the full live view); the top edge stays put.
    void SizeTo(double h, int? reveal)
    {
        if (panel == null) return;
        var d = panel.Dpi;
        var want = h <= 0 ? expanded.Height : Math.Max(minimized ? 44 : 80, h);
        var r = Clamp(new Rectangle(panel.Left, panel.Top, (int)((minimized ? 116 : expanded.Width) * d), (int)(want * d)));
        if (r != panel.Bounds) panel.Bounds = r;
        if (reveal is { } id) Js("settled", id);
    }
    void Resize(double w, double h)
    {
        if (!Running || minimized || panel == null) return;
        var b = Screen.FromControl(panel).WorkingArea; var d = panel.Dpi;
        expanded = new Size((int)Math.Min(b.Width / d - 24, Math.Max(280, w)), (int)Math.Min(b.Height / d - 24, Math.Max(380, h)));
        SizeTo(0, null);
    }
    void Compact(bool on) { if (!Running) return; minimized = on; Js("compact", on); if (!on) Focus(); }

    public void Focus()
    {
        if (panel == null) return;
        if (!panel.Visible) panel.Show();
        Native.keybd_event(0xE8, 0, 0, IntPtr.Zero); Native.keybd_event(0xE8, 0, 2, IntPtr.Zero);   // counts as input: Windows lets us come forward
        panel.Activate(); Native.SetForegroundWindow(panel.Handle); web?.Focus();
        Js("focus");
    }

    // ---- the journal ----
    void SaveEvent(object e)
    {
        if (!Prefs.Bool("meetingSave") || archive == null) return;
        try { archive.Append(e); }
        catch { archive = null; Js("saving", false); Js("error", "Transcript could not save. Check available disk space."); }
    }
    void BeginArchive()
    {
        if (!Prefs.Bool("meetingSave") || archive != null) { Js("saving", archive != null); return; }
        try
        {
            archive = new MeetingArchive(meetingTitle != "" ? meetingTitle : callApp + " · " + began.ToString("MMM d, yyyy h:mm tt"), callApp, began);
            archiveEnded = false; SaveEvent(new { kind = "notes", text = brief }); Js("saving", true);
        }
        catch { Js("saving", false); Js("error", "Could not save this transcript. Check available disk space."); }
    }
    void FinishArchive(bool summarize = true)
    {
        if (archive == null || archiveEnded) return;
        try { archive.Append(new { kind = "ended", at = Now, duration = elapsed }); archiveEnded = true; }
        catch { archive = null; Js("saving", false); Js("error", "Transcript could not save. Check available disk space."); return; }
        if (!summarize) return;
        var a = archive;
        Process p;
        try { p = Py.Start(new[] { "--meetings" }, input: true); } catch { return; }
        finishing = p;
        Task.Run(() =>
        {
            var o = Talk(p, new JsonObject { ["action"] = "finish", ["id"] = a.Id }.ToJsonString(), 120);
            Program.Post(() =>
            {
                if (finishing != p) return; finishing = null;
                if (a != archive || Running) return;
                if (o["meeting"] is JsonObject m) Js("recap", m);
            });
        });
    }
    public void SavingChanged(bool on)
    {
        if (on && Running) BeginArchive();
        if (!on) { Py.Stop(finishing); finishing = null; if (Running) elapsed = (DateTime.Now - began).TotalSeconds; FinishArchive(false); archive = null; Js("saving", false); }
    }
    public void Forget(string id) { if (archive?.Id == id) { Py.Stop(finishing); finishing = null; archive = null; Js("saving", false); } }   // Settings deleted it

    // ---- listening ----
    void Start(string context)
    {
        if (!loaded || Running || !Prefs.Bool("meeting") || Prefs.Bool("paused")) return;
        CancelWork(); brief = context.Length > 1500 ? context[..1500] : context; briefEdited = false; Running = true; began = DateTime.Now.AddSeconds(-elapsed); Nudge = null;
        Program.Box?.Close(false);
        if (callApp == "Call") callApp = demo ? "Demo" : Calls.OnMic().OrderBy(a => a).FirstOrDefault() ?? "Call";
        BeginArchive();
        if (archiveEnded) { SaveEvent(new { kind = "resumed", duration = elapsed }); archiveEnded = false; }
        Js("listen"); Program.Tray?.Listening(true);
        // Short notes are the context as-is (no AI call); long notes or a link get boiled down while it listens.
        if (context.Length > 1500 || Regex.IsMatch(context, @"^https?://\S+$")) Prepare(context);
        var g = Generation;
        if (!Program.Speech.Ready) { Stop(); Js("error", "Speech recognition isn’t ready. Download the speech model in Settings, then Resume."); return; }
        if (!demo && !Speech.MicAllowed()) { Stop(); Js("error", "Allow microphone access in Settings → Privacy & security → Microphone."); return; }
        callSeg = new Segmenter(); micSeg = new Segmenter();
        audio = new MeetingAudio
        {
            OnSamples = (s, mic) => Hear(s, mic, g),
            OnError = e => Program.Post(() => { if (g == Generation && Running) { Stop(); Js("error", e); } })
        };
        var err = audio.Start(withMic: !demo);
        if (err != "") { Stop(); Js("error", err); return; }
        Js("capturing");
        if (demo) PlayDemo();
        clock = new System.Windows.Forms.Timer { Interval = 500 }; ticks = 0;
        clock.Tick += (_, _) =>
        {
            if (g != Generation || !Running) return;
            ticks++;
            if (ticks % 2 == 0) Js("clock", Seconds);
            if (ticks % 3 == 0) { ReadAhead(callSeg, false, g); ReadAhead(micSeg, true, g); }
            if (ticks % 4 == 0)
            {
                var part = themPartial + "\n" + youPartial;
                if (part != lastPartial) { lastPartial = part; SaveEvent(new { kind = "partial", seconds = Seconds, at = Now, them = themPartial, you = youPartial }); }
                SaveEvent(new { kind = "checkpoint", duration = Seconds });
            }
        };
        clock.Start();
    }

    string Prompt => string.Join(", ", new[] { name, "Atticus", demo ? "Project Cedar" : "" }.Where(s => s != "")) + ".";

    void Hear(float[] s, bool mic, int g)   // capture threads
    {
        var seg = mic ? micSeg : callSeg; List<float[]> done; float level;
        lock (seg) { done = seg.Push(s); level = seg.Level; }
        if ((minimized || voice) && (DateTime.Now - levelAt).TotalMilliseconds > 100 && (mic || !voice))
        {
            levelAt = DateTime.Now;   // voice waves follow only your mic
            Program.Post(() => { if (g == Generation && Running) Js("level", Math.Min(1, level * 4)); });
        }
        foreach (var a in done) _ = Read(a, mic, g, -1);
    }
    void ReadAhead(Segmenter seg, bool mic, int g)   // words so far, when Whisper is free
    {
        float[] a; int cut;
        lock (seg) { a = seg.Current(); cut = seg.Cuts; }
        if (a != null && a.Length >= Segmenter.Rate && whisper.CurrentCount > 0) _ = Task.Run(() => Read(a, mic, g, cut));
    }
    async Task Read(float[] a, bool mic, int g, int partialOf)   // partialOf -1: a finished utterance
    {
        if (partialOf < 0) await whisper.WaitAsync(); else if (!whisper.Wait(0)) return;
        try
        {
            if (g != Generation) return;
            var t = await Program.Speech.TranscribeOnce(a, Prompt);
            var seg = mic ? micSeg : callSeg;
            Program.Post(() => { if (g == Generation && Running && (partialOf < 0 || partialOf == seg.Cuts)) Speech2(t, partialOf < 0, mic); });
        }
        catch { }
        finally { whisper.Release(); }
    }
    // Whisper on a cut-out utterance can start with the comma it heard before it, or end a question "?.".
    public static string Tidy(string t) => Regex.Replace(Regex.Replace(t ?? "", @"^[\s,.;:]+", ""), @"([?!])\.$", "$1").Trim();
    void Speech2(string piece, bool final, bool mic)
    {
        piece = Tidy(piece);
        if (mic && voice)
        {
            if (final) { voiceText += " " + piece; voicePartial = ""; } else voicePartial = piece;
            Js("heard", (voiceText + " " + voicePartial).Trim()); return;
        }
        if (mic && DateTime.Now < voiceTail) return;   // the last words of a voice ask finishing; already in the ask
        if (mic) youPartial = final ? "" : piece; else themPartial = final ? "" : piece;
        Js("partial", new { mic = mic && !Room, text = final ? "" : piece });
        if (final) Line(piece, mic);
    }
    void Line(string text, bool mic, bool answer = true)
    {
        text = (text ?? "").Trim();
        if (!Running || text == "") return;
        var you = mic && !Room;
        if (you && Echo(text, transcript.Where(r => r["mic"] is false && Now - (double)r["at"] < 20).Select(r => (string)r["text"]))) return;
        var at = Now; var sec = Seconds; serial++;
        transcript.Add(new() { ["at"] = at, ["text"] = text, ["seconds"] = sec, ["mic"] = you });
        SaveEvent(new { kind = "transcript", at, text, seconds = sec, mic = you, speaker = you ? "You" : Room ? "Room" : callApp == "Call" ? "Them" : callApp });
        var row = new Dictionary<string, object> { ["id"] = serial, ["text"] = text, ["seconds"] = sec, ["mic"] = you };
        Js("line", row);
        if (answer && !you && Question(text, name))   // your own words are never answered for you
        {
            var auto = Prefs.Str("meetingAuto") is { Length: > 0 } m ? m : "sure";
            row["auto"] = auto != "ask"; row["heard"] = true; Js("question", row);
            if (auto != "ask") Enqueue(serial, text, auto == "always", at);
        }
    }
    void Enqueue(int id, string question, bool best, double? at = null, string image = null)
    {
        if (!Running || question.Trim() == "" || answering == id || queue.Any(q => (int)q["id"] == id)) return;
        var rows = new JsonArray(transcript.Select(r => (JsonNode)JsonSerializer.SerializeToNode(r)).ToArray());
        var data = new JsonObject { ["id"] = id, ["transcript"] = rows, ["now"] = Now, ["question"] = question, ["best"] = best, ["at"] = at ?? Now };
        if (image != null) data["image"] = image;
        queue.Enqueue(data); Next();
    }
    void Prepare(string context) { pendingBrief = context.Length > 20000 ? context[..20000] : context; briefEdited = false; Next(); }
    void Next()
    {
        if (!Running || process != null) return;
        if (pendingBrief is { } context)
        {
            pendingBrief = null;
            Worker("--meeting-prep", new JsonObject { ["text"] = context }, r =>
            {
                if (!Running) return;
                if (!briefEdited && pendingBrief == null)
                {
                    if (r["brief"]?.ToString() is { Length: > 0 } b) { brief = b.Length > 1500 ? b[..1500] : b; SaveEvent(new { kind = "notes", text = brief }); }
                    Js("prepared", r);
                }
                Next();
            });
            return;
        }
        if (queue.Count == 0) return;
        var data = queue.Dequeue(); var id = (int)data["id"];
        data["brief"] = brief; answering = id;
        Js("thinking", id);
        Worker("--meeting-answer", data, r =>
        {
            if (!Running) return;
            answering = null; r["id"] = id;
            if (r["kind"]?.ToString() == "answer") SaveEvent(new { kind = "answer", question = data["question"]?.ToString() ?? "", lines = r["lines"], at = Now });
            Js("answer", r); Next();
        });
    }

    // One atticus.py worker at a time; its JSON reply on the UI thread, unless Stop or a newer meeting made it stale.
    void Worker(string command, JsonObject data, Action<JsonObject> done)
    {
        var g = Generation;
        Process p;
        try { p = Py.Start(new[] { command }, input: true); } catch { done(new JsonObject { ["error"] = "Could not start" }); return; }
        process = p;
        var body = data.ToJsonString();
        Task.Run(() =>
        {
            var r = Talk(p, body, 90);
            Program.Post(() => { if (g != Generation || process != p) return; process = null; done(r); });
        });
    }
    public static JsonObject Talk(Process p, string body, int secs)
    {
        try
        {
            _ = p.StandardError.ReadToEndAsync();
            p.StandardInput.Write(body); p.StandardInput.Close();
            var o = p.StandardOutput.ReadToEndAsync();
            if (!p.WaitForExit(secs * 1000)) { Py.Stop(p); return new JsonObject { ["error"] = "Could not finish" }; }
            var line = o.Result.Split('\n').Select(l => l.Trim()).LastOrDefault(l => l.StartsWith('{')) ?? "";
            return JsonNode.Parse(line) as JsonObject ?? new JsonObject { ["error"] = "Could not finish" };
        }
        catch { return new JsonObject { ["error"] = "Could not finish" }; }
    }

    void PlayDemo()
    {
        try { demoProcess = Py.Start(new[] { "--meeting-demo" }); _ = demoProcess.StandardOutput.ReadToEndAsync(); _ = demoProcess.StandardError.ReadToEndAsync(); } catch { }
    }
    void CancelWork() { Py.Stop(process); process = null; Py.Stop(finishing); finishing = null; queue.Clear(); pendingBrief = null; answering = null; Py.Stop(demoProcess); demoProcess = null; }

    public void Stop(bool paused = false)
    {
        if (paused) { Py.Stop(finishing); finishing = null; }
        if (panel == null) return;
        if (!Running && (Nudge != null || transcript.Count == 0)) { Discard(); return; }   // pause or switch-off during a card: just close it
        if (!Running) return;
        voice = false; voiceText = voicePartial = "";
        Line(themPartial, false, false); Line(youPartial, true, false); themPartial = youPartial = "";   // unfinished speech is kept as it was last read
        elapsed = (DateTime.Now - began).TotalSeconds; Running = false; minimized = false; Generation++;
        clock?.Stop(); clock?.Dispose(); clock = null;
        CancelWork(); audio?.Dispose(); audio = null;
        Program.Tray?.Listening(false);
        Js("stopped", paused); Js("saving", archive != null);
        if (!paused) FinishArchive(); else SaveEvent(new { kind = "checkpoint", duration = elapsed });
        Focus();
    }
    public void Discard()
    {
        if (Running) Stop(true);
        FinishArchive(false); archive = null; Generation++; CancelWork(); transcript.Clear(); brief = ""; Nudge = null;
        var p = panel; panel = null; web = null; loaded = false;
        if (p is { IsDisposed: false }) { p.Close(); p.Dispose(); }
        Program.Tray?.Listening(false);
    }

    // Notes from files: plain text and Markdown as they are, Word documents by their paragraphs.
    void Material(IEnumerable<string> paths)
    {
        var g = Generation;
        Task.Run(() =>
        {
            var text = new StringBuilder(); var took = new List<string>();
            foreach (var path in paths.Take(8))
            {
                try
                {
                    var ext = Path.GetExtension(path).ToLowerInvariant();
                    if (new FileInfo(path).Length >= 10_000_000) continue;
                    var part = ext is ".txt" or ".md" ? File.ReadAllText(path) : ext == ".docx" ? Docx(path) : null;
                    if (string.IsNullOrWhiteSpace(part)) continue;
                    took.Add(path); text.Append(Path.GetFileName(path)).Append('\n').Append(part.Length > 20000 ? part[..20000] : part).Append('\n');
                }
                catch { }
            }
            Program.Post(() =>
            {
                if (g != Generation) return;
                if (took.Count == 0) { Js("error", "Add a readable Word, text or Markdown file under 10 MB"); return; }
                var all = text.ToString(); Js("material", all.Length > 20000 ? all[..20000] : all);
                foreach (var f in took) Js("attachment", new { name = Path.GetFileName(f), folder = Path.GetDirectoryName(f), img = "" });
            });
        });
    }
    public static string Docx(string path)
    {
        using var z = ZipFile.OpenRead(path);
        using var r = new StreamReader(z.GetEntry("word/document.xml")!.Open());
        var xml = Regex.Replace(r.ReadToEnd(), @"</w:p>|<w:br/>|<w:tab/>", m => m.Value == "<w:tab/>" ? "\t" : "\n");
        return System.Net.WebUtility.HtmlDecode(Regex.Replace(xml, "<[^>]+>", "")).Trim();
    }

    // "Explain the screen": one picture of the screen the panel is on (the panel and the Atticus box never show in it).
    static string Shot(Screen s)
    {
        var b = s.Bounds;
        using var full = new Bitmap(b.Width, b.Height);
        using (var gr = Graphics.FromImage(full)) gr.CopyFromScreen(b.Location, Point.Empty, b.Size);
        var w = Math.Min(1280, b.Width);
        using var small = new Bitmap(full, w, Math.Max(1, b.Height * w / b.Width));
        using var ms = new MemoryStream();
        var jpg = ImageCodecInfo.GetImageEncoders().First(c => c.FormatID == ImageFormat.Jpeg.Guid);
        using var q = new EncoderParameters(1); q.Param[0] = new EncoderParameter(System.Drawing.Imaging.Encoder.Quality, 65L);
        small.Save(ms, jpg, q);
        return Convert.ToBase64String(ms.ToArray());
    }

    void OnMessage(JsonElement b)
    {
        if (b.ValueKind != JsonValueKind.Object || !b.TryGetProperty("do", out var d)) return;
        string S(string k) => b.TryGetProperty(k, out var v) && v.ValueKind == JsonValueKind.String ? v.GetString() : null;
        double N(string k, double or) => b.TryGetProperty(k, out var v) && v.ValueKind == JsonValueKind.Number ? v.GetDouble() : or;
        bool B(string k) => b.TryGetProperty(k, out var v) && v.ValueKind == JsonValueKind.True;
        switch (d.GetString())
        {
            case "drag": if (panel != null) dragFrom = panel.Location; break;
            case "move": if (panel != null) panel.Bounds = Clamp(new Rectangle(dragFrom.X + (int)N("dx", 0), dragFrom.Y + (int)N("dy", 0), panel.Width, panel.Height)); break;
            case "resetFrame": ResetFrame(); break;
            case "rememberFrame": RememberFrame(); break;
            case "ready": if (web == null) Program.Post(Ready); else Ready(); break;
            case "start": if (B("room")) callApp = "In person"; else if (Room) callApp = "Call"; Start(S("context") ?? ""); break;
            case "size": SizeTo(N("h", 0), b.TryGetProperty("reveal", out var rv) && rv.ValueKind == JsonValueKind.Number ? rv.GetInt32() : null); break;
            case "resize": Resize(N("w", 340), N("h", 650)); break;
            case "compact": Compact(B("on")); break;
            case "focus": Nudge = null; Focus(); break;
            case "prepare": if (Running) Prepare(S("context") ?? ""); break;
            case "brief": if (Running) { briefEdited = true; brief = S("text") ?? ""; if (brief.Length > 1500) brief = brief[..1500]; SaveEvent(new { kind = "notes", text = brief }); } break;
            case "stop": Stop(); break;
            case "voice":
                if (!Running) return;
                if (B("on"))
                {
                    if (audio == null || demo) { Js("voiceEnd"); Js("error", "Voice asks need the microphone"); return; }
                    Line(youPartial, true, false); youPartial = ""; lock (micSeg) micSeg.Cut();
                    voice = true; voiceText = voicePartial = "";
                }
                else if (voice) { voice = false; voiceText = voicePartial = ""; voiceTail = DateTime.Now.AddSeconds(1.5); }
                break;
            case "pause": Stop(true); break;
            case "resume": Start(brief); break;
            case "meetings": Program.Settings.Open("meetings"); break;
            case "delete": Discard(); break;
            case "save": if (!Running) Discard(); break;
            case "deleteSaved":
                if (Running || archive == null) return;
                if (!Meetings.ConfirmDelete(panel)) return;
                var gone = archive; Forget(gone.Id);
                Meetings.Run(new JsonObject { ["action"] = "delete", ["id"] = gone.Id }, r => { if (r["deleted"] != null) Discard(); else Js("error", "Could not delete. Try again."); });
                break;
            case "chooseFiles":
                using (var dlg = new OpenFileDialog { Filter = "Notes (Word, text, Markdown)|*.docx;*.txt;*.md", Multiselect = true, Title = "Add notes for this meeting" })
                    if (dlg.ShowDialog(panel) == DialogResult.OK) Material(dlg.FileNames);
                break;
            case "ask": case "answer": case "explain":
                if (!Running) return;
                var id = b.TryGetProperty("id", out var iv) && iv.ValueKind == JsonValueKind.Number ? iv.GetInt32() : ++serial;
                var text = S("text") ?? ""; var act = d.GetString();
                if (act != "answer") Js("question", new { id, text = act == "explain" ? "Explain: " + text : text, seconds = Seconds, auto = true });
                Enqueue(id, act == "explain" ? "Explain: " + text : text, true);
                break;
            case "screen":
                if (!Running || panel == null) return;
                string jpg; try { jpg = Shot(Screen.FromControl(panel)); } catch { Js("error", "Could not look at screen"); return; }
                serial++; var question = S("text") ?? "Explain the shared slide.";
                Js("question", new { id = serial, text = question, seconds = Seconds, auto = true }); Enqueue(serial, question, true, image: jpg);
                break;
            case "copy": if (S("text") is { } t) try { Clipboard.SetText(t); } catch { } break;
        }
    }

    // ---- noticing calls (once a second) ----
    readonly Dictionary<string, DateTime> onMicSince = new();
    readonly HashSet<string> nudged = new();
    bool callSeen; DateTime? callGone;
    public void Watch()
    {
        var t = new System.Windows.Forms.Timer { Interval = 1000 };
        t.Tick += (_, _) => CheckMic();
        t.Start();
    }
    void CheckMic()
    {
        var watching = Prefs.Bool("meeting") && Prefs.Bool("meetingDetect") && !Prefs.Bool("paused");
        var now = (Running && callApp is not ("Call" or "Demo" or "In person")) || watching ? Calls.OnMic() : new HashSet<string>();
        // The call app let go of the mic (hung up): end the meeting like End does. Grace covers a mute that briefly drops input.
        if (Running && callApp is not ("Call" or "Demo" or "In person"))
        {
            if (now.Contains(callApp)) { callSeen = true; callGone = null; }
            else if (callSeen && callGone is { } gone && (DateTime.Now - gone).TotalSeconds >= 2) { callSeen = false; callGone = null; Stop(); }
            else if (callSeen && callGone == null) callGone = DateTime.Now;
        }
        else { callSeen = false; callGone = null; }
        if (!watching) { onMicSince.Clear(); return; }
        foreach (var k in onMicSince.Keys.Where(k => !now.Contains(k)).ToList()) onMicSince.Remove(k);
        nudged.IntersectWith(now);   // a call that ends can be noticed again next time
        foreach (var a in now) onMicSince.TryAdd(a, DateTime.Now);
        if (Running || panel != null) { nudged.UnionWith(now); return; }   // already open for this call
        var app = onMicSince.FirstOrDefault(kv => !nudged.Contains(kv.Key) && (DateTime.Now - kv.Value).TotalSeconds >= 3).Key;
        if (app == null) return;
        nudged.Add(app);
        Show(nudge: new JsonObject { ["title"] = app == "Slack" ? "Huddle detected" : "Meeting detected", ["when"] = $"In {app} · Want Atticus to listen?", ["who"] = "", ["context"] = "", ["app"] = app });
        var g = Generation;   // ignored, it goes by itself; "Get ready" clears Nudge so it stays
        var t = new System.Windows.Forms.Timer { Interval = 45000 };
        t.Tick += (_, _) => { t.Dispose(); if (Generation == g && !Running && Nudge != null) Discard(); };
        t.Start();
    }
}

// Settings → Meetings and the panel share these: the archive through atticus.py --meetings, and the native confirmations.
static class Meetings
{
    public static void Run(JsonObject data, Action<JsonObject> done) => Task.Run(() =>
    {
        JsonObject r;
        try { r = MeetingCopilot.Talk(Py.Start(new[] { "--meetings" }, input: true), data.ToJsonString(), 120); } catch { r = new JsonObject(); }
        if (r.Count == 0 || r["error"] != null && r.Count == 1 && r["error"]!.ToString() == "Could not finish") r = new JsonObject { ["error"] = "Could not open saved meetings. Try again." };
        Program.Post(() => done(r));
    });
    public static bool ConfirmDelete(IWin32Window owner) =>
        MessageBox.Show(owner, "Its transcript, notes and recap will be removed from this PC.", "Delete this meeting?", MessageBoxButtons.OKCancel, MessageBoxIcon.Warning, MessageBoxDefaultButton.Button2) == DialogResult.OK;
}
