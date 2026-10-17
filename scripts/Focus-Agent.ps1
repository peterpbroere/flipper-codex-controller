param([Parameter(Mandatory = $true)][ValidatePattern('^[a-zA-Z0-9_-]{1,80}$')][string]$Tab)
$ErrorActionPreference = 'Stop'
try {
    Add-Type -AssemblyName UIAutomationClient
    Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public static class AgentDeckFocus {
    [DllImport("user32.dll")] public static extern bool ShowWindowAsync(IntPtr hWnd, int nCmdShow);
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr hWnd);
}
"@
    $root = [System.Windows.Automation.AutomationElement]::RootElement
    $tabCondition = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ControlTypeProperty, [System.Windows.Automation.ControlType]::TabItem)
    $candidates = @()
    foreach ($process in (Get-Process WindowsTerminal -ErrorAction SilentlyContinue)) {
        $condition = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ProcessIdProperty, $process.Id)
        $windows = $root.FindAll([System.Windows.Automation.TreeScope]::Children, $condition)
        foreach ($window in $windows) {
            $tabs = $window.FindAll([System.Windows.Automation.TreeScope]::Descendants, $tabCondition)
            foreach ($item in $tabs) {
                if ($item.Current.Name.IndexOf($Tab, [StringComparison]::Ordinal) -ge 0) {
                    $candidates += [pscustomobject]@{ Item = $item; Handle = [IntPtr]$window.Current.NativeWindowHandle }
                }
            }
        }
    }
    if ($candidates.Count -ne 1) { throw "Expected one matching Terminal tab; found $($candidates.Count)." }
    $target = $candidates[0]
    $target.Item.GetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern).Select()
    if ([AgentDeckFocus]::IsIconic($target.Handle)) { [AgentDeckFocus]::ShowWindowAsync($target.Handle, 9) | Out-Null }
    [AgentDeckFocus]::SetForegroundWindow($target.Handle) | Out-Null
    $deadline = (Get-Date).AddSeconds(2)
    while ([AgentDeckFocus]::GetForegroundWindow() -ne $target.Handle -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 50 }
    if ([AgentDeckFocus]::GetForegroundWindow() -ne $target.Handle) { throw 'Windows did not allow Terminal to come to the foreground.' }
    exit 0
} catch {
    Write-Error $_
    exit 1
}
