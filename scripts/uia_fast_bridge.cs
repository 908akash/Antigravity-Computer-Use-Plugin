using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Web.Script.Serialization;
using System.Windows;
using System.Windows.Automation;

internal static class UiaFastBridge
{
    [DllImport("user32.dll")]
    private static extern bool SetProcessDpiAwarenessContext(IntPtr value);

    [DllImport("shcore.dll")]
    private static extern int SetProcessDpiAwareness(int value);

    [DllImport("user32.dll")]
    private static extern bool SetProcessDPIAware();

    private static void EnablePerMonitorDpiV2()
    {
        try
        {
            if (SetProcessDpiAwarenessContext(new IntPtr(-4))) return;
        }
        catch { }
        try
        {
            if (SetProcessDpiAwareness(2) == 0) return;
        }
        catch { }
        try
        {
            SetProcessDPIAware();
        }
        catch { }
    }

    private static void StartParentWatcher(int parentPid)
    {
        if (parentPid <= 0) return;
        var t = new Thread(() =>
        {
            try
            {
                var p = Process.GetProcessById(parentPid);
                p.WaitForExit();
            }
            catch { }
            Environment.Exit(0);
        }) { IsBackground = true };
        t.Start();
    }

    private static readonly HashSet<string> KeepTypes = new HashSet<string>(StringComparer.OrdinalIgnoreCase)
    {
        "Button", "Edit", "Document", "ComboBox", "CheckBox", "RadioButton",
        "MenuItem", "TabItem", "ListItem", "Hyperlink", "TreeItem", "Slider",
        "SplitButton", "DataItem"
    };

    private struct QueueNode
    {
        public AutomationElement Element;
        public int Depth;
    }

    [MTAThread]
    public static int Main(string[] args)
    {
        // Must execute BEFORE any UIAutomation/WPF type loads so BoundingRectangle returns physical pixels on 125%-200% DPI
        EnablePerMonitorDpiV2();

        if (args != null)
        {
            for (int i = 0; i + 1 < args.Length; i++)
            {
                if (string.Equals(args[i], "--parent-pid", StringComparison.OrdinalIgnoreCase))
                {
                    int ppid;
                    if (int.TryParse(args[i + 1], out ppid) && ppid > 0)
                    {
                        StartParentWatcher(ppid);
                    }
                }
            }
        }

        Console.InputEncoding = new UTF8Encoding(false);
        Console.OutputEncoding = new UTF8Encoding(false);
        var reader = new StreamReader(Console.OpenStandardInput(), new UTF8Encoding(false));
        var writer = new StreamWriter(Console.OpenStandardOutput(), new UTF8Encoding(false)) { AutoFlush = true };
        var json = new JavaScriptSerializer { MaxJsonLength = 16 * 1024 * 1024 };

        var cacheReq = new CacheRequest
        {
            AutomationElementMode = AutomationElementMode.Full,
            TreeFilter = Automation.ControlViewCondition,
            TreeScope = TreeScope.Element
        };
        cacheReq.Add(AutomationElement.ControlTypeProperty);
        cacheReq.Add(AutomationElement.NameProperty);
        cacheReq.Add(AutomationElement.AutomationIdProperty);
        cacheReq.Add(AutomationElement.BoundingRectangleProperty);
        cacheReq.Add(AutomationElement.NativeWindowHandleProperty);
        cacheReq.Add(AutomationElement.IsEnabledProperty);
        cacheReq.Add(AutomationElement.IsOffscreenProperty);
        cacheReq.Add(AutomationElement.HasKeyboardFocusProperty);
        cacheReq.Add(AutomationElement.IsPasswordProperty);

        string line;
        while ((line = reader.ReadLine()) != null)
        {
            line = line.Trim();
            if (line.Length == 0) continue;
            try
            {
                var req = json.Deserialize<Dictionary<string, object>>(line);
                if (req == null) continue;
                if (req.ContainsKey("ping"))
                {
                    writer.WriteLine("{\"pong\":true}");
                    continue;
                }

                long hwnd = Convert.ToInt64(req["hwnd"]);
                int maxElements = req.ContainsKey("maxElements") ? Convert.ToInt32(req["maxElements"]) : 80;
                int setValueIndex = req.ContainsKey("setValueIndex") ? Convert.ToInt32(req["setValueIndex"]) : -1;
                string valueText = req.ContainsKey("valueText") && req["valueText"] != null ? Convert.ToString(req["valueText"]) : "";
                int invokeIndex = req.ContainsKey("invokeIndex") ? Convert.ToInt32(req["invokeIndex"]) : -1;

                var resp = HandleRequest(hwnd, maxElements, setValueIndex, valueText, invokeIndex, cacheReq);
                writer.WriteLine(json.Serialize(resp));
            }
            catch (Exception ex)
            {
                var err = new Dictionary<string, object>
                {
                    { "error", ex.GetType().Name + ": " + ex.Message }
                };
                writer.WriteLine(json.Serialize(err));
            }
        }
        return 0;
    }

    private static Dictionary<string, object> HandleRequest(
        long hwnd,
        int maxElements,
        int setValueIndex,
        string valueText,
        int invokeIndex,
        CacheRequest cacheReq)
    {
        var sw = Stopwatch.StartNew();
        AutomationElement root;
        using (cacheReq.Activate())
        {
            root = AutomationElement.FromHandle(new IntPtr(hwnd));
        }
        if (root == null)
        {
            throw new InvalidOperationException("Cannot bind AutomationElement for HWND " + hwnd);
        }

        string focusedStr = null;
        var walker = TreeWalker.ControlViewWalker;
        var items = new List<Dictionary<string, object>>(Math.Min(maxElements, 128));
        var queue = new Queue<QueueNode>();
        queue.Enqueue(new QueueNode { Element = root, Depth = 0 });
        int visited = 0;
        const int MaxVisitedNodes = 1500;
        const int MaxDepth = 16;
        const long BudgetMs = 1100;

        while (queue.Count > 0 && items.Count < maxElements && visited < MaxVisitedNodes && sw.ElapsedMilliseconds < BudgetMs)
        {
            var cur = queue.Dequeue();
            var el = cur.Element;
            int depth = cur.Depth;
            visited++;

            if (depth > 0)
            {
                try
                {
                    var c = el.Cached;
                    if (c.ControlType != null)
                    {
                        string ct = c.ControlType.ProgrammaticName.Replace("ControlType.", "");
                        string name = c.Name ?? "";
                        if (focusedStr == null && c.HasKeyboardFocus)
                        {
                            focusedStr = ct + " '" + name + "'";
                        }

                        bool offscreen = c.IsOffscreen;
                        Rect r = c.BoundingRectangle;
                        bool rectOk = !r.IsEmpty
                            && !double.IsNaN(r.Left) && !double.IsInfinity(r.Left)
                            && !double.IsNaN(r.Top) && !double.IsInfinity(r.Top)
                            && !double.IsNaN(r.Width) && !double.IsInfinity(r.Width)
                            && !double.IsNaN(r.Height) && !double.IsInfinity(r.Height);
                        if (!offscreen && rectOk && r.Width > 2 && r.Height > 2)
                        {
                            string aid = c.AutomationId ?? "";
                            if (KeepTypes.Contains(ct) || (name.Length > 0 && depth <= 4))
                            {
                                bool isPassword = false;
                                try { isPassword = c.IsPassword; } catch { isPassword = true; }
                                string val = null;
                                object vpObj;
                                ValuePattern vp = null;
                                bool isEditable = ct == "Edit" || ct == "ComboBox" || ct == "Document" || ct == "Slider";
                                bool checkVal = (setValueIndex == items.Count) || (c.IsEnabled && isEditable);
                                if (checkVal && el.TryGetCurrentPattern(ValuePattern.Pattern, out vpObj))
                                {
                                    vp = vpObj as ValuePattern;
                                    try
                                    {
                                        if (vp != null && !isPassword)
                                        {
                                            val = vp.Current.Value;
                                            if (val != null && val.Length > 120) val = val.Substring(0, 120) + "...";
                                        }
                                    }
                                    catch { }
                                }

                                int idx = items.Count;
                                int cx = (int)(r.Left + r.Width / 2.0);
                                int cy = (int)(r.Top + r.Height / 2.0);

                                if (setValueIndex == idx)
                                {
                                    if (vp != null)
                                    {
                                        try
                                        {
                                            vp.SetValue(valueText);
                                            return new Dictionary<string, object>
                                            {
                                                { "action", "set_value" },
                                                { "method", "ValuePattern" },
                                                { "index", idx },
                                                { "name", name }
                                            };
                                        }
                                        catch { }
                                    }
                                    return new Dictionary<string, object>
                                    {
                                        { "action", "set_value" },
                                        { "method", "fallback_needed" },
                                        { "index", idx },
                                        { "cx", cx },
                                        { "cy", cy }
                                    };
                                }

                                if (invokeIndex == idx)
                                {
                                    object ipObj;
                                    if (el.TryGetCurrentPattern(InvokePattern.Pattern, out ipObj))
                                    {
                                        var ip = ipObj as InvokePattern;
                                        if (ip != null)
                                        {
                                            try
                                            {
                                                ip.Invoke();
                                                return new Dictionary<string, object>
                                                {
                                                    { "action", "invoke" },
                                                    { "method", "InvokePattern" },
                                                    { "index", idx },
                                                    { "name", name }
                                                };
                                            }
                                            catch { }
                                        }
                                    }
                                    object spObj;
                                    if (el.TryGetCurrentPattern(SelectionItemPattern.Pattern, out spObj))
                                    {
                                        var sp = spObj as SelectionItemPattern;
                                        if (sp != null)
                                        {
                                            try
                                            {
                                                sp.Select();
                                                return new Dictionary<string, object>
                                                {
                                                    { "action", "invoke" },
                                                    { "method", "SelectionItemPattern" },
                                                    { "index", idx },
                                                    { "name", name }
                                                };
                                            }
                                            catch { }
                                        }
                                    }
                                    object tpObj;
                                    if (el.TryGetCurrentPattern(TogglePattern.Pattern, out tpObj))
                                    {
                                        var tp = tpObj as TogglePattern;
                                        if (tp != null)
                                        {
                                            try
                                            {
                                                tp.Toggle();
                                                return new Dictionary<string, object>
                                                {
                                                    { "action", "invoke" },
                                                    { "method", "TogglePattern" },
                                                    { "index", idx },
                                                    { "name", name }
                                                };
                                            }
                                            catch { }
                                        }
                                    }
                                    return new Dictionary<string, object>
                                    {
                                        { "action", "invoke" },
                                        { "method", "fallback_click" },
                                        { "index", idx },
                                        { "cx", cx },
                                        { "cy", cy }
                                    };
                                }

                                items.Add(new Dictionary<string, object>
                                {
                                    { "index", idx },
                                    { "depth", depth },
                                    { "controlType", ct },
                                    { "name", name },
                                    { "automationId", aid },
                                    { "nativeHwnd", (long)c.NativeWindowHandle },
                                    { "enabled", c.IsEnabled },
                                    { "value", val },
                                    { "bounds", new Dictionary<string, object>
                                        {
                                            { "left", (int)r.Left },
                                            { "top", (int)r.Top },
                                            { "width", (int)r.Width },
                                            { "height", (int)r.Height },
                                            { "cx", cx },
                                            { "cy", cy }
                                        }
                                    }
                                });
                            }
                        }
                    }
                }
                catch { }
            }

            if (depth < MaxDepth && sw.ElapsedMilliseconds < BudgetMs && visited < MaxVisitedNodes)
            {
                try
                {
                    var child = walker.GetFirstChild(el, cacheReq);
                    while (child != null && sw.ElapsedMilliseconds < BudgetMs)
                    {
                        queue.Enqueue(new QueueNode { Element = child, Depth = depth + 1 });
                        child = walker.GetNextSibling(child, cacheReq);
                    }
                }
                catch { }
            }
        }

        return new Dictionary<string, object>
        {
            { "focusedElement", focusedStr },
            { "count", items.Count },
            { "timedOut", sw.ElapsedMilliseconds >= BudgetMs || visited >= MaxVisitedNodes },
            { "elements", items }
        };
    }
}
