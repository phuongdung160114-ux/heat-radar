param([switch]$CoreOnly,[switch]$NoLaunch)
$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$SourceRoot = Split-Path $PSScriptRoot -Parent
$HomeDir = Join-Path $env:LOCALAPPDATA 'AshareHeatRadar'
$AppDir = Join-Path $HomeDir 'app'
$DataDir = Join-Path $HomeDir 'data'
$RuntimeDir = Join-Path $HomeDir 'python312'
$VenvDir = Join-Path $HomeDir 'venv'
$CacheDir = Join-Path $HomeDir 'cache'
$LogDir = Join-Path $HomeDir 'logs'
if (Test-Path -LiteralPath (Join-Path $SourceRoot '.venv\Scripts\python.exe')) {
    $HomeDir = $SourceRoot
    $AppDir = $SourceRoot
    $DataDir = Join-Path $SourceRoot 'data'
    $RuntimeDir = Join-Path $SourceRoot 'python312'
    $VenvDir = Join-Path $SourceRoot '.venv'
    $CacheDir = Join-Path $SourceRoot 'cache'
    $LogDir = Join-Path $DataDir 'logs'
}
foreach ($path in @($HomeDir,$DataDir,$CacheDir,$LogDir)) { New-Item -ItemType Directory -Force -Path $path | Out-Null }
$LogFile = Join-Path $LogDir ('install-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.log')
Start-Transcript -Path $LogFile | Out-Null
function Invoke-Checked([string]$Program,[string[]]$Arguments) {
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) { throw ('Command failed, exit ' + $LASTEXITCODE + ': ' + $Program + ' ' + ($Arguments -join ' ')) }
}
function Download-Verified([string]$Url,[string]$Destination,[string]$Digest) {
    if (Test-Path $Destination) {
        if ((Get-FileHash -Algorithm MD5 $Destination).Hash -eq $Digest) { return }
        Remove-Item $Destination -Force
    }
    for ($i=0;$i -lt 3;$i++) {
        try {
            Write-Host ('Downloading from official source: ' + $Url)
            Invoke-WebRequest -UseBasicParsing -Uri $Url -OutFile ($Destination+'.part') -TimeoutSec 240
            if ((Get-FileHash -Algorithm MD5 ($Destination+'.part')).Hash -ne $Digest) { throw 'Official release checksum mismatch; file will not execute.' }
            Move-Item ($Destination+'.part') $Destination -Force
            return
        } catch {
            if (Test-Path ($Destination+'.part')) { Remove-Item ($Destination+'.part') -Force }
            if ($i -eq 2) { throw }
            Start-Sleep -Seconds (2*($i+1))
        }
    }
}
function Is-Compatible([string]$Path) {
    if (-not $Path -or -not (Test-Path $Path) -or $Path -match '\\WindowsApps\\') { return $false }
    try {
        & $Path -c 'import sys,struct;sys.exit(0 if (3,11)<=sys.version_info[:2]<=(3,13) and struct.calcsize(chr(80))==8 else 1)' 2>$null
        return ($LASTEXITCODE -eq 0)
    } catch { return $false }
}
try {
    if (-not [Environment]::Is64BitOperatingSystem) { throw 'Windows x64 is required. This package does not install 32-bit dependencies.' }
    if ($env:PROCESSOR_ARCHITECTURE -eq 'ARM64') { throw 'This installer targets Windows x64. On ARM64 use an existing compatible Python and the documented manual setup.' }
    Write-Host 'HEAT RADAR 2.0 Installer' -ForegroundColor Cyan
    if (([IO.Path]::GetFullPath($SourceRoot)).TrimEnd('\') -ne ([IO.Path]::GetFullPath($AppDir)).TrimEnd('\')) {
        New-Item -ItemType Directory -Force -Path $AppDir | Out-Null
        foreach ($item in Get-ChildItem -LiteralPath $SourceRoot -Force) {
            if ($item.Name -in @('data','.venv','venv','__pycache__','.git','.pytest_cache')) { continue }
            Copy-Item -LiteralPath $item.FullName -Destination $AppDir -Recurse -Force
        }
    }
    $Python = Join-Path $RuntimeDir 'python.exe'
    if (-not (Is-Compatible $Python)) {
        $Python = $null
        $Launcher = Get-Command py.exe -ErrorAction SilentlyContinue
        if ($Launcher) {
            foreach ($version in @('-3.13','-3.12','-3.11')) {
                try { $candidate = & $Launcher.Source $version -c 'import sys; print(sys.executable)' 2>$null }
                catch { continue }
                if ($LASTEXITCODE -eq 0 -and (Is-Compatible ([string]$candidate))) { $Python=[string]$candidate; break }
            }
        }
        if (-not $Python) {
            foreach ($hive in @('HKCU:\Software\Python\PythonCore','HKLM:\Software\Python\PythonCore')) {
                foreach ($ver in @('3.13','3.12','3.11')) {
                    $key=Join-Path $hive ($ver+'\InstallPath')
                    if (Test-Path $key) {
                        $base=(Get-Item -LiteralPath $key).GetValue('')
                        if ($base) {
                            $candidate=Join-Path $base 'python.exe'
                            if (Is-Compatible $candidate) { $Python=$candidate; break }
                        }
                    }
                }
                if ($Python) { break }
            }
        }
        if (-not $Python) {
            $candidateCommand = Get-Command python.exe -ErrorAction SilentlyContinue
            if ($candidateCommand -and (Is-Compatible $candidateCommand.Source)) { $Python=$candidateCommand.Source }
        }
    }
    if (-not $Python) {
        $Installer = Join-Path $CacheDir 'python-3.12.10-amd64.exe'
        Download-Verified 'https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe' $Installer '5eddb0b6f12c852725de071ae681dde4'
        $Signature = Get-AuthenticodeSignature -FilePath $Installer
        if ($Signature.Status -ne 'Valid' -or $Signature.SignerCertificate.Subject -notmatch 'Python Software Foundation') { throw 'Official Python signature could not be validated. Installation stopped safely.' }
        $Arguments = '/quiet InstallAllUsers=0 Include_pip=1 Include_test=0 Include_doc=0 Include_launcher=0 InstallLauncherAllUsers=0 PrependPath=0 AssociateFiles=0 Shortcuts=0 TargetDir="'+$RuntimeDir+'"'
        $Process = Start-Process -FilePath $Installer -ArgumentList $Arguments -WindowStyle Hidden -Wait -PassThru
        if ($Process.ExitCode -notin @(0,3010)) { throw ('Python installer failed with code '+$Process.ExitCode) }
        $Python = Join-Path $RuntimeDir 'python.exe'
        if (-not (Is-Compatible $Python)) { throw 'Private Python was not created. Check existing Python installer registration and the installation log.' }
    }
    Write-Host ('Using Python: '+$Python)
    $Python | Set-Content -Encoding UTF8 (Join-Path $HomeDir 'runtime-path.txt')
    $VenvPython = Join-Path $VenvDir 'Scripts\python.exe'
    if (-not (Test-Path $VenvPython)) { Invoke-Checked $Python @('-m','venv',$VenvDir) }
    $env:PYTHONUTF8='1'; $env:PYTHONIOENCODING='utf-8'; $env:PIP_DISABLE_PIP_VERSION_CHECK='1'
    Invoke-Checked $VenvPython @('-m','pip','install','--upgrade','pip','setuptools','wheel','--retries','2','--timeout','40')
    $Req=Join-Path $AppDir 'requirements.txt'
    if ($CoreOnly) { $Req=Join-Path $AppDir 'requirements-core.txt' }
    Invoke-Checked $VenvPython @('-m','pip','install','--upgrade','-r',$Req,'--retries','2','--timeout','40')
    Invoke-Checked $VenvPython @('-m','pip','check')
    & $VenvPython -m pip freeze | Set-Content -Encoding UTF8 (Join-Path $LogDir 'installed-packages.txt')
    $Shell=New-Object -ComObject WScript.Shell
    $Shortcut=$Shell.CreateShortcut((Join-Path ([Environment]::GetFolderPath('Desktop')) 'Ashare Heat Radar.lnk'))
    $Shortcut.TargetPath=$VenvPython; $Shortcut.Arguments='"'+(Join-Path $AppDir 'run.py')+'" --data-dir "'+$DataDir+'"'
    $Shortcut.WorkingDirectory=$AppDir; $Shortcut.Description='Local A-share attention acceleration monitor'; $Shortcut.Save()
    Write-Host ''
    Write-Host 'Installation complete.' -ForegroundColor Green
    Write-Host ('App: '+$AppDir); Write-Host ('Data: '+$DataDir); Write-Host ('Log: '+$LogFile)
    if (-not $NoLaunch) { Start-Process -FilePath $VenvPython -WorkingDirectory $AppDir -WindowStyle Hidden -ArgumentList ('"'+(Join-Path $AppDir 'run.py')+'" --data-dir "'+$DataDir+'"') }
    Stop-Transcript | Out-Null
    exit 0
} catch {
    Write-Host ('INSTALLATION STOPPED: '+$_.Exception.Message) -ForegroundColor Red
    Write-Host ('See log: '+$LogFile)
    Stop-Transcript | Out-Null
    exit 1
}
