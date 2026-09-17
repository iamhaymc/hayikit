$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Push-Location $ScriptDir

function Test-CommandExists {
    param([string]$Name)
    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

function Update-ProcessPath {
    $machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "$machinePath;$userPath;$env:Path"
}

function Install-Uv {
    if (Test-CommandExists "uv") {
        Write-Host "uv already installed: $(uv --version)"
        return
    }
    Write-Host "uv not found; installing it..."
    if (Test-CommandExists "winget") {
        winget install --id astral-sh.uv -e --accept-source-agreements --accept-package-agreements
    }
    elseif (Test-CommandExists "choco") {
        choco install uv -y
    }
    elseif (Test-CommandExists "cargo") {
        cargo install --locked uv
    }
    else {
        $installer = Join-Path $env:TEMP "uv-install.ps1"
        Invoke-WebRequest "https://astral.sh/uv/install.ps1" -OutFile $installer
        & $installer
        Remove-Item $installer -ErrorAction SilentlyContinue
    }
    Update-ProcessPath
    if (-not (Test-CommandExists "uv")) {
        throw "uv was installed but is not on PATH. Open a new terminal and run setup.ps1 again."
    }
}

function Install-Python {
    $found = uv python find 3.12 2>$null
    if ($LASTEXITCODE -eq 0 -and $found) {
        Write-Host "Python 3.12 available: $found"
    } else {
        Write-Host "Python 3.12 not found; installing it via uv..."
        uv python install 3.12
    }
    $venvPython = Join-Path $ScriptDir ".venv\Scripts\python.exe"
    if (-not (Test-Path $venvPython)) {
        Write-Host "Creating virtual environment in .venv..."
        uv venv --python 3.12 .venv
    }
    uv pip install --python $venvPython -e .
    Update-ProcessPath
    if (-not (Test-CommandExists "python")) {
        throw "Python was installed but is not on PATH. Open a new terminal and run setup.ps1 again."
    }
}

function Install-CCompiler {
    $compiler = @("cc", "clang", "gcc") | Where-Object { Test-CommandExists $_ } | Select-Object -First 1
    if ($compiler) {
        Write-Host "C compiler already installed."
        return
    }
    Write-Host "C compiler not found; installing LLVM/Clang..."
    if (Test-CommandExists "winget") {
        winget install --id LLVM.LLVM -e --accept-source-agreements --accept-package-agreements
    }
    elseif (Test-CommandExists "choco") {
        choco install llvm -y
    }
    else {
        Write-Host "WARNING: no winget or Chocolatey found; install LLVM/Clang or GCC manually."
        return
    }
    Update-ProcessPath
    if (-not (Test-CommandExists "clang")) {
        Write-Host "WARNING: LLVM was installed but clang is not on PATH. Open a new terminal and run setup.ps1 again."
    }
}

try {
    Install-Uv
    Install-Python
    Install-Clang
    Write-Host "Setup complete."
    Write-Host "Activate the environment with: .venv\Scripts\Activate.ps1"
}
catch {
    Write-Error "Setup failed: $_"
    exit 1
}
finally {
    Pop-Location
}