// The mini screen: a live view of the window Atticus works in, drawn in the box where the page leaves room for it.
// DWM draws it (DwmRegisterThumbnail), like the taskbar's previews: live even when the window is covered, nothing copied
// frame by frame, no capture permission, and it is part of the box, so it is hidden from screen capture with it.
// It sits on top of the page (DWM composites thumbnails above a window's content), so the page's tag shows only around it.
using System.Runtime.InteropServices;
using System.Text.Json.Nodes;

namespace Atticus;

sealed class MiniScreen
{
    [DllImport("dwmapi")] static extern int DwmRegisterThumbnail(IntPtr dest, IntPtr src, out IntPtr thumb);
    [DllImport("dwmapi")] static extern int DwmUnregisterThumbnail(IntPtr thumb);
    [DllImport("dwmapi")] static extern int DwmUpdateThumbnailProperties(IntPtr thumb, ref Props p);
    [DllImport("dwmapi")] static extern int DwmQueryThumbnailSourceSize(IntPtr thumb, out Native.POINT size);
    [StructLayout(LayoutKind.Sequential)]
    struct Props { public uint flags; public Native.RECT dest, src; public byte opacity; public int visible, clientOnly; }

    readonly Form host;
    public Action<JsonObject> Publish;
    JsonObject target;
    IntPtr hwnd, thumb;
    System.Windows.Forms.Timer timer;
    bool shown, live, done;
    double[] rect, clip;
    double opacity = 1;
    static readonly Dictionary<string, string> icons = new();

    public MiniScreen(Form host) { this.host = host; }
    public bool HasTarget => target != null;
    public bool Holds => target != null && shown && done;   // the finished view stays until the next ask, typing, or a click on it

    // "▣ {pid, windowID, windowFrame, app, window, exe, action}" from atticus.py
    public static bool Valid(JsonObject v, out IntPtr h)
    {
        h = IntPtr.Zero;
        try
        {
            if (v?["pid"]?.GetValue<double>() is not > 0 || v["windowID"]?.GetValue<string>() is not { Length: > 0 and < 20 } id || !id.All(char.IsAsciiDigit)) return false;
            var f = v["windowFrame"]?.AsArray().Select(n => n.GetValue<double>()).ToArray();
            if (f is not { Length: 4 } || !f.All(double.IsFinite) || f[2] <= 0 || f[3] <= 0) return false;
            h = new IntPtr(long.Parse(id));
            return h != IntPtr.Zero;
        }
        catch { return false; }
    }

    public void Receive(string json)
    {
        JsonObject v;
        try { v = JsonNode.Parse(json) as JsonObject; } catch { return; }
        if (!Valid(v, out var h) || !Native.IsWindow(h)) return;
        if (h != hwnd) { Drop(); live = false; hwnd = h; }
        var exe = v["exe"]?.GetValue<string>() ?? ""; v.Remove("exe");
        if (Icon(exe) is { } icon) v["icon"] = icon;
        target = v; done = false;
        if (timer == null) { timer = new System.Windows.Forms.Timer { Interval = 500 }; timer.Tick += (_, _) => { try { Refresh(); } catch { } }; timer.Start(); }
        Refresh(true);
    }

    static string Icon(string exe)
    {
        if (exe == "" || !File.Exists(exe)) return null;
        if (icons.TryGetValue(exe, out var s)) return s;
        try
        {
            using var i = System.Drawing.Icon.ExtractAssociatedIcon(exe);
            using var b = i.ToBitmap(); using var m = new MemoryStream();
            b.Save(m, System.Drawing.Imaging.ImageFormat.Png);
            return icons[exe] = "data:image/png;base64," + Convert.ToBase64String(m.ToArray());
        }
        catch { return icons[exe] = null; }
    }

    // The visible frame (no invisible resize borders), screen pixels.
    static Rectangle Frame(IntPtr h)
    {
        if (Native.DwmGetWindowAttribute(h, 9, out Native.RECT r, Marshal.SizeOf<Native.RECT>()) != 0) Native.GetWindowRect(h, out r);
        return Rectangle.FromLTRB(r.Left, r.Top, r.Right, r.Bottom);
    }

    void Refresh(bool force = false)
    {
        if (target == null) return;
        var wasLive = live;
        bool moved = false;
        if (!Native.IsWindow(hwnd)) { Drop(); live = false; }   // closed: the page shows its icon and title
        else
        {
            var f = Frame(hwnd);
            var old = target["windowFrame"]?.AsArray().Select(n => n.GetValue<double>()).ToArray();
            if (f.Width > 0 && f.Height > 0 && !Native.IsIconic(hwnd) && (old == null || old[0] != f.X || old[1] != f.Y || old[2] != f.Width || old[3] != f.Height))
            { target["windowFrame"] = new JsonArray((double)f.X, (double)f.Y, (double)f.Width, (double)f.Height); moved = true; }
            if (Native.IsIconic(hwnd)) { Drop(); live = false; }   // minimized windows draw nothing
            else if (thumb == IntPtr.Zero) { if (DwmRegisterThumbnail(host.Handle, hwnd, out thumb) != 0) thumb = IntPtr.Zero; live = thumb != IntPtr.Zero; }
        }
        var next = Native.IsWindow(hwnd) && NeedsPreview(hwnd, host);
        var changed = next != shown; shown = next;
        Place();
        if (force || changed || moved || wasLive != live) Send();
    }

    void Send()
    {
        if (target == null) return;
        var v = (JsonObject)target.DeepClone(); v["shown"] = shown; v["live"] = live;
        Publish?.Invoke(v);
    }

    // No preview for a window the user can already see: in front, on the box's screen, nothing over it.
    public static bool NeedsPreview(IntPtr h, Form host)
    {
        if (Native.GetForegroundWindow() != h) return true;
        var f = Frame(h);
        if (!Screen.FromHandle(host.Handle).Bounds.Contains(f)) return true;
        for (var w = Native.GetWindow(h, 3); w != IntPtr.Zero; w = Native.GetWindow(w, 3))   // GW_HWNDPREV: the windows above it
        {
            Native.GetWindowThreadProcessId(w, out var pid);
            if (pid == Environment.ProcessId || !Native.IsWindowVisible(w) || Native.Cloaked(w) || (Native.GetWindowLong(w, -20) & Native.WS_EX_TRANSPARENT) != 0) continue;
            if (!Native.GetWindowRect(w, out var r)) continue;
            var o = Rectangle.Intersect(f, Rectangle.FromLTRB(r.Left, r.Top, r.Right, r.Bottom));
            if (o.Width > 1 && o.Height > 1) return true;
        }
        return false;
    }

    // The page's rect for the picture and the part of it that is in view (CSS pixels), and its fade.
    public void Layout(System.Text.Json.JsonElement b)
    {
        double[] Nums(string k) => b.TryGetProperty(k, out var a) && a.ValueKind == System.Text.Json.JsonValueKind.Array && a.GetArrayLength() == 4
            && a.EnumerateArray().All(n => n.ValueKind == System.Text.Json.JsonValueKind.Number) ? a.EnumerateArray().Select(n => n.GetDouble()).ToArray() : null;
        rect = Nums("rect"); clip = Nums("clip");
        if (rect != null && !rect.All(double.IsFinite) || clip != null && !clip.All(double.IsFinite)) rect = clip = null;
        opacity = b.TryGetProperty("opacity", out var o) && o.ValueKind == System.Text.Json.JsonValueKind.Number && double.IsFinite(o.GetDouble()) ? Math.Clamp(o.GetDouble(), 0, 1) : 1;
        Place();
    }

    // Fit the source (letterboxed, centered) into rect, cut to clip; k = device pixels per CSS pixel.
    // Returns (destination in the box, source in the window's thumbnail), or null when nothing is in view.
    public static (Rectangle dest, Rectangle src)? Fit(double[] rect, double[] clip, double k, RectangleF source)
    {
        if (rect == null || clip == null || rect[2] <= 0 || rect[3] <= 0 || source.Width <= 0 || source.Height <= 0) return null;
        double rx = rect[0] * k, ry = rect[1] * k, rw = rect[2] * k, rh = rect[3] * k;
        var s = Math.Min(rw / source.Width, rh / source.Height);
        double dw = source.Width * s, dh = source.Height * s, dx = rx + (rw - dw) / 2, dy = ry + (rh - dh) / 2;
        double l = Math.Max(dx, clip[0] * k), t = Math.Max(dy, clip[1] * k), r = Math.Min(dx + dw, (clip[0] + clip[2]) * k), bt = Math.Min(dy + dh, (clip[1] + clip[3]) * k);
        if (r - l < 1 || bt - t < 1) return null;
        int L = (int)Math.Round(l), T = (int)Math.Round(t), R = (int)Math.Round(r), B = (int)Math.Round(bt);
        return (Rectangle.FromLTRB(L, T, R, B), Rectangle.FromLTRB((int)Math.Round(source.X + (L - dx) / s), (int)Math.Round(source.Y + (T - dy) / s),
                                                                   (int)Math.Round(source.X + (R - dx) / s), (int)Math.Round(source.Y + (B - dy) / s)));
    }

    void Place()
    {
        if (thumb == IntPtr.Zero) return;
        var p = new Props { flags = 0x1F, opacity = (byte)Math.Round(255 * opacity) };   // destination, source, opacity, visible, client-only
        (Rectangle dest, Rectangle src)? fit = null;
        if (live && shown && Native.GetWindowRect(hwnd, out var wr) && DwmQueryThumbnailSourceSize(thumb, out var size) == 0 && wr.Right > wr.Left)
        {
            var f = Frame(hwnd); var s = size.X / (double)(wr.Right - wr.Left);   // the thumbnail's own pixels: the whole window rect, borders and all
            fit = Fit(rect, clip, host.DeviceDpi / 96.0, new RectangleF((float)((f.X - wr.Left) * s), (float)((f.Y - wr.Top) * s), (float)(f.Width * s), (float)(f.Height * s)));
        }
        if (fit is { } x)
        {
            p.visible = 1;
            p.dest = new Native.RECT { Left = x.dest.Left, Top = x.dest.Top, Right = x.dest.Right, Bottom = x.dest.Bottom };
            p.src = new Native.RECT { Left = x.src.Left, Top = x.src.Top, Right = x.src.Right, Bottom = x.src.Bottom };
        }
        DwmUpdateThumbnailProperties(thumb, ref p);
    }

    public void Finish() { if (target != null) done = true; }   // the view keeps following the window until the next ask

    // A click on it: the user wants to see that window.
    public void Open()
    {
        if (target == null || !Native.IsWindow(hwnd)) return;
        if (Native.IsIconic(hwnd)) Native.ShowWindow(hwnd, 9);   // SW_RESTORE
        Native.Bring(hwnd);
    }

    void Drop() { if (thumb != IntPtr.Zero) DwmUnregisterThumbnail(thumb); thumb = IntPtr.Zero; }

    public void Reset()
    {
        timer?.Stop(); timer?.Dispose(); timer = null;
        Drop(); target = null; hwnd = IntPtr.Zero; shown = live = done = false;
    }
}
