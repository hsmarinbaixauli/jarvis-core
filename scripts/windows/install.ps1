<#
.SYNOPSIS
  Instala Jarvis en este PC paso a paso. Se puede ejecutar varias veces sin romper nada.
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\windows\install.ps1
#>
param([switch]$NonInteractive)
$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $Root
$env:PYTHONUTF8 = "1"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

function Step($text) { Write-Host "`n== $text" -ForegroundColor Cyan }
function Ask($question) {
    if ($NonInteractive) { return $false }
    return (Read-Host "$question [s/n]") -match "^[sSyY]"
}
function Has($command) { return [bool](Get-Command $command -ErrorAction SilentlyContinue) }

Step "1/6 Python 3.12"
$hasPython = $false
if (Has "py") { & py -3.12 --version 2>$null; $hasPython = ($LASTEXITCODE -eq 0) }
if (-not $hasPython) {
    if ((Has "winget") -and (Ask "No tienes Python 3.12. ¿Lo instalo con winget?")) {
        winget install --id Python.Python.3.12 -e --accept-package-agreements --accept-source-agreements
        Write-Host "Cierra esta ventana, abre otra de PowerShell y vuelve a ejecutar este script." -ForegroundColor Yellow
        exit 0
    }
    throw "Instala Python 3.12 desde https://www.python.org/downloads/ (marca 'Add Python to PATH') y repite."
}

Step "2/6 Node.js y Google Chrome (para el navegador de Jarvis)"
if (Has "node") { node --version } elseif ((Has "winget") -and (Ask "No tienes Node.js. ¿Lo instalo con winget?")) {
    winget install --id OpenJS.NodeJS.LTS -e --accept-package-agreements --accept-source-agreements
} else {
    Write-Host "Sin Node.js Jarvis funciona, pero sin navegador. Instálalo desde https://nodejs.org cuando quieras." -ForegroundColor Yellow
}

$chromePaths = @("$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
    "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
    "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe")
if ($chromePaths | Where-Object { Test-Path $_ }) { Write-Host "✓ Google Chrome instalado" }
elseif ((Has "winget") -and (Ask "No tienes Google Chrome (el navegador de Jarvis). ¿Lo instalo con winget?")) {
    winget install --id Google.Chrome -e --accept-package-agreements --accept-source-agreements
} else {
    Write-Host "Instala Google Chrome desde https://www.google.com/chrome/ para que Jarvis pueda navegar." -ForegroundColor Yellow
}

Step "3/6 Entorno de Python y dependencias"
if (-not (Test-Path ".venv\Scripts\python.exe")) { & py -3.12 -m venv .venv }
$Python = Join-Path $Root ".venv\Scripts\python.exe"
& $Python -m pip install --upgrade pip --quiet
& $Python -m pip install -r requirements.txt --quiet
if ($LASTEXITCODE -ne 0) { throw "Falló la instalación de dependencias (mira el error de arriba)." }
Write-Host "✓ Dependencias instaladas"

Step "4/6 Archivos de configuración"
foreach ($pair in @(@("jarvis.example.yaml", "jarvis.yaml"), @("tasks.example.yaml", "tasks.yaml"))) {
    if (-not (Test-Path $pair[1])) { Copy-Item $pair[0] $pair[1]; Write-Host "✓ Creado $($pair[1])" }
    else { Write-Host "· $($pair[1]) ya existía, no lo toco" }
}

Step "5/6 Claves (se guardan en el Administrador de credenciales de Windows, no en archivos)"
foreach ($name in @("ANTHROPIC_API_KEY", "TELEGRAM_BOT_TOKEN")) {
    & $Python -m jarvis secrets has $name
    if ($LASTEXITCODE -eq 0) { Write-Host "✓ $name ya guardada"; continue }
    if (Ask "¿Guardar ahora $name?") { & $Python -m jarvis secrets set $name }
}

if (Has "docker") {
    docker info *> $null
    if ($LASTEXITCODE -eq 0 -and (Ask "¿Preparar el sandbox de Docker para el modo autónomo? (tarda unos minutos)")) {
        & $Python -m jarvis sandbox build
    }
}

Step "6/6 Comprobación"
& $Python -m jarvis doctor
Write-Host ""
if (Ask "¿Quieres que Jarvis arranque solo cada vez que inicies sesión?") {
    & (Join-Path $PSScriptRoot "autostart.ps1") install
} else {
    Write-Host "Para arrancarlo a mano:  .\.venv\Scripts\python.exe -m jarvis run"
}
Write-Host "`nListo. Guía completa: docs\INSTALACION.md" -ForegroundColor Green
