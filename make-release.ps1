<#
.SYNOPSIS
  Builds the release archives into release\, each with a .sha256:
    ThroneSiteAdder-host-v<host version>.zip            host + installer (version: VERSION in host\throne_host.py)
    ThroneSiteAdder-extension-v<extension version>.zip  unpacked extension for "Load unpacked"
                                                         (version: extension\manifest.json)
#>
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$out = Join-Path $Root 'release'
New-Item -ItemType Directory -Force -Path $out | Out-Null
Add-Type -AssemblyName System.IO.Compression, System.IO.Compression.FileSystem

function New-ReleaseZip($name, $stage) {
    $zip = Join-Path $out "$name.zip"
    if (Test-Path $zip) { Remove-Item $zip -Force }
    # Entries are added one by one: Compress-Archive and ZipFile.CreateFromDirectory in Windows PowerShell 5.1
    # store paths with backslashes, against the zip spec (other unzip tools then complain).
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
    $hash = (Get-FileHash $zip -Algorithm SHA256).Hash.ToLower()
    [IO.File]::WriteAllText("$zip.sha256", "$hash  $name.zip`n")
    Write-Host "Built $zip"
    Write-Host "SHA256 $hash"
}

# --- host ---
$match = Select-String -Path (Join-Path $Root 'host\throne_host.py') -Pattern '^VERSION = "([^"]+)"'
if (-not $match) { throw 'VERSION not found in host\throne_host.py' }
$name = "ThroneSiteAdder-host-v$($match.Matches[0].Groups[1].Value)"
$stage = Join-Path $out $name
if (Test-Path $stage) { Remove-Item $stage -Recurse -Force }
New-Item -ItemType Directory -Force -Path (Join-Path $stage 'host') | Out-Null
Copy-Item (Join-Path $Root 'host\*.py') (Join-Path $stage 'host')
foreach ($f in 'install.cmd', 'install.ps1', 'uninstall.cmd', 'uninstall.ps1', 'README.md', 'LICENSE', 'CHANGELOG.md') {
    Copy-Item (Join-Path $Root $f) $stage
}
New-ReleaseZip $name $stage
Remove-Item $stage -Recurse -Force

# --- extension ---
$manifest = Join-Path $Root 'extension\manifest.json'
if (Test-Path $manifest) {
    $extVersion = (Get-Content $manifest -Raw -Encoding UTF8 | ConvertFrom-Json).version
    New-ReleaseZip "ThroneSiteAdder-extension-v$extVersion" (Join-Path $Root 'extension')
}
