# What is actually installed, from the two places #411 says disagree:
# Programs and Features, and the install folder's dist-info directories.
param(
    [string]$Label = "state",
    [switch]$Verdict
)

Write-Host "=== $Label ==="

$keys = @(
    "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*",
    "HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*",
    "HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*"
)
$entries = @()
foreach ($k in $keys) {
    $entries += Get-ItemProperty $k -ErrorAction SilentlyContinue |
        Where-Object { $_.DisplayName -like "*BelfrySCAD*" }
}
Write-Host "Programs and Features entries: $($entries.Count)"
foreach ($e in $entries) {
    Write-Host ("  {0}  version={1}  scope={2}" -f `
        $e.DisplayName, $e.DisplayVersion,
        $(if ($e.PSPath -like "*HKEY_CURRENT_USER*") { "per-user" } else { "per-machine" }))
    Write-Host ("      {0}" -f $e.PSChildName)
}

# Ask the registry where it actually went. Guessing two well-known paths
# missed a default-scope install entirely, and reported "0 dist-info" for a
# product that had installed perfectly well somewhere else.
$dirs = @()
foreach ($e in $entries) {
    if ($e.InstallLocation) { $dirs += $e.InstallLocation.TrimEnd("\") }
}
$dirs += @("C:\Program Files\BelfrySCAD", "C:\Program Files\Revar Desmera\BelfrySCAD",
           "$env:LOCALAPPDATA\Programs\BelfrySCAD")
$dirs = $dirs | Select-Object -Unique
foreach ($d in $dirs) {
    if (-not (Test-Path $d)) { continue }
    Write-Host "Install folder: $d"
    $dist = Get-ChildItem -Path $d -Recurse -Directory -Filter "belfryscad-*.dist-info" `
        -ErrorAction SilentlyContinue
    Write-Host "  belfryscad dist-info directories: $($dist.Count)"
    foreach ($x in $dist) { Write-Host "    $($x.Name)" }
}

if ($Verdict) {
    # One product, one metadata directory. Anything else is the bug, and the
    # step must fail so the run says so without anyone reading the log.
    $dist = @()
    foreach ($d in $dirs) {
        if (Test-Path $d) {
            $dist += Get-ChildItem -Path $d -Recurse -Directory `
                -Filter "belfryscad-*.dist-info" -ErrorAction SilentlyContinue
        }
    }
    Write-Host ""
    if ($entries.Count -eq 1 -and $dist.Count -eq 1) {
        Write-Host "VERDICT: upgraded in place -- 1 product, 1 dist-info."
    } else {
        Write-Host ("VERDICT: STACKED -- {0} products, {1} dist-info directories." -f `
            $entries.Count, $dist.Count)
        exit 1
    }
}
