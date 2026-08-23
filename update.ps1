[CmdletBinding()]
param(
    [string]$DestinationRoot = (Join-Path ([Environment]::GetFolderPath('UserProfile')) '.codex\skills'),
    [switch]$InstallDependencies
)

$ErrorActionPreference = 'Stop'
$repoRoot = $PSScriptRoot
$changes = git -C $repoRoot status --porcelain
if ($LASTEXITCODE -ne 0) {
    throw 'This update script must run from a Git clone of the skill repository.'
}
if ($changes) {
    throw 'The repository has local changes. Commit or discard them before updating.'
}

git -C $repoRoot pull --ff-only
if ($LASTEXITCODE -ne 0) {
    throw 'Git update failed.'
}

& (Join-Path $repoRoot 'install.ps1') -DestinationRoot $DestinationRoot -InstallDependencies:$InstallDependencies
