<#
.SYNOPSIS
  Удаляет native host «Добавить сайт в Throne»: ключи реестра HKCU и папку host.
  Резервные копии конфига Throne (%LOCALAPPDATA%\ThroneSiteAdder\backups) сохраняются,
  если не указан -RemoveBackups. Сам конфиг Throne не меняется.
#>
param([switch]$RemoveBackups)

$HostName = 'com.throne.site_adder'
$AppDir = Join-Path $env:LOCALAPPDATA 'ThroneSiteAdder'
foreach ($key in 'HKCU:\Software\Google\Chrome\NativeMessagingHosts', 'HKCU:\Software\Chromium\NativeMessagingHosts',
                 'HKCU:\Software\Microsoft\Edge\NativeMessagingHosts', 'HKCU:\Software\Mozilla\NativeMessagingHosts') {
    $k = Join-Path $key $HostName
    if (Test-Path $k) { Remove-Item $k -Recurse -Force; Write-Host "Удалён $k" }
}
if (Test-Path "$AppDir\host") { Remove-Item "$AppDir\host" -Recurse -Force; Write-Host "Удалена папка $AppDir\host" }
if ($RemoveBackups -and (Test-Path $AppDir)) { Remove-Item $AppDir -Recurse -Force; Write-Host "Удалена папка $AppDir (вместе с бэкапами)" }
elseif (Test-Path "$AppDir\backups") { Write-Host "Бэкапы конфига оставлены в $AppDir\backups" }
Write-Host 'Готово. Само расширение удалите в браузере (меню расширений → Удалить).'
