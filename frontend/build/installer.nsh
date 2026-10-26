; The stop, before a file is touched (RFC-041 section 7.4).
;
; electron-builder checks for a running app before it installs over one and
; before it uninstalls, and this macro takes the place of its check. Closing the
; window does not stop the rest at once: the launcher, its two children and the
; cluster run python.exe and postgres.exe from inside resources\runtime, and a
; file in use half way through a removal leaves half an installation.
;
; 1. When the installed Python exists, `python.exe -m app.launcher stop`, with
;    no window and the 60 s of the window's own close (RFC-037 section 6.4). It
;    asks the launcher of this user's data root to stop; the launcher stops the
;    children and the cluster, with a running job handed back to the queue, and
;    the window, which owns it, leaves on `stopped`.
; 2. Then wait for no process to be running from inside $INSTDIR, for 60 s,
;    and end what is left: a cluster left behind is adopted by the next start
;    (RFC-036 section 7.4).
;
; The installation folder reaches PowerShell through the environment, and not
; inside the command: a quote in a user name cannot break the command.

!macro customCheckAppRunning
  ${If} ${FileExists} "$INSTDIR\resources\runtime\python\python.exe"
    DetailPrint "Parando o SolidVision..."
    nsExec::Exec /TIMEOUT=60000 `"$INSTDIR\resources\runtime\python\python.exe" -X utf8 -m app.launcher stop`
    Pop $0
  ${EndIf}
  System::Call 'kernel32::SetEnvironmentVariable(t "SOLIDVISION_INSTDIR", t "$INSTDIR")i'
  nsExec::Exec /TIMEOUT=90000 `"$PowerShellPath" -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "$$root = $$env:SOLIDVISION_INSTDIR.TrimEnd('\') + '\'; $$deadline = (Get-Date).AddSeconds(60); do { $$left = @(Get-CimInstance -ClassName Win32_Process | Where-Object { $$_.ExecutablePath -and $$_.ExecutablePath.StartsWith($$root, [System.StringComparison]::OrdinalIgnoreCase) }); if ($$left.Count -eq 0) { exit 0 }; Start-Sleep -Milliseconds 500 } while ((Get-Date) -lt $$deadline); $$left | ForEach-Object { Stop-Process -Id $$_.ProcessId -Force -ErrorAction SilentlyContinue }; exit 1"`
  Pop $0
!macroend
