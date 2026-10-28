<#
.SYNOPSIS
    One-command deployment of AI Based Chatbot: SKU validation, infrastructure (ARM) and application code.

.DESCRIPTION
    PowerShell equivalent of deploy.sh. Reads the SKU preset Deployment/ARM/parameters/<Environment>.parameters.json,
    validates it (and any -Set overrides), deploys azuredeploy.json, then packages and deploys the web app.

    Prerequisites: Azure CLI (az login) and Python 3 (python or python3 on PATH). The signed-in identity
    needs Owner (or Contributor + User Access Administrator) on the resource group.

    -DeploymentName defaults to chatbot-<Environment>. With -SkipInfra, the latest chatbot deployment in the
    resource group is used when that name does not exist (for example after a Deploy to Azure button deployment).

.EXAMPLE
    ./Deployment/scripts/deploy.ps1 -ResourceGroup rg-chatbot-dev -Environment dev -Seed

.EXAMPLE
    ./Deployment/scripts/deploy.ps1 -ResourceGroup rg-chatbot-prod -Environment prod -Set searchReplicaCount=3,chatCapacity=200

.NOTES
    -Param NAME=VALUE passes template parameters that are not printed or validated, for secrets such as
    uploadApiKey and demoPassword. The demo deployment from Azure Cloud Shell (run-from-package code
    deployment with management calls only) is in deploy.sh: Deployment/scripts/deploy.sh -e demo.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)] [string] $ResourceGroup,
    [Parameter(Mandatory)] [string] $Environment,
    [string] $Location,
    [string] $Subscription,
    [string] $DeploymentName,
    [string[]] $Set = @(),
    [string[]] $Param = @(),
    [switch] $Seed,
    [switch] $SkipInfra,
    [switch] $SkipCode,
    [switch] $SkipRegionCheck
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$InformationPreference = 'Continue'

$Root = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
$Scripts = Join-Path $Root 'Deployment/scripts'
$Params = Join-Path $Root "Deployment/ARM/parameters/$Environment.parameters.json"
if (-not (Test-Path $Params)) { throw "Parameters file not found: $Params" }
if (-not (Get-Command az -ErrorAction SilentlyContinue)) { throw 'Azure CLI (az) not found. Install: https://learn.microsoft.com/cli/azure/install-azure-cli' }
$Python = @('python3', 'python') | Where-Object { Get-Command $_ -ErrorAction SilentlyContinue } | Select-Object -First 1
if (-not $Python) { throw 'Python 3 not found (python3 or python).' }

function Assert-AzSucceeded([string] $Step) {
    # Native commands do not throw on failure; stop the deployment when az fails.
    if ($LASTEXITCODE -ne 0) { throw "$Step failed (az exit code $LASTEXITCODE)" }
}

if (-not $Location) {
    $file = Get-Content $Params -Raw | ConvertFrom-Json
    $Location = if ($file.parameters.PSObject.Properties['location']) { $file.parameters.location.value } else { 'centralindia' }
}
if ($Subscription) { & az account set --subscription $Subscription; Assert-AzSucceeded 'Selecting the subscription' }
& az account show -o none 2>$null
if ($LASTEXITCODE -ne 0) { throw "Run 'az login' first." }
Write-Information "Subscription: $(& az account show --query name -o tsv)"

$script:NewUploadKey = ''
$Deployment = if ($DeploymentName) { $DeploymentName } else { "chatbot-$Environment" }
if ($SkipInfra) {
    & az deployment group show -g $ResourceGroup -n $Deployment -o none 2>$null
    if ($LASTEXITCODE -ne 0) {
        $latest = & az deployment group list -g $ResourceGroup --query "sort_by([?properties.outputs.webAppName], &properties.timestamp)[-1].name" -o tsv 2>$null
        if (-not $latest) { throw "No chatbot deployment found in $ResourceGroup. Deploy the infrastructure first." }
        Write-Information "Using deployment $latest"
        $Deployment = $latest
    }
}
# Accept -Set a=1,b=2 from PowerShell and from `pwsh -File` (where it arrives as one string).
$Set = @($Set | ForEach-Object { $_ -split ',(?=[A-Za-z][A-Za-z0-9]*=)' } | Where-Object { $_ })
$SetArgs = @()
foreach ($item in $Set) { $SetArgs += @('--set', $item) }

if (-not $SkipInfra) {
    Write-Information '==> Validating the SKU selection'
    $check = @()
    if (-not $SkipRegionCheck) { $check += '--check-region' }
    & $Python (Join-Path $Scripts 'validate_skus.py') -e $Environment -l $Location @SetArgs @check
    if ($LASTEXITCODE -ne 0) { throw 'The SKU selection is not valid (see above).' }

    $userType = & az account show --query user.type -o tsv
    if ($userType -eq 'servicePrincipal') {
        $deployerId = & az ad sp show --id (& az account show --query user.name -o tsv) --query id -o tsv 2>$null
        $deployerType = 'ServicePrincipal'
    } else {
        $deployerId = & az ad signed-in-user show --query id -o tsv 2>$null
        $deployerType = 'User'
    }
    if (-not $deployerId) { Write-Information 'Note: could not resolve the deploying identity; skipping its data-access role assignments.'; $deployerId = '' }

    Write-Information "==> Resource group $ResourceGroup ($Location)"
    & az group create -n $ResourceGroup -l $Location -o none
    Assert-AzSucceeded 'Creating the resource group'

    # Secrets go in a private file, not on the command line. Key Vault keeps the stored upload key and
    # demo password when they are not passed again, so only the first deployment generates a key.
    $secretFile = New-TemporaryFile
    try {
        $secretParams = @{ parameters = @{} }
        foreach ($item in ($Param | ForEach-Object { $_ -split ',(?=[A-Za-z][A-Za-z0-9]*=)' } | Where-Object { $_ })) {
            $name, $value = $item -split '=', 2
            $secretParams.parameters[$name] = @{ value = $value }
        }
        & az deployment group show -g $ResourceGroup -n $Deployment -o none 2>$null
        if ($LASTEXITCODE -ne 0 -and -not $secretParams.parameters.ContainsKey('uploadApiKey')) {
            $bytes = [byte[]]::new(24)
            [System.Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
            $script:NewUploadKey = [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
            $secretParams.parameters.uploadApiKey = @{ value = $script:NewUploadKey }
        }
        $secretParams | ConvertTo-Json -Depth 5 | Set-Content -Path $secretFile -Encoding utf8

        $extra = @()
        if ($Set.Count -gt 0) { $extra = @('--parameters') + $Set }
        Write-Information "==> Deploying infrastructure with $(Split-Path $Params -Leaf) (about 10-15 minutes)"
        & az deployment group create -g $ResourceGroup -n $Deployment `
            --template-file (Join-Path $Root 'Deployment/ARM/azuredeploy.json') `
            --parameters "@$Params" `
            --parameters "@$secretFile" `
            --parameters "deployerPrincipalId=$deployerId" "deployerPrincipalType=$deployerType" `
            @extra -o none
        Assert-AzSucceeded 'The infrastructure deployment'
    } finally {
        Remove-Item $secretFile -Force -ErrorAction SilentlyContinue
    }
}

function Get-Output([string] $Name) {
    $value = & az deployment group show -g $ResourceGroup -n $Deployment --query "properties.outputs.$Name.value" -o tsv
    Assert-AzSucceeded "Reading deployment output $Name"
    $value
}
$WebApp = Get-Output 'webAppName'
$WebUrl = Get-Output 'webAppUrl'
$Vault = Get-Output 'keyVaultName'

if (-not $SkipCode) {
    Write-Information '==> Packaging'
    & $Python (Join-Path $Scripts 'package.py')
    if ($LASTEXITCODE -ne 0) { throw 'Packaging failed.' }
    Write-Information "==> Deploying web app $WebApp (remote build, a few minutes)"
    & az webapp deploy -g $ResourceGroup -n $WebApp --src-path (Join-Path $Root 'build/webapp.zip') --type zip -o none
    Assert-AzSucceeded 'The code deployment'

    Write-Information "==> Waiting for $WebUrl/healthz"
    $state = ''
    foreach ($attempt in 1..40) {
        try { $state = (Invoke-RestMethod -Uri "$WebUrl/healthz" -TimeoutSec 10).searchSetup } catch { $state = '' }
        if ($state -in @('ready', 'failed')) { break }
        Start-Sleep -Seconds 15
    }
    switch ($state) {
        'ready' { Write-Information 'Web app is healthy and the search index is in place.' }
        'failed' { Write-Warning "The web app is up but could not create the search index. See $WebUrl/healthz and Application Insights." }
        default { Write-Warning "The web app did not report ready yet. Check $WebUrl/healthz in a few minutes." }
    }
}

if ($Seed) {
    & (Join-Path $Scripts 'ingest.ps1') -ResourceGroup $ResourceGroup -DeploymentName $Deployment -Path (Join-Path $Root 'Data/sample_docs')
}

Write-Information ''
Write-Information "Done. Chat page: $WebUrl"
if ($script:NewUploadKey) {
    Write-Information "Admin key:       $($script:NewUploadKey)"
    Write-Information "                 Only for deleting documents through the API; adding documents needs no key."
    Write-Information "                 Save it now. It is stored in Key Vault $Vault as upload-api-key."
} else {
    Write-Information "Admin key:       Key Vault $Vault, secret upload-api-key (unchanged; only for deleting documents)."
}
