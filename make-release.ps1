<#
.SYNOPSIS
  Builds release\ThroneSiteAdder-host-v<version>.zip and its .sha256.
  The version comes from VERSION in host\throne_host.py.
#>
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path

$match = Select-String -Path (Join-Path $Root 'host\throne_host.py') -Pattern '^VERSION = "([^"]+)"'
if (-not $match) { throw 'VERSION not found in host\throne_host.py' }
$version = $match.Matches[0].Groups[1].Value
$name = "ThroneSiteAdder-host-v$version"

$out = Join-Path $Root 'release'
$stage = Join-Path $out $name
if (Test-Path $stage) { Remove-Item $stage -Recurse -Force }
New-Item -ItemType Directory -Force -Path (Join-Path $stage 'host') | Out-Null

Copy-Item (Join-Path $Root 'host\*.py') (Join-Path $stage 'host')
foreach ($f in 'install.cmd', 'install.ps1', 'uninstall.cmd', 'uninstall.ps1', 'README.md', 'LICENSE', 'CHANGELOG.md') {
    Copy-Item (Join-Path $Root $f) $stage
}

$zip = Join-Path $out "$name.zip"
if (Test-Path $zip) { Remove-Item $zip -Force }
# Entries are added one by one: Compress-Archive and ZipFile.CreateFromDirectory in Windows PowerShell 5.1
# store paths with backslashes, against the zip spec (other unzip tools then complain).
Add-Type -AssemblyName System.IO.Compression, System.IO.Compression.FileSystem
$archive = [IO.Compression.ZipFile]::Open($zip, [IO.Compression.ZipArchiveMode]::Create)
try {
    foreach ($file in Get-ChildItem $stage -Recurse -File) {
        $entryName = "$name/" + $file.FullName.Substring($stage.Length + 1).Replace('\', '/')
        [void][IO.Compression.ZipFileExtensions]::CreateEntryFromFile($archive, $file.FullName, $entryName,
            [IO.Compression.CompressionLevel]::Optimal)
    }
} finally {
    $archive.Dispose()
}
Remove-Item $stage -Recurse -Force

$hash = (Get-FileHash $zip -Algorithm SHA256).Hash.ToLower()
[IO.File]::WriteAllText("$zip.sha256", "$hash  $name.zip`n")
Write-Host "Built $zip"
Write-Host "SHA256 $hash"
