# スマホから xwatch を見るための設定。phone-setup.bat から呼ばれる。
# 1. Tailscale が入っているか確認する（なければ入れ方を案内して終わる）
# 2. Tailscale にログインしているか確認する（していなければログインの画面を開く）
# 3. tailscale serve で、同じ tailnet の端末からだけ開ける https のアドレスを作る（インターネットには公開しない）
# 4. スマホで開くアドレスを表示して、クリップボードにも入れる
$ErrorActionPreference = 'Stop'
$port = 8443

function Find-Tailscale {
    $cmd = Get-Command tailscale -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $exe = Join-Path $env:ProgramFiles 'Tailscale\tailscale.exe'
    if (Test-Path $exe) { return $exe }
    return $null
}

function Get-TsStatus($ts) {
    try { return (& $ts status --json 2>$null | Out-String | ConvertFrom-Json) } catch { return $null }
}

Write-Host "スマホから xwatch を見るための設定を始めます。"
Write-Host ""

$ts = Find-Tailscale
if (-not $ts) {
    Write-Host "Tailscale が見つかりませんでした。スマホから見るには、このパソコンとスマホの両方に Tailscale が必要です。"
    Write-Host ""
    $ans = Read-Host "winget で Tailscale を入れますか？ (y/n)"
    if ($ans -eq 'y') {
        winget install -e --id Tailscale.Tailscale
    } else {
        Write-Host "https://tailscale.com/download/windows から Tailscale を入れてください。"
        Start-Process 'https://tailscale.com/download/windows'
    }
    Write-Host ""
    Write-Host "Tailscale を入れ終えたら、もう一度 phone-setup.bat を実行してください。"
    exit 1
}

$st = Get-TsStatus $ts
if (-not $st -or $st.BackendState -ne 'Running') {
    Write-Host "Tailscale にログインしていません。ブラウザでログインの画面が開きます。"
    Write-Host "スマホの Tailscale と同じアカウントでログインしてください。"
    Write-Host ""
    & $ts up
    $st = Get-TsStatus $ts
    if (-not $st -or $st.BackendState -ne 'Running') {
        Write-Host ""
        Write-Host "ログインを確認できませんでした。タスクバーの Tailscale のアイコンからログインして、もう一度 phone-setup.bat を実行してください。"
        exit 1
    }
}
$account = $st.CurrentTailnet.Name
Write-Host "Tailscale にログインしています（$account）。"
Write-Host "スマホの Tailscale も、このアカウントでログインしていることを確認してください。"
Write-Host ""

Write-Host "同じ tailnet の端末からだけ開けるアドレスを作ります。"
Write-Host "初めてのときは、tailnet で HTTPS を有効にするためのアドレスが表示されることがあります。そのときはそのアドレスを開いて有効にしてください。"
& $ts serve --bg --https=$port http://127.0.0.1:8765
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "アドレスを作れませんでした。上に出たメッセージを確認してください。"
    exit 1
}

$st = Get-TsStatus $ts
$name = $st.Self.DNSName.TrimEnd('.')
$url = "https://${name}:$port"
try { Set-Clipboard -Value $url } catch {}
Write-Host ""
Write-Host "設定が終わりました。スマホで開くアドレスは次のとおりです（クリップボードにも入れました）。"
Write-Host ""
Write-Host "    $url"
Write-Host ""
Write-Host "スマホの Tailscale をオンにして、Chrome でこのアドレスを開いてください。"
Write-Host "メニューの「アプリをインストール」か「ホーム画面に追加」を選ぶと、アイコンから開けるようになります。"
Write-Host "このアドレスは、同じ tailnet につながっている端末からしか開けません。"
