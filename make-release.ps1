<#
.SYNOPSIS
  Builds one release into release\: the archive, its .sha256 and notes.md.

.DESCRIPTION
  host       ThroneSiteAdder-host-v<version>.zip       version: VERSION in host\throne_host.py,
                                                        notes: its section of CHANGELOG.md
  extension  ThroneSiteAdder-extension-v<version>.zip  version: extension\manifest.json,
                                                        notes: its section of CHANGELOG-extension.md
  notes.md holds only the changes of this version, for the GitHub release page.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File make-release.ps1 host
#>
param(
    [Parameter(Mandatory)][ValidateSet('host', 'extension')][string]$Component
)
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$out = Join-Path $Root 'release'
if (Test-Path $out) { Remove-Item $out -Recurse -Force }
New-Item -ItemType Directory -Force -Path $out | Out-Null
Add-Type -AssemblyName System.IO.Compression, System.IO.Compression.FileSystem
$utf8 = New-Object System.Text.UTF8Encoding($false)

function New-ReleaseZip($name, $stage) {
    $zip = Join-Path $out "$name.zip"
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
    [IO.File]::WriteAllText("$zip.sha256", "$hash  $name.zip`n", $utf8)
    Write-Host "Built $zip"
    Write-Host "SHA256 $hash"
}

# The "## <version>" section of a changelog, without the heading.
function Get-ChangelogSection($path, $version) {
    $lines = [IO.File]::ReadAllLines($path, $utf8)
    $start = [Array]::FindIndex($lines, [Predicate[string]]{ param($l) $l.Trim() -eq "## $version" })
    if ($start -lt 0) { throw "No '## $version' section in $path" }
    $section = New-Object System.Collections.Generic.List[string]
    for ($i = $start + 1; $i -lt $lines.Length -and -not $lines[$i].StartsWith('## '); $i++) { $section.Add($lines[$i]) }
    $text = ($section -join "`n").Trim()
    if (-not $text) { throw "Section '## $version' in $path is empty" }
    return $text
}

if ($Component -eq 'host') {
    $match = Select-String -Path (Join-Path $Root 'host\throne_host.py') -Pattern '^VERSION = "([^"]+)"'
    if (-not $match) { throw 'VERSION not found in host\throne_host.py' }
    $version = $match.Matches[0].Groups[1].Value
    $name = "ThroneSiteAdder-host-v$version"
    $stage = Join-Path $out $name
    New-Item -ItemType Directory -Force -Path (Join-Path $stage 'host') | Out-Null
    Copy-Item (Join-Path $Root 'host\*.py') (Join-Path $stage 'host')
    foreach ($f in 'install.cmd', 'install.ps1', 'uninstall.cmd', 'uninstall.ps1', 'README.md', 'LICENSE', 'CHANGELOG.md') {
        Copy-Item (Join-Path $Root $f) $stage
    }
    New-ReleaseZip $name $stage
    Remove-Item $stage -Recurse -Force
    $notes = Get-ChangelogSection (Join-Path $Root 'CHANGELOG.md') $version
} else {
    $version = (Get-Content (Join-Path $Root 'extension\manifest.json') -Raw -Encoding UTF8 | ConvertFrom-Json).version
    New-ReleaseZip "ThroneSiteAdder-extension-v$version" (Join-Path $Root 'extension')
    $notes = Get-ChangelogSection (Join-Path $Root 'CHANGELOG-extension.md') $version
}

[IO.File]::WriteAllText((Join-Path $out 'notes.md'), $notes + "`n", $utf8)
[IO.File]::WriteAllText((Join-Path $out 'version.txt'), $version, $utf8)
Write-Host "Notes: release\notes.md ($Component $version)"
