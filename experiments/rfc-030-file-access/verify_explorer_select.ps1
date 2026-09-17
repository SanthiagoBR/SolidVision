<#
Which `explorer.exe /select,` command line actually selects a file?

RFC-030 section 5.1 specified `subprocess.run(["explorer", f"/select,{path}"])`,
one list item for the switch and the path together. `subprocess` quotes a list
item that contains a space, so for a path with a space Explorer receives
`"/select,C:\...\fazenda São João.jpg"` -- and whether Explorer's own parser
accepts that is a question about Explorer, which no unit test can answer.

This script answers it on the machine it runs on. For each form it:

  1. records the Explorer windows already open (so it never touches the
     user's own windows);
  2. starts `explorer.exe` with exactly the command line `subprocess` would
     build, or -- for the last two cases -- runs the real
     `WindowsFileRevealer` adapter through the backend virtualenv;
  3. finds the one new window through the `Shell.Application` COM object,
     polls for up to 20 s until its view reports a selection, and reads back
     the folder it opened and the item it selected -- reported separately,
     because the folder is known as soon as the window exists while the
     selection is applied later and can be missed by a read that comes too
     early;
  4. closes that window.

**It opens and closes Explorer windows on the desktop**, which is why it is a
script run on purpose rather than a test in the suite.

Run from the repository root:

    powershell -ExecutionPolicy Bypass -File experiments/rfc-030-file-access/verify_explorer_select.ps1
#>
param(
    [string]$Root = (Join-Path $env:TEMP "rfc030-reveal-check")
)

$ErrorActionPreference = "Stop"
$repository = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$python = Join-Path $repository "backend\.venv\Scripts\python.exe"
$explorer = Join-Path $env:SystemRoot "explorer.exe"
$shell = New-Object -ComObject Shell.Application

$plainDir = Join-Path $Root "plain"
$spaceDir = Join-Path $Root "com espaço e ação"
New-Item -ItemType Directory -Force $plainDir, $spaceDir | Out-Null
$plain = Join-Path $plainDir "plain.jpg"
$spaced = Join-Path $spaceDir "fazenda São João.jpg"
Set-Content -Path $plain -Value "x"
Set-Content -Path $spaced -Value "x"

function Get-WindowHandles {
    @($shell.Windows() | ForEach-Object { $_.HWND })
}

function Invoke-Adapter([string]$Path) {
    $code = @"
import sys
sys.path.insert(0, r'$repository\backend')
from app.domain.value_objects.image_path import ImagePath
from app.infrastructure.filesystem.file_revealer import WindowsFileRevealer
WindowsFileRevealer().reveal(ImagePath(sys.argv[1]))
"@
    & $python -c $code $Path
}

function Test-Form([string]$Label, [scriptblock]$Launch, [string]$Folder, [string]$Expected) {
    $before = Get-WindowHandles
    & $Launch

    $found = $null
    $deadline = (Get-Date).AddSeconds(10)
    while ((Get-Date) -lt $deadline -and $null -eq $found) {
        Start-Sleep -Milliseconds 250
        foreach ($window in @($shell.Windows())) {
            if ($before -contains $window.HWND) { continue }
            $found = $window
        }
    }
    if ($null -eq $found) {
        "{0,-34} no new Explorer window appeared" -f $Label
        return
    }

    $path = ""
    $selected = @()
    $deadline = (Get-Date).AddSeconds(20)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 300
        try {
            $path = $found.Document.Folder.Self.Path
            $selected = @($found.Document.SelectedItems() | ForEach-Object { $_.Path })
        } catch {
            $path = "<unreadable: $($_.Exception.Message)>"
        }
        if ($selected.Count -gt 0) { break }
    }
    $folderVerdict = if ($path -eq $Folder) { "right folder" } else { "WRONG FOLDER" }
    $selectionVerdict = if ($selected -contains $Expected) { "file selected" } else { "no selection read back in 20 s" }
    $opened = $path.Replace($Root, "<root>")
    $picked = ($selected | ForEach-Object { $_.Replace($Root, "<root>") }) -join ", "
    "{0,-34} {1}, {2}`n{3,-34} opened:   {4}`n{3,-34} selected: {5}" -f $Label, $folderVerdict, $selectionVerdict, "", $opened, $(if ($picked) { $picked } else { "(nothing)" })
    $found.Quit()
    Start-Sleep -Milliseconds 500
}

function Start-Explorer([string]$Arguments) {
    $info = New-Object System.Diagnostics.ProcessStartInfo
    $info.FileName = $explorer
    $info.Arguments = $Arguments
    $info.UseShellExecute = $false
    [System.Diagnostics.Process]::Start($info) | Out-Null
}

"Windows: $([System.Environment]::OSVersion.VersionString)"
"root:    $Root"
""
"command line as subprocess.list2cmdline builds it:"
Test-Form "one item, path without space" { Start-Explorer ("/select," + $plain) } $plainDir $plain
Test-Form "one item, path with space" { Start-Explorer ('"/select,' + $spaced + '"') } $spaceDir $spaced
Test-Form "two items, path without space" { Start-Explorer ("/select, " + $plain) } $plainDir $plain
Test-Form "two items, path with space" { Start-Explorer ('/select, "' + $spaced + '"') } $spaceDir $spaced
""
"the real WindowsFileRevealer, through subprocess.Popen:"
Test-Form "adapter, path without space" { Invoke-Adapter $plain } $plainDir $plain
Test-Form "adapter, path with space" { Invoke-Adapter $spaced } $spaceDir $spaced

Remove-Item -Recurse -Force $Root
