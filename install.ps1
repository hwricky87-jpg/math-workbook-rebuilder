[CmdletBinding()]
param(
    [string]$DestinationRoot = (Join-Path ([Environment]::GetFolderPath('UserProfile')) '.codex\skills'),
    [switch]$InstallDependencies
)

$ErrorActionPreference = 'Stop'
$repoRoot = $PSScriptRoot
$skillName = 'math-workbook-rebuilder'
$destinationRootFull = [IO.Path]::GetFullPath($DestinationRoot)
$destination = Join-Path $destinationRootFull $skillName

New-Item -ItemType Directory -Path $destinationRootFull -Force | Out-Null
New-Item -ItemType Directory -Path $destination -Force | Out-Null

Copy-Item -LiteralPath (Join-Path $repoRoot 'SKILL.md') -Destination $destination -Force
foreach ($file in @('requirements.txt', 'package.json', 'package-lock.json')) {
    $sourceFile = Join-Path $repoRoot $file
    if (Test-Path -LiteralPath $sourceFile) {
        Copy-Item -LiteralPath $sourceFile -Destination $destination -Force
    }
}
foreach ($folder in @('agents', 'assets', 'references', 'scripts')) {
    $sourceFolder = Join-Path $repoRoot $folder
    $destinationFolder = Join-Path $destination $folder
    New-Item -ItemType Directory -Path $destinationFolder -Force | Out-Null
    Copy-Item -Path (Join-Path $sourceFolder '*') -Destination $destinationFolder -Recurse -Force
}

if ($InstallDependencies) {
    python -m pip install --disable-pip-version-check --only-binary=:all: -r (Join-Path $repoRoot 'requirements.txt')
    if ($LASTEXITCODE -ne 0) {
        throw 'Python dependency installation failed.'
    }
    npm --prefix $destination install --omit=dev --ignore-scripts
    if ($LASTEXITCODE -ne 0) {
        throw 'Node dependency installation failed.'
    }
}

Write-Output "Installed $skillName to $destination"
Write-Output 'Restart Codex if the skill does not appear immediately.'
