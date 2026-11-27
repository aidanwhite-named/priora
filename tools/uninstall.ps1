# Priora 삭제 스크립트. uninstall.bat 이 부른다.
#
# 지우는 것: 이 프로그램이 만든 것 전부 - 검색·구성대비 기록(runs, compare_out), 캐시(.cache), 설정(settings.json),
#            키(.env), 가상 환경(.venv), 설치 기록, 바탕화면 바로가기, 그리고 이 폴더(.cache)에서 생긴 LLM CLI 세션.
# 지우지 않는 것: 프로그램 파일, Python, LLM CLI 와 그 로그인, 다른 폴더의 CLI 세션.
# 옵션: /dryrun (무엇을 지울지만 보여 줌)  /y (묻지 않음)  /keepkeys (.env 는 남김)
# Windows PowerShell 5.1 에서 돌아야 하므로 7 전용 문법을 쓰지 않는다.

$ErrorActionPreference = 'Stop'

$Root = "$env:PRIORA_ROOT"
if (-not $Root) { $Root = Split-Path -Parent $PSScriptRoot }
$Root = $Root.TrimEnd('\')
$Opt = @{ yes = $false; dryrun = $false; keepkeys = $false }
foreach ($tok in ("$env:PRIORA_ARGS" -split '\s+')) {
    $t = $tok.Trim().TrimStart('/', '-').ToLower()
    if (-not $t) { continue }
    if ($t -eq 'y' -or $t -eq 'yes') { $Opt.yes = $true }
    elseif ($t -eq 'dryrun' -or $t -eq 'dry-run') { $Opt.dryrun = $true }
    elseif ($t -eq 'keepkeys') { $Opt.keepkeys = $true }
    else { Write-Host "알 수 없는 옵션: $tok (쓸 수 있는 옵션: /dryrun  /y  /keepkeys)" -ForegroundColor Yellow }
}
$Problems = New-Object System.Collections.Generic.List[string]
$CliLabels = [ordered]@{ claude = 'Claude Code'; codex = 'Codex CLI'; antigravity = 'Antigravity CLI' }

function Say([string]$Text = '', [string]$Color = '') {
    if ($Color) { Write-Host $Text -ForegroundColor $Color } else { Write-Host $Text }
}

function Get-Size([string]$Path) {
    try {
        $item = Get-Item -LiteralPath $Path -Force
        if (-not $item.PSIsContainer) { return $item.Length }
        $sum = (Get-ChildItem -LiteralPath $Path -Recurse -File -Force -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum
        if ($sum) { return $sum }
    } catch { }
    return 0
}

function Format-Size([long]$Bytes) {
    if ($Bytes -ge 1MB) { return ('{0:N1}MB' -f ($Bytes / 1MB)) }
    if ($Bytes -ge 1KB) { return ('{0:N0}KB' -f ($Bytes / 1KB)) }
    return "${Bytes}B"
}

# 프로그램 폴더 바로 아래의 정해진 이름만 지운다. 폴더는 rd 로 지워, 안에 다른 곳을 가리키는 링크가 있어도
# 링크만 지우고 가리키는 곳의 내용은 건드리지 않는다.
function Remove-Target([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return }
    $full = [IO.Path]::GetFullPath($Path)
    if (-not $full.ToLower().StartsWith($Root.ToLower() + '\')) { $Problems.Add("프로그램 폴더 밖이라 지우지 않음: $full"); return }
    try {
        if ((Get-Item -LiteralPath $full -Force).PSIsContainer) {
            $env:PRIORA_RD = $full
            & cmd /d /c 'rd /s /q "%PRIORA_RD%"' 2>$null | Out-Null
        } else {
            Remove-Item -LiteralPath $full -Force
        }
    } catch { }
    if (Test-Path -LiteralPath $full) { $Problems.Add("지우지 못함(다른 프로그램이 열고 있을 수 있음): $full") }
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

# CLI 세션 정리는 SQLite 와 JSON 을 고쳐야 해서 파이썬 도구(tools\cli_sessions.py)가 한다.
function Find-Python {
    $candidates = @((Join-Path $Root '.venv\Scripts\python.exe'))
    foreach ($name in 'python', 'python3') {
        foreach ($cmd in @(Get-Command $name -CommandType Application -All -ErrorAction SilentlyContinue)) { $candidates += $cmd.Source }
    }
    foreach ($exe in $candidates) {
        if (-not (Test-Path -LiteralPath $exe)) { continue }
        try {
            $out = & $exe -c "import sqlite3, json; print('PYOK')" 2>$null
            if ("$out" -match 'PYOK') { return $exe }
        } catch { }
    }
    $py = Get-Command py -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($py) {
        try { if ("$(& $py.Source -3 -c "print('PYOK')" 2>$null)" -match 'PYOK') { return $py.Source } } catch { }
    }
    return ''
}

function Invoke-SessionTool([string]$Python, [bool]$Apply) {
    $toolArgs = @((Join-Path $Root 'tools\cli_sessions.py'), '--workdir', (Join-Path $Root '.cache'))
    if ($Python -like '*\py.exe') { $toolArgs = @('-3') + $toolArgs }
    if ($env:PRIORA_TEST_HOME) { $toolArgs += @('--home', $env:PRIORA_TEST_HOME) }
    if ($Apply) { $toolArgs += '--apply' }
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $out = & $Python @toolArgs 2>$null
        $line = @($out | Where-Object { "$_".TrimStart().StartsWith('{') }) | Select-Object -Last 1
        if (-not $line) { return $null }
        return ($line | ConvertFrom-Json)
    } catch { return $null } finally { $ErrorActionPreference = $old }
}

function Main {
    Say ''
    Say '================================================================' 'Cyan'
    Say '  Priora 삭제' 'Cyan'
    Say "  폴더: $Root"
    Say '================================================================' 'Cyan'
    if (-not (Test-Path -LiteralPath (Join-Path $Root 'patsearch\__init__.py'))) {
        throw "PRIORA_FATAL: 이 폴더는 Priora 프로그램 폴더가 아닙니다. 아무것도 지우지 않았습니다."
    }

    # ---------------------------------------------------------------- 무엇을 지울지 모은다
    $targets = @()
    $add = {
        param([string]$Rel, [string]$What)
        $path = Join-Path $Root $Rel
        if (Test-Path -LiteralPath $path) { return @{ Path = $path; Rel = $Rel; What = $What; Size = (Get-Size $path) } }
    }
    $runs = 0
    if (Test-Path -LiteralPath (Join-Path $Root 'runs')) { $runs = @(Get-ChildItem -LiteralPath (Join-Path $Root 'runs') -Directory -ErrorAction SilentlyContinue).Count }
    $targets += & $add 'runs' "검색·구성대비 기록 ${runs}건 (넣어 둔 인용발명 PDF 포함)"
    $targets += & $add 'compare_out' '터미널로 돌린 구성대비 결과'
    $targets += & $add '.cache' 'LLM·검색 응답 캐시, CLI 작업 폴더'
    $targets += & $add 'settings.json' '화면에서 저장한 설정'
    if (-not $Opt.keepkeys) { $targets += & $add '.env' 'EPO·OpenAlex 키' }
    $targets += & $add '.venv' '가상 환경 (설치한 패키지)'
    $targets += & $add 'install.log' '설치 기록'
    $targets += & $add '.install.json' '설치 기록'
    foreach ($sub in 'patsearch', 'tools') {
        if (-not (Test-Path -LiteralPath (Join-Path $Root $sub))) { continue }
        foreach ($dir in @(Get-ChildItem -LiteralPath (Join-Path $Root $sub) -Recurse -Directory -Force -Filter '__pycache__' -ErrorAction SilentlyContinue)) {
            $targets += @{ Path = $dir.FullName; Rel = $dir.FullName.Substring($Root.Length + 1); What = '파이썬 실행 캐시'; Size = (Get-Size $dir.FullName) }
        }
    }
    $targets = @($targets | Where-Object { $_ })

    # 바탕화면 바로가기는 이 폴더를 가리키는 것만 지운다
    $shortcuts = @()
    $lnkPaths = @((Join-Path ([Environment]::GetFolderPath('Desktop')) 'Priora.lnk'))
    try {
        $manifest = Get-Content -LiteralPath (Join-Path $Root '.install.json') -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($manifest.shortcut) { $lnkPaths += "$($manifest.shortcut)" }
    } catch { $manifest = $null }
    try {
        $shell = New-Object -ComObject WScript.Shell
        foreach ($lnk in ($lnkPaths | Select-Object -Unique)) {
            if ((Test-Path -LiteralPath $lnk) -and "$($shell.CreateShortcut($lnk).TargetPath)".ToLower().StartsWith($Root.ToLower() + '\')) { $shortcuts += $lnk }
        }
    } catch { }

    $python = Find-Python
    $sessions = $null
    if ($python) { $sessions = Invoke-SessionTool $python $false }
    $running = Get-PrioraProcesses

    # ---------------------------------------------------------------- 보여 주고 확인
    Say ''
    Say '  지울 것' 'Cyan'
    if (-not $targets.Count -and -not $shortcuts.Count) { Say '   (이 폴더에는 지울 기록이 없습니다)' }
    foreach ($t in $targets) { Say ("   {0,-28} {1,9}  {2}" -f $t.Rel, (Format-Size $t.Size), $t.What) }
    foreach ($lnk in $shortcuts) { Say "   바탕화면 바로가기              $lnk" }
    Say ''
    Say "  이 폴더에서 생긴 LLM CLI 세션 (작업 폴더: $(Join-Path $Root '.cache'))" 'Cyan'
    $sessionItems = 0
    if ($sessions -and $sessions.clis) {
        foreach ($id in $CliLabels.Keys) {
            $c = $sessions.clis.$id
            if (-not $c) { continue }
            $count = @($c.items).Count
            $sessionItems += $count
            if ($count) { Say ("   {0,-16} 세션 {1}건, 파일·항목 {2}개" -f $CliLabels[$id], $c.sessions, $count) }
            else { Say ("   {0,-16} 없음" -f $CliLabels[$id]) }
            if ($Opt.dryrun) { foreach ($item in @($c.items)) { Say "      $item" 'DarkGray' } }
            foreach ($e in @($c.errors)) { Say "      [주의] $e" 'Yellow' }
        }
    } elseif (-not $python) {
        Say '   Python 을 찾지 못해 CLI 세션은 확인·정리할 수 없습니다.' 'Yellow'
    } else {
        Say '   CLI 세션을 확인하지 못했습니다 (tools\cli_sessions.py 실행 실패).' 'Yellow'
    }
    if ($running.Count) {
        Say ''
        Say "  실행 중인 Priora (PID $(($running | ForEach-Object { $_.ProcessId }) -join ', ')) 는 먼저 끕니다. 돌고 있던 검색은 중단됩니다." 'Yellow'
    }
    Say ''
    Say '  남는 것: 프로그램 파일(이 폴더), Python, LLM CLI 와 그 로그인 상태, 다른 폴더의 CLI 세션'
    if ($Opt.keepkeys) { Say '           .env (키) - /keepkeys' }

    if ($Opt.dryrun) {
        Say ''
        Say '  /dryrun: 아무것도 지우지 않았습니다.' 'Green'
        return
    }
    if (-not $targets.Count -and -not $shortcuts.Count -and -not $sessionItems -and -not $running.Count) {
        Say ''
        Say '  지울 것이 없습니다.' 'Green'
        return
    }
    if (-not $Opt.yes) {
        Say ''
        Say '  지운 기록은 되돌릴 수 없습니다 (휴지통으로 가지 않습니다).' 'Yellow'
        $answer = "$(Read-Host '  정말 지우려면 yes 를 입력하세요')".Trim().ToLower()
        if ($answer -ne 'yes' -and $answer -ne 'y') {
            Say '  취소했습니다. 아무것도 지우지 않았습니다.' 'Green'
            return
        }
    }

    # ---------------------------------------------------------------- 지운다
    Say ''
    foreach ($p in $running) { Stop-Tree $p.ProcessId }
    if ($running.Count) { Start-Sleep -Seconds 1; Say '  실행 중이던 Priora 를 껐습니다.' }

    if ($sessionItems) {
        $done = Invoke-SessionTool $python $true
        if ($done -and $done.clis) {
            foreach ($id in $CliLabels.Keys) {
                $c = $done.clis.$id
                if (-not $c) { continue }
                if (@($c.items).Count) { Say "  $($CliLabels[$id]) 세션 기록을 지웠습니다 (파일·항목 $(@($c.items).Count)개)." }
                foreach ($e in @($c.errors)) { $Problems.Add("$($CliLabels[$id]): $e") }
            }
        } else { $Problems.Add('CLI 세션 정리 도구가 끝까지 실행되지 못했습니다.') }
    }

    foreach ($lnk in $shortcuts) {
        try { Remove-Item -LiteralPath $lnk -Force; Say "  바로가기를 지웠습니다: $lnk" } catch { $Problems.Add("바로가기를 지우지 못함: $lnk") }
    }
    # .venv 는 세션 정리에 쓴 파이썬이 들어 있을 수 있어 맨 마지막에 지운다
    foreach ($t in @($targets | Where-Object { $_.Rel -ne '.venv' }) + @($targets | Where-Object { $_.Rel -eq '.venv' })) {
        Remove-Target $t.Path
        if (-not (Test-Path -LiteralPath $t.Path)) { Say "  지웠습니다: $($t.Rel)" }
    }

    Say ''
    Say '================================================================' 'Cyan'
    if ($Problems.Count) {
        Say "  삭제를 마쳤지만 $($Problems.Count)가지가 남았습니다:" 'Yellow'
        foreach ($p in $Problems) { Say "   - $p" 'Yellow' }
        Say '  열려 있는 Priora 창·탐색기·편집기를 닫고 uninstall.bat 을 다시 실행하세요.' 'Yellow'
    } else {
        Say '  이 프로그램이 만든 기록과 CLI 세션을 모두 지웠습니다.' 'Green'
    }
    Say ''
    Say '  프로그램까지 없애려면 이 폴더를 통째로 지우면 됩니다.'
    Say '  다시 쓰려면 install.bat 을 실행하세요.'
    $notes = @()
    if ($manifest -and $manifest.python_installed) { $notes += 'Python 3.13 (설정 > 앱 > 설치된 앱에서 제거)' }
    $uninstallHint = @{
        claude      = 'Claude Code: winget uninstall Anthropic.ClaudeCode 또는 npm uninstall -g @anthropic-ai/claude-code, 직접 설치본은 %USERPROFILE%\.local\bin\claude.exe 삭제'
        codex       = 'Codex CLI: npm uninstall -g @openai/codex'
        antigravity = 'Antigravity CLI: winget uninstall Google.AntigravityCLI'
    }
    if ($manifest) { foreach ($id in @($manifest.clis_installed)) { if ($uninstallHint.ContainsKey("$id")) { $notes += $uninstallHint["$id"] } } }
    if ($notes.Count) {
        Say ''
        Say '  설치할 때 함께 깔았던 프로그램은 다른 곳에서도 쓸 수 있어 지우지 않았습니다. 필요 없으면 직접 제거하세요:'
        foreach ($n in $notes) { Say "   - $n" }
    }
    Say '================================================================' 'Cyan'
}

$exitCode = 0
$oldEncoding = $null
try {
    try { $oldEncoding = [Console]::OutputEncoding; [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch { }
    $env:PYTHONIOENCODING = 'utf-8'
    Main
    if ($Problems.Count) { $exitCode = 2 }
} catch {
    $message = "$($_.Exception.Message)"
    if ($message.StartsWith('PRIORA_FATAL: ')) { $message = $message.Substring(14) }
    else { $message = "예상하지 못한 오류: $message  ($($_.InvocationInfo.PositionMessage -replace '\s+', ' '))" }
    Say ''
    Say "  [중단] $message" 'Red'
    $exitCode = 1
} finally {
    if ($oldEncoding) { try { [Console]::OutputEncoding = $oldEncoding } catch { } }
}
exit $exitCode
