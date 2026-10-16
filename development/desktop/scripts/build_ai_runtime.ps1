param(
    [Parameter(Mandatory = $true)][string]$ResourceCache,
    [Parameter(Mandatory = $true)][string]$BaseExe,
    [Parameter(Mandatory = $true)][string]$OutputExe,
    [Parameter(Mandatory = $true)][string]$WorkDir,
    [Parameter(Mandatory = $true)][string]$VCRedistDir,
    [string]$InitialFeedPath,
    [string]$PythonCommand = 'python.exe'
)

$ErrorActionPreference = 'Stop'
$desktopRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$lockPath = Join-Path $desktopRoot 'resources\ai-watch\resource-lock.json'
$lock = Get-Content -Raw -LiteralPath $lockPath | ConvertFrom-Json
$cacheRoot = [IO.Path]::GetFullPath($ResourceCache)
$basePath = (Resolve-Path -LiteralPath $BaseExe).Path
$vcRedistRoot = (Resolve-Path -LiteralPath $VCRedistDir).Path
$outputPath = [IO.Path]::GetFullPath($OutputExe)
$workPath = [IO.Path]::GetFullPath($WorkDir)

if (Test-Path -LiteralPath $outputPath) { throw 'Output already exists; refusing to overwrite.' }
if (Test-Path -LiteralPath $workPath) { throw 'WorkDir already exists; use a new revision directory.' }
New-Item -ItemType Directory -Path $workPath | Out-Null

function Get-VerifiedAsset([string]$RelativePath, [string]$Url, [long]$Size, [string]$Sha256) {
    $target = Join-Path $cacheRoot $RelativePath
    $parent = Split-Path -Parent $target
    if (-not (Test-Path -LiteralPath $parent)) { New-Item -ItemType Directory -Path $parent | Out-Null }
    if (Test-Path -LiteralPath $target) {
        $item = Get-Item -LiteralPath $target
        $hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $target).Hash.ToLowerInvariant()
        if ($item.Length -ne $Size -or $hash -ne $Sha256.ToLowerInvariant()) {
            throw "Cached resource differs from resource-lock.json: $RelativePath"
        }
        return $target
    }

    $partial = "$target.download.$([guid]::NewGuid().ToString('N'))"
    try {
        Invoke-WebRequest -Uri $Url -OutFile $partial -TimeoutSec 900
        $item = Get-Item -LiteralPath $partial
        $hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $partial).Hash.ToLowerInvariant()
        if ($item.Length -ne $Size -or $hash -ne $Sha256.ToLowerInvariant()) {
            throw "Downloaded resource failed size or SHA-256 verification: $RelativePath"
        }
        if (Test-Path -LiteralPath $target) { throw "Resource appeared during download; refusing overwrite: $RelativePath" }
        Move-Item -LiteralPath $partial -Destination $target
        return $target
    } catch {
        if (Test-Path -LiteralPath $partial) { Remove-Item -LiteralPath $partial }
        throw
    }
}

$model = $lock.model
$llama = $lock.inference_runtime
$python = $lock.python_runtime
$modelPath = Get-VerifiedAsset (Join-Path 'model' $model.file) "https://huggingface.co/$($model.repository)/resolve/$($model.revision)/$($model.file)" $model.size_bytes $model.sha256
$llamaPath = Get-VerifiedAsset (Join-Path 'llama' $llama.asset) "https://github.com/$($llama.repository)/releases/download/$($llama.release)/$($llama.asset)" $llama.size_bytes $llama.sha256
$pythonUrl = "https://www.python.org/ftp/python/$($python.version)/$($python.asset)"
$pythonPath = Get-VerifiedAsset (Join-Path 'python' $python.asset) $pythonUrl $python.size_bytes $python.sha256
$pythonSbomUrl = "https://www.python.org/ftp/python/$($python.version)/$($python.sbom)"
$pythonSbomPath = Get-VerifiedAsset (Join-Path 'python' $python.sbom) $pythonSbomUrl $python.sbom_size_bytes $python.sbom_sha256

$payload = Join-Path $workPath 'payload'
$pythonExtract = Join-Path $payload 'python'
$llamaExtract = Join-Path $workPath 'llama-extracted'
New-Item -ItemType Directory -Path $payload, $pythonExtract, (Join-Path $payload 'worker\ai_radar'), (Join-Path $payload 'llama'), (Join-Path $payload 'model'), (Join-Path $payload 'schema'), (Join-Path $payload 'feed'), (Join-Path $payload 'licenses'), $llamaExtract | Out-Null

Expand-Archive -LiteralPath $pythonPath -DestinationPath $pythonExtract
$pythonFiles = Get-ChildItem -File -LiteralPath $pythonExtract
foreach ($file in $pythonFiles) {
    if ($file.Name -eq 'python.exe' -or $file.Name -eq 'LICENSE.txt' -or $file.Name -eq 'python313._pth' -or $file.Extension.ToLowerInvariant() -in @('.dll', '.pyd', '.zip', '.cat')) {
        continue
    }
    Remove-Item -LiteralPath $file.FullName
}
$pth = Join-Path $pythonExtract 'python313._pth'
if (-not (Test-Path -LiteralPath $pth)) { throw 'Expected Python 3.13 embeddable path file is missing.' }
[IO.File]::WriteAllText($pth, "python313.zip`r`n.`r`n..\worker`r`n", [Text.Encoding]::ASCII)
$workerSource = Join-Path $desktopRoot 'ai_radar'
foreach ($name in @('__init__.py', '__main__.py', 'core.py', 'sources.json', 'watch.py')) {
    Copy-Item -LiteralPath (Join-Path $workerSource $name) -Destination (Join-Path $payload "worker\ai_radar\$name")
}
$embeddedPython = Join-Path $pythonExtract 'python.exe'
$embeddedVersion = (& $embeddedPython --version 2>&1) -join ''
if ($embeddedVersion.Trim() -ne 'Python 3.13.16') { throw "Unexpected embedded Python version: $embeddedVersion" }
& $embeddedPython -B -c 'import json, sqlite3, ssl, urllib.request, ai_radar.watch; print("embedded worker runtime OK")'
if ($LASTEXITCODE -ne 0) { throw 'Embedded Python cannot import the packaged worker or collector runtime.' }

Copy-Item -LiteralPath $modelPath -Destination (Join-Path $payload "model\$($model.file)")
Copy-Item -LiteralPath (Join-Path $desktopRoot 'resources\ai-watch\summary-schema.json') -Destination (Join-Path $payload 'schema\summary-schema.json')
Copy-Item -LiteralPath (Join-Path $desktopRoot 'resources\ai-watch\licenses\Apache-2.0.txt') -Destination (Join-Path $payload 'licenses\Apache-2.0.txt')
Copy-Item -LiteralPath (Join-Path $desktopRoot 'resources\ai-watch\licenses\MIT-llama.cpp.txt') -Destination (Join-Path $payload 'licenses\MIT-llama.cpp.txt')
Copy-Item -LiteralPath (Join-Path $desktopRoot 'resources\ai-watch\licenses\LLVM-OpenMP.txt') -Destination (Join-Path $payload 'licenses\LLVM-OpenMP.txt')
Copy-Item -LiteralPath $pythonSbomPath -Destination (Join-Path $payload 'licenses\Python-3.13.16-EMBED-SBOM.json')
Copy-Item -LiteralPath (Join-Path $desktopRoot 'resources\ai-watch\licenses\Microsoft-VCRedist-NOTICE.md') -Destination (Join-Path $payload 'licenses\Microsoft-VCRedist-NOTICE.md')

Expand-Archive -LiteralPath $llamaPath -DestinationPath $llamaExtract
$runner = Get-ChildItem -File -LiteralPath $llamaExtract -Filter 'llama-completion.exe' | Select-Object -First 1
if (-not $runner) { throw 'Pinned llama.cpp package does not contain llama-completion.exe.' }
Copy-Item -LiteralPath $runner.FullName -Destination (Join-Path $payload 'llama\llama-completion.exe')
Get-ChildItem -File -LiteralPath $llamaExtract -Filter '*.dll' | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $payload "llama\$($_.Name)")
}
foreach ($runtime in $lock.msvc_runtime.files) {
    $runtimePath = Join-Path $vcRedistRoot $runtime.name
    if (-not (Test-Path -LiteralPath $runtimePath -PathType Leaf)) { throw "Licensed MSVC runtime file is missing: $($runtime.name)" }
    $runtimeItem = Get-Item -LiteralPath $runtimePath
    $runtimeHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $runtimePath).Hash.ToLowerInvariant()
    if ($runtimeItem.Length -ne $runtime.size_bytes -or $runtimeHash -ne $runtime.sha256.ToLowerInvariant()) {
        throw "MSVC runtime file differs from resource-lock.json: $($runtime.name)"
    }
    $runtimeDestination = Join-Path $payload "llama\$($runtime.name)"
    if (Test-Path -LiteralPath $runtimeDestination) {
        if ((Get-FileHash -Algorithm SHA256 -LiteralPath $runtimeDestination).Hash.ToLowerInvariant() -ne $runtime.sha256.ToLowerInvariant()) {
            throw "llama.cpp package conflicts with locked MSVC runtime: $($runtime.name)"
        }
    } else {
        Copy-Item -LiteralPath $runtimePath -Destination $runtimeDestination
    }
}

$pythonLicense = Join-Path $pythonExtract 'LICENSE.txt'
if (-not (Test-Path -LiteralPath $pythonLicense)) { throw 'Embedded Python license file is missing.' }

$seedPath = Join-Path $payload 'feed\initial-ai-intelligence.json'
if ($InitialFeedPath) {
    $seedSource = (Resolve-Path -LiteralPath $InitialFeedPath).Path
    $seed = Get-Content -Raw -LiteralPath $seedSource | ConvertFrom-Json
    if ($seed.version -ne 1 -or $seed.events -isnot [System.Array]) { throw 'Initial feed must be a version 1 feed.' }
    Copy-Item -LiteralPath $seedSource -Destination $seedPath
} else {
    $emptyFeed = @{ version = 1; generated_at = $null; health = @{}; failed_sources = @(); pending_sources = @(); events = @() } | ConvertTo-Json -Depth 5
    [IO.File]::WriteAllText($seedPath, $emptyFeed + "`n", [Text.UTF8Encoding]::new($false))
}

$modelCheck = Join-Path $PSScriptRoot 'test_ai_model.py'
$modelCheckArgs = @($modelCheck, '--embedded-worker', '--runner', (Join-Path $payload 'llama\llama-completion.exe'), '--model', (Join-Path $payload "model\$($model.file)"), '--schema', (Join-Path $payload 'schema\summary-schema.json'))
& $embeddedPython -B @modelCheckArgs
if ($LASTEXITCODE -ne 0) { throw "Offline local-model check failed with exit code $LASTEXITCODE." }

$packer = Join-Path $PSScriptRoot 'pack_ai_runtime.py'
$manifestOutput = Join-Path $workPath 'package-manifest.json'
$pythonArgs = @($packer, '--base-exe', $basePath, '--payload', $payload, '--resource-lock', $lockPath, '--output', $outputPath, '--manifest-output', $manifestOutput)
& $PythonCommand @pythonArgs
if ($LASTEXITCODE -ne 0) { throw "Single-file package creation failed with exit code $LASTEXITCODE." }
