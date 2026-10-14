// Selected text and the text cursor in the user's app, read with UI Automation the moment the shortcut goes down, and
// Replace / Delete / Insert put back there. A paste (the user's clipboard put back after) keeps undo and formatting; plain fields
// that ignore it get their value set directly. Success means the text is in the field, never just that keys were sent.
// UI Automation through its own COM interfaces (only the calls used here), so the app needs no WPF.
using System.Runtime.InteropServices;
using static Atticus.Native;

namespace Atticus;

sealed class TextSelection
{
    public const int Most = 6000;   // more than this never goes to the AI
    public IntPtr Hwnd;
    public string Text = "";
    public bool Editable, Pastable;   // Editable: its value can be set; Pastable: a text box that takes keys
    IUIAutomationElement el;
    IUIAutomationTextRange range;
    IntPtr edit; int start, end;   // a classic edit box with no UI Automation text: its window and selection
    public bool Caret => string.IsNullOrWhiteSpace(Text);   // nothing highlighted: the cursor sits in a text box

    const int TextPat = 10014, ValuePat = 10002, ProcessIdProp = 30002, ControlTypeProp = 30003, IsPasswordProp = 30019, ReadOnlyAttr = 40015;
    const int Edit = 50004, Document = 50030, ComboBox = 50003;

    static IUIAutomation Ua() => (IUIAutomation)Activator.CreateInstance(Type.GetTypeFromCLSID(new Guid("E22AD333-B25F-460C-83D0-0581107395C9")));
    static object Prop(IUIAutomationElement e, int id) { try { return e.GetCurrentPropertyValue(id); } catch { return null; } }
    static T Pattern<T>(IUIAutomationElement e, int id) where T : class { try { return e.GetCurrentPattern(id) as T; } catch { return null; } }

    // Off the UI thread, with a time limit: a hung app must never hold up the shortcut.
    public static Task<TextSelection> CaptureAsync(IntPtr hwnd, bool allowEmpty = false, int ms = 400)
    {
        var t = Task.Run(() => { try { return Capture(hwnd, allowEmpty); } catch { return null; } });
        return Task.WhenAny(t, Task.Delay(ms)).ContinueWith(w => w.Result == t ? t.Result : null);
    }

    static TextSelection Capture(IntPtr hwnd, bool allowEmpty)
    {
        if (hwnd == IntPtr.Zero) return null;
        GetWindowThreadProcessId(hwnd, out var pid);
        if (pid == Environment.ProcessId) return null;   // Atticus's own box
        var e = Ua().GetFocusedElement();
        var epid = Convert.ToUInt32(Prop(e, ProcessIdProp) ?? 0);
        if (epid == Environment.ProcessId || Prop(e, IsPasswordProp) is true) return null;   // never a password field
        if (epid != pid && Native.ProcessName(hwnd) != "Application Frame Host") return null;   // focus is not in the front window
        var tp = Pattern<IUIAutomationTextPattern>(e, TextPat);
        if (tp == null) return Classic(hwnd, e, allowEmpty);
        IUIAutomationTextRange r = null;
        var sel = tp.GetSelection();
        if (sel != null && sel.Length > 0) r = sel.GetElement(0);
        else if (tp is IUIAutomationTextPattern2 tp2) { try { r = tp2.GetCaretRange(out _); } catch { } }
        if (r == null) return null;
        var text = r.GetText(Most + 1) ?? "";
        if (!allowEmpty && text.Length > Most) return null;
        var vp = Pattern<IUIAutomationValuePattern>(e, ValuePat);
        bool ro = vp != null && vp.CurrentIsReadOnly != 0;
        try { ro |= r.GetAttributeValue(ReadOnlyAttr) is true; } catch { }   // a web page or a viewer: selectable, not editable
        var ct = Convert.ToInt32(Prop(e, ControlTypeProp) ?? 0);
        var s = new TextSelection { Hwnd = hwnd, Text = text, el = e, range = r, Editable = vp != null && !ro, Pastable = !ro && ct is Edit or Document or ComboBox };
        return allowEmpty || !s.Caret || s.Editable || s.Pastable ? s : null;
    }

    // Classic edit boxes (Win32, older WinForms): the selection comes from the control itself.
    static TextSelection Classic(IntPtr hwnd, IUIAutomationElement e, bool allowEmpty)
    {
        var f = FocusOf(hwnd);
        if (f == IntPtr.Zero || !ClassOf(f).Contains("edit", StringComparison.OrdinalIgnoreCase) || (GetWindowLong(f, -16) & 0x20) != 0) return null;   // ES_PASSWORD
        var all = WinText(f); var (a, b) = WinSel(f);
        if (all == null || a < 0 || b < a || b > all.Length || !allowEmpty && b - a > Most) return null;
        var ok = (GetWindowLong(f, -16) & 0x800) == 0;   // not ES_READONLY
        var s = new TextSelection { Hwnd = hwnd, Text = all[a..b], el = e, edit = f, start = a, end = b, Editable = ok, Pastable = ok };
        return allowEmpty || !s.Caret || ok ? s : null;
    }
    [StructLayout(LayoutKind.Sequential)] struct GUITHREADINFO { public int cbSize, flags; public IntPtr active, focus, capture, menuOwner, moveSize, caret; public RECT rcCaret; }
    [DllImport("user32")] static extern bool GetGUIThreadInfo(uint thread, ref GUITHREADINFO g);
    [DllImport("user32", CharSet = CharSet.Unicode)] static extern int GetClassName(IntPtr h, System.Text.StringBuilder s, int n);
    [DllImport("user32", EntryPoint = "SendMessageTimeoutW")] static extern IntPtr Send(IntPtr h, int msg, IntPtr w, IntPtr l, uint flags, uint ms, out IntPtr result);
    [DllImport("user32", EntryPoint = "SendMessageTimeoutW", CharSet = CharSet.Unicode)] static extern IntPtr Send(IntPtr h, int msg, IntPtr w, System.Text.StringBuilder l, uint flags, uint ms, out IntPtr result);
    [DllImport("user32", EntryPoint = "SendMessageTimeoutW", CharSet = CharSet.Unicode)] static extern IntPtr Send(IntPtr h, int msg, IntPtr w, string l, uint flags, uint ms, out IntPtr result);
    static IntPtr FocusOf(IntPtr hwnd) { var g = new GUITHREADINFO { cbSize = Marshal.SizeOf<GUITHREADINFO>() }; return GetGUIThreadInfo(GetWindowThreadProcessId(hwnd, out _), ref g) ? g.focus : IntPtr.Zero; }
    static string ClassOf(IntPtr h) { var sb = new System.Text.StringBuilder(256); GetClassName(h, sb, 256); return sb.ToString(); }
    static string WinText(IntPtr h)   // up to 64K characters: EM_GETSEL positions stop there
    {
        if (Send(h, 0x0E, IntPtr.Zero, IntPtr.Zero, 2, 300, out var n) == IntPtr.Zero || (long)n > 65535) return null;   // WM_GETTEXTLENGTH
        var sb = new System.Text.StringBuilder((int)n + 1);
        return Send(h, 0x0D, (IntPtr)((int)n + 1), sb, 2, 300, out _) == IntPtr.Zero ? null : sb.ToString();   // WM_GETTEXT
    }
    static (int, int) WinSel(IntPtr h) => Send(h, 0xB0, IntPtr.Zero, IntPtr.Zero, 2, 300, out var r) == IntPtr.Zero ? (-1, -1) : ((int)((long)r & 0xFFFF), (int)(((long)r >> 16) & 0xFFFF));   // EM_GETSEL

    // The same element still has the focus and the same text highlighted (or the cursor in the same place).
    bool Unchanged()
    {
        if (edit != IntPtr.Zero) return FocusOf(Hwnd) == edit && WinSel(edit) == (start, end) && WinText(edit) is { } t && end <= t.Length && t[start..end] == Text;
        try
        {
            var ua = Ua();
            if (ua.CompareElements(ua.GetFocusedElement(), el) == 0) return false;
            var sel = Pattern<IUIAutomationTextPattern>(el, TextPat)?.GetSelection();
            var cur = sel != null && sel.Length > 0 ? sel.GetElement(0) : null;
            return cur != null && (cur.GetText(Most + 1) ?? "") == Text
                && cur.CompareEndpoints(0, range, 0) == 0 && cur.CompareEndpoints(1, range, 1) == 0;   // 0 start, 1 end
        }
        catch { return false; }
    }

    // The field's whole text, to check a change landed; null when it can't be read (then there is nothing to check against).
    string Value()
    {
        if (edit != IntPtr.Zero) return WinText(edit);
        try
        {
            var vp = Pattern<IUIAutomationValuePattern>(el, ValuePat);
            if (vp != null) return vp.CurrentValue;
            return Pattern<IUIAutomationTextPattern>(el, TextPat)?.DocumentRange.GetText(1_000_000);
        }
        catch { return null; }
    }

    static string Squash(string s) => new(s.Where(c => !char.IsWhiteSpace(c)).ToArray());
    public static bool Landed(string draft, string before, string now) =>
        before == null || now == null || now != before && (draft == "" || Squash(now).Contains(Squash(draft)));

    // value with the selected text swapped for draft; null unless the selection is exactly at start.
    public static string Splice(string value, int start, string text, string draft) =>
        start < 0 || start + text.Length > value.Length || string.CompareOrdinal(value, start, text, 0, text.Length) != 0 ? null
            : value[..start] + draft + value[(start + text.Length)..];

    // Plain fields only (value and text agree): set the whole value with the selection swapped. Loses undo, so a paste goes first.
    bool SetDirect(string draft)
    {
        if (edit != IntPtr.Zero)
            return Unchanged() && WinText(edit) is { } all && Splice(all, start, Text, draft) is { } v && Send(edit, 0x0C, IntPtr.Zero, v, 2, 500, out _) != IntPtr.Zero;   // WM_SETTEXT
        try
        {
            var vp = Pattern<IUIAutomationValuePattern>(el, ValuePat);
            var doc = Pattern<IUIAutomationTextPattern>(el, TextPat)?.DocumentRange;
            if (vp == null || vp.CurrentIsReadOnly != 0 || doc == null || !Unchanged()) return false;
            var value = vp.CurrentValue;
            if (doc.GetText(1_000_000) != value) return false;
            var before = doc.Clone(); before.MoveEndpointByRange(1, range, 0);   // document start up to the selection
            var now = Splice(value, (before.GetText(1_000_000) ?? "").Length, Text, draft);
            if (now == null) return false;
            vp.SetValue(now);
            return true;
        }
        catch { return false; }
    }

    // valid(): the request is still current (not cancelled, the setting still on). owner: a window of ours for the clipboard.
    public async Task<bool> Apply(string draft, Func<bool> valid, IntPtr owner)
    {
        if (!valid() || !IsWindow(Hwnd)) return false;
        var before = await Task.Run(Value);
        if (GetForegroundWindow() != Hwnd)   // the user clicked Replace in the box: give the app its keyboard back
        {
            Tap(0xE8); SetForegroundWindow(Hwnd);
            await Task.Delay(150);
        }
        if (!valid() || GetForegroundWindow() != Hwnd || !await Task.Run(Unchanged)) return false;
        if (!Pastable) return Editable && await Task.Run(() => SetDirect(draft) && Landed(draft, before, Value()));
        List<(uint, byte[])> saved = null; uint seq = 0;
        if (draft == "") Tap(0x08);   // delete: Backspace, no clipboard
        else
        {
            saved = Clip.Save(owner);
            if (!Clip.SetText(owner, draft)) { Clip.Restore(owner, saved); return false; }
            seq = Clip.Seq();
            keybd_event(0x11, 0, 0, IntPtr.Zero); keybd_event(0x56, 0, 0, IntPtr.Zero); keybd_event(0x56, 0, 2, IntPtr.Zero); keybd_event(0x11, 0, 2, IntPtr.Zero);
        }
        var ok = false;
        foreach (var wait in new[] { 300, 500 })   // slow apps (browsers, Office) take a moment to show it
        {
            await Task.Delay(wait);
            if (ok = Landed(draft, before, await Task.Run(Value))) break;
        }
        if (saved != null && Clip.Seq() == seq) Clip.Restore(owner, saved);   // the user copied something new meanwhile: keep that
        if (!ok && Editable && draft != "" && valid()) ok = await Task.Run(() => SetDirect(draft) && Landed(draft, before, Value()));   // the app ignored the paste
        return ok;
    }

    [DllImport("user32")] static extern bool IsWindow(IntPtr h);
}

// The user's clipboard, saved whole (text, rich text, HTML, pictures, files) and put back after a paste.
static class Clip
{
    [DllImport("user32")] static extern bool OpenClipboard(IntPtr h);
    [DllImport("user32")] static extern bool CloseClipboard();
    [DllImport("user32")] static extern bool EmptyClipboard();
    [DllImport("user32")] static extern uint EnumClipboardFormats(uint f);
    [DllImport("user32")] static extern IntPtr GetClipboardData(uint f);
    [DllImport("user32")] static extern IntPtr SetClipboardData(uint f, IntPtr h);
    [DllImport("user32")] static extern uint GetClipboardSequenceNumber();
    [DllImport("user32", CharSet = CharSet.Unicode)] static extern uint RegisterClipboardFormat(string name);
    [DllImport("kernel32")] static extern IntPtr GlobalAlloc(uint flags, UIntPtr size);
    [DllImport("kernel32")] static extern IntPtr GlobalLock(IntPtr h);
    [DllImport("kernel32")] static extern bool GlobalUnlock(IntPtr h);
    [DllImport("kernel32")] static extern UIntPtr GlobalSize(IntPtr h);
    [DllImport("kernel32")] static extern IntPtr GlobalFree(IntPtr h);

    public static uint Seq() => GetClipboardSequenceNumber();
    static bool Open(IntPtr owner) { for (var i = 0; i < 20; i++) { if (OpenClipboard(owner)) return true; Thread.Sleep(30); } return false; }   // another app may hold it for a moment
    // Memory-handle formats only; bitmaps and metafiles also come as DIB / memory formats, which are kept.
    static bool Kept(uint f) => f is not (2 or 3 or 9 or 14) and not (>= 0x80 and <= 0x8E) and not (>= 0x200 and <= 0x3FF);

    public static List<(uint, byte[])> Save(IntPtr owner)
    {
        var all = new List<(uint, byte[])>();
        if (!Open(owner)) return all;
        try
        {
            for (var f = EnumClipboardFormats(0); f != 0; f = EnumClipboardFormats(f))
            {
                if (!Kept(f)) continue;
                var h = GetClipboardData(f); var p = h == IntPtr.Zero ? IntPtr.Zero : GlobalLock(h);
                if (p == IntPtr.Zero) continue;
                try { var b = new byte[(int)GlobalSize(h)]; Marshal.Copy(p, b, 0, b.Length); all.Add((f, b)); }
                finally { GlobalUnlock(h); }
            }
        }
        finally { CloseClipboard(); }
        return all;
    }

    static void Put(uint f, byte[] b)
    {
        var h = GlobalAlloc(0x0002, (UIntPtr)Math.Max(1, b.Length));   // GMEM_MOVEABLE
        var p = GlobalLock(h); Marshal.Copy(b, 0, p, b.Length); GlobalUnlock(h);
        if (SetClipboardData(f, h) == IntPtr.Zero) GlobalFree(h);
    }

    public static void Restore(IntPtr owner, List<(uint, byte[])> saved)
    {
        if (!Open(owner)) return;
        try { EmptyClipboard(); foreach (var (f, b) in saved) Put(f, b); }
        finally { CloseClipboard(); }
    }

    // The draft, kept out of clipboard history (Win+V) and cloud sync: it only passes through.
    public static bool SetText(IntPtr owner, string text)
    {
        if (!Open(owner)) return false;
        try
        {
            EmptyClipboard();
            Put(RegisterClipboardFormat("ExcludeClipboardContentFromMonitorProcessing"), new byte[4]);
            Put(13, System.Text.Encoding.Unicode.GetBytes(text + "\0"));   // CF_UNICODETEXT
            return GetClipboardData(13) != IntPtr.Zero;
        }
        finally { CloseClipboard(); }
    }
}

// UI Automation's COM interfaces, cut to the methods used (in vtable order; the rest are placeholders).
[ComImport, Guid("30CBE57D-D9D0-452A-AB13-7AC5AC4825EE"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IUIAutomation
{
    int CompareElements(IUIAutomationElement a, IUIAutomationElement b);
    void _CompareRuntimeIds(); void _GetRootElement(); void _ElementFromHandle(); void _ElementFromPoint();
    IUIAutomationElement GetFocusedElement();
}

[ComImport, Guid("D22108AA-8AC5-49A5-837B-37BBB3D7591E"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IUIAutomationElement
{
    void _SetFocus(); void _GetRuntimeId(); void _FindFirst(); void _FindAll(); void _FindFirstBuildCache(); void _FindAllBuildCache(); void _BuildUpdatedCache();
    [return: MarshalAs(UnmanagedType.Struct)] object GetCurrentPropertyValue(int id);
    void _GetCurrentPropertyValueEx(); void _GetCachedPropertyValue(); void _GetCachedPropertyValueEx(); void _GetCurrentPatternAs(); void _GetCachedPatternAs();
    [return: MarshalAs(UnmanagedType.IUnknown)] object GetCurrentPattern(int id);
}

[ComImport, Guid("32EBA289-3583-42C9-9C59-3B6D9A1E9B6A"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IUIAutomationTextPattern
{
    void _RangeFromPoint(); void _RangeFromChild();
    IUIAutomationTextRangeArray GetSelection();
    void _GetVisibleRanges();
    IUIAutomationTextRange DocumentRange { get; }
}

[ComImport, Guid("506A921A-FCC9-409F-B23B-37EB74106872"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IUIAutomationTextPattern2
{
    void _RangeFromPoint(); void _RangeFromChild(); void _GetSelection(); void _GetVisibleRanges(); void _DocumentRange(); void _SupportedTextSelection();
    void _RangeFromAnnotation();
    IUIAutomationTextRange GetCaretRange(out int active);
}

[ComImport, Guid("CE4AE76A-E717-4C98-81EA-47371D028EB6"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IUIAutomationTextRangeArray
{
    int Length { get; }
    IUIAutomationTextRange GetElement(int i);
}

[ComImport, Guid("A543CC6A-F4AE-494B-8239-C814481187A8"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IUIAutomationTextRange
{
    IUIAutomationTextRange Clone();
    void _Compare();
    int CompareEndpoints(int end, IUIAutomationTextRange other, int otherEnd);
    void _ExpandToEnclosingUnit(); void _FindAttribute(); void _FindText();
    [return: MarshalAs(UnmanagedType.Struct)] object GetAttributeValue(int attr);
    void _GetBoundingRectangles(); void _GetEnclosingElement();
    [return: MarshalAs(UnmanagedType.BStr)] string GetText(int most);
    void _Move(); void _MoveEndpointByUnit();
    void MoveEndpointByRange(int end, IUIAutomationTextRange other, int otherEnd);
}

[ComImport, Guid("A94CD8B1-0844-4CD6-9D2D-640537AB39E9"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IUIAutomationValuePattern
{
    void SetValue([MarshalAs(UnmanagedType.BStr)] string v);
    string CurrentValue { [return: MarshalAs(UnmanagedType.BStr)] get; }
    int CurrentIsReadOnly { get; }
}
