// The Settings window (ui\settings.html) and the first-run setup window (ui\onboarding.html).
using System.Diagnostics;
using System.Text.Json;
using System.Text.Json.Nodes;
using Microsoft.Web.WebView2.WinForms;

namespace Atticus;

class PageWindow : Form
{
    protected WebView2 web;
    readonly string page, handler;
    protected PageWindow(string page, string handler, string title, int w, int h)
    {
        this.page = page; this.handler = handler;
        Text = title; StartPosition = FormStartPosition.CenterScreen; BackColor = Color.Black;
        ClientSize = new Size((int)(w * DeviceDpi / 96.0), (int)(h * DeviceDpi / 96.0));
        MinimizeBox = true; MaximizeBox = false; FormBorderStyle = FormBorderStyle.FixedSingle;
        var ico = Path.Combine(Paths.App, "ui", "AppIcon.ico");
        if (File.Exists(ico)) Icon = new Icon(ico);
    }
    protected override void OnHandleCreated(EventArgs e) { base.OnHandleCreated(e); Native.Backdrop(Handle, acrylic: false, round: true); }
    protected async Task Ensure()
    {
        if (web != null) return;
        web = await Web.Load(this, page, (n, b) => { if (n == handler) OnMessage(b); }, true);
        web.NavigationCompleted += (_, _) => Loaded();
    }
    protected virtual void Loaded() { }
    protected virtual void OnMessage(JsonElement b) { }
    public void Js(string s) => Web.Js(web, s);
    protected void Send(string call, object v) => Program.Post(() => Js($"{call}({Web.Json(v)})"));
    protected void Raise() { if (WindowState == FormWindowState.Minimized) WindowState = FormWindowState.Normal; Show(); Activate(); Native.SetForegroundWindow(Handle); }
}

sealed class SettingsWindow : PageWindow
{
    string tab = "";
    System.Windows.Forms.Timer perms;
    public SettingsWindow() : base("settings.html", "st", "Atticus Settings", 780, 560)
    {
        FormClosing += (_, e) => { if (e.CloseReason == CloseReason.UserClosing) { e.Cancel = true; if (Hooks.Recording) Done(0, 0, null); Hide(); perms?.Stop(); } };
    }

    public async void Open(string t = "")
    {
        tab = t;
        Raise();
        await Ensure();
        Refresh2(); if (t != "") { Js($"st.tab('{t}')"); tab = ""; }
        perms ??= new System.Windows.Forms.Timer { Interval = 1500 };
        perms.Tick -= Perms; perms.Tick += Perms; perms.Start();
    }
    void Perms(object s, EventArgs e) { if (Visible) Send("st.perms", OnboardingWindow.Perms()); }

    protected override void Loaded() { Push(); Refresh2(); Send("st.perms", OnboardingWindow.Perms()); if (tab != "") { Js($"st.tab('{tab}')"); tab = ""; } }

    public void Push()   // the settings, and the app icon for the sidebar
    {
        if (web == null) return;
        var d = new Dictionary<string, object> { ["prefs"] = Prefs.D, ["version"] = Paths.Install("version"), ["update"] = Updater.Status == "" ? (Updater.Pending ? "Update ready. Restarting when you’re not using it…" : "") : Updater.Status, ["sounds"] = Sound.All() };
        var png = Path.Combine(Paths.App, "ui", "AppIcon.png");
        if (File.Exists(png)) d["icon"] = "data:image/png;base64," + Convert.ToBase64String(File.ReadAllBytes(png));
        Js($"st.load({Web.Json(d)})");
    }

    // usage, saved rules, Claude plan: atticus.py --state (it runs Claude Code, so off the UI thread)
    void Refresh2() => Task.Run(() => { var s = Py.Run(new[] { "--state" }); if (s.StartsWith("{")) Program.Post(() => Js($"st.state({s})")); });

    void Helper(params string[] args) => Task.Run(() => { Py.Run(args); Program.Post(Refresh2); });

    public void Apply(string k, object v)   // one setting changed, from this window or by voice (atticus.py --set)
    {
        if (k == "meetingSave" && v is false && Prefs.Bool("meetingSave"))   // saving off: the current journal stops; keep or delete the past ones
        {
            Program.Meeting.SavingChanged(false); Prefs.Set(k, false); Push();
            var keep = new TaskDialogButton("Keep Meetings"); var drop = new TaskDialogButton("Delete All Meetings");
            var page = new TaskDialogPage { Caption = "Atticus", Heading = "Keep your saved meetings?", Text = "New transcripts will no longer be saved. You can keep or delete the meetings already on this PC.", Buttons = { keep, drop } };
            if (TaskDialog.ShowDialog(this, page) == drop) Meetings.Run(new JsonObject { ["action"] = "clear" }, r => Send("st.meetings", r));
            return;
        }
        Prefs.Set(k, v);
        if (k == "meetingSave") Program.Meeting.SavingChanged(v is true);
        if (k == "meeting" && v is false) Program.Meeting.Stop();
        if (k == "paused" && v is true) Program.Meeting.Stop(paused: true);
        if (k == "speak" && v is false) Voice.Shush();
        if (k == "menuIcon") Program.Tray.Visible = v is true;
        if (k == "paused" && v is true) Program.Box.Close(false);
        if (k == "safe") Program.Box.js("mode", v is true);
        if (k == "speechModel") _ = Program.Speech.Load();
        if (k == "login") Startup(v is true);
    }

    public static void Startup(bool on)
    {
        using var run = Microsoft.Win32.Registry.CurrentUser.CreateSubKey(@"Software\Microsoft\Windows\CurrentVersion\Run");
        if (on) run.SetValue("Atticus", $"\"{Environment.ProcessPath}\""); else run.DeleteValue("Atticus", false);
    }

    void Record()
    {
        if (Hooks.Recording) return;
        Hooks.RecordingMods = m => Program.Post(() => Js($"st.rec({Web.Json(m)})"));
        Hooks.Recorded = (vk, mods, label) => Program.Post(() => Done(vk, mods, label));
        Hooks.Recording = true;
    }

    void Done(int vk, int mods, string label)
    {
        Hooks.Recording = false;
        if (label != null && vk != 0) { Prefs.Set("hotkeyCode", vk); Prefs.Set("hotkeyMods", mods); Prefs.Set("hotkeyLabel", label); }
        Push();
    }

    protected override void OnDeactivate(EventArgs e) { base.OnDeactivate(e); if (Hooks.Recording) Done(0, 0, null); }

    protected override void OnMessage(JsonElement b)
    {
        if (b.ValueKind != JsonValueKind.Object || !b.TryGetProperty("do", out var d)) return;
        string S(string k) => b.TryGetProperty(k, out var v) && v.ValueKind == JsonValueKind.String ? v.GetString() : null;
        switch (d.GetString())
        {
            case "set":
                if (S("k") is not { } k || !b.TryGetProperty("v", out var v)) return;
                Apply(k, v.ValueKind switch { JsonValueKind.True => true, JsonValueKind.False => false, JsonValueKind.Number => v.GetDouble(), _ => (object)v.GetString() });
                break;
            case "record": Record(); break;
            case "cancelRecord": if (Hooks.Recording) Done(0, 0, null); break;
            case "unlearn": if (S("say") is { } say) Helper("--unlearn", say); break;
            case "checkUpdate": Updater.Check(Push); break;
            case "github": Web.Open("https://github.com/FindlayHi/atticus-windows"); break;
            case "resetHotkey": Prefs.Set("hotkeyCode", 0x20); Prefs.Set("hotkeyMods", 3); Prefs.Set("hotkeyLabel", "Ctrl+Alt+Space"); Push(); break;
            case "preview": if (S("sound") is { } n) Sound.Play(n, Prefs.Num("volume") * 2); break;
            case "setup": Program.Onboarding.Open(); break;
            case "webChrome": Helper("--web", "setup", "Google Chrome"); break;   // opens the browser's own remote-debugging switch
            case "webEdge": Helper("--web", "setup", "Microsoft Edge"); break;
            case "openLog": if (File.Exists(Paths.Log)) Web.Open(Paths.Log); break;
            case "clearRecent": try { File.Delete(Paths.Recent); } catch { } Program.Box.lastDone = DateTime.MinValue; break;
            case "me": if (S("what") is { } w && S("text") is { } t) Helper("--me", w, t); break;
            case "forget": case "remember": if (S("line") is { } line) Helper("--" + d.GetString(), line); break;
            case "grant": if (S("id") is { } id) Program.Onboarding.Grant(id); break;
            case "integrations": Task.Run(() => { var j = Py.Run(new[] { "--int-list" }); if (j.TrimStart().StartsWith("[")) Program.Post(() => Js($"st.integrations({j})")); }); break;
            case "intCustom":
                if (!b.TryGetProperty("values", out var ov)) return;
                var olines = string.Join("\n", ov.EnumerateObject().Select(p => p.Name + "=" + (p.Value.GetString() ?? "").Replace("\n", " ")));
                Task.Run(() =>
                {
                    var r = Py.RunWithInput(new[] { "--int-custom" }, olines + "\n", 30);
                    var j = Py.Run(new[] { "--int-list" });
                    Program.Post(() => { if (j.TrimStart().StartsWith("[")) Js($"st.integrations({j})"); Js($"st.ownResult({Web.Json(r)})"); });
                });
                break;
            case "intFolder": Task.Run(() => Py.Run(new[] { "--int-folder" })); break;
            case "intRemove":
                if (S("name") is { } rn) Task.Run(() => { Py.Run(new[] { "--int", rn, "remove" }); var j = Py.Run(new[] { "--int-list" }); if (j.TrimStart().StartsWith("[")) Program.Post(() => Js($"st.integrations({j})")); });
                break;
            case "intSave":
                if (S("name") is not { } name || !b.TryGetProperty("values", out var vals)) return;
                var lines = string.Join("\n", vals.EnumerateObject().Select(p => p.Name + "=" + (p.Value.GetString() ?? "").Replace("\n", "")));
                Task.Run(() =>
                {
                    var r = Py.RunWithInput(new[] { "--int", name, "setup" }, lines + "\n", 30);
                    var j = Py.Run(new[] { "--int-list" });   // redraw (now "On"), then show what the test said
                    Program.Post(() => { if (j.TrimStart().StartsWith("[")) Js($"st.integrations({j})"); Js($"st.intResult({Web.Json(new[] { name, r })})"); });
                });
                break;
            case "meetings": var q = S("query") ?? ""; Meetings.Run(new JsonObject { ["action"] = "list", ["query"] = q }, r => Send("st.meetings", r)); break;
            case "meetingOpen": if (S("id") is { } mid) Meetings.Run(new JsonObject { ["action"] = "get", ["id"] = mid }, r => Send("st.meetings", r)); break;
            case "meetingDelete":
                if (S("id") is not { } did || !Meetings.ConfirmDelete(this)) return;
                Program.Meeting.Forget(did);
                Meetings.Run(new JsonObject { ["action"] = "delete", ["id"] = did }, r => Send("st.meetings", r));
                break;
            case "meetingExport":
                if (S("id") is not { } eid) return;
                Meetings.Run(new JsonObject { ["action"] = "export", ["id"] = eid }, r =>
                {
                    if (r["markdown"]?.ToString() is not { } md) { Send("st.meetings", r); return; }
                    using var dlg = new SaveFileDialog { Filter = "Markdown|*.md", FileName = "Meeting.md", Title = "Export meeting" };
                    if (dlg.ShowDialog(this) != DialogResult.OK) return;
                    try { MeetingArchive.Export(md, dlg.FileName); Send("st.meetings", new { notice = "Markdown exported." }); }
                    catch { Send("st.meetings", new { error = "Could not export. Try another location." }); }
                });
                break;
            case "meetingCopy": if (S("text") is { } mt) { try { Clipboard.SetText(mt); } catch { } Send("st.meetings", new { notice = "Copied." }); } break;
            case "meetingDemo": if (Prefs.Bool("meeting")) Program.Meeting.Show(demo: true); break;
            case "quit": Application.Exit(); break;
        }
    }
}

sealed class OnboardingWindow : PageWindow
{
    System.Windows.Forms.Timer timer;
    bool downloading;
    public OnboardingWindow() : base("onboarding.html", "ob", "Welcome to Atticus", 640, 640)
    {
        FormClosing += (_, e) => { if (e.CloseReason == CloseReason.UserClosing) { e.Cancel = true; Hide(); timer?.Stop(); } };
    }

    public async void Open()
    {
        Raise();
        await Ensure();
        int tick = 0;
        timer ??= new System.Windows.Forms.Timer { Interval = 50 };   // keys 20×/s, permissions 1×/s, Claude every 5 s
        timer.Tick += (_, _) =>
        {
            if (!Visible) return;
            Js($"ob.keys({(Native.Down(Native.VK_CONTROL) ? "true" : "false")}, {(Native.Down(Native.VK_MENU) ? "true" : "false")})");
            tick++;
            if (tick % 20 == 1) Push();
            if (tick % 100 == 1) CheckPlan();
        };
        timer.Start();
    }

    void Push()
    {
        Send("ob.set", Perms());
        Js($"ob.mode({(Prefs.Str("talkMode") == "toggle" ? "true" : "false")})");
    }

    public static Dictionary<string, bool> Perms() => new() { ["mic"] = Speech.MicAllowed(), ["speech"] = File.Exists(Paths.Model) };

    // Claude Code installed, signed in, on a paid plan? (atticus.py --check)
    public static JsonElement Plan()
    {
        var s = Py.Run(new[] { "--check" }, 30);
        try { return JsonDocument.Parse(s).RootElement.Clone(); }
        catch { return JsonDocument.Parse("{\"ok\":false,\"need\":\"install\",\"text\":\"Could not run Atticus's helper (Python missing? Run build.ps1 again).\"}").RootElement.Clone(); }
    }

    void CheckPlan() => Task.Run(() => { var c = Plan(); Program.Post(() => Js($"ob.plan({c.GetRawText()})")); });

    // The Claude row's button: install Claude Code, sign in, or upgrade, in a terminal window the user can watch.
    void FixPlan() => Task.Run(() =>
    {
        var need = Plan().TryGetProperty("need", out var n) ? n.GetString() : "";
        if (need == "upgrade") { Web.Open("https://claude.ai/upgrade"); return; }
        var cmd = need == "install" ? "irm https://claude.ai/install.ps1 | iex; & \"$env:USERPROFILE\\.local\\bin\\claude.exe\" auth login" : "& claude auth login";
        Process.Start(new ProcessStartInfo("powershell.exe", $"-NoProfile -ExecutionPolicy Bypass -NoExit -Command \"$env:Path = \\\"$env:USERPROFILE\\.local\\bin;$env:Path\\\"; Write-Host 'Atticus: setting up Claude Code'; {cmd.Replace("\"", "\\\"")}; Write-Host; Write-Host 'Done. You can close this window.'\"") { UseShellExecute = true });
        Program.Post(CheckPlan);
    });

    public void Tried() => Js("ob.tried()");

    public void Grant(string id)
    {
        switch (id)
        {
            case "mic": Web.Open("ms-settings:privacy-microphone"); break;
            case "claude": FixPlan(); break;
            case "speech": GetModel(); break;
        }
    }

    async void GetModel()   // the speech model, once: progress shows in the row
    {
        if (downloading || File.Exists(Paths.Model)) return;
        downloading = true;
        var ok = await Speech.Download(p => Program.Post(() => Js($"ob.progress && ob.progress('speech', {p:0.###})")));
        downloading = false;
        if (ok) await Program.Speech.Load();
        Push();
        Program.Settings.Js("st.perms && st.perms(" + Web.Json(Perms()) + ")");
    }

    protected override void OnMessage(JsonElement b)
    {
        if (b.ValueKind != JsonValueKind.Object) return;
        if (b.TryGetProperty("do", out var d) && d.GetString() == "done") { Prefs.Set("onboarded", true); Hide(); timer?.Stop(); return; }
        if (b.TryGetProperty("id", out var id)) Grant(id.GetString() ?? "");
    }
}
