# xwatch のセットアップ。setup.bat から呼ばれる。
# 1. Python 3.9 以上があるか確認する（なければ入れ方を案内して終わる）
# 2. ログオン時に xwatch を窓なしで起動するよう、スタートアップにショートカットを置く
# 3. デスクトップに「xwatch」のショートカットを置く（押すと、止まっていれば起動してからダッシュボードを開く）
# 4. その場で起動してダッシュボードを開く
$ErrorActionPreference = 'Stop'
$dir = $PSScriptRoot

function Test-Python($exe, $pre) {
    try {
        & $exe @pre -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" 2>$null | Out-Null
        return $LASTEXITCODE -eq 0
    } catch { return $false }
}

Write-Host "xwatch のセットアップを始めます。"
Write-Host "場所: $dir"
Write-Host ""

$ok = (Test-Python 'py' @('-3')) -or (Test-Python 'pythonw' @()) -or (Test-Python 'python' @())
if (-not $ok) {
    Write-Host "Python 3.9 以上が見つかりませんでした。xwatch を動かすには Python が必要です。"
    Write-Host ""
    $ans = Read-Host "winget で Python 3.12 を入れますか？ (y/n)"
    if ($ans -eq 'y') {
        winget install -e --id Python.Python.3.12 --scope user
        Write-Host ""
        Write-Host "Python を入れ終えたら、いったんこの窓を閉じて、もう一度 setup.bat を実行してください。"
    } else {
        Write-Host "https://www.python.org/downloads/ から Python を入れてください。"
        Write-Host "入れるときに「Add python.exe to PATH」にチェックを入れ、終わったらもう一度 setup.bat を実行してください。"
        Start-Process 'https://www.python.org/downloads/'
    }
    exit 1
}
Write-Host "Python が見つかりました。"

$sh = New-Object -ComObject WScript.Shell

$startup = Join-Path ([Environment]::GetFolderPath('Startup')) 'xwatch.lnk'
$lnk = $sh.CreateShortcut($startup)
$lnk.TargetPath = Join-Path $env:WINDIR 'System32\wscript.exe'
$lnk.Arguments = '"' + (Join-Path $dir 'start_hidden.vbs') + '"'
$lnk.WorkingDirectory = $dir
$lnk.Description = 'xwatch を窓なしで起動する'
$lnk.Save()
Write-Host "パソコンにログオンしたとき、xwatch が自動で動くように登録しました。"

$desktop = Join-Path ([Environment]::GetFolderPath('Desktop')) 'xwatch.lnk'
$lnk = $sh.CreateShortcut($desktop)
$lnk.TargetPath = Join-Path $env:WINDIR 'System32\wscript.exe'
$lnk.Arguments = '"' + (Join-Path $dir 'open.vbs') + '"'
$lnk.WorkingDirectory = $dir
$lnk.IconLocation = (Join-Path $dir 'icon.ico') + ',0'
$lnk.Description = 'xwatch のダッシュボードを開く'
$lnk.Save()
Write-Host "デスクトップに「xwatch」のショートカットを作りました。"

Write-Host ""
Write-Host "xwatch を起動して、ダッシュボードを開きます。"
Start-Process (Join-Path $env:WINDIR 'System32\wscript.exe') -ArgumentList ('"' + (Join-Path $dir 'open.vbs') + '"')
Write-Host ""
Write-Host "セットアップが終わりました。ダッシュボードの左下にアカウントを入れて追加してください。"
Write-Host "データはこのフォルダの中の data フォルダに保存されます。フォルダごと動かしたときは、もう一度 setup.bat を実行してください。"
