// The talk shortcut (any key plus modifiers, held or pressed), Esc while the box is up, clicks outside the box,
// and which app the user is in. The keyboard and mouse hooks run on their own thread, which does nothing else: Windows makes
// every key and click on the PC wait for a low-level hook, so a busy UI thread (WebView2, the mini screen) must never be that thread.
// Their handlers only decide swallow-or-pass and post the rest to the UI thread.
using System.Runtime.InteropServices;
using static Atticus.Native;

namespace Atticus;

static class Hooks
{
    static IntPtr kb, mouse, fg;
    static readonly HookProc kbProc = OnKey, mouseProc = OnMouse;   // kept alive: the hook holds only a pointer
    static readonly WinEventProc fgProc = OnForeground;
    public static Action<bool> Shortcut;     // pressed / released
    public static Func<bool> EscWanted;      // the box is up: Esc is ours
    public static Action Escape;
    public static Action<int, int> ClickAt;  // a click anywhere (screen pixels)
    public static Action<int, int, string> Recorded;   // Settings → Change Shortcut: (vk, mods, label), vk 0 = cancelled
    public static Action<string> RecordingMods;
    public static bool Recording;
    public static string Mine = "";          // the app the user is in, never Atticus itself
    public static IntPtr MineHwnd;
    public static DateTime LastInput;        // the user's own last key or click (never injected): a switch they made
    public static Func<IntPtr, bool> Guard;  // a window came forward by itself while Atticus works: true = it was put back
    static bool down;

    [StructLayout(LayoutKind.Sequential)] struct MSG { public IntPtr hwnd; public uint message; public IntPtr wParam, lParam; public uint time; public int x, y; }
    [DllImport("user32")] static extern int GetMessage(out MSG m, IntPtr hwnd, uint min, uint max);
    [DllImport("user32")] static extern bool TranslateMessage(ref MSG m);
    [DllImport("user32")] static extern IntPtr DispatchMessage(ref MSG m);

    public static void Install()
    {
        new Thread(() =>
        {
            var mod = GetModuleHandle(null);
            kb = SetWindowsHookEx(WH_KEYBOARD_LL, kbProc, mod, 0);
            mouse = SetWindowsHookEx(WH_MOUSE_LL, mouseProc, mod, 0);
            while (GetMessage(out var m, IntPtr.Zero, 0, 0) > 0) { TranslateMessage(ref m); DispatchMessage(ref m); }   // the hooks need a message loop
        }) { IsBackground = true, Name = "input hooks", Priority = ThreadPriority.AboveNormal }.Start();
        fg = SetWinEventHook(3, 3, IntPtr.Zero, fgProc, 0, 0, 0);   // EVENT_SYSTEM_FOREGROUND, out of context
        OnForeground(IntPtr.Zero, 3, GetForegroundWindow(), 0, 0, 0, 0);
    }

    public static int Mods() => (Down(VK_MENU) ? 1 : 0) | (Down(VK_CONTROL) ? 2 : 0) | (Down(VK_SHIFT) ? 4 : 0) | (Down(VK_LWIN) || Down(VK_RWIN) ? 8 : 0);
    public static bool ModsHeld() { var want = (int)Prefs.Num("hotkeyMods"); return want == 0 || (Mods() & want) == want; }

    static bool IsMod(uint vk) => vk is 0x10 or 0x11 or 0x12 or 0x5B or 0x5C or >= 0xA0 and <= 0xA5;

    public static string Label(int vk, int mods)
    {
        var parts = new List<string>();
        if ((mods & 2) != 0) parts.Add("Ctrl"); if ((mods & 1) != 0) parts.Add("Alt"); if ((mods & 4) != 0) parts.Add("Shift"); if ((mods & 8) != 0) parts.Add("Win");
        parts.Add(vk switch
        {
            0x20 => "Space", 0x0D => "Enter", 0x09 => "Tab", 0x08 => "Backspace", 0x25 => "←", 0x26 => "↑", 0x27 => "→", 0x28 => "↓",
            >= 0x70 and <= 0x87 => "F" + (vk - 0x6F), >= 0x30 and <= 0x5A => ((char)vk).ToString(), 0xC0 => "`", _ => $"Key {vk}"
        });
        return string.Join("+", parts);
    }

    static IntPtr OnKey(int code, IntPtr w, IntPtr l)
    {
        if (code < 0) return CallNextHookEx(kb, code, w, l);
        var k = Marshal.PtrToStructure<KBDLLHOOKSTRUCT>(l);
        bool press = w == WM_KEYDOWN || w == WM_SYSKEYDOWN, up = w == WM_KEYUP || w == WM_SYSKEYUP;
        if ((k.flags & 0x10) != 0) return CallNextHookEx(kb, code, w, l);   // injected (ours, or the AI typing): never a shortcut
        if (Recording)
        {
            if (IsMod(k.vkCode)) { RecordingMods?.Invoke(Label(0, Mods()).Replace("+Key 0", "").Replace("Key 0", "")); return CallNextHookEx(kb, code, w, l); }
            if (!press) return 1;
            int m = Mods();
            if (k.vkCode == VK_ESCAPE && m == 0) { Recording = false; Recorded?.Invoke(0, 0, ""); return 1; }
            bool fkey = k.vkCode is >= 0x70 and <= 0x87;
            if (m == 0 && !fkey) { RecordingMods?.Invoke("Add Ctrl, Alt, Shift or Win"); return 1; }
            Recording = false; Recorded?.Invoke((int)k.vkCode, m, Label((int)k.vkCode, m));
            return 1;
        }
        int vk = (int)Prefs.Num("hotkeyCode"), mods = (int)Prefs.Num("hotkeyMods");
        if (k.vkCode == vk)
        {
            if (press && Mods() == mods)
            {
                if (!down) { down = true; if ((mods & 1) != 0 || (mods & 8) != 0) Tap(0xE8); Program.Post(() => Shortcut?.Invoke(true)); }   // 0xE8: Alt/Win alone would open a menu
                return 1;   // swallowed, key repeat too
            }
            if (up && down) { down = false; Program.Post(() => Shortcut?.Invoke(false)); return 1; }
        }
        if (press && k.vkCode == VK_ESCAPE && Mods() == 0 && EscWanted?.Invoke() == true) { Program.Post(() => Escape?.Invoke()); return 1; }
        if (press && !down && !IsMod(k.vkCode)) LastInput = DateTime.Now;   // a key the user typed into an app; never Atticus's own shortcut
        return CallNextHookEx(kb, code, w, l);
    }

    static IntPtr OnMouse(int code, IntPtr w, IntPtr l)
    {
        if (code >= 0 && (w == WM_LBUTTONDOWN || w == WM_RBUTTONDOWN))
        {
            var m = Marshal.PtrToStructure<MSLLHOOKSTRUCT>(l);
            if ((m.flags & 1) == 0) LastInput = DateTime.Now;   // LLMHF_INJECTED: a helper's click, not the user's
            Program.Post(() => ClickAt?.Invoke(m.pt.X, m.pt.Y));
        }
        return CallNextHookEx(mouse, code, w, l);
    }

    static void OnForeground(IntPtr hook, uint ev, IntPtr hwnd, int obj, int child, uint thread, uint time)
    {
        if (hwnd == IntPtr.Zero) return;
        GetWindowThreadProcessId(hwnd, out var pid);
        if (pid == Environment.ProcessId) return;
        var name = ProcessName(hwnd);
        if (name is "" or "Search" or "Start" or "Windows Start Experience Host" or "Windows Shell Experience Host") return;   // the Start menu passes through
        if (Guard?.Invoke(hwnd) == true) return;   // the user's app stays theirs
        Mine = name; MineHwnd = hwnd;
    }
}
