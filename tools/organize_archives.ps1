[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$workspaceRoot = (Resolve-Path -LiteralPath (Join-Path $projectRoot "..")).Path

if ($projectRoot -ne "C:\ProyectoBotV4\polymarket_quant_bot") {
    throw "Raíz de proyecto inesperada: $projectRoot"
}
if ($workspaceRoot -ne "C:\ProyectoBotV4") {
    throw "Raíz de workspace inesperada: $workspaceRoot"
}

$moves = @(
    @{ Source = Join-Path $workspaceRoot "polymarket_quant_bot_fase1_3_v0_4.zip"; Destination = Join-Path $workspaceRoot "archives\legacy_packages\polymarket_quant_bot_fase1_3_v0_4.zip" },
    @{ Source = Join-Path $workspaceRoot "polymarket_quant_bot_fase1_3_v0_4_1.zip"; Destination = Join-Path $workspaceRoot "archives\legacy_packages\polymarket_quant_bot_fase1_3_v0_4_1.zip" },
    @{ Source = Join-Path $workspaceRoot "polymarket_quant_bot_fase2_piloto_v0_5_0.zip"; Destination = Join-Path $workspaceRoot "archives\legacy_packages\polymarket_quant_bot_fase2_piloto_v0_5_0.zip" },
    @{ Source = Join-Path $workspaceRoot "polymarket_quant_bot_fase2_completa_v0_5_1.zip"; Destination = Join-Path $workspaceRoot "archives\legacy_packages\polymarket_quant_bot_fase2_completa_v0_5_1.zip" },
    @{ Source = Join-Path $workspaceRoot "polymarket_quant_bot_fase3_gold_v0_6_0.zip"; Destination = Join-Path $workspaceRoot "archives\legacy_packages\polymarket_quant_bot_fase3_gold_v0_6_0.zip" },
    @{ Source = Join-Path $workspaceRoot "polymarket_quant_bot_fase4_1_shadow_v0_8_0.zip"; Destination = Join-Path $workspaceRoot "archives\legacy_packages\polymarket_quant_bot_fase4_1_shadow_v0_8_0.zip" },
    @{ Source = Join-Path $workspaceRoot "AUDITORIA_BALTHAZAR_X.md"; Destination = Join-Path $workspaceRoot "archives\research\balthazar\AUDITORIA_BALTHAZAR_X.md" },
    @{ Source = Join-Path $projectRoot "acelerador_forward_v094.zip"; Destination = Join-Path $projectRoot "archive\packages\acelerador_forward_v094.zip" },
    @{ Source = Join-Path $projectRoot "export_72h_muestra.zip"; Destination = Join-Path $projectRoot "archive\packages\export_72h_muestra.zip" },
    @{ Source = Join-Path $projectRoot "parche_watchdog_v094a1.zip"; Destination = Join-Path $projectRoot "archive\packages\parche_watchdog_v094a1.zip" },
    @{ Source = Join-Path $projectRoot "polymarket_bot_fase42_v0.9.0_actualizacion.zip"; Destination = Join-Path $projectRoot "archive\packages\polymarket_bot_fase42_v0.9.0_actualizacion.zip" },
    @{ Source = Join-Path $projectRoot "polymarket_bot_fase42_v0.9.1_actualizacion.zip"; Destination = Join-Path $projectRoot "archive\packages\polymarket_bot_fase42_v0.9.1_actualizacion.zip" },
    @{ Source = Join-Path $projectRoot "polymarket_bot_fase42_v0.9.3_actualizacion.zip"; Destination = Join-Path $projectRoot "archive\packages\polymarket_bot_fase42_v0.9.3_actualizacion.zip" },
    @{ Source = Join-Path $projectRoot "prueba_rtds_twap_fase42.zip"; Destination = Join-Path $projectRoot "archive\packages\prueba_rtds_twap_fase42.zip" },
    @{ Source = Join-Path $projectRoot "prueba_rtds_twap_oficial_fase42.zip"; Destination = Join-Path $projectRoot "archive\packages\prueba_rtds_twap_oficial_fase42.zip" },
    @{ Source = Join-Path $projectRoot "execution_collector_v012_pre_reintentos_http.bak"; Destination = Join-Path $projectRoot "archive\source_backups\execution_collector_v012_pre_reintentos_http.bak" },
    @{ Source = Join-Path $projectRoot "execution_collector_v012_pre_tick_dinamico.bak"; Destination = Join-Path $projectRoot "archive\source_backups\execution_collector_v012_pre_tick_dinamico.bak" },
    @{ Source = Join-Path $projectRoot "0"; Destination = Join-Path $projectRoot "archive\unknown_artifacts\0" },
    @{ Source = Join-Path $projectRoot "de"; Destination = Join-Path $projectRoot "archive\unknown_artifacts\de" },
    @{ Source = Join-Path $projectRoot "lo"; Destination = Join-Path $projectRoot "archive\unknown_artifacts\lo" }
)

$allowedDestinationRoots = @(
    (Join-Path $workspaceRoot "archives"),
    (Join-Path $projectRoot "archive")
)
$results = @()

foreach ($item in $moves) {
    $source = [System.IO.Path]::GetFullPath($item.Source)
    $destination = [System.IO.Path]::GetFullPath($item.Destination)
    $destinationAllowed = $false
    foreach ($allowedRoot in $allowedDestinationRoots) {
        $normalizedRoot = [System.IO.Path]::GetFullPath($allowedRoot).TrimEnd('\') + '\'
        if ($destination.StartsWith($normalizedRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
            $destinationAllowed = $true
            break
        }
    }
    if (-not $destinationAllowed) {
        throw "Destino fuera de las carpetas permitidas: $destination"
    }

    $sourceExists = Test-Path -LiteralPath $source -PathType Leaf
    $destinationExists = Test-Path -LiteralPath $destination -PathType Leaf
    if ($sourceExists -and $destinationExists) {
        throw "Conflicto: existen origen y destino: $source | $destination"
    }
    if (-not $sourceExists -and -not $destinationExists) {
        $results += [ordered]@{ source = $source; destination = $destination; status = "not_found" }
        continue
    }
    if ($destinationExists) {
        $file = Get-Item -LiteralPath $destination
        $results += [ordered]@{
            source = $source
            destination = $destination
            status = "already_moved"
            size_bytes = $file.Length
            sha256 = (Get-FileHash -LiteralPath $destination -Algorithm SHA256).Hash.ToLowerInvariant()
        }
        continue
    }

    $sourceFile = Get-Item -LiteralPath $source
    $hash = (Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash.ToLowerInvariant()
    $destinationDirectory = Split-Path -Parent $destination
    New-Item -ItemType Directory -Path $destinationDirectory -Force | Out-Null
    Move-Item -LiteralPath $source -Destination $destination
    $destinationHash = (Get-FileHash -LiteralPath $destination -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($hash -ne $destinationHash) {
        throw "La verificación SHA256 falló para: $destination"
    }
    $results += [ordered]@{
        source = $source
        destination = $destination
        status = "moved"
        size_bytes = $sourceFile.Length
        sha256 = $destinationHash
    }
}

$manifestPath = Join-Path $workspaceRoot "archives\archive_move_manifest.json"
$manifest = [ordered]@{
    schema_version = "1.0.0"
    generated_utc = [DateTime]::UtcNow.ToString("o")
    operation = "non_destructive_archive_move"
    items = $results
}
$manifest | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $manifestPath -Encoding utf8
$manifest | ConvertTo-Json -Depth 5
