[CmdletBinding()]
param(
    [string]$TargetRepo = "",
    [switch]$AllConfigured,
    [switch]$Force
)

$ErrorActionPreference = "Stop"

$AutoReportRoot = Split-Path -Parent $PSScriptRoot
$HooksDirectory = Join-Path $AutoReportRoot ".githooks"
$PostCommitHook = Join-Path $HooksDirectory "post-commit"
$SyncScript = Join-Path $PSScriptRoot "sync_commit_to_jira.py"

if (-not (Test-Path -LiteralPath $PostCommitHook -PathType Leaf)) {
    throw "Tracked post-commit hook not found: $PostCommitHook"
}
if (-not (Test-Path -LiteralPath $SyncScript -PathType Leaf)) {
    throw "Commit-to-Jira sync script not found: $SyncScript"
}

if ($AllConfigured -and -not [string]::IsNullOrWhiteSpace($TargetRepo)) {
    throw "Use either -TargetRepo or -AllConfigured, not both"
}

# Use the absolute tracked hook directory so the same AutoReport hook can be
# installed into monitored repositories without copying generated files around.
$GitHooksPath = $HooksDirectory.Replace("\", "/")

function Install-AutoReportHook {
    param([Parameter(Mandatory = $true)][string]$RepositoryPath)

    $ResolvedTarget = (Resolve-Path -LiteralPath $RepositoryPath).Path
    $repoRootOutput = & git -C $ResolvedTarget rev-parse --show-toplevel 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Target is not a Git repository: $ResolvedTarget :: $repoRootOutput"
    }
    $RepoRoot = ($repoRootOutput | Select-Object -First 1).Trim()
    $existingOutput = & git -C $RepoRoot config --local --get core.hooksPath 2>$null
    $ExistingHooksPath = ($existingOutput | Out-String).Trim()
    if (
        -not [string]::IsNullOrWhiteSpace($ExistingHooksPath) -and
        $ExistingHooksPath -ne $GitHooksPath
    ) {
        # Preserve an existing hook collection (for example a repository-specific
        # pre-commit policy). Add only a small post-commit chain wrapper instead of
        # replacing core.hooksPath and disabling the other hooks.
        $ExistingHooksDirectory = if ([IO.Path]::IsPathRooted($ExistingHooksPath)) {
            $ExistingHooksPath
        } else {
            Join-Path $RepoRoot $ExistingHooksPath
        }
        New-Item -ItemType Directory -Path $ExistingHooksDirectory -Force | Out-Null
        $ExistingPostCommit = Join-Path $ExistingHooksDirectory "post-commit"
        $TrackedPostCommitForShell = $PostCommitHook.Replace("\", "/")
        $Signature = "AutoReport chained post-commit hook"
        if (Test-Path -LiteralPath $ExistingPostCommit -PathType Leaf) {
            $CurrentPostCommit = Get-Content -LiteralPath $ExistingPostCommit -Raw
            if ($CurrentPostCommit -notmatch [regex]::Escape($Signature) -and -not $Force) {
                throw (
                    "Existing post-commit hook requires manual review: $ExistingPostCommit. " +
                    "Chain AutoReport manually or rerun with -Force after review."
                )
            }
        }
        $Wrapper = "#!/bin/sh`n# $Signature`nexec `"$TrackedPostCommitForShell`"`n"
        [IO.File]::WriteAllText(
            $ExistingPostCommit,
            $Wrapper,
            [Text.UTF8Encoding]::new($false)
        )
        Write-Host "Installed AutoReport chained post-commit hook"
        Write-Host "  Repository : $RepoRoot"
        Write-Host "  Hooks path : $ExistingHooksPath (preserved)"
        Write-Host "  Wrapper    : $ExistingPostCommit"
        return
    }

    & git -C $RepoRoot config --local core.hooksPath $GitHooksPath
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to configure core.hooksPath for $RepoRoot"
    }

    $configured = (& git -C $RepoRoot config --local --get core.hooksPath).Trim()
    if ($LASTEXITCODE -ne 0 -or $configured -ne $GitHooksPath) {
        throw "core.hooksPath verification failed for $RepoRoot"
    }

    Write-Host "Installed AutoReport Git hooks"
    Write-Host "  Repository : $RepoRoot"
    Write-Host "  Hooks path : $configured"
    Write-Host "  Behavior   : plan/proposal queue; Jira apply only when JIRA_AUTO_APPLY=1"
}

if ($AllConfigured) {
    $ProjectsConfig = Join-Path $PSScriptRoot "startup_projects.json"
    $Projects = (Get-Content -LiteralPath $ProjectsConfig -Raw | ConvertFrom-Json).projects
    $Targets = @(
        $Projects |
            Where-Object { $_.enabled -eq $true -and -not [string]::IsNullOrWhiteSpace($_.path) } |
            ForEach-Object { [string]$_.path } |
            Select-Object -Unique
    )
} elseif ([string]::IsNullOrWhiteSpace($TargetRepo)) {
    $Targets = @($AutoReportRoot)
} else {
    $Targets = @($TargetRepo)
}

foreach ($Target in $Targets) {
    Install-AutoReportHook -RepositoryPath $Target
}
