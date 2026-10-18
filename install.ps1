<#
.SYNOPSIS
  Устанавливает native host «Добавить сайт в Throne» для текущего пользователя.

.DESCRIPTION
  Копирует host в %LOCALAPPDATA%\ThroneSiteAdder\host, пишет config.json с путём к Throne,
  создаёт манифесты Native Messaging и регистрирует их в HKCU для Chrome/Yandex, Chromium,
  Edge и Firefox. Права администратора не нужны. Политики браузера не трогаются.

.PARAMETER ExtensionId
  ID расширения (32 буквы a-p), можно несколько через запятую. По умолчанию — ID из Chrome Web Store
  lnfbcmcfedcjfkgomfmepbcnglmhnmho (у локальной dev-сборки расширения тот же ID).
.PARAMETER ThroneDir
  Папка Throne (где лежит Throne.exe). По умолчанию — папка запущенного Throne.exe, иначе папка
  из записи установщика Throne, иначе скрипт спросит.
.PARAMETER RestartMethod
  Как применять изменения: api (по умолчанию; Throne 1.4+ с включённым Throne API — штатный
  перезапуск профиля), kill (старые версии: завершить ядро, как «Restart Proxy» в трее;
  в режиме TUN возможна ошибка адаптера), none (только сохранить).

.EXAMPLE
  install.cmd
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File install.ps1 -ThroneDir "D:\Apps\Throne"
#>
param(
    [string[]]$ExtensionId = @(),
    [string]$ThroneDir = '',
    [ValidateSet('api', 'kill', 'none')][string]$RestartMethod = 'api'
)

$ErrorActionPreference = 'Stop'
$HostName = 'com.throne.site_adder'
$FirefoxId = 'throne-site-adder@local'
$CwsId = 'lnfbcmcfedcjfkgomfmepbcnglmhnmho'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$AppDir = Join-Path $env:LOCALAPPDATA 'ThroneSiteAdder'
$HostDir = Join-Path $AppDir 'host'

function Fail($msg) { Write-Host "ОШИБКА: $msg" -ForegroundColor Red; exit 1 }

# --- extension IDs ---
$ids = @()
foreach ($item in $ExtensionId) { $ids += ($item -split '[,\s]+' | Where-Object { $_ }) }
$ids = @($ids | ForEach-Object { $_.Trim().ToLower() } | Select-Object -Unique)
foreach ($id in $ids) { if ($id -notmatch '^[a-p]{32}$') { Fail "Некорректный ID расширения: '$id' (нужно 32 буквы a-p)" } }
if ($ids.Count -eq 0) { $ids = @($CwsId) }

# --- Throne folder ---
if (-not $ThroneDir) {
    $proc = Get-Process -Name Throne -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($proc -and $proc.Path) { $ThroneDir = Split-Path -Parent $proc.Path }
}
if (-not $ThroneDir) {
    # Throne is closed: take the folder from its installer's uninstall entry.
    $uninst = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*',
              'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*',
              'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*'
    $entry = Get-ItemProperty $uninst -ErrorAction SilentlyContinue |
        Where-Object { $_.DisplayName -like 'Throne*' -and $_.InstallLocation -and
                       (Test-Path (Join-Path $_.InstallLocation 'config\throne.db')) } | Select-Object -First 1
    if ($entry) { $ThroneDir = $entry.InstallLocation }
}
if (-not $ThroneDir -and [Environment]::UserInteractive) {
    $ThroneDir = (Read-Host 'Не нашёл Throne. Введите папку, где лежит Throne.exe').Trim('" ')
}
if (-not $ThroneDir) { Fail 'Throne не найден. Запустите Throne или укажите -ThroneDir "W:\stop car\Throne"' }
$ThroneDir = (Resolve-Path $ThroneDir).Path
if (-not (Test-Path (Join-Path $ThroneDir 'config\throne.db'))) {
    Fail "В '$ThroneDir' нет config\throne.db. Нужен Throne 1.2+ (хранит настройки в SQLite)."
}

# --- Python (the Microsoft Store stub in WindowsApps does not count) ---
$python = $null
$candidates = @()
$py = Get-Command py.exe -ErrorAction SilentlyContinue
if ($py) { try { $candidates += (& $py.Source -3 -c 'import sys; print(sys.executable)' 2>$null) } catch {} }
$candidates += (Get-Command python.exe -All -ErrorAction SilentlyContinue | ForEach-Object { $_.Source })
$candidates += (Get-ChildItem "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe" -ErrorAction SilentlyContinue | Sort-Object FullName -Descending | ForEach-Object { $_.FullName })
foreach ($c in $candidates) {
    if (-not $c -or $c -like '*\WindowsApps\*' -or -not (Test-Path $c)) { continue }
    # No quotes inside: Windows PowerShell 5.1 strips them from native arguments.
    try { $ver = & $c -c 'import sys, sqlite3; print(sys.version_info[0] * 100 + sys.version_info[1])' 2>$null } catch { continue }
    if ($LASTEXITCODE -eq 0 -and [int]$ver -ge 308) { $python = $c; break }
}
if (-not $python) { Fail 'Не найден Python 3.8+ (https://www.python.org/downloads/). Установите и запустите скрипт снова.' }

# --- copy host and write its config ---
New-Item -ItemType Directory -Force -Path $HostDir | Out-Null
Copy-Item (Join-Path $Root 'host\*.py') $HostDir -Force
$utf8 = New-Object System.Text.UTF8Encoding($false)
[IO.File]::WriteAllText((Join-Path $HostDir 'config.json'), ([ordered]@{ throneDir = $ThroneDir; restartMethod = $RestartMethod } | ConvertTo-Json), $utf8)
$bat = Join-Path $HostDir 'run_host.bat'
[IO.File]::WriteAllText($bat, "@echo off`r`n`"$python`" -I -u `"%~dp0throne_host.py`" %*`r`n", $utf8)

# --- Native Messaging manifests ---
$chromeManifest = Join-Path $HostDir "$HostName.json"
$firefoxManifest = Join-Path $HostDir "$HostName.firefox.json"
function New-HostManifest { [ordered]@{ name = $HostName; description = 'Throne site adder native host'; path = $bat; type = 'stdio' } }
$c = New-HostManifest; $c.allowed_origins = @($ids | ForEach-Object { "chrome-extension://$_/" })
$f = New-HostManifest; $f.allowed_extensions = @($FirefoxId)
[IO.File]::WriteAllText($chromeManifest, ($c | ConvertTo-Json), $utf8)
[IO.File]::WriteAllText($firefoxManifest, ($f | ConvertTo-Json), $utf8)

# Yandex Browser reads the Google Chrome key, so one registration covers both.
$regs = @{
    'HKCU:\Software\Google\Chrome\NativeMessagingHosts'  = $chromeManifest
    'HKCU:\Software\Chromium\NativeMessagingHosts'       = $chromeManifest
    'HKCU:\Software\Microsoft\Edge\NativeMessagingHosts' = $chromeManifest
    'HKCU:\Software\Mozilla\NativeMessagingHosts'        = $firefoxManifest
}
foreach ($key in $regs.Keys) {
    $k = Join-Path $key $HostName
    New-Item -Path $k -Force | Out-Null
    Set-ItemProperty -Path $k -Name '(default)' -Value $regs[$key]
}

# --- self-test: one status request through the real launcher ---
# The request goes through a file: a redirected StandardInput in .NET Framework would prepend a BOM.
$req = [Text.Encoding]::UTF8.GetBytes('{"cmd":"status"}')
$reqFile = Join-Path $AppDir 'selftest.bin'
[IO.File]::WriteAllBytes($reqFile, [BitConverter]::GetBytes([uint32]$req.Length) + $req)
$psi = New-Object Diagnostics.ProcessStartInfo 'cmd.exe', "/d /s /c `"`"$bat`" < `"$reqFile`"`""
$psi.UseShellExecute = $false; $psi.RedirectStandardOutput = $true
$p = [Diagnostics.Process]::Start($psi)
$out = New-Object IO.MemoryStream
$p.StandardOutput.BaseStream.CopyTo($out); $p.WaitForExit()
$bytes = $out.ToArray()
if ($bytes.Length -lt 4) { Fail 'Host не ответил при самопроверке.' }
$reply = [Text.Encoding]::UTF8.GetString($bytes, 4, $bytes.Length - 4) | ConvertFrom-Json

Write-Host ''
Write-Host 'Native host установлен.' -ForegroundColor Green
Write-Host "  Python:      $python"
Write-Host "  Throne:      $ThroneDir"
Write-Host "  Host:        $HostDir"
Write-Host "  Применение:  $RestartMethod"
Write-Host "  Расширения:  $(if ($ids.Count) { $ids -join ', ' } else { '—' }) ; Firefox: $FirefoxId"
if ($reply.ok) {
    Write-Host "  Самопроверка: OK — профиль «$($reply.profile.name)», записей: $($reply.entries.Count), Throne запущен: $($reply.throneRunning)" -ForegroundColor Green
    if ($RestartMethod -eq 'api' -and -not $reply.api.enabled) {
        Write-Host '  Throne API выключен: включите его в Throne (Настройки → Integration Settings → Throne API),' -ForegroundColor Yellow
        Write-Host '  иначе изменения будут сохраняться, но не применяться автоматически. Ключ host читает сам.' -ForegroundColor Yellow
    } elseif ($RestartMethod -eq 'api') {
        Write-Host "  Throne API: включён, порт $($reply.api.port)" -ForegroundColor Green
    }
} else {
    Write-Host "  Самопроверка: $($reply.code) — $($reply.message)" -ForegroundColor Yellow
}
Write-Host 'Перезапустите браузер, чтобы он увидел host.'
