param(
    [string]$Owner = "joshstevens19",
    [string]$OutputDirectory = "data/github_forensics_20260911"
)

$ErrorActionPreference = "Stop"
$headers = @{
    "User-Agent" = "PolyLedger-GitHub-Forensics"
    "Accept" = "application/vnd.github+json"
    "X-GitHub-Api-Version" = "2022-11-28"
}

$output = [System.IO.Path]::GetFullPath((Join-Path (Get-Location) $OutputDirectory))
New-Item -ItemType Directory -Path $output -Force | Out-Null

$reposUrl = "https://api.github.com/users/$Owner/repos?per_page=100&type=owner&sort=full_name"
$repoResponse = Invoke-RestMethod -Headers $headers -Uri $reposUrl
$repos = if ($repoResponse.PSObject.Properties.Name -contains "value") {
    @($repoResponse.value)
} else {
    @($repoResponse)
}
$repos | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath (Join-Path $output "joshstevens19_repositories_raw.json") -Encoding utf8

$inventory = foreach ($repo in $repos) {
    Write-Host "metadata $($repo.name)"
    $languages = $null
    try {
        $languages = Invoke-RestMethod -Headers $headers -Uri $repo.languages_url
    }
    catch {
        $languages = [pscustomobject]@{}
    }

    $refs = @()
    try {
        $refs = @(git ls-remote --heads --tags $repo.clone_url 2>$null)
    }
    catch {
        $refs = @()
    }
    $branches = @($refs | Where-Object { $_ -match "refs/heads/" }).Count
    $tags = @($refs | Where-Object { $_ -match "refs/tags/" -and $_ -notmatch "\^\{\}$" }).Count
    $languageMap = @{}
    if ($languages) {
        foreach ($property in $languages.PSObject.Properties) {
            $languageMap[$property.Name] = $property.Value
        }
    }
    $additional = @($languageMap.Keys | Where-Object { $_ -ne $repo.language } | Sort-Object)
    $topics = @($repo.topics)
    $daysSincePush = if ($repo.pushed_at) {
        [math]::Floor(((Get-Date).ToUniversalTime() - [datetime]$repo.pushed_at).TotalDays)
    } else { $null }
    $activity = if ($null -eq $daysSincePush) { "unknown" }
        elseif ($daysSincePush -le 90) { "high" }
        elseif ($daysSincePush -le 365) { "medium" }
        elseif ($daysSincePush -le 1095) { "low" }
        else { "dormant" }

    [pscustomobject]@{
        repository_name = $repo.name
        url = $repo.html_url
        description = $repo.description
        creation_date = $repo.created_at
        last_update = $repo.updated_at
        last_push_proxy = $repo.pushed_at
        last_meaningful_commit = "Requires Tier S/A/B history review"
        stars = $repo.stargazers_count
        forks = $repo.forks_count
        watchers = $repo.subscribers_count
        primary_language = $repo.language
        additional_languages = ($additional -join ";")
        language_bytes_json = ($languageMap | ConvertTo-Json -Compress)
        archived = $repo.archived
        fork = $repo.fork
        fork_source = if ($repo.fork) { "Requires repository-detail validation" } else { "original" }
        license = if ($repo.license) { $repo.license.spdx_id } else { "NOASSERTION" }
        topics = ($topics -join ";")
        default_branch = $repo.default_branch
        branch_count = $branches
        tag_count = $tags
        releases = "Validated separately for Tier S/A/B"
        size_kb = $repo.size
        open_issues = $repo.open_issues_count
        activity_level = $activity
        api_source = $repo.url
        date_accessed = (Get-Date).ToString("yyyy-MM-dd")
    }
}

$inventory = @($inventory | Where-Object { $_ -isnot [string] })
$inventory | Export-Csv -LiteralPath (Join-Path $output "github_repository_inventory.csv") -NoTypeInformation -Encoding utf8
$inventory | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath (Join-Path $output "github_repository_inventory.json") -Encoding utf8

$rate = Invoke-RestMethod -Headers $headers -Uri "https://api.github.com/rate_limit"
$rate | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath (Join-Path $output "github_rate_limit_after_inventory.json") -Encoding utf8

Write-Output "completed=$($inventory.Count) output=$output"
