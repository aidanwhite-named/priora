# Priora 설치 스크립트. install.bat 이 부른다 (직접 실행: powershell -ExecutionPolicy Bypass -File tools\install.ps1).
#
# 하는 일: 환경 점검 → Python 3.11+ 확인(없으면 설치) → .venv 와 PyMuPDF → LLM CLI 확인(없으면 설치)
#          → .env 준비 → 포트 점검 → 바탕화면 바로가기. 다시 실행하면 고치기·의존성 업데이트로 동작한다.
# 프로그램 폴더 밖에 남기는 것: 바탕화면 바로가기 하나, 그리고 사용자가 고른 Python·CLI 설치.
# Windows PowerShell 5.1 에서 돌아야 하므로 7 전용 문법(삼항, ??, &&)을 쓰지 않는다.

$ErrorActionPreference = 'Stop'
$MinPython = [version]'3.11'
$PortFirst = 8765
$PortLast = 8774

# --- 인자 · 경로 ---------------------------------------------------------------
$Root = "$env:PRIORA_ROOT"
if (-not $Root) { $Root = Split-Path -Parent $PSScriptRoot }
$Root = $Root.TrimEnd('\')
$Opt = @{ yes = $false; cli = $null; noshortcut = $false }
foreach ($tok in ("$env:PRIORA_ARGS" -split '\s+')) {
    $t = $tok.Trim().TrimStart('/', '-').ToLower()
    if (-not $t) { continue }
    if ($t -eq 'y' -or $t -eq 'yes') { $Opt.yes = $true }
    elseif ($t -eq 'noshortcut') { $Opt.noshortcut = $true }
    elseif ($t -match '^cli[:=](.*)$') { $Opt.cli = $Matches[1] }
    else { Write-Host "알 수 없는 옵션: $tok (쓸 수 있는 옵션: /y  /cli:claude,codex,agy  /cli:none  /noshortcut)" -ForegroundColor Yellow }
}
$LogFile = Join-Path $Root 'install.log'
$VenvDir = Join-Path $Root '.venv'
$VenvPy = Join-Path $VenvDir 'Scripts\python.exe'
$Warnings = New-Object System.Collections.Generic.List[string]
$State = @{ python_installed = $false; clis_installed = @(); shortcut = '' }

# --- 출력 · 기록 ---------------------------------------------------------------
function Log([string]$Text) {
    try { Add-Content -LiteralPath $LogFile -Value $Text -Encoding UTF8 } catch { }
}
function Say([string]$Text = '', [string]$Color = '') {
    if ($Color) { Write-Host $Text -ForegroundColor $Color } else { Write-Host $Text }
    Log $Text
}
function Step([string]$Text) { Say ''; Say $Text 'Cyan' }
function Ok([string]$Text) { Say "  [확인] $Text" 'Green' }
function Info([string]$Text) { Say "  $Text" }
function Warn([string]$Text) { Say "  [주의] $Text" 'Yellow'; $Warnings.Add($Text) }
function Fail([string]$Text) { throw "PRIORA_FATAL: $Text" }

function Ask([string]$Question, [bool]$Default) {
    $hint = '[y/N]'
    if ($Default) { $hint = '[Y/n]' }
    if ($Opt.yes) { Log "  (자동) $Question -> $Default"; return $Default }
    $answer = "$(Read-Host "  $Question $hint")".Trim().ToLower()
    Log "  (질문) $Question -> '$answer'"
    if (-not $answer) { return $Default }
    return $answer.StartsWith('y')
}

# --- 프로그램 실행 -------------------------------------------------------------
function Quote-Arg([string]$Value) {
    if ($Value -notmatch '[\s"]' -and $Value -ne '') { return $Value }
    return '"' + ($Value -replace '(\\*)"', '$1$1\"' -replace '(\\+)$', '$1$1') + '"'
}

# 짧은 확인용: 출력을 받아 온다. 시간이 넘으면 죽이고 Code = $null.
function Invoke-Captured([string]$Exe, [string[]]$Arguments, [int]$TimeoutSec = 30, [string]$WorkDir = '') {
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $Exe
    $psi.Arguments = ($Arguments | ForEach-Object { Quote-Arg $_ }) -join ' '
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.RedirectStandardInput = $true
    $psi.StandardOutputEncoding = [Text.Encoding]::UTF8
    $psi.StandardErrorEncoding = [Text.Encoding]::UTF8
    if ($WorkDir) { $psi.WorkingDirectory = $WorkDir }
    try { $p = [System.Diagnostics.Process]::Start($psi) } catch { return @{ Code = $null; Out = ''; Err = "$($_.Exception.Message)" } }
    $p.StandardInput.Close()
    $out = $p.StandardOutput.ReadToEndAsync()
    $err = $p.StandardError.ReadToEndAsync()
    if (-not $p.WaitForExit($TimeoutSec * 1000)) {
        Stop-Tree $p.Id
        return @{ Code = $null; Out = ''; Err = "응답 시간 초과 (${TimeoutSec}초)" }
    }
    $p.WaitForExit()
    return @{ Code = $p.ExitCode; Out = "$($out.Result)".Trim(); Err = "$($err.Result)".Trim() }
}

# 오래 걸리는 설치용: 진행 상황을 화면과 install.log 에 그대로 흘린다. 종료 코드를 돌려준다.
function Invoke-Visible([string]$Exe, [string[]]$Arguments) {
    Log "  > $Exe $($Arguments -join ' ')"
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $Exe @Arguments 2>&1 | ForEach-Object {
            $line = "$_".TrimEnd()
            if ($line) { Write-Host "    $line" -ForegroundColor DarkGray; Log "    $line" }
        }
        return $LASTEXITCODE
    } catch {
        Log "    실행 실패: $($_.Exception.Message)"
        return -1
    } finally { $ErrorActionPreference = $old }
}

function Find-Exe([string]$Name) {
    $cmd = Get-Command $Name -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($cmd) { return $cmd.Source }
    return ''
}

# 방금 설치한 프로그램은 이 창의 PATH 에 없다. 레지스트리의 PATH 와 CLI 들이 깔리는 폴더를 다시 읽어 붙인다.
function Update-Path {
    $extra = @(
        [Environment]::GetEnvironmentVariable('Path', 'Machine'),
        [Environment]::GetEnvironmentVariable('Path', 'User'),
        (Join-Path $env:USERPROFILE '.local\bin'),
        (Join-Path $env:APPDATA 'npm'),
        (Join-Path $env:LOCALAPPDATA 'agy\bin')
    ) -join ';'
    $seen = @{}
    $parts = @()
    foreach ($p in ("$env:Path;$extra" -split ';')) {
        $p = $p.Trim()
        if ($p -and -not $seen.ContainsKey($p.ToLower())) { $seen[$p.ToLower()] = 1; $parts += $p }
    }
    $env:Path = $parts -join ';'
}

function Test-Online([string]$Url) {
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -Uri $Url -Method Head -UseBasicParsing -TimeoutSec 15 | Out-Null
        return $true
    } catch {
        Log "  연결 실패 $Url : $($_.Exception.Message)"
        return $false
    }
}

# --- Python --------------------------------------------------------------------
# 돌려주는 값: @{ Exe; Pre(앞에 붙일 인자); Version; Bits; Path } 또는 $null
function Test-Python([string]$Exe, [string[]]$Pre = @()) {
    $code = "import sys;print('PYOK|%d.%d.%d|%d|%s' % (sys.version_info[0], sys.version_info[1], sys.version_info[2], 64 if sys.maxsize > 2**32 else 32, sys.executable))"
    $r = Invoke-Captured $Exe ($Pre + @('-c', $code)) 25
    foreach ($line in ("$($r.Out)" -split "`n")) {
        if ($line -match '^PYOK\|([\d.]+)\|(\d+)\|(.+)$') {
            return @{ Exe = $Exe; Pre = $Pre; Version = [version]$Matches[1]; Bits = [int]$Matches[2]; Path = $Matches[3].Trim() }
        }
    }
    return $null
}

function Find-Python {
    $tried = @{}
    $found = @()
    $candidates = @()
    $py = Find-Exe 'py'
    if ($py) { $candidates += , @($py, '-3') }
    foreach ($name in 'python', 'python3') {
        foreach ($cmd in @(Get-Command $name -CommandType Application -All -ErrorAction SilentlyContinue)) { $candidates += , @($cmd.Source) }
    }
    foreach ($pattern in "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe", "$env:ProgramFiles\Python3*\python.exe") {
        foreach ($f in @(Get-Item $pattern -ErrorAction SilentlyContinue | Sort-Object Name -Descending)) { $candidates += , @($f.FullName) }
    }
    foreach ($c in $candidates) {
        $key = ($c -join ' ').ToLower()
        if ($tried.ContainsKey($key)) { continue }
        $tried[$key] = 1
        $pre = @()
        if ($c.Count -gt 1) { $pre = @($c[1..($c.Count - 1)]) }
        $info = Test-Python $c[0] $pre   # Microsoft Store 의 가짜 python.exe 는 여기서 걸러진다(실행해도 버전이 안 나온다)
        if ($info -and -not $tried.ContainsKey("path:$($info.Path.ToLower())")) {
            $tried["path:$($info.Path.ToLower())"] = 1
            $found += , $info
            Log "  Python 후보: $($info.Path) $($info.Version) $($info.Bits)bit"
        }
    }
    $good = @($found | Where-Object { $_.Version -ge $MinPython })
    $best = $good | Where-Object { $_.Bits -eq 64 } | Select-Object -First 1
    if (-not $best) { $best = $good | Select-Object -First 1 }
    return @{ Best = $best; All = $found }
}

function Install-Python {
    $winget = Find-Exe 'winget'
    if ($winget) {
        Info 'winget 으로 Python 3.13 을 설치합니다 (몇 분 걸릴 수 있습니다).'
        $code = Invoke-Visible $winget @('install', '-e', '--id', 'Python.Python.3.13', '--source', 'winget', '--silent',
            '--accept-package-agreements', '--accept-source-agreements')
        Update-Path
        $r = Find-Python
        if ($r.Best) { return $r.Best }
        Info "winget 설치가 끝났지만 Python 을 찾지 못했습니다 (종료 코드 $code). python.org 에서 직접 받아 봅니다."
    } else {
        Info 'winget 이 없어 python.org 에서 설치 파일을 직접 받습니다.'
    }
    $arch = 'amd64'
    if ($env:PROCESSOR_ARCHITECTURE -eq 'ARM64') { $arch = 'arm64' }
    $url = "https://www.python.org/ftp/python/3.13.5/python-3.13.5-$arch.exe"
    $dst = Join-Path $env:TEMP "priora-python-3.13.5-$arch.exe"
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
        Info "받는 중: $url"
        Invoke-WebRequest -Uri $url -OutFile $dst -UseBasicParsing -TimeoutSec 600
        $sig = Get-AuthenticodeSignature -LiteralPath $dst
        if ($sig.Status -ne 'Valid' -or "$($sig.SignerCertificate.Subject)" -notmatch 'Python Software Foundation') {
            Warn "받은 Python 설치 파일의 서명을 확인하지 못해 실행하지 않았습니다 ($($sig.Status))."
            return $null
        }
        Info '설치 중 (현재 사용자용, 관리자 권한 불필요)...'
        $p = Start-Process -FilePath $dst -ArgumentList '/quiet', 'InstallAllUsers=0', 'PrependPath=1', 'Include_test=0' -Wait -PassThru
        Log "  Python 설치 프로그램 종료 코드 $($p.ExitCode)"
    } catch {
        Warn "Python 설치 파일을 받거나 실행하지 못했습니다: $($_.Exception.Message)"
        return $null
    } finally {
        Remove-Item -LiteralPath $dst -Force -ErrorAction SilentlyContinue
    }
    Update-Path
    return (Find-Python).Best
}

# --- LLM CLI -------------------------------------------------------------------
# 설치 방법은 위에서부터 차례로 해 보고, 명령을 찾으면 멈춘다.
$Clis = @(
    @{ Id = 'claude'; Exe = 'claude'; Label = 'Claude Code'; Version = $true; Methods = @(
            @{ Kind = 'script'; Url = 'https://claude.ai/install.ps1' },
            @{ Kind = 'winget'; Package = 'Anthropic.ClaudeCode' },
            @{ Kind = 'npm'; Package = '@anthropic-ai/claude-code@latest' })
    },
    @{ Id = 'codex'; Exe = 'codex'; Label = 'Codex CLI'; Version = $true; Methods = @(
            @{ Kind = 'npm'; Package = '@openai/codex' },
            @{ Kind = 'script'; Url = 'https://chatgpt.com/codex/install.ps1' })
    },
    @{ Id = 'antigravity'; Exe = 'agy'; Label = 'Antigravity CLI'; Version = $false; Methods = @(
            @{ Kind = 'winget'; Package = 'Google.AntigravityCLI' },
            @{ Kind = 'script'; Url = 'https://antigravity.google/cli/install.ps1' })
    }
)

function Get-CliStatus($Cli) {
    $path = Find-Exe $Cli.Exe
    $version = ''
    if ($path -and $Cli.Version) {
        $r = Invoke-Captured $path @('--version') 25
        if ($r.Code -eq 0 -and $r.Out) { $version = ($r.Out -split "`n")[0].Trim() }
    }
    return @{ Path = $path; Version = $version }
}

function Install-Cli($Cli) {
    foreach ($m in $Cli.Methods) {
        $code = $null
        if ($m.Kind -eq 'script') {
            Info "설치 명령: irm $($m.Url) | iex"
            $command = "[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12; irm $($m.Url) | iex"
            $code = Invoke-Visible 'powershell' @('-NoLogo', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', $command)
        } elseif ($m.Kind -eq 'winget') {
            $winget = Find-Exe 'winget'
            if (-not $winget) { Log "  winget 없음 - $($m.Package) 건너뜀"; continue }
            Info "설치 명령: winget install -e --id $($m.Package)"
            $code = Invoke-Visible $winget @('install', '-e', '--id', $m.Package, '--source', 'winget', '--silent',
                '--accept-package-agreements', '--accept-source-agreements')
        } elseif ($m.Kind -eq 'npm') {
            $npm = Find-Exe 'npm'
            if (-not $npm) { Log "  npm 없음 - $($m.Package) 건너뜀"; continue }
            Info "설치 명령: npm install -g $($m.Package)"
            $code = Invoke-Visible $npm @('install', '-g', $m.Package)
        }
        Update-Path
        $path = Find-Exe $Cli.Exe
        if ($path) { return $path }
        Info "이 방법으로는 $($Cli.Exe) 명령이 생기지 않았습니다 (종료 코드 $code). 다른 방법을 시도합니다."
    }
    return ''
}

# --- 포트 ----------------------------------------------------------------------
function Test-Port([int]$Port) {
    $listener = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Loopback, $Port)
    try {
        $listener.ExclusiveAddressUse = $true
        $listener.Start()
        return 'free'
    } catch {
        $e = $_.Exception.GetBaseException()
        if ($e -is [System.Net.Sockets.SocketException] -and $e.SocketErrorCode -eq 'AccessDenied') { return 'denied' }
        return 'busy'
    } finally {
        try { $listener.Stop() } catch { }
    }
}

function Get-PortOwner([int]$Port) {
    try {
        $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop | Select-Object -First 1
        $proc = Get-CimInstance Win32_Process -Filter "ProcessId = $($conn.OwningProcess)" -ErrorAction Stop
        $priora = "$($proc.CommandLine)" -match 'patsearch'
        return @{ Name = "$($proc.Name)"; ProcessId = $conn.OwningProcess; Priora = $priora }
    } catch { return $null }
}

# 프로세스와 그 아래에서 돌던 것까지 끈다. 이미 꺼진 프로세스라도 오류로 치지 않는다.
function Stop-Tree([int]$ProcessId) {
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'SilentlyContinue'
    try { & taskkill /PID $ProcessId /T /F 2>&1 | Out-Null } catch { } finally { $ErrorActionPreference = $old }
}

# 이 폴더에서 띄운 Priora 만 찾는다. 다른 폴더에 깔린 Priora 는 건드리지 않는다.
# 판별: 화면 서버가 남긴 표시(.cache\server-<pid>.json), 이 폴더의 .venv 파이썬, 명령줄에 이 폴더 경로가 있는 실행.
function Get-PrioraProcesses {
    try { $all = @(Get-CimInstance Win32_Process -Filter "Name LIKE 'python%'" -ErrorAction Stop) } catch { return @() }
    $prefix = $Root.ToLower() + '\'
    $marked = @{}
    foreach ($f in @(Get-ChildItem -LiteralPath (Join-Path $Root '.cache') -Filter 'server-*.json' -File -ErrorAction SilentlyContinue)) {
        if ($f.BaseName -match '^server-(\d+)$') { $marked[[int]$Matches[1]] = $f.FullName }
    }
    $mine = @()
    foreach ($p in $all) {
        $cmd = "$($p.CommandLine)"
        if ($cmd -notmatch 'patsearch') { continue }
        if ($marked.ContainsKey([int]$p.ProcessId) -or "$($p.ExecutablePath)".ToLower().StartsWith($prefix) -or $cmd.ToLower().Contains($prefix)) { $mine += $p }
    }
    $alive = @{}
    foreach ($p in $all) { $alive[[int]$p.ProcessId] = 1 }
    foreach ($id in @($marked.Keys)) {  # 꺼진 서버가 남긴 표시는 치운다
        if (-not $alive.ContainsKey($id)) { Remove-Item -LiteralPath $marked[$id] -Force -ErrorAction SilentlyContinue }
    }
    return $mine
}

# =============================================================================
function Main {
    Say ''
    Say '================================================================' 'Cyan'
    Say '  Priora 설치' 'Cyan'
    Say "  폴더: $Root"
    Say '================================================================' 'Cyan'

    # ---------------------------------------------------------------- 1. 환경
    Step '[1/7] 환경 점검'
    if (-not (Test-Path -LiteralPath (Join-Path $Root 'patsearch\__init__.py'))) {
        Fail "이 폴더에 프로그램 파일(patsearch)이 없습니다. 압축을 전부 푼 폴더에서 install.bat 을 실행하세요."
    }
    if ($PSVersionTable.PSVersion.Major -lt 5) { Fail "Windows PowerShell 5.1 이상이 필요합니다 (지금: $($PSVersionTable.PSVersion))." }
    $os = [Environment]::OSVersion.Version
    if ($os.Major -lt 10) { Fail "Windows 10 이상에서만 설치할 수 있습니다 (지금: $os)." }
    if ($os.Build -lt 17763) { Warn "Windows 빌드 $($os.Build): 오래된 Windows 10 입니다. LLM CLI 는 1809(빌드 17763) 이상을 요구합니다." }
    else {
        $winName = 'Windows 10'
        if ($os.Build -ge 22000) { $winName = 'Windows 11' }
        Ok "$winName (빌드 $($os.Build)), PowerShell $($PSVersionTable.PSVersion)"
    }

    $probe = Join-Path $Root ".priora-write-test-$PID"
    try { Set-Content -LiteralPath $probe -Value 'x'; Remove-Item -LiteralPath $probe -Force }
    catch { Fail "이 폴더에 파일을 쓸 수 없습니다. Program Files 같은 보호된 위치가 아니라 문서 폴더나 D:\ 같은 곳에 풀어서 다시 실행하세요." }
    if ($Root.StartsWith('\\')) { Warn '네트워크 경로에서 실행 중입니다. 가상 환경이 느리거나 동작하지 않을 수 있으니 이 PC 의 디스크로 복사해 쓰세요.' }
    if ($env:OneDrive -and $Root.ToLower().StartsWith("$env:OneDrive".ToLower())) {
        Warn 'OneDrive 동기화 폴더 안입니다. .venv 와 검색 기록까지 동기화되어 느려질 수 있습니다. 동기화되지 않는 폴더를 권합니다.'
    }
    if ($Root.Length -gt 150) { Warn "폴더 경로가 깁니다($($Root.Length)자). 설치 중 경로 길이 오류가 나면 더 짧은 경로로 옮기세요." }
    try {
        $free = (New-Object System.IO.DriveInfo ([System.IO.Path]::GetPathRoot($Root))).AvailableFreeSpace
        if ($free -lt 600MB) { Warn ("디스크 여유 공간이 {0:N0}MB 뿐입니다. 600MB 이상을 권합니다." -f ($free / 1MB)) }
        else { Ok ("디스크 여유 공간 {0:N1}GB" -f ($free / 1GB)) }
    } catch { Log '  디스크 여유 공간을 읽지 못함' }
    try {  # 인터넷에서 받은 zip 을 풀면 파일마다 "차단" 표시가 붙어 실행할 때마다 경고가 뜬다
        Get-ChildItem -LiteralPath $Root -Filter '*.bat' -File | Unblock-File -ErrorAction SilentlyContinue
        Get-ChildItem -LiteralPath (Join-Path $Root 'tools') -File | Unblock-File -ErrorAction SilentlyContinue
    } catch { }

    $running = Get-PrioraProcesses
    if ($running.Count) {
        Info "실행 중인 Priora 가 있습니다 (PID $(($running | ForEach-Object { $_.ProcessId }) -join ', ')). 켜 둔 채로는 파일이 잠겨 설치가 실패할 수 있습니다."
        if (Ask '지금 끄고 계속할까요? (실행 중인 검색은 중단됩니다)' $true) {
            foreach ($p in $running) { Stop-Tree $p.ProcessId }
            Start-Sleep -Seconds 1
            Get-PrioraProcesses | Out-Null   # 꺼진 서버가 남긴 표시 파일을 치운다
            Ok '실행 중이던 Priora 를 껐습니다'
        } else { Warn '실행 중인 Priora 를 그대로 두었습니다. 설치가 실패하면 Priora 창을 닫고 다시 실행하세요.' }
    }

    # ---------------------------------------------------------------- 2. Python
    Step "[2/7] Python $MinPython 이상"
    Update-Path
    $venvInfo = $null
    if (Test-Path -LiteralPath $VenvPy) { $venvInfo = Test-Python $VenvPy }
    $base = $null
    if ($venvInfo -and $venvInfo.Version -ge $MinPython) {
        Ok "가상 환경의 Python $($venvInfo.Version) 을 그대로 씁니다 (.venv)"
    } else {
        $found = Find-Python
        $base = $found.Best
        if (-not $base) {
            foreach ($old in $found.All) { Info "찾았지만 쓸 수 없음: Python $($old.Version) ($($old.Path)) - $MinPython 이상이 필요합니다" }
            if (-not $found.All.Count) { Info 'Python 을 찾지 못했습니다.' }
            if (-not (Ask 'Python 3.13 을 지금 설치할까요? (현재 사용자용)' $true)) {
                Fail 'Python 이 필요합니다. https://www.python.org/downloads/ 에서 3.11 이상을 설치("Add python.exe to PATH" 체크)한 뒤 install.bat 을 다시 실행하세요.'
            }
            $base = Install-Python
            if (-not $base) {
                Fail 'Python 을 자동으로 설치하지 못했습니다. https://www.python.org/downloads/ 에서 3.11 이상을 설치("Add python.exe to PATH" 체크)한 뒤 install.bat 을 다시 실행하세요.'
            }
            $State.python_installed = $true
        }
        Ok "Python $($base.Version) $($base.Bits)bit - $($base.Path)"
        if ($base.Bits -ne 64) { Warn '32비트 Python 입니다. PDF 를 읽는 PyMuPDF 가 설치되지 않을 수 있습니다. 64비트 Python 을 권합니다.' }
    }

    # ---------------------------------------------------------------- 3. 가상 환경 · 패키지
    Step '[3/7] 가상 환경(.venv)과 패키지'
    if ($base) {
        if (Test-Path -LiteralPath $VenvDir) {
            Info '기존 .venv 가 망가졌거나 Python 이 오래되어 새로 만듭니다.'
            $env:PRIORA_RD = $VenvDir
            & cmd /d /c 'rd /s /q "%PRIORA_RD%"' 2>$null | Out-Null
            if (Test-Path -LiteralPath $VenvDir) { Fail '.venv 폴더를 지우지 못했습니다. Priora 창과 이 폴더를 연 프로그램을 닫고 다시 실행하세요.' }
        }
        Info '가상 환경을 만드는 중...'
        $code = Invoke-Visible $base.Exe ($base.Pre + @('-m', 'venv', $VenvDir))
        $venvInfo = $null
        if (Test-Path -LiteralPath $VenvPy) { $venvInfo = Test-Python $VenvPy }
        if (-not $venvInfo) { Fail "가상 환경을 만들지 못했습니다 (종료 코드 $code). install.log 를 확인하세요. 백신이 막았거나 Python 설치가 불완전할 수 있습니다." }
        Ok "가상 환경 생성 - Python $($venvInfo.Version)"
    }

    $hasPdf = (Invoke-Captured $VenvPy @('-c', 'import fitz') 60).Code -eq 0
    $online = Test-Online 'https://pypi.org/simple/pymupdf/'
    if (-not $online -and $hasPdf) {
        Ok 'PyMuPDF 가 이미 설치되어 있습니다 (인터넷에 연결되지 않아 업데이트 확인은 건너뜀)'
    } else {
        if (-not $online) { Info 'pypi.org 에 연결되지 않습니다. 그래도 설치를 시도합니다.' }
        Info '패키지를 설치하는 중 (PyMuPDF)...'
        $code = Invoke-Visible $VenvPy @('-m', 'pip', 'install', '--disable-pip-version-check', '--no-input', '--only-binary=:all:',
            '--upgrade', '-r', (Join-Path $Root 'requirements.txt'))
        $check = Invoke-Captured $VenvPy @('-c', 'import fitz; print(fitz.__doc__ or "ok")') 60
        if ($check.Code -eq 0) {
            Ok "PyMuPDF 준비됨 - $(("$($check.Out)" -split "`n")[0].Trim())"
        } elseif ("$($check.Err)" -match 'DLL load failed') {
            Warn 'PyMuPDF 는 설치됐지만 불러오지 못했습니다(DLL). "Microsoft Visual C++ 재배포 가능 패키지(x64)"가 필요합니다: winget install -e --id Microsoft.VCRedist.2015+.x64  실행 뒤 install.bat 을 다시 돌리세요. 그 전까지 검색은 되지만 구성대비(PDF)는 안 됩니다.'
        } elseif ($code -ne 0) {
            $why = '인터넷에 연결되어 있는지 확인한 뒤'
            if ($online) { $why = '회사 프록시나 보안 프로그램이 막고 있지 않은지 확인한 뒤(원인은 install.log 의 pip 출력)' }
            Warn "PyMuPDF 를 설치하지 못했습니다 (pip 종료 코드 $code). $why install.bat 을 다시 실행하세요. 그 전까지 검색은 되지만 구성대비(PDF)는 안 됩니다."
        } else {
            Warn "PyMuPDF 를 불러오지 못했습니다: $(("$($check.Err)" -split "`n")[-1])"
        }
    }
    $smoke = Invoke-Captured $VenvPy @('-c', 'import patsearch.web, patsearch.compare, patsearch.__main__') 60 $Root
    if ($smoke.Code -eq 0) { Ok '프로그램 모듈을 불러오는 데 문제가 없습니다' }
    else { Fail "프로그램을 불러오지 못했습니다. 파일이 빠졌거나 손상됐을 수 있습니다: $(("$($smoke.Err)" -split "`n")[-1])" }

    # ---------------------------------------------------------------- 4. LLM CLI
    Step '[4/7] LLM CLI (하나 이상 필요)'
    $status = @{}
    $n = 0
    foreach ($cli in $Clis) {
        $n++
        $status[$cli.Id] = Get-CliStatus $cli
        $text = '없음'
        if ($status[$cli.Id].Path) { $text = "설치됨 $($status[$cli.Id].Version)  ($($status[$cli.Id].Path))" }
        Info ("[{0}] {1,-16} {2}" -f $n, $cli.Label, $text)
    }
    $installedCount = @($Clis | Where-Object { $status[$_.Id].Path }).Count
    $wanted = @()
    if ($null -ne $Opt.cli) {
        $names = @($Opt.cli -split ',' | ForEach-Object { $_.Trim().ToLower() } | Where-Object { $_ -and $_ -ne 'none' })
        $wanted = @($Clis | Where-Object { $names -contains $_.Id -or $names -contains $_.Exe })
    } elseif ($Opt.yes) {
        if (-not $installedCount) { $wanted = @($Clis[0]) }
    } else {
        if ($installedCount) { $answer = Read-Host '  더 설치할 CLI 번호 (쉼표로 여러 개, Enter = 건너뜀)' }
        else { $answer = Read-Host '  설치할 CLI 번호 (쉼표로 여러 개, Enter = 1 Claude Code, 0 = 건너뜀)'; if (-not "$answer".Trim()) { $answer = '1' } }
        Log "  (질문) CLI 번호 -> '$answer'"
        foreach ($num in ("$answer" -split '[,\s]+')) {
            if ($num -match '^[1-9]$' -and [int]$num -le $Clis.Count) { $wanted += $Clis[[int]$num - 1] }
        }
    }
    foreach ($cli in $wanted) {
        if ($status[$cli.Id].Path) { Info "$($cli.Label) 는 이미 설치되어 있어 건너뜁니다. (업데이트는 Priora 화면의 CLI 연결에서)"; continue }
        Info "$($cli.Label) 설치 중..."
        $path = Install-Cli $cli
        if ($path) {
            $status[$cli.Id] = Get-CliStatus $cli
            $State.clis_installed += $cli.Id
            Ok "$($cli.Label) 설치됨 - $path"
        } else {
            Warn "$($cli.Label) 를 자동으로 설치하지 못했습니다. 인터넷 연결을 확인하거나 직접 설치한 뒤 install.bat 을 다시 실행하세요. (install.log 참고)"
        }
    }
    $ready = @($Clis | Where-Object { $status[$_.Id].Path })
    if ($ready.Count) {
        Ok "쓸 수 있는 CLI: $(($ready | ForEach-Object { $_.Label }) -join ', ')"
        Info '로그인은 Priora 화면 왼쪽 아래 "CLI 연결"에서 합니다.'
    } else {
        Warn 'LLM CLI 가 하나도 없습니다. 화면은 열리지만 검색·구성대비는 CLI 를 설치하고 로그인해야 동작합니다.'
    }

    # ---------------------------------------------------------------- 5. 설정 파일
    Step '[5/7] 설정 파일'
    $envFile = Join-Path $Root '.env'
    if (Test-Path -LiteralPath $envFile) { Ok '.env 가 이미 있어 그대로 둡니다 (저장된 키 유지)' }
    elseif (Test-Path -LiteralPath (Join-Path $Root '.env.example')) {
        Copy-Item -LiteralPath (Join-Path $Root '.env.example') -Destination $envFile
        Ok '.env 를 만들었습니다. EPO 키는 Priora 화면의 "설정"에서 넣으면 됩니다.'
    } else { Info '.env.example 이 없어 .env 는 만들지 않았습니다. 키는 화면의 "설정"에서 넣으면 저장됩니다.' }
    $settingsFile = Join-Path $Root 'settings.json'
    if (-not (Test-Path -LiteralPath $settingsFile) -and $ready.Count -and -not $status['claude'].Path) {
        # 기본 CLI 는 claude 다. 없으면 설치된 것으로 맞춰 둬야 첫 검색에서 오류가 나지 않는다.
        [IO.File]::WriteAllText($settingsFile, "{`n  `"cli`": `"$($ready[0].Id)`"`n}`n", (New-Object Text.UTF8Encoding $false))
        Ok "Claude Code 가 없어 기본 CLI 를 $($ready[0].Label) 로 맞췄습니다 (settings.json)"
    }

    # ---------------------------------------------------------------- 6. 포트
    Step "[6/7] 포트 점검 ($PortFirst~$PortLast, 이 PC 안에서만 접속)"
    $freePort = 0
    $denied = 0
    foreach ($port in $PortFirst..$PortLast) {
        $result = Test-Port $port
        if ($result -eq 'free') { $freePort = $port; break }
        if ($result -eq 'denied') { $denied++; Info "포트 $port : Windows 가 예약해 둔 포트라 열 수 없습니다" ; continue }
        $owner = Get-PortOwner $port
        if ($owner -and $owner.Priora) { Info "포트 $port : 이미 실행 중인 Priora 가 쓰고 있습니다 (PID $($owner.ProcessId))" }
        elseif ($owner) { Info "포트 $port : 다른 프로그램이 사용 중 - $($owner.Name) (PID $($owner.ProcessId))" }
        else { Info "포트 $port : 다른 프로그램이 사용 중" }
    }
    if ($freePort -eq $PortFirst) { Ok "포트 $PortFirst 사용 가능 - 화면 주소는 http://127.0.0.1:$PortFirst/" }
    elseif ($freePort) { Ok "$PortFirst 번 대신 비어 있는 $freePort 번 포트로 자동으로 열립니다 (주소는 실행 창에 표시)." }
    else {
        $extra = ''
        if ($denied) { $extra = ' Hyper-V·WSL·Docker 가 포트 범위를 예약했을 수 있습니다(netsh int ipv4 show excludedportrange protocol=tcp).' }
        Warn "$PortFirst~$PortLast 가 모두 막혀 있습니다.$extra Priora 는 Windows 가 주는 다른 빈 포트로 열리며, 주소는 실행 창에 표시됩니다."
    }

    # ---------------------------------------------------------------- 7. 바로가기
    Step '[7/7] 바탕화면 바로가기'
    if ($Opt.noshortcut) { Info '/noshortcut: 바로가기를 만들지 않습니다.' }
    else {
        try {
            $desktop = [Environment]::GetFolderPath('Desktop')
            $lnk = Join-Path $desktop 'Priora.lnk'
            $target = Join-Path $Root 'start-ui.bat'
            $shell = New-Object -ComObject WScript.Shell
            $skip = $false
            if (Test-Path -LiteralPath $lnk) {
                $old = $shell.CreateShortcut($lnk).TargetPath
                if ($old -and $old.ToLower() -ne $target.ToLower()) {
                    $skip = $true
                    Warn "바탕화면에 다른 폴더를 가리키는 Priora 바로가기가 이미 있어 그대로 두었습니다 ($old). 이 폴더의 start-ui.bat 으로 실행하세요."
                }
            }
            if (-not $skip) {
                $sc = $shell.CreateShortcut($lnk)
                $sc.TargetPath = $target
                $sc.WorkingDirectory = $Root
                $sc.Description = 'Priora - 청구항 기반 선행기술 검색'
                $icon = Join-Path $Root 'patsearch\static\favicon.ico'
                if (Test-Path -LiteralPath $icon) { $sc.IconLocation = "$icon,0" }
                $sc.Save()
                $State.shortcut = $lnk
                Ok "바로가기를 만들었습니다 - $lnk"
            }
        } catch { Warn "바탕화면 바로가기를 만들지 못했습니다: $($_.Exception.Message). start-ui.bat 을 직접 실행하면 됩니다." }
    }

    # 삭제(uninstall.bat)가 참고하는 설치 기록. 예전 기록의 "이 스크립트가 설치한 것"은 이어받는다.
    $manifestFile = Join-Path $Root '.install.json'
    try {
        if (Test-Path -LiteralPath $manifestFile) {
            $prev = Get-Content -LiteralPath $manifestFile -Raw -Encoding UTF8 | ConvertFrom-Json
            if ($prev.python_installed) { $State.python_installed = $true }
            $State.clis_installed = @(@($prev.clis_installed) + $State.clis_installed | Where-Object { $_ } | Select-Object -Unique)
            if (-not $State.shortcut -and $prev.shortcut) { $State.shortcut = "$($prev.shortcut)" }
        }
    } catch { }
    $manifest = [ordered]@{
        installed_at     = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss')
        root             = $Root
        python           = "$($venvInfo.Version)"
        python_installed = [bool]$State.python_installed
        clis_installed   = @($State.clis_installed)
        shortcut         = "$($State.shortcut)"
    }
    [IO.File]::WriteAllText($manifestFile, ($manifest | ConvertTo-Json), (New-Object Text.UTF8Encoding $false))

    Say ''
    Say '================================================================' 'Cyan'
    if ($Warnings.Count) {
        Say "  설치를 마쳤습니다. 확인할 점이 $($Warnings.Count)가지 있습니다:" 'Yellow'
        $i = 0
        foreach ($w in $Warnings) { $i++; Say "   $i. $w" 'Yellow' }
    } else {
        Say '  설치를 마쳤습니다. 문제는 발견되지 않았습니다.' 'Green'
    }
    Say ''
    Say '  실행: 바탕화면의 Priora, 또는 이 폴더의 start-ui.bat'
    Say '  처음 할 일: 화면 왼쪽 아래 "설정"에서 EPO 키 입력 → "CLI 연결"에서 로그인'
    Say '  업데이트: 새 버전 파일을 이 폴더에 덮어쓴 뒤 install.bat 을 다시 실행 (기록·키는 그대로)'
    Say '  삭제: uninstall.bat'
    Say "  기록: $LogFile"
    Say '================================================================' 'Cyan'
}

$exitCode = 0
$oldEncoding = $null
try {
    try { $oldEncoding = [Console]::OutputEncoding; [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch { }
    $env:PYTHONIOENCODING = 'utf-8'
    Log ''
    Log "==== $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') install  옵션: $env:PRIORA_ARGS ===="
    Main
} catch {
    $message = "$($_.Exception.Message)"
    if ($message.StartsWith('PRIORA_FATAL: ')) { $message = $message.Substring(14) }
    else { $message = "예상하지 못한 오류: $message  ($($_.InvocationInfo.PositionMessage -replace '\s+', ' '))" }
    Say ''
    Say "  [중단] $message" 'Red'
    Say "  기록: $LogFile"
    $exitCode = 1
} finally {
    if ($oldEncoding) { try { [Console]::OutputEncoding = $oldEncoding } catch { } }
}
exit $exitCode
