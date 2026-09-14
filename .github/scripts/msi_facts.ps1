# Dump the upgrade-relevant tables of an MSI.
#
# Everything goes through IDispatch late binding because WindowsInstaller's
# COM types are not available to PowerShell directly. The `,` before each
# returned array is load-bearing: PowerShell unrolls nested arrays on return,
# which turns one row of one field into a bare string and makes the caller's
# indexing silently wrong.
param([Parameter(Mandatory=$true)][string]$Path)

$installer = New-Object -ComObject WindowsInstaller.Installer
$db = $installer.GetType().InvokeMember(
    "OpenDatabase", "InvokeMethod", $null, $installer, @($Path, 0))

function Invoke-MsiQuery {
    param([string]$Sql)
    $view = $db.GetType().InvokeMember("OpenView", "InvokeMethod", $null, $db, @($Sql))
    $view.GetType().InvokeMember("Execute", "InvokeMethod", $null, $view, $null)
    $rows = @()
    while ($true) {
        $rec = $view.GetType().InvokeMember("Fetch", "InvokeMethod", $null, $view, $null)
        if ($null -eq $rec) { break }
        $count = $rec.GetType().InvokeMember("FieldCount", "GetProperty", $null, $rec, $null)
        $vals = @()
        for ($i = 1; $i -le $count; $i++) {
            $vals += [string]$rec.GetType().InvokeMember(
                "StringData", "GetProperty", $null, $rec, @($i))
        }
        $rows += ,$vals
    }
    $view.GetType().InvokeMember("Close", "InvokeMethod", $null, $view, $null)
    return ,$rows
}

Write-Host "=== $(Split-Path $Path -Leaf) ==="

foreach ($p in @("ProductCode", "UpgradeCode", "ProductVersion", "ProductName",
                 "ALLUSERS", "MSIINSTALLPERUSER", "Manufacturer")) {
    $rows = Invoke-MsiQuery "SELECT Value FROM Property WHERE Property='$p'"
    $value = if ($rows.Count -gt 0) { $rows[0][0] } else { "(unset)" }
    Write-Host ("  {0,-18} {1}" -f $p, $value)
}

# The Upgrade table is what makes a new MSI recognise an old install. An
# empty one means the new package was never told to look.
$upgrades = Invoke-MsiQuery "SELECT UpgradeCode,VersionMin,VersionMax,Attributes,ActionProperty FROM Upgrade"
Write-Host "  Upgrade rows: $($upgrades.Count)"
foreach ($row in $upgrades) {
    Write-Host ("    code={0}  min={1}  max={2}  attrs={3}  prop={4}" -f `
        $row[0], $row[1], $row[2], $row[3], $row[4])
}

# RemoveExistingProducts must be sequenced, and where it sits decides whether
# the old product goes before or after the new files are laid down.
Write-Host "  Sequencing:"
foreach ($row in Invoke-MsiQuery "SELECT Action,Sequence FROM InstallExecuteSequence") {
    if ($row[0] -match "^(RemoveExistingProducts|FindRelatedProducts|MigrateFeatureStates|InstallValidate|InstallInitialize)$") {
        Write-Host ("    {0,-24} {1}" -f $row[0], $row[1])
    }
}
