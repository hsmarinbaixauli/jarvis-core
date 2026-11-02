<#
.SYNOPSIS
  Arranque automático de Jarvis al iniciar sesión en Windows (tarea programada "Jarvis").
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\windows\autostart.ps1 install
  powershell -ExecutionPolicy Bypass -File scripts\windows\autostart.ps1 status
  powershell -ExecutionPolicy Bypass -File scripts\windows\autostart.ps1 uninstall
#>
param(
    [ValidateSet("install", "uninstall", "status", "start", "stop")]
    [string]$Action = "install",
    [switch]$NoStart,
    [switch]$SkipChecks
)
$ErrorActionPreference = "Stop"
$TaskName = "Jarvis"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Launcher = Join-Path $PSScriptRoot "jarvis_hidden.vbs"
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$User = "$env:USERDOMAIN\$env:USERNAME"

function Get-JarvisTask { Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue }

switch ($Action) {
    "install" {
        if (-not $SkipChecks -and -not (Test-Path $Python)) {
            throw "No encuentro $Python. Ejecuta antes: powershell -ExecutionPolicy Bypass -File scripts\windows\install.ps1"
        }
        # Runs as the logged-in user (not as a service), so it can use their keyring, browser profile and show nothing.
        $taskAction = New-ScheduledTaskAction -Execute "wscript.exe" -Argument "`"$Launcher`"" -WorkingDirectory $Root
        $trigger = New-ScheduledTaskTrigger -AtLogOn -User $User
        $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero)
        $principal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Limited
        Register-ScheduledTask -TaskName $TaskName -Action $taskAction -Trigger $trigger -Settings $settings `
            -Principal $principal -Description "Jarvis, asistente personal" -Force | Out-Null
        Write-Host "✓ Tarea '$TaskName' creada: Jarvis arrancará solo cada vez que inicies sesión."
        if (-not $NoStart) {
            Start-ScheduledTask -TaskName $TaskName
            Write-Host "✓ Jarvis arrancado en segundo plano. Logs en $Root\data\logs\"
        }
    }
    "uninstall" {
        if (Get-JarvisTask) {
            Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
            Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
            Write-Host "✓ Arranque automático quitado."
        } else {
            Write-Host "No había arranque automático instalado."
        }
    }
    "status" {
        $task = Get-JarvisTask
        if (-not $task) { Write-Host "Arranque automático: no instalado."; exit 1 }
        $info = $task | Get-ScheduledTaskInfo
        Write-Host "Estado: $($task.State) · última ejecución: $($info.LastRunTime) · resultado: $($info.LastTaskResult)"
    }
    "start" { Start-ScheduledTask -TaskName $TaskName; Write-Host "✓ Jarvis arrancado." }
    "stop" {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        # The task only starts the launcher; stop the Python processes running from this folder too.
        Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
            Where-Object { $_.CommandLine -like "*-m jarvis*" -and $_.ExecutablePath -like "$Root*" } |
            ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
        Write-Host "✓ Jarvis detenido."
    }
}
