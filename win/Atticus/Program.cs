// Atticus for Windows: a tray app. Hold Ctrl+Alt+Space, talk, let go. atticus.py (the brain) does the work through Claude Code.
// Build and install: .\build.ps1 (starts at login from HKCU\...\Run).
using System.IO.Pipes;
using System.Text;
using NAudio.Wave;

namespace Atticus;

static class Program
{
    public static Overlay Box;
    public static SettingsWindow Settings;
    public static OnboardingWindow Onboarding;
    public static Tray Tray;
    public static Speech Speech = new();
    static SynchronizationContext ui;

    [STAThread]
    static void Main(string[] args)
    {
        if (args.SequenceEqual(new[] { "--selftest" }))
        {
            Check(Overlay.TranscriptKey(" Open   Notes. ") == Overlay.TranscriptKey("open notes"));
            Check(Overlay.TranscriptKey("calculate 1.5") != Overlay.TranscriptKey("calculate 15"));
            Check(Overlay.TranscriptKey("search a b") != Overlay.TranscriptKey("search ab"));
            Check(Speech.Clean(" [BLANK_AUDIO] Open notepad. ") == "Open notepad." && Speech.Clean("Thank you.") == "");
            Check(Hooks.Label(0x20, 3) == "Ctrl+Alt+Space" && Hooks.Label(0x71, 0) == "F2");
            var ok = System.Text.Json.Nodes.JsonNode.Parse("{\"pid\":9,\"windowID\":\"4242\",\"windowFrame\":[0,0,800,500]}").AsObject();
            Check(MiniScreen.Valid(ok, out var h) && h == (IntPtr)4242);
            foreach (var bad in new[] { "{\"pid\":0,\"windowID\":\"1\",\"windowFrame\":[0,0,1,1]}", "{\"pid\":9,\"windowID\":\"x1\",\"windowFrame\":[0,0,1,1]}", "{\"pid\":9,\"windowID\":\"1\",\"windowFrame\":[0,0,0,1]}", "{\"pid\":9,\"windowID\":\"1\"}" })
                Check(!MiniScreen.Valid(System.Text.Json.Nodes.JsonNode.Parse(bad).AsObject(), out _));
            // a 16:10 window in a 16:10 rect at 150%: fills it; half scrolled out of view: the top half of the window
            var fit = MiniScreen.Fit(new double[] { 10, 20, 160, 100 }, new double[] { 10, 20, 160, 100 }, 1.5, new RectangleF(7, 0, 1600, 1000));
            Check(fit is { } f1 && f1.dest == Rectangle.FromLTRB(15, 30, 255, 180) && f1.src == Rectangle.FromLTRB(7, 0, 1607, 1000));
            var half = MiniScreen.Fit(new double[] { 0, 0, 160, 100 }, new double[] { 0, 0, 160, 50 }, 1, new RectangleF(0, 0, 1600, 1000));
            Check(half is { } f2 && f2.dest == Rectangle.FromLTRB(0, 0, 160, 50) && f2.src == Rectangle.FromLTRB(0, 0, 1600, 500));
            var tall = MiniScreen.Fit(new double[] { 0, 0, 160, 100 }, new double[] { 0, 0, 160, 100 }, 1, new RectangleF(0, 0, 500, 1000));   // letterboxed, centered
            Check(tall is { } f3 && f3.dest == Rectangle.FromLTRB(55, 0, 105, 100));
            Check(MiniScreen.Fit(new double[] { 0, 0, 160, 100 }, new double[] { 0, 200, 160, 0 }, 1, new RectangleF(0, 0, 10, 10)) == null && MiniScreen.Fit(null, null, 1, new RectangleF(0, 0, 10, 10)) == null);
            Console.WriteLine("native selftest ok"); return;
        }
        if (args.Length >= 2 && args[0] == "--text") Environment.Exit(WindowText.Run(args[1], args.Length > 2 ? args[2] : ""));
        if (args.Length == 2 && args[0] == "--transcribe")   // diagnostics: a 16 kHz mono WAV through the same speech path
        {
            Speech.Load().Wait();
            if (!Speech.Ready) { Console.Error.WriteLine("no speech model: " + Paths.Model + " " + Speech.Error); Environment.Exit(1); }
            using var r = new NAudio.Wave.AudioFileReader(args[1]);
            var all = new List<float>(); var buf = new float[16000]; int n;
            while ((n = ((NAudio.Wave.ISampleProvider)r).Read(buf.AsSpan())) > 0) all.AddRange(buf.Take(n));
            var t0 = DateTime.Now;
            Console.WriteLine(Speech.TranscribeOnce(all.ToArray()).Result + $"  [{(DateTime.Now - t0).TotalSeconds:0.00} s]");
            return;
        }
        using var one = new Mutex(true, @"Local\Atticus", out var first);
        if (!first) { Send("open"); return; }   // already running: it shows Settings (or setup)
        ApplicationConfiguration.Initialize();
        Application.SetColorMode(SystemColorMode.Dark);
        var ctx = new ApplicationContext();
        Application.Idle += Started;
        void Started(object s, EventArgs e)
        {
            Application.Idle -= Started;
            _ = Launch();
        }
        Application.Run(ctx);
        Box?.Hush(); Voice.Shush();
    }

    static void Check(bool ok) { if (!ok) { Console.Error.WriteLine("native selftest failed"); Environment.Exit(1); } }

    static async Task Launch()
    {
        ui = SynchronizationContext.Current;
        Directory.CreateDirectory(Paths.Config);
        Settings = new SettingsWindow();
        Onboarding = new OnboardingWindow();
        Tray = new Tray();
        Box = new Overlay(Speech);
        await Box.Init();
        Hooks.Install();
        _ = Speech.Load();
        Listen();
        Updater.Schedule();
        if (!Prefs.Bool("onboarded") || !File.Exists(Paths.Model)) Onboarding.Open();
        else _ = Task.Run(() => OnboardingWindow.Plan()).ContinueWith(t => { if (t.Result.TryGetProperty("ok", out var ok) && !ok.GetBoolean()) Post(() => Onboarding.Open()); });   // set up once, but Claude signed out since
        if (Environment.GetEnvironmentVariable("ATTICUS_DEMO") is { Length: > 0 } demo) { await Task.Delay(1500); Box.Demo(demo); }
    }

    public static void Post(Action a) => ui?.Post(_ => a(), null);

    public static void Err(string line)
    {
        try { File.AppendAllText(Path.Combine(Paths.Config, "app.err"), line + "\n"); } catch { }
    }

    // atticus.py and a second copy of Atticus talk to the running app through a named pipe: one line each.
    const string PipeName = "atticus";
    static void Listen() => Task.Run(async () =>
    {
        while (true)
        {
            try
            {
                using var s = new NamedPipeServerStream(PipeName, PipeDirection.In, 1, PipeTransmissionMode.Byte, PipeOptions.Asynchronous);
                await s.WaitForConnectionAsync();
                using var r = new StreamReader(s, Encoding.UTF8);
                var line = await r.ReadLineAsync() ?? "";
                Post(() => Command(line));
            }
            catch { await Task.Delay(500); }
        }
    });

    static void Command(string line)
    {
        var p = line.Split('\t', 2);
        switch (p[0])
        {
            case "notify": Tray.Notify(p.Length > 1 ? p[1] : ""); break;
            case "open": if (Prefs.Bool("onboarded")) Settings.Open(); else Onboarding.Open(); break;
            case "update": Updater.Check(() => Settings.Push()); break;   // check now (support and tests)
        }
    }

    static void Send(string line)
    {
        try { using var c = new NamedPipeClientStream(".", PipeName, PipeDirection.Out); c.Connect(1500); var b = Encoding.UTF8.GetBytes(line + "\n"); c.Write(b); } catch { }
    }
}

// Read answers aloud in Windows' voice. Esc or the shortcut stops it.
static class Voice
{
    static System.Speech.Synthesis.SpeechSynthesizer synth;
    public static bool Speaking;
    public static void Say(string text, Action done)
    {
        Shush();
        synth ??= new System.Speech.Synthesis.SpeechSynthesizer();
        synth.SetOutputToDefaultAudioDevice();
        Speaking = true;
        var prompt = synth.SpeakAsync(text);
        synth.SpeakCompleted += Ended;
        void Ended(object s, System.Speech.Synthesis.SpeakCompletedEventArgs e)
        {
            if (e.Prompt != prompt) return;
            synth.SpeakCompleted -= Ended;
            Program.Post(() => { if (!Speaking) return; Speaking = false; done(); });
        }
    }
    public static bool Shush()
    {
        if (!Speaking) return false;
        Speaking = false;
        try { synth?.SpeakAsyncCancelAll(); } catch { }
        return true;
    }
}

// Cues from Windows' own sounds (C:\Windows\Media), at Atticus's volume.
static class Sound
{
    public static string PathOf(string name) => Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.Windows), "Media", name + ".wav");
    public static void Play(string name, double volume)
    {
        var f = PathOf(name);
        if (!File.Exists(f)) return;
        Task.Run(() =>
        {
            try
            {
                using var r = new AudioFileReader(f) { Volume = (float)Math.Clamp(volume, 0, 1) };
                using var o = new WaveOut();
                o.Init(r); o.Play();
                while (o.PlaybackState == PlaybackState.Playing) Thread.Sleep(30);
            }
            catch { }
        });
    }
    public static void Cue(string k) { var n = Prefs.Str(k); if (n != "None" && n != "") Play(n, Prefs.Num("volume") * 2); }   // soundStart / soundDone / soundStop
    public static string[] All() => new[] { "None" }.Concat(Directory.Exists(Path.GetDirectoryName(PathOf("x"))) ?
        Directory.GetFiles(Path.GetDirectoryName(PathOf("x"))!, "*.wav").Select(Path.GetFileNameWithoutExtension).Where(n => !n.StartsWith("Alarm") && !n.StartsWith("Ring")).OrderBy(n => n) : Array.Empty<string>()).ToArray();
}

// The tray icon: quick actions, today's usage.
sealed class Tray
{
    readonly NotifyIcon icon;
    public Tray()
    {
        var ico = Path.Combine(Paths.App, "ui", "AppIcon.ico");
        icon = new NotifyIcon { Icon = File.Exists(ico) ? new Icon(ico) : SystemIcons.Application, Text = "Atticus", Visible = Prefs.Bool("menuIcon") };
        icon.ContextMenuStrip = new ContextMenuStrip();
        icon.ContextMenuStrip.Opening += (_, _) => Build();
        icon.MouseClick += (_, e) => { if (e.Button == MouseButtons.Left) Program.Settings.Open(); };
        Build();
    }
    public bool Visible { set => icon.Visible = value; }
    public void Notify(string text) { icon.Visible = true; icon.ShowBalloonTip(5000, "Atticus", text, ToolTipIcon.None); if (!Prefs.Bool("menuIcon")) Task.Delay(8000).ContinueWith(_ => Program.Post(() => icon.Visible = Prefs.Bool("menuIcon"))); }
    void Build()
    {
        var m = icon.ContextMenuStrip; m.Items.Clear();
        var keys = Prefs.Str("hotkeyLabel");
        var hint = Prefs.Str("input") == "type" ? $"Press {keys} to type" : Prefs.Str("talkMode") == "toggle" ? $"Press {keys} to talk · again to send" : $"Hold {keys} and talk";
        m.Items.Add(new ToolStripMenuItem(Prefs.Bool("paused") ? "Atticus is paused" : hint) { Enabled = false });
        m.Items.Add(new ToolStripSeparator());
        m.Items.Add("Ask Atticus", null, (_, _) => { Program.Box.clickTalk = true; Program.Box.Start(); });
        m.Items.Add(Prefs.Bool("paused") ? "Resume Atticus" : "Pause Atticus", null, (_, _) => { Program.Settings.Apply("paused", !Prefs.Bool("paused")); Program.Settings.Push(); });
        m.Items.Add(new ToolStripSeparator());
        m.Items.Add(new ToolStripMenuItem(Today()) { Enabled = false });
        m.Items.Add(new ToolStripSeparator());
        m.Items.Add("Settings…", null, (_, _) => Program.Settings.Open());
        m.Items.Add("Setup…", null, (_, _) => Program.Onboarding.Open());
        m.Items.Add(new ToolStripSeparator());
        m.Items.Add("Quit Atticus", null, (_, _) => { icon.Visible = false; Application.Exit(); });
    }
    static string Today()
    {
        var day = DateTime.Now.ToString("yyyy-MM-dd");
        int n = 0; double usd = 0;
        try
        {
            foreach (var l in File.ReadLines(Paths.Log))
            {
                try
                {
                    using var d = System.Text.Json.JsonDocument.Parse(l);
                    if (!(d.RootElement.GetProperty("at").GetString() ?? "").StartsWith(day)) continue;
                    n++; if (d.RootElement.TryGetProperty("usd", out var u)) usd += u.GetDouble();
                }
                catch { }
            }
        }
        catch { }
        return $"Today: {n} ask{(n == 1 ? "" : "s")} · " + (usd < 1 ? $"{usd * 100:0.00}¢" : $"${usd:0.00}");
    }
}
