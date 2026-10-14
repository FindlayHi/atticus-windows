// Win32 calls Atticus needs: keyboard and mouse hooks, the foreground window, DWM backdrops, capture exclusion, job objects.
using System.Runtime.InteropServices;
using System.Text;

namespace Atticus;

static class Native
{
    public const int WH_KEYBOARD_LL = 13, WH_MOUSE_LL = 14;
    public const int WM_KEYDOWN = 0x100, WM_KEYUP = 0x101, WM_SYSKEYDOWN = 0x104, WM_SYSKEYUP = 0x105;
    public const int WM_LBUTTONDOWN = 0x201, WM_RBUTTONDOWN = 0x204, WM_NCLBUTTONDOWN = 0xA1;
    public const int VK_SHIFT = 0x10, VK_CONTROL = 0x11, VK_MENU = 0x12, VK_ESCAPE = 0x1B, VK_LWIN = 0x5B, VK_RWIN = 0x5C;
    public const int WS_EX_TOOLWINDOW = 0x80, WS_EX_NOACTIVATE = 0x08000000, WS_EX_TOPMOST = 0x8, WS_EX_LAYERED = 0x80000, WS_EX_TRANSPARENT = 0x20;

    [StructLayout(LayoutKind.Sequential)]
    public struct KBDLLHOOKSTRUCT { public uint vkCode, scanCode, flags, time; public IntPtr extra; }
    [StructLayout(LayoutKind.Sequential)]
    public struct MSLLHOOKSTRUCT { public POINT pt; public uint mouseData, flags, time; public IntPtr extra; }
    [StructLayout(LayoutKind.Sequential)]
    public struct POINT { public int X, Y; }
    [StructLayout(LayoutKind.Sequential)]
    public struct RECT { public int Left, Top, Right, Bottom; }
    [StructLayout(LayoutKind.Sequential)]
    public struct MARGINS { public int Left, Right, Top, Bottom; }

    public delegate IntPtr HookProc(int code, IntPtr wParam, IntPtr lParam);
    [DllImport("user32", SetLastError = true)] public static extern IntPtr SetWindowsHookEx(int id, HookProc fn, IntPtr mod, uint thread);
    [DllImport("user32")] public static extern bool UnhookWindowsHookEx(IntPtr h);
    [DllImport("user32")] public static extern IntPtr CallNextHookEx(IntPtr h, int code, IntPtr w, IntPtr l);
    [DllImport("kernel32")] public static extern IntPtr GetModuleHandle(string name);
    [DllImport("user32")] public static extern short GetAsyncKeyState(int vk);
    [DllImport("user32")] public static extern void keybd_event(byte vk, byte scan, uint flags, IntPtr extra);
    public static bool Down(int vk) => (GetAsyncKeyState(vk) & 0x8000) != 0;
    public static void Tap(byte vk) { keybd_event(vk, 0, 0, IntPtr.Zero); keybd_event(vk, 0, 2, IntPtr.Zero); }

    [DllImport("user32")] public static extern IntPtr GetForegroundWindow();
    [DllImport("user32")] public static extern bool SetForegroundWindow(IntPtr h);
    [DllImport("user32")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
    [DllImport("user32")] public static extern bool ReleaseCapture();
    [DllImport("user32")] public static extern IntPtr SendMessage(IntPtr h, int msg, IntPtr w, IntPtr l);
    [DllImport("user32")] public static extern bool SetWindowDisplayAffinity(IntPtr h, uint affinity);   // 0x11 = WDA_EXCLUDEFROMCAPTURE
    [DllImport("user32")] public static extern int GetWindowLong(IntPtr h, int i);
    [DllImport("user32")] public static extern int SetWindowLong(IntPtr h, int i, int v);
    [DllImport("user32")] public static extern bool GetCursorPos(out POINT p);
    [DllImport("user32")] public static extern bool IsWindow(IntPtr h);
    [DllImport("user32")] public static extern bool IsIconic(IntPtr h);
    [DllImport("user32")] public static extern bool IsWindowVisible(IntPtr h);
    [DllImport("user32")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
    [DllImport("user32")] public static extern IntPtr GetWindow(IntPtr h, uint cmd);
    [DllImport("user32")] public static extern bool ShowWindow(IntPtr h, int cmd);
    [DllImport("user32")] static extern bool BringWindowToTop(IntPtr h);
    [DllImport("user32")] static extern bool AttachThreadInput(uint a, uint b, bool attach);
    [DllImport("kernel32")] static extern uint GetCurrentThreadId();
    [DllImport("dwmapi")] public static extern int DwmGetWindowAttribute(IntPtr h, int attr, out RECT r, int size);
    [DllImport("dwmapi")] static extern int DwmGetWindowAttribute(IntPtr h, int attr, out int v, int size);
    public static bool Cloaked(IntPtr h) => DwmGetWindowAttribute(h, 14, out int v, 4) == 0 && v != 0;   // DWMWA_CLOAKED: on another desktop, or a hidden UWP frame

    // Bring a window forward. Windows lets only the app with the last input do that: borrow the front window's input queue.
    public static bool Bring(IntPtr h)
    {
        var fg = GetForegroundWindow();
        uint me = GetCurrentThreadId(), them = fg != IntPtr.Zero ? GetWindowThreadProcessId(fg, out _) : 0;
        var attached = them != 0 && them != me && AttachThreadInput(me, them, true);
        Tap(0xE8);   // an unassigned key: counts as input, does nothing
        BringWindowToTop(h);
        var ok = SetForegroundWindow(h);
        if (attached) AttachThreadInput(me, them, false);
        return ok;
    }

    public delegate void WinEventProc(IntPtr hook, uint ev, IntPtr hwnd, int obj, int child, uint thread, uint time);
    [DllImport("user32")] public static extern IntPtr SetWinEventHook(uint min, uint max, IntPtr mod, WinEventProc fn, uint pid, uint thread, uint flags);

    [DllImport("dwmapi")] public static extern int DwmSetWindowAttribute(IntPtr h, int attr, ref int value, int size);
    [DllImport("dwmapi")] public static extern int DwmExtendFrameIntoClientArea(IntPtr h, ref MARGINS m);

    // The look: dark acrylic behind the page, rounded corners, no title bar (Windows 11; Windows 10 gets a plain dark box).
    public static void Backdrop(IntPtr h, bool acrylic = true, bool round = true)
    {
        int dark = 1; DwmSetWindowAttribute(h, 20, ref dark, 4);   // DWMWA_USE_IMMERSIVE_DARK_MODE
        if (round) { int c = 2; DwmSetWindowAttribute(h, 33, ref c, 4); }   // DWMWA_WINDOW_CORNER_PREFERENCE = ROUND
        var m = new MARGINS { Left = -1, Right = -1, Top = -1, Bottom = -1 }; DwmExtendFrameIntoClientArea(h, ref m);
        int b = acrylic ? 3 : 2; DwmSetWindowAttribute(h, 38, ref b, 4);   // DWMWA_SYSTEMBACKDROP_TYPE: 3 = acrylic, 2 = mica
    }

    public static string ProcessName(IntPtr hwnd)
    {
        GetWindowThreadProcessId(hwnd, out var pid);
        try
        {
            using var p = System.Diagnostics.Process.GetProcessById((int)pid);
            var path = p.MainModule?.FileName;
            var d = path != null ? System.Diagnostics.FileVersionInfo.GetVersionInfo(path).FileDescription : null;
            return string.IsNullOrWhiteSpace(d) || d.Length > 40 ? p.ProcessName : d.Trim();
        }
        catch { return ""; }
    }

    // Job object: everything Atticus starts ends with it (Claude Code, its tools, hooks); watchers may break away.
    [DllImport("kernel32")] static extern IntPtr CreateJobObject(IntPtr a, string name);
    [DllImport("kernel32")] static extern bool SetInformationJobObject(IntPtr job, int cls, ref JOBOBJECT_EXTENDED_LIMIT_INFORMATION info, int size);
    [DllImport("kernel32")] public static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);
    [StructLayout(LayoutKind.Sequential)] struct IO_COUNTERS { public ulong a, b, c, d, e, f; }
    [StructLayout(LayoutKind.Sequential)]
    struct JOBOBJECT_BASIC_LIMIT_INFORMATION { public long t1, t2; public uint flags; public UIntPtr minWs, maxWs; public uint procs; public UIntPtr affinity; public uint prio, sched; }
    [StructLayout(LayoutKind.Sequential)]
    struct JOBOBJECT_EXTENDED_LIMIT_INFORMATION { public JOBOBJECT_BASIC_LIMIT_INFORMATION basic; public IO_COUNTERS io; public UIntPtr procMem, jobMem, peakProc, peakJob; }
    static IntPtr job;
    public static IntPtr Job
    {
        get
        {
            if (job != IntPtr.Zero) return job;
            job = CreateJobObject(IntPtr.Zero, null);
            var info = new JOBOBJECT_EXTENDED_LIMIT_INFORMATION();
            info.basic.flags = 0x2000 | 0x800;   // KILL_ON_JOB_CLOSE | BREAKAWAY_OK
            SetInformationJobObject(job, 9, ref info, Marshal.SizeOf<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>());
            return job;
        }
    }
}
