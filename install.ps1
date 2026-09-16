# Install the acc skill for Claude Code (Windows, PowerShell).
#
# Symlinks this checkout into your Claude skills dir so /acc resolves in any
# project, then verifies the install. Re-runnable (idempotent).
#
#   ./install.ps1            # symlink (needs Developer Mode or an elevated shell)
#   ./install.ps1 -Copy      # copy files instead of symlinking
#
# Uninstall: remove the folder it reports below, or run `make uninstall` (bash).
[CmdletBinding()]
param([switch]$Copy)

$ErrorActionPreference = "Stop"

$Src = Split-Path -Parent $MyInvocation.MyCommand.Path
$SkillsDir = if ($env:CLAUDE_SKILLS_DIR) { $env:CLAUDE_SKILLS_DIR } else { Join-Path $HOME ".claude\skills" }
$Dest = Join-Path $SkillsDir "acc"

function Resolve-PhysicalDirectoryPath {
    param([Parameter(Mandatory = $true)][string]$Path)

    $full = [System.IO.Path]::GetFullPath($Path)
    $root = [System.IO.Path]::GetPathRoot($full)
    $current = $root
    $parts = $full.Substring($root.Length).Split(
        [char[]]@([System.IO.Path]::DirectorySeparatorChar, [System.IO.Path]::AltDirectorySeparatorChar),
        [System.StringSplitOptions]::RemoveEmptyEntries
    )

    foreach ($part in $parts) {
        $candidate = Join-Path $current $part
        if (Test-Path -LiteralPath $candidate -PathType Container) {
            $item = Get-Item -LiteralPath $candidate -Force
            if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
                $target = @($item.Target)[0]
                if (-not [System.IO.Path]::IsPathRooted($target)) {
                    $target = Join-Path (Split-Path -Parent $candidate) $target
                }
                $current = Resolve-PhysicalDirectoryPath $target
            } else {
                # FullName expands Windows short (8.3) names and normalizes the
                # casing reported by the filesystem.
                $current = $item.FullName
            }
        } else {
            $current = $candidate
        }
    }

    $resolved = [System.IO.Path]::GetFullPath($current)
    if ($resolved -eq [System.IO.Path]::GetPathRoot($resolved)) {
        return $resolved
    }
    return $resolved.TrimEnd(
        [System.IO.Path]::DirectorySeparatorChar,
        [System.IO.Path]::AltDirectorySeparatorChar
    )
}

function Resolve-DestinationEntryPath {
    param([Parameter(Mandatory = $true)][string]$Path)

    $full = [System.IO.Path]::GetFullPath($Path)
    $parent = Resolve-PhysicalDirectoryPath (Split-Path -Parent $full)
    return [System.IO.Path]::GetFullPath((Join-Path $parent (Split-Path -Leaf $full))).TrimEnd(
        [System.IO.Path]::DirectorySeparatorChar,
        [System.IO.Path]::AltDirectorySeparatorChar
    )
}

function Test-PathContains {
    param(
        [Parameter(Mandatory = $true)][string]$Parent,
        [Parameter(Mandatory = $true)][string]$Child
    )

    if ($Parent.Equals($Child, [System.StringComparison]::OrdinalIgnoreCase)) {
        return $true
    }
    $prefix = $Parent + [System.IO.Path]::DirectorySeparatorChar
    return $Child.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)
}

$Src = Resolve-PhysicalDirectoryPath $Src
$DestEntry = Resolve-DestinationEntryPath $Dest

# Reject identity and overlap before creating or deleting anything. Either
# direction is unsafe in copy mode: removing an ancestor destroys the source,
# while copying into a descendant recursively consumes its own output. The
# same guard also keeps symlink installs from placing the link inside $Src.
if ((Test-PathContains $Src $DestEntry) -or (Test-PathContains $DestEntry $Src)) {
    Write-Error ("refusing to install because source and destination overlap.`n" +
        "source:      $Src`n" +
        "destination: $DestEntry")
}

if (-not (Test-Path (Join-Path $Src "SKILL.md"))) {
    Write-Error "$Src doesn't look like the acc skill (no SKILL.md)"
}

New-Item -ItemType Directory -Force -Path $SkillsDir | Out-Null

# Clear a prior install.
if (Test-Path $Dest) {
    $item = Get-Item $Dest -Force
    if ($item.LinkType) {
        # Remove only the link entry. Recursing through a junction can touch
        # the linked checkout on older PowerShell/filesystem combinations.
        [System.IO.Directory]::Delete($Dest)
    } elseif ($Copy) {
        Remove-Item -LiteralPath $Dest -Recurse -Force
    } else {
        Write-Error "$Dest already exists and is not a symlink. Remove it first to reinstall."
    }
}

if ($Copy) {
    Copy-Item -Recurse $Src $Dest
} else {
    # Directory symlink; requires Developer Mode or an elevated prompt.
    New-Item -ItemType SymbolicLink -Path $Dest -Target $Src | Out-Null
}

if (Test-Path (Join-Path $Dest "SKILL.md")) {
    $mode = if ($Copy) { "copy" } else { "symlink" }
    Write-Host "Installed acc ($mode) -> $Dest"
    Write-Host "Open Claude Code in any project and run /acc to confirm."
} else {
    Write-Error "install verification failed; SKILL.md missing under $Dest"
}
