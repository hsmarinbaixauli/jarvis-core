<#
.SYNOPSIS
  Installs Jarvis's security hook for Claude Code at user level: every project, not only Jarvis.

.DESCRIPTION
  Copies .claude/hooks/scan_before_install.py to your Claude folder and registers it in
  settings.json as a PreToolUse hook for Bash and PowerShell. Before Claude Code clones or installs
  a GitHub repo, NVIDIA SkillSpector scans it: a critical finding blocks it, any other risk asks you.

  Keeps everything already in settings.json (makes settings.json.bak first) and can be run again
  safely: it never adds the hook twice. Needs SkillSpector installed:
    uv tool install git+https://github.com/NVIDIA/SkillSpector.git@v2.13.2

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\windows\install-scan-hook.ps1
#>
param(
    [string]$ClaudeHome = (Join-Path $env:USERPROFILE ".claude")
)
$ErrorActionPreference = "Stop"
# Windows PowerShell 5.1 otherwise serializes some arrays as {"value": [...], "Count": n}.
Remove-TypeData System.Array -ErrorAction SilentlyContinue

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Source = Join-Path $Root ".claude\hooks\scan_before_install.py"
if (-not (Test-Path $Source)) { throw "No encuentro $Source. Ejecuta este script desde tu copia del repo de Jarvis." }

$HooksDir = Join-Path $ClaudeHome "hooks"
New-Item -ItemType Directory -Force -Path $HooksDir | Out-Null
$Target = Join-Path $HooksDir "scan_before_install.py"
Copy-Item -Force $Source $Target
Write-Host "✓ Hook copiado en $Target"

# Forward slashes work both in Git Bash and in cmd, whichever shell Claude Code uses for hooks.
$Command = 'python "' + ($Target -replace '\\', '/') + '"'
$SettingsPath = Join-Path $ClaudeHome "settings.json"

$Settings = $null
if (Test-Path $SettingsPath) {
    $raw = [System.IO.File]::ReadAllText($SettingsPath)
    if ($raw.Trim()) {
        try { $Settings = $raw | ConvertFrom-Json }
        catch { throw "$SettingsPath no es JSON válido; arréglalo antes (no lo toco): $_" }
    }
    Copy-Item -Force $SettingsPath "$SettingsPath.bak"
    Write-Host "✓ Copia de seguridad: $SettingsPath.bak"
}
if ($null -eq $Settings) { $Settings = New-Object PSObject }

if (-not $Settings.PSObject.Properties["hooks"]) {
    $Settings | Add-Member -NotePropertyName hooks -NotePropertyValue (New-Object PSObject)
}
if (-not $Settings.hooks.PSObject.Properties["PreToolUse"]) {
    $Settings.hooks | Add-Member -NotePropertyName PreToolUse -NotePropertyValue @()
}

$Existing = @($Settings.hooks.PreToolUse | Where-Object { @($_.hooks | ForEach-Object { $_.command }) -contains $Command })
if ($Existing.Count -gt 0) {
    Write-Host "· El hook ya estaba registrado; no cambio nada más."
} else {
    $Entry = New-Object PSObject -Property @{
        matcher = "Bash|PowerShell"
        hooks   = @(New-Object PSObject -Property @{ type = "command"; command = $Command; timeout = 600 })
    }
    $Settings.hooks.PreToolUse = @($Settings.hooks.PreToolUse) + $Entry
    # No BOM: Claude Code reads settings.json as plain UTF-8 JSON.
    $Json = $Settings | ConvertTo-Json -Depth 32
    [System.IO.File]::WriteAllText($SettingsPath, $Json, (New-Object System.Text.UTF8Encoding $false))
    Write-Host "✓ Hook registrado en $SettingsPath"
}

if (Get-Command skillspector -ErrorAction SilentlyContinue) {
    Write-Host "✓ SkillSpector encontrado."
} else {
    Write-Warning "SkillSpector no está en el PATH: el hook bloqueará cualquier instalación de repos hasta que lo instales:"
    Write-Warning "  uv tool install git+https://github.com/NVIDIA/SkillSpector.git@v2.13.2   (y luego: uv tool update-shell)"
}
Write-Host ""
Write-Host "Listo. Reinicia Claude Code (o VS Code) para que cargue el hook."
