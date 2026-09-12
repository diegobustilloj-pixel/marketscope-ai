param(
    [Parameter(Mandatory = $true)]
    [string]$ReposRoot,
    [Parameter(Mandatory = $true)]
    [string]$OutputPath
)

$ErrorActionPreference = 'Stop'

function Invoke-GitSafe {
    param([string]$RepoPath, [string[]]$Arguments)
    $safe = (Resolve-Path -LiteralPath $RepoPath).Path.Replace('\', '/')
    $output = & git -c "safe.directory=$safe" -C $RepoPath @Arguments 2>$null
    if ($LASTEXITCODE -ne 0) { return $null }
    return ($output -join "`n").Trim()
}

function Get-TextPreview {
    param([string]$Path, [int]$MaxChars = 12000)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $null }
    $text = Get-Content -Raw -LiteralPath $Path -ErrorAction SilentlyContinue
    if ($null -eq $text) { return $null }
    if ($text.Length -gt $MaxChars) { return $text.Substring(0, $MaxChars) }
    return $text
}

$manifestNames = @(
    'Cargo.toml', 'package.json', 'pyproject.toml', 'requirements.txt',
    'foundry.toml', 'hardhat.config.ts', 'hardhat.config.js', 'go.mod'
)

$results = @()
Get-ChildItem -LiteralPath $ReposRoot -Directory | Sort-Object Name | ForEach-Object {
    $repoPath = $_.FullName
    if (-not (Test-Path -LiteralPath (Join-Path $repoPath '.git'))) { return }

    $readme = Get-ChildItem -LiteralPath $repoPath -File -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match '^README(\..+)?$' } |
        Select-Object -First 1
    $license = Get-ChildItem -LiteralPath $repoPath -File -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match '^(LICENSE|COPYING)(\..+)?$' } |
        Select-Object -First 1

    $manifests = foreach ($name in $manifestNames) {
        $candidate = Join-Path $repoPath $name
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            [pscustomobject]@{ name = $name; content = Get-TextPreview -Path $candidate -MaxChars 20000 }
        }
    }

    $workflowsPath = Join-Path $repoPath '.github\workflows'
    $workflows = @()
    if (Test-Path -LiteralPath $workflowsPath -PathType Container) {
        $workflows = @(Get-ChildItem -LiteralPath $workflowsPath -File -Recurse |
            ForEach-Object { $_.FullName.Substring($repoPath.Length + 1).Replace('\', '/') })
    }

    $topDirs = @(Get-ChildItem -LiteralPath $repoPath -Directory -Force -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -ne '.git' } | Select-Object -ExpandProperty Name)
    $fileCount = @(Get-ChildItem -LiteralPath $repoPath -File -Recurse -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -notmatch '[\\/]\.git[\\/]' }).Count

    $branches = @(Invoke-GitSafe -RepoPath $repoPath -Arguments @('branch', '-a', '--format=%(refname:short)') -split "`n" | Where-Object { $_ })
    $tags = @(Invoke-GitSafe -RepoPath $repoPath -Arguments @('tag', '--sort=-creatordate') -split "`n" | Where-Object { $_ } | Select-Object -First 30)
    $recentCommits = @(Invoke-GitSafe -RepoPath $repoPath -Arguments @('log', '-n', '20', '--date=iso-strict', '--pretty=format:%H%x09%ad%x09%an%x09%s') -split "`n" | Where-Object { $_ })

    $results += [pscustomobject]@{
        name = $_.Name
        path = $repoPath
        head = Invoke-GitSafe -RepoPath $repoPath -Arguments @('rev-parse', 'HEAD')
        origin = Invoke-GitSafe -RepoPath $repoPath -Arguments @('remote', 'get-url', 'origin')
        branches = $branches
        tags = $tags
        recent_commits = $recentCommits
        top_directories = $topDirs
        file_count = $fileCount
        workflows = $workflows
        manifests = @($manifests)
        readme = if ($readme) { Get-TextPreview -Path $readme.FullName -MaxChars 24000 } else { $null }
        license_file = if ($license) { $license.Name } else { $null }
        license_text = if ($license) { Get-TextPreview -Path $license.FullName -MaxChars 12000 } else { $null }
    }
}

$results | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $OutputPath -Encoding UTF8
Write-Output "Inspected $($results.Count) repositories -> $OutputPath"
