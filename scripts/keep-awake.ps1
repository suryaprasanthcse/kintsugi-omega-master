# Keeps the Windows host awake while it runs (Stage 13a; the runtime lives in WSL2, ruling D2).
#
#   powershell -ExecutionPolicy Bypass -File scripts\keep-awake.ps1
#
# It asks Windows not to sleep for as long as this process lives (SetThreadExecutionState with
# ES_CONTINUOUS | ES_SYSTEM_REQUIRED), the same request a media player makes. It changes no power
# setting: closing the window or Ctrl+C ends the request. The display may still turn off.
$signature = @'
[DllImport("kernel32.dll", SetLastError = true)]
public static extern uint SetThreadExecutionState(uint esFlags);
'@
$power = Add-Type -MemberDefinition $signature -Name Power -Namespace KintsugiOmega -PassThru
$ES_CONTINUOUS = [uint32]'0x80000000'
$ES_SYSTEM_REQUIRED = [uint32]'0x00000001'
if ($power::SetThreadExecutionState($ES_CONTINUOUS -bor $ES_SYSTEM_REQUIRED) -eq 0) {
    Write-Error 'SetThreadExecutionState was refused'
    exit 1
}
Write-Host 'keep-awake: Windows will not sleep while this window is open (Ctrl+C to stop).'
try {
    while ($true) { Start-Sleep -Seconds 60 }
} finally {
    $null = $power::SetThreadExecutionState($ES_CONTINUOUS)
    Write-Host 'keep-awake: released.'
}
