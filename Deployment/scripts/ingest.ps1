<#
.SYNOPSIS
    Add documents to a deployed AI Based Chatbot: upload files to the documents container and start the search indexer.

.DESCRIPTION
    PowerShell equivalent of ingest.sh. Uses your az login identity (Microsoft Entra ID; storage keys are disabled),
    which needs Storage Blob Data Contributor and Search Service Contributor (deploy.ps1 grants both to the deployer).

.EXAMPLE
    ./Deployment/scripts/ingest.ps1 -ResourceGroup rg-chatbot-dev -Path ./policies, ./faq.pdf
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)] [string] $ResourceGroup,
    [Parameter(Mandatory)] [string[]] $Path,
    [string] $DeploymentName
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$InformationPreference = 'Continue'
$Extensions = @('.pdf', '.docx', '.pptx', '.xlsx', '.txt', '.md', '.html', '.htm', '.csv', '.json')

if (-not $DeploymentName) {
    # Default: the latest chatbot deployment in the resource group.
    $DeploymentName = & az deployment group list -g $ResourceGroup --query "sort_by([?properties.outputs.webAppName], &properties.timestamp)[-1].name" -o tsv
    if (-not $DeploymentName) { throw "No chatbot deployment found in $ResourceGroup." }
}
$ShowDeployment = @('deployment', 'group', 'show', '-g', $ResourceGroup, '-n', $DeploymentName)
function Get-Output([string] $Name) {
    $value = & az @ShowDeployment --query "properties.outputs.$Name.value" -o tsv
    if ($LASTEXITCODE -ne 0) { throw "Deployment $DeploymentName not found in $ResourceGroup (use -DeploymentName for another environment)." }
    $value
}
$Account = Get-Output 'storageAccountName'
$Container = Get-Output 'documentsContainer'
$Search = Get-Output 'searchEndpoint'
$Indexer = Get-Output 'searchIndexerName'

# `pwsh -File ingest.ps1 -Path a,b` passes one string; split it unless it is itself an existing path.
$Path = @($Path | ForEach-Object { if (Test-Path -LiteralPath $_) { $_ } else { $_ -split ',' } })
$files = foreach ($p in $Path) {
    if (Test-Path $p -PathType Container) {
        Get-ChildItem -Path $p -File | Where-Object { $Extensions -contains $_.Extension.ToLowerInvariant() }
    } elseif (Test-Path $p -PathType Leaf) {
        $item = Get-Item $p
        if ($Extensions -contains $item.Extension.ToLowerInvariant()) { $item } else { Write-Information "Skipping $p (unsupported type)" }
    } else {
        throw "Not found: $p"
    }
}
if (-not $files) { throw "No supported files found ($($Extensions -join ' '))." }

# Role assignments made by a fresh deployment can take a few minutes to apply; retry.
function Invoke-WithRetry([scriptblock] $Action) {
    foreach ($attempt in 1..10) {
        if (& $Action) { return }
        Write-Information "  not ready yet (attempt $attempt/10); waiting 30 s (new role assignments take a few minutes)"
        Start-Sleep -Seconds 30
    }
    throw 'Gave up after 10 attempts.'
}

Write-Information "==> Uploading $(@($files).Count) file(s) to $Account/$Container"
foreach ($f in $files) {
    Write-Information "  $($f.Name)"
    Invoke-WithRetry {
        & az storage blob upload --auth-mode login --account-name $Account -c $Container -f $f.FullName -n $f.Name --overwrite -o none --only-show-errors
        $LASTEXITCODE -eq 0
    }
}

Write-Information "==> Starting indexer $Indexer"
Invoke-WithRetry {
    $result = & az rest --method post --url "$Search/indexers/$Indexer/run?api-version=2024-07-01" --resource https://search.azure.com 2>&1
    if ($LASTEXITCODE -eq 0) { return $true }
    if ("$result" -match '409|already running') { Write-Information '  The indexer is already running; it picks up the new files on its next pass.'; return $true }
    if ("$result" -match '404') { Write-Information '  The indexer does not exist yet. The web app creates it on startup.' }
    else { Write-Information "$result" }
    $false
}
Write-Information 'Done. Documents are searchable after the indexer finishes (usually a few minutes).'
