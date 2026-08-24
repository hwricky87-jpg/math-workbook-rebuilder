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

function Assert-NoReparseTree {
    param(
        [Parameter(Mandatory = $true)]
        [string]$LiteralPath,
        [Parameter(Mandatory = $true)]
        [string]$Purpose
    )

    if (-not (Test-Path -LiteralPath $LiteralPath)) {
        return
    }
    $rootItem = Get-Item -LiteralPath $LiteralPath -Force
    if ($rootItem.Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw "Refusing a reparse point in $Purpose`: $LiteralPath"
    }
    if (-not $rootItem.PSIsContainer) {
        return
    }
    foreach ($child in Get-ChildItem -LiteralPath $LiteralPath -Force -Recurse) {
        if ($child.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw "Refusing a reparse point in $Purpose`: $($child.FullName)"
        }
    }
}

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
$repoRootFull = [IO.Path]::GetFullPath($repoRoot).TrimEnd([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar)
if ([string]::Equals($repoRootFull, $destination, [StringComparison]::OrdinalIgnoreCase)) {
    if ($InstallDependencies) {
        python (Join-Path $destination 'scripts\bootstrap.py') --verify
        if ($LASTEXITCODE -ne 0) {
            throw 'In-place skill dependency setup or verification failed.'
        }
    }
    Write-Output "The skill is already located at $destination"
    if ($InstallDependencies) {
        Write-Output 'Dependencies were isolated inside the installed skill folder.'
    }
    return
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
    Assert-NoReparseTree -LiteralPath $legacyPath -Purpose 'legacy installed skill content'
    $legacyItem = Get-Item -LiteralPath $legacyPath
    if ($legacyItem.PSIsContainer) {
        Remove-Item -LiteralPath $legacyPath -Recurse -Force
    } else {
        Remove-Item -LiteralPath $legacyPath -Force
    }
}

foreach ($file in @('SKILL.md', 'LICENSE', 'NOTICE', 'install.ps1', 'run.py', 'requirements.txt', 'requirements-lock.txt', 'package.json', 'package-lock.json')) {
    $sourceFile = Join-Path $repoRoot $file
    if (Test-Path -LiteralPath $sourceFile) {
        Assert-NoReparseTree -LiteralPath (Join-Path $destination $file) -Purpose 'installed skill file'
        Copy-Item -LiteralPath $sourceFile -Destination $destination -Force
    }
}
foreach ($folder in @('agents', 'assets', 'references', 'scripts')) {
    $sourceFolder = Join-Path $repoRoot $folder
    $destinationFolder = Join-Path $destination $folder
    Assert-NoReparseTree -LiteralPath $destinationFolder -Purpose 'installed skill content'
    New-Item -ItemType Directory -Path $destinationFolder -Force | Out-Null
    Copy-Item -Path (Join-Path $sourceFolder '*') -Destination $destinationFolder -Recurse -Force
}

if ($InstallDependencies) {
    $venv = Join-Path $destination '.venv'
    if ((Test-Path -LiteralPath $venv) -and ((Get-Item -LiteralPath $venv).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw "Refusing to use a reparse-point virtual environment: $venv"
    }
    python (Join-Path $destination 'scripts\bootstrap.py') --verify
    if ($LASTEXITCODE -ne 0) {
        throw 'Skill-local dependency setup or verification failed.'
    }
}

Write-Output "Installed $skillName to $destination"
if ($InstallDependencies) {
    Write-Output 'Dependencies were isolated inside the installed skill folder.'
}
Write-Output "Restart $Agent if the skill does not appear immediately."
