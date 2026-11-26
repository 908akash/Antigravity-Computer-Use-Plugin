$ErrorActionPreference = 'Stop'

# Enable Per-Monitor DPI v2 BEFORE loading UIAutomationClient so BoundingRectangle matches physical screen pixels
try {
    Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public static class CuaDpiInit {
    [DllImport("user32.dll")] public static extern bool SetProcessDpiAwarenessContext(IntPtr value);
    [DllImport("shcore.dll")] public static extern int SetProcessDpiAwareness(int value);
    [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
    public static void Enable() {
        try { if (SetProcessDpiAwarenessContext(new IntPtr(-4))) return; } catch {}
        try { if (SetProcessDpiAwareness(2) == 0) return; } catch {}
        try { SetProcessDPIAware(); } catch {}
    }
}
"@
    [CuaDpiInit]::Enable()
} catch {}

$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$reader = New-Object System.IO.StreamReader([Console]::OpenStandardInput(), $utf8NoBom)
$writer = New-Object System.IO.StreamWriter([Console]::OpenStandardOutput(), $utf8NoBom)
$writer.AutoFlush = $true

Add-Type -AssemblyName UIAutomationClient,UIAutomationTypes,WindowsBase

$keepSet = New-Object 'System.Collections.Generic.HashSet[string]' ([System.StringComparer]::OrdinalIgnoreCase)
foreach ($t in @('Button','Edit','Document','ComboBox','CheckBox','RadioButton','MenuItem','TabItem','ListItem','Hyperlink','TreeItem','Slider','SplitButton','DataItem')) {
    [void]$keepSet.Add($t)
}

$cacheReq = New-Object System.Windows.Automation.CacheRequest
$cacheReq.AutomationElementMode = [System.Windows.Automation.AutomationElementMode]::Full
$cacheReq.TreeFilter = [System.Windows.Automation.Automation]::ControlViewCondition
$cacheReq.TreeScope = [System.Windows.Automation.TreeScope]::Element
foreach ($prop in @(
    [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
    [System.Windows.Automation.AutomationElement]::NameProperty,
    [System.Windows.Automation.AutomationElement]::AutomationIdProperty,
    [System.Windows.Automation.AutomationElement]::BoundingRectangleProperty,
    [System.Windows.Automation.AutomationElement]::NativeWindowHandleProperty,
    [System.Windows.Automation.AutomationElement]::IsEnabledProperty,
    [System.Windows.Automation.AutomationElement]::IsOffscreenProperty,
    [System.Windows.Automation.AutomationElement]::HasKeyboardFocusProperty,
    [System.Windows.Automation.AutomationElement]::IsPasswordProperty
)) { $cacheReq.Add($prop) }
$walker = [System.Windows.Automation.TreeWalker]::ControlViewWalker

$writer.WriteLine('{"pong":true}')

while ($null -ne ($line = $reader.ReadLine())) {
    $line = $line.Trim()
    if ($line.Length -eq 0) { continue }
    try {
        $req = $line | ConvertFrom-Json
        if ($null -ne $req.PSObject.Properties['ping']) {
            $writer.WriteLine('{"pong":true}')
            continue
        }
        $hwnd = [int64]$req.hwnd
        $maxElements = if ($null -ne $req.PSObject.Properties['maxElements']) { [int]$req.maxElements } else { 80 }
        $setValueIndex = if ($null -ne $req.PSObject.Properties['setValueIndex']) { [int]$req.setValueIndex } else { -1 }
        $valueText = if ($null -ne $req.PSObject.Properties['valueText'] -and $null -ne $req.valueText) { [string]$req.valueText } else { '' }
        $invokeIndex = if ($null -ne $req.PSObject.Properties['invokeIndex']) { [int]$req.invokeIndex } else { -1 }

        $sw = [System.Diagnostics.Stopwatch]::StartNew()
        $root = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]$hwnd)
        if ($null -eq $root) { throw "Cannot bind AutomationElement for HWND $hwnd" }

        $focusedStr = $null
        $queue = New-Object System.Collections.Generic.Queue[object]
        $queue.Enqueue(@{ el = $root; depth = 0 })
        $items = New-Object System.Collections.Generic.List[object]
        $earlyResp = $null
        $visited = 0
        $maxVisited = 1500
        $maxDepth = 16
        $budgetMs = 1100

        while ($queue.Count -gt 0 -and $items.Count -lt $maxElements -and $null -eq $earlyResp -and $visited -lt $maxVisited -and $sw.ElapsedMilliseconds -lt $budgetMs) {
            $cur = $queue.Dequeue()
            $el = $cur.el
            $depth = [int]$cur.depth
            $visited++
            if ($depth -gt 0) {
                try {
                    $c = $el.Cached
                    if ($null -ne $c.ControlType) {
                        $ct = $c.ControlType.ProgrammaticName.Replace('ControlType.','')
                        $name = [string]$c.Name
                        if ($null -eq $focusedStr -and $c.HasKeyboardFocus) {
                            $focusedStr = "$ct '$name'"
                        }
                        $r = $c.BoundingRectangle
                        $rectOk = (-not $r.IsEmpty) -and (-not [double]::IsNaN($r.Left)) -and (-not [double]::IsInfinity($r.Left)) -and (-not [double]::IsNaN($r.Top)) -and (-not [double]::IsInfinity($r.Top)) -and (-not [double]::IsNaN($r.Width)) -and (-not [double]::IsInfinity($r.Width)) -and (-not [double]::IsNaN($r.Height)) -and (-not [double]::IsInfinity($r.Height))
                        if (-not $c.IsOffscreen -and $rectOk -and $r.Width -gt 2 -and $r.Height -gt 2) {
                            $aid = [string]$c.AutomationId
                            if ($keepSet.Contains($ct) -or ($name.Length -gt 0 -and $depth -le 4)) {
                                $isPassword = $false
                                try { $isPassword = [bool]$c.IsPassword } catch { $isPassword = $true }
                                $val = $null
                                $vpObj = $null
                                $checkVal = ($setValueIndex -eq $items.Count) -or ($c.IsEnabled -and ($ct -in @('Edit','ComboBox','Document','Slider')))
                                if ($checkVal -and $el.TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern, [ref]$vpObj)) {
                                    if (-not $isPassword) {
                                        try { $val = ([System.Windows.Automation.ValuePattern]$vpObj).Current.Value } catch {}
                                        if ($null -ne $val -and $val.Length -gt 120) { $val = $val.Substring(0, 120) + '...' }
                                    }
                                }
                                $idx = $items.Count
                                $cx = [int]($r.Left + $r.Width / 2)
                                $cy = [int]($r.Top + $r.Height / 2)
                                if ($setValueIndex -eq $idx) {
                                    $setOk = $false
                                    if ($null -ne $vpObj) {
                                        try {
                                            ([System.Windows.Automation.ValuePattern]$vpObj).SetValue($valueText)
                                            $earlyResp = @{ action = 'set_value'; method = 'ValuePattern'; index = $idx; name = $name }
                                            $setOk = $true
                                        } catch {}
                                    }
                                    if (-not $setOk) {
                                        $earlyResp = @{ action = 'set_value'; method = 'fallback_needed'; index = $idx; cx = $cx; cy = $cy }
                                    }
                                    break
                                }
                                if ($invokeIndex -eq $idx) {
                                    $ipObj = $null
                                    $spObj = $null
                                    $tpObj = $null
                                    $invOk = $false
                                    if ($el.TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern, [ref]$ipObj)) {
                                        try {
                                            ([System.Windows.Automation.InvokePattern]$ipObj).Invoke()
                                            $earlyResp = @{ action = 'invoke'; method = 'InvokePattern'; index = $idx; name = $name }
                                            $invOk = $true
                                        } catch {}
                                    }
                                    if (-not $invOk -and $el.TryGetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern, [ref]$spObj)) {
                                        try {
                                            ([System.Windows.Automation.SelectionItemPattern]$spObj).Select()
                                            $earlyResp = @{ action = 'invoke'; method = 'SelectionItemPattern'; index = $idx; name = $name }
                                            $invOk = $true
                                        } catch {}
                                    }
                                    if (-not $invOk -and $el.TryGetCurrentPattern([System.Windows.Automation.TogglePattern]::Pattern, [ref]$tpObj)) {
                                        try {
                                            ([System.Windows.Automation.TogglePattern]$tpObj).Toggle()
                                            $earlyResp = @{ action = 'invoke'; method = 'TogglePattern'; index = $idx; name = $name }
                                            $invOk = $true
                                        } catch {}
                                    }
                                    if (-not $invOk) {
                                        $earlyResp = @{ action = 'invoke'; method = 'fallback_click'; index = $idx; cx = $cx; cy = $cy }
                                    }
                                    break
                                }
                                $items.Add(@{
                                    index = $idx; depth = $depth; controlType = $ct; name = $name; automationId = $aid;
                                    nativeHwnd = [int64]$c.NativeWindowHandle; enabled = [bool]$c.IsEnabled; value = $val;
                                    bounds = @{ left = [int]$r.Left; top = [int]$r.Top; width = [int]$r.Width; height = [int]$r.Height; cx = $cx; cy = $cy }
                                })
                            }
                        }
                    }
                } catch {}
            }
            if ($depth -lt $maxDepth -and $null -eq $earlyResp -and $visited -lt $maxVisited -and $sw.ElapsedMilliseconds -lt $budgetMs) {
                try {
                    $ch = $walker.GetFirstChild($el, $cacheReq)
                    while ($null -ne $ch -and $sw.ElapsedMilliseconds -lt $budgetMs) {
                        $queue.Enqueue(@{ el = $ch; depth = ($depth + 1) })
                        $ch = $walker.GetNextSibling($ch, $cacheReq)
                    }
                } catch {}
            }
        }
        $outObj = if ($null -ne $earlyResp) {
            $earlyResp
        } else {
            @{
                focusedElement = $focusedStr
                count = $items.Count
                timedOut = ($sw.ElapsedMilliseconds -ge $budgetMs -or $visited -ge $maxVisited)
                elements = $items
            }
        }
        $writer.WriteLine(($outObj | ConvertTo-Json -Depth 5 -Compress))
    } catch {
        $writer.WriteLine((@{ error = $_.Exception.Message } | ConvertTo-Json -Compress))
    }
}
