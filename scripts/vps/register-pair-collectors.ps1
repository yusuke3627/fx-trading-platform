# 追加通貨ペアの tick collector を Windows VPS に常駐タスクとして登録する。
#
# 使い方（VPS の管理者 PowerShell、リポジトリを git pull した後）:
#   powershell -ExecutionPolicy Bypass -File scripts\vps\register-pair-collectors.ps1
#   symbol を絞る場合:
#   powershell -ExecutionPolicy Bypass -File scripts\vps\register-pair-collectors.ps1 -Symbols EURUSD
#
# 生成物: scripts\vps\generated\fx-tick-<symbol>.cmd / .vbs（gitignore 対象）
#         logs\fx-tick-<symbol>.log
# 登録:   タスク名 fx-tick-<symbol>。システム起動時トリガ + 登録直後に起動。
#         既存の fx-tick-collector（USDJPY）には触れない。
#
# 登録後、taskschd.msc で既存 fx-tick-collector と実行主体・ログオン種別が
# 揃っているかを確認すること（MT5 terminal へ到達できるセッションで動く必要が
# あるため、動作実績のある既存タスクの設定が truth source）。
#
# bars は登録しない: bar_service は「その symbol を宣言する strategy の
# timeframe 設定」を必要とし（strategy 未対応の間は起動できない）、bars は
# 保存 tick から後で PIT を保ったまま再構築できる派生データのため、tick の
# 蓄積だけを先行させる。
param(
    [string[]]$Symbols = @("EURUSD", "GBPUSD", "GBPJPY"),
    [string]$TradingEnv = "demo"
)

$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    throw "venv python not found: $Python (run: python -m venv .venv; .venv\Scripts\pip install -e '.[dev,db,mt5]')"
}
$Generated = Join-Path $PSScriptRoot "generated"
$Logs = Join-Path $RepoRoot "logs"
New-Item -ItemType Directory -Force -Path $Generated, $Logs | Out-Null

foreach ($Symbol in $Symbols) {
    $taskName = "fx-tick-" + $Symbol.ToLower()
    $cmdPath = Join-Path $Generated "$taskName.cmd"
    $vbsPath = Join-Path $Generated "$taskName.vbs"
    $logPath = Join-Path $Logs "$taskName.log"

    # self-loop: collector が例外や切断で終了しても 10 秒後に再起動する。
    # Task Scheduler の既定作業ディレクトリは %windir%\system32 で、
    # load_config() は相対パスの config/ を読むため、先にリポジトリへ移る。
    @"
@echo off
cd /d "$RepoRoot"
:loop
"$Python" -u -m trading.data.market.collector --env $TradingEnv --symbol $Symbol >> "$logPath" 2>&1
timeout /t 10 /nobreak >nul
goto loop
"@ | Set-Content -Path $cmdPath -Encoding ascii

    # コンソール窓を出さずに cmd を起動し、終了を「待つ」（第3引数 True）。
    # 待たないとタスクのアクションが即完了扱いになり、多重起動制御と
    # ExecutionTimeLimit が self-loop 本体に効かず、再実行のたびに collector
    # が重複起動する。
    "CreateObject(""WScript.Shell"").Run """"""$cmdPath"""""", 0, True" |
        Set-Content -Path $vbsPath -Encoding ascii

    # 再登録時は走行中インスタンス（プロセスツリーごと）を先に止める。
    # 止めずに -Force で上書きすると旧 self-loop が追跡外で残り続ける。
    if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    }

    $action = New-ScheduledTaskAction -Execute "wscript.exe" -Argument """$vbsPath"""
    $trigger = New-ScheduledTaskTrigger -AtStartup
    # 既定の 72h 実行上限で常駐タスクが殺されないよう上限を無効化し、
    # 走行中の再 Start は無視させる（重複 self-loop の防止）
    $settings = New-ScheduledTaskSettingsSet `
        -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
        -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
        -Settings $settings -RunLevel Highest -Force | Out-Null
    Start-ScheduledTask -TaskName $taskName
    Write-Host "registered + started $taskName ($Symbol, env=$TradingEnv, log=$logPath)"
}
