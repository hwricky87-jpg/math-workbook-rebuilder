[CmdletBinding()]
param(
    [string]$DestinationRoot,
    [ValidateSet('Codex', 'Claude')]
    [string]$Agent = 'Codex',
    [switch]$InstallDependencies
)

$ErrorActionPreference = 'Stop'
$repoRoot = $PSScriptRoot
$skillName = 'math-workbook-rebuilder'
if (-not $PSBoundParameters.ContainsKey('DestinationRoot')) {
    $agentSkillsFolder = if ($Agent -eq 'Claude') { '.claude\skills' } else { '.codex\skills' }
    $DestinationRoot = Join-Path ([Environment]::GetFolderPath('UserProfile')) $agentSkillsFolder
}
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
if (Test-Path -LiteralPath (Join-Path $destination '.git')) {
    throw "Refusing to overwrite a Git worktree. Install to a separate skill destination: $destination"
}

New-Item -ItemType Directory -Path $destinationRootFull -Force | Out-Null
New-Item -ItemType Directory -Path $destination -Force | Out-Null

# Older releases could leave repository-only files in the installed skill folder.
# Remove only this fixed, known list; never enumerate or modify sibling skills.
foreach ($entry in @('.gitattributes', '.gitignore', '.github', 'README.md', 'CONTRIBUTING.md', 'SECURITY.md', 'requirements-audit.txt', 'install.ps1', 'update.ps1')) {
    $legacyPath = Join-Path $destination $entry
    if (-not (Test-Path -LiteralPath $legacyPath)) {
        continue
    }
    $legacyItem = Get-Item -LiteralPath $legacyPath
    if ($legacyItem.Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw "Refusing to remove a reparse-point legacy entry: $legacyPath"
    }
    if ($legacyItem.PSIsContainer) {
        Remove-Item -LiteralPath $legacyPath -Recurse -Force
    } else {
        Remove-Item -LiteralPath $legacyPath -Force
    }
}

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
Write-Output "Restart $Agent if the skill does not appear immediately."
