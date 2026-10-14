// Speech to text on this PC: the microphone (16 kHz mono) into Whisper (whisper.cpp). Audio never leaves the machine.
// While the user talks, the words so far are re-read about every 0.7 s (live words in the box); on release, one last pass
// over everything is the confirmed transcript.
using NAudio.Wave;
using Whisper.net;

namespace Atticus;

sealed class Speech
{
    WhisperFactory factory;
    readonly SemaphoreSlim busy = new(1, 1);
    WaveIn mic;
    readonly List<float> audio = new();
    float loudest;
    System.Threading.Timer partial;
    int session;
    public string Prompt = "";   // app names and the user's own words: Whisper leans toward them
    public bool Running => mic != null;
    public bool Ready => factory != null;
    public string Error = "";

    public static string Url(string model) => $"https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-{model}.bin";

    public Task Load() => Task.Run(() =>
    {
        try { if (File.Exists(Paths.Model) && factory == null) factory = WhisperFactory.FromPath(Paths.Model); Error = ""; }
        catch (Exception e) { Error = e.Message; }
    });

    // Settings / setup: fetch the model once (~150 MB for base.en). progress: 0..1
    public static async Task<bool> Download(Action<double> progress)
    {
        var dest = Paths.Model;
        Directory.CreateDirectory(Path.GetDirectoryName(dest)!);
        try
        {
            using var http = new HttpClient { Timeout = TimeSpan.FromMinutes(30) };
            using var r = await http.GetAsync(Url(Prefs.Str("speechModel")), HttpCompletionOption.ResponseHeadersRead);
            r.EnsureSuccessStatusCode();
            long total = r.Content.Headers.ContentLength ?? 0, got = 0;
            var tmp = dest + ".part";
            await using (var src = await r.Content.ReadAsStreamAsync())
            await using (var f = File.Create(tmp))
            {
                var buf = new byte[1 << 16]; int n;
                while ((n = await src.ReadAsync(buf)) > 0) { await f.WriteAsync(buf.AsMemory(0, n)); got += n; if (total > 0) progress(got / (double)total); }
            }
            File.Move(tmp, dest, true);
            return true;
        }
        catch { return false; }
    }

    public static bool MicAllowed()
    {
        try
        {
            using var k = Microsoft.Win32.Registry.CurrentUser.OpenSubKey(@"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone");
            using var all = Microsoft.Win32.Registry.LocalMachine.OpenSubKey(@"SOFTWARE\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone");
            using var desktop = k?.OpenSubKey("NonPackaged");
            return !"Deny".Equals(k?.GetValue("Value")) && !"Deny".Equals(all?.GetValue("Value")) && !"Deny".Equals(desktop?.GetValue("Value")) && WaveIn.DeviceCount > 0;
        }
        catch { return WaveIn.DeviceCount > 0; }
    }

    // onLevel: 0..1 loudness, onWords: the words so far. Both arrive on the UI thread.
    public string Start(Action<double> onLevel, Action<string> onWords)
    {
        Cancel();
        if (WaveIn.DeviceCount == 0) return "No microphone is available. Connect one and try again.";
        var ui = SynchronizationContext.Current;
        lock (audio) { audio.Clear(); loudest = 0; }
        var me = ++session;
        mic = new WaveIn { WaveFormat = new WaveFormat(16000, 16, 1), BufferMilliseconds = 50 };
        int n = 0;
        mic.DataAvailable += (_, e) =>
        {
            float sum = 0;
            lock (audio)
            {
                for (int i = 0; i + 1 < e.BytesRecorded; i += 2)
                {
                    var s = BitConverter.ToInt16(e.Buffer, i) / 32768f;
                    audio.Add(s); sum += s * s;
                }
            }
            var rms = MathF.Sqrt(sum / Math.Max(1, e.BytesRecorded / 2));
            loudest = Math.Max(loudest, rms);
            if (++n % 2 == 0) ui?.Post(_ => { if (session == me) onLevel(Math.Min(1, rms * 14)); }, null);
        };
        try { mic.StartRecording(); }
        catch (Exception e) { mic.Dispose(); mic = null; return MicAllowed() ? "Mic error: " + e.Message : "Allow microphone access in Settings → Privacy & security → Microphone (Let desktop apps access your microphone)."; }
        partial = new System.Threading.Timer(async _ =>
        {
            if (session != me || !Ready || !busy.Wait(0)) return;   // one read at a time; a slow one just skips a beat
            try
            {
                float[] a; lock (audio) a = audio.ToArray();
                if (a.Length < 8000 || loudest < 0.01f) return;
                var t = await Transcribe(a);
                if (session == me && mic != null) ui?.Post(_ => { if (session == me) onWords(t); }, null);
            }
            catch { }
            finally { busy.Release(); }
        }, null, 700, 700);
        return "";
    }

    // Keys up: the last pass over all of it. null = could not confirm (no model, or it failed).
    public async Task<string> Stop()
    {
        var me = session;
        partial?.Dispose(); partial = null;
        try { mic?.StopRecording(); } catch { }
        mic?.Dispose(); mic = null;
        float[] a; lock (audio) a = audio.ToArray();
        if (!Ready) return null;
        if (a.Length < 4000 || loudest < 0.01f) return "";   // nothing said
        await busy.WaitAsync();
        try { return session == me ? await Transcribe(a) : null; }
        catch { return null; }
        finally { busy.Release(); }
    }

    public void Cancel()
    {
        session++;
        partial?.Dispose(); partial = null;
        try { mic?.StopRecording(); } catch { }
        mic?.Dispose(); mic = null;
    }

    public Task<string> TranscribeOnce(float[] a, string prompt = null) => Transcribe(a, prompt);   // the meeting panel: its own words to lean toward

    async Task<string> Transcribe(float[] a, string prompt = null)
    {
        prompt ??= Prompt;
        var b = factory.CreateBuilder().WithLanguage(Prefs.Str("speechModel").EndsWith(".en") ? "en" : "auto")
            .WithThreads(Math.Clamp(Environment.ProcessorCount - 1, 2, 8)).WithNoContext().WithSingleSegment();
        if (prompt.Length > 0) b = b.WithPrompt(prompt);
        await using var p = b.Build();
        var text = new System.Text.StringBuilder();
        await foreach (var seg in p.ProcessAsync(a)) text.Append(seg.Text);
        return Clean(text.ToString());
    }

    // Whisper marks silence and noise ("[BLANK_AUDIO]", "(music)") and sometimes invents a polite "Thank you." in a quiet room.
    public static string Clean(string t)
    {
        t = System.Text.RegularExpressions.Regex.Replace(t, @"\[[^\]]*\]|\([^)]*\)|\*[^*]*\*", " ");
        t = string.Join(" ", t.Split((char[])null, StringSplitOptions.RemoveEmptyEntries));
        return t is "Thank you." or "Thanks for watching!" or "you" or "." ? "" : t;
    }
}
