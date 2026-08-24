[CmdletBinding()]
param(
    [string]$DestinationRoot = (Join-Path ([Environment]::GetFolderPath('UserProfile')) '.codex\skills'),
    [switch]$InstallDependencies
)

$ErrorActionPreference = 'Stop'
$repoRoot = $PSScriptRoot
$skillName = 'math-workbook-rebuilder'
$destinationRootFull = [IO.Path]::GetFullPath($DestinationRoot)
$pathRoot = [IO.Path]::GetPathRoot($destinationRootFull)
if (-not [string]::Equals($destinationRootFull, $pathRoot, [StringComparison]::OrdinalIgnoreCase)) {
    $destinationRootFull = $destinationRootFull.TrimEnd([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar)
}
$destination = [IO.Path]::GetFullPath((Join-Path $destinationRootFull $skillName))
if (-not [string]::Equals([IO.Path]::GetDirectoryName($destination), $destinationRootFull, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing to install outside the requested destination root: $destination"
}
if ((Test-Path -LiteralPath $destination) -and ((Get-Item -LiteralPath $destination).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
    throw "Refusing to install through a reparse-point destination: $destination"
}

New-Item -ItemType Directory -Path $destinationRootFull -Force | Out-Null
New-Item -ItemType Directory -Path $destination -Force | Out-Null

foreach ($file in @('SKILL.md', 'LICENSE', 'NOTICE', 'run.py', 'requirements.txt', 'requirements-lock.txt', 'package.json', 'package-lock.json')) {
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
    $venv = Join-Path $destination '.venv'
    if ((Test-Path -LiteralPath $venv) -and ((Get-Item -LiteralPath $venv).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw "Refusing to replace a reparse-point virtual environment: $venv"
    }
    python -m venv --clear $venv
    if ($LASTEXITCODE -ne 0) {
        throw 'Skill-local Python virtual environment creation failed.'
    }
    $venvPython = if ([Environment]::OSVersion.Platform -eq [PlatformID]::Win32NT) {
        Join-Path $venv 'Scripts\python.exe'
    } else {
        Join-Path $venv 'bin/python'
    }
    & $venvPython -m pip install --disable-pip-version-check --only-binary=:all: --require-virtualenv -r (Join-Path $destination 'requirements-lock.txt')
    if ($LASTEXITCODE -ne 0) {
        throw 'Skill-local Python dependency installation failed.'
    }
    npm --prefix $destination ci --omit=dev --ignore-scripts
    if ($LASTEXITCODE -ne 0) {
        throw 'Node dependency installation failed.'
    }
}

Write-Output "Installed $skillName to $destination"
if ($InstallDependencies) {
    Write-Output 'Dependencies were isolated inside the installed skill folder.'
}
Write-Output 'Restart Codex if the skill does not appear immediately.'
