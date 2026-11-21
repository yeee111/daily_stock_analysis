# 只读诊断：查 Windows Application/System 事件日志中 python 崩溃/资源耗尽记录
# 用法: powershell -NoProfile -File 诊断_WER事件.ps1 [-Days 12]
# 不写任何注册表/文件/生产库；仅 Get-WinEvent 读取。
param([int]$Days = 12)
if (-not $Days -or $Days -le 0) { $Days = 12 }  # IEX 方式运行时 param 不绑定，兜底
$start = (Get-Date).AddDays(-$Days)
$end = Get-Date

Write-Output "=== Application: Application Error / WER / Hang ($start ~ $end) ==="
try {
    $evts = Get-WinEvent -FilterHashtable @{LogName='Application'; StartTime=$start; EndTime=$end} -ErrorAction Stop |
        Where-Object { $_.ProviderName -match 'Application Error|Windows Error Reporting|Application Hang' }
    if ($evts) {
        $evts | Select-Object TimeCreated, ProviderName, Id,
            @{n='Msg';e={ ($_.Message -split "`n")[0..3] -join ' | ' }} |
            Format-List | Out-String -Width 240
    } else { Write-Output "(无匹配事件)" }
} catch { Write-Output "查询失败: $($_.Exception.Message)" }

Write-Output "=== System: Resource-Exhaustion (2004) / 内存池 (2019/2020) ==="
try {
    $sys = Get-WinEvent -FilterHashtable @{LogName='System'; StartTime=$start; EndTime=$end; Id=2004,2019,2020} -ErrorAction Stop
    if ($sys) {
        $sys | Select-Object TimeCreated, Id,
            @{n='Msg';e={ ($_.Message -split "`n")[0..2] -join ' | ' }} |
            Format-List | Out-String -Width 240
    } else { Write-Output "(无匹配事件)" }
} catch { Write-Output "查询失败或无事件: $($_.Exception.Message)" }

Write-Output "=== WER 归档目录中 python 相关报告（只读列目录） ==="
foreach ($d in @("$env:ProgramData\Microsoft\Windows\WER\ReportArchive", "$env:ProgramData\Microsoft\Windows\WER\ReportQueue")) {
    if (Test-Path $d) {
        Get-ChildItem $d -Directory -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -match 'python' } |
            Select-Object Name, LastWriteTime | Format-Table -AutoSize | Out-String -Width 200
    }
}
Write-Output "=== 完成 ==="