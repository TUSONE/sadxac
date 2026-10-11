param(
    [string]$HostIp = '192.168.1.105',
    [switch]$Start
)
$ErrorActionPreference = 'Stop'
if ($HostIp -notmatch '^[A-Za-z0-9][A-Za-z0-9.-]*$') { throw 'Invalid hostname' }
$Remote = "tusone@$HostIp"
$Files = @('gemini_assistant_dialog.py', 'mic_processing.py', 'audio_diagnostics.py', 'spotify_api.py', 'slavik-audio', 'slavik-spotify-player', 'start-gemini-assistant.sh', 'install-linux.sh', 'deploy-linux.sh', 'install-spotify-connect.sh', 'set-output-max.sh', 'spotify-connect-debug.sh', 'requirements.txt', 'README.md', 'tests/test_agent.py', 'tests/test_audio_bridge.py', 'tests/test_diagnostics.py', 'tests/test_mic.py', 'tests/test_spotify.py', 'tests/test_connect.py')
$Files += @('repair-aec.sh', 'audio-buffer.sh', 'trace-audio.sh', 'music_duck.py', 'tests/test_duck.py', 'tune-audio.sh', 'aec_measurement.py', 'tests/test_aec.py', 'install-connect-fix.sh', 'patches/librespot-empty-skip-target.patch', 'vendor/librespot-armhf/librespot.gz', 'vendor/librespot-armhf/LICENSES.tar.gz', 'vendor/librespot-armhf/SHA256SUMS', 'vendor/librespot-armhf/LICENSE.librespot', 'vendor/librespot-armhf/README.md', 'vendor/librespot-armhf/Cargo.lock', 'vendor/librespot-armhf/Dockerfile', 'vendor/librespot-armhf/build.sh', 'vendor/librespot-armhf/BUILD.json')
$Stage = Join-Path ([IO.Path]::GetTempPath()) ('slavik-' + [guid]::NewGuid().ToString('N'))
$RemoteStage = '.slavik-upload-' + [guid]::NewGuid().ToString('N')
New-Item -ItemType Directory -Path $Stage | Out-Null
try {
    $Utf8 = New-Object System.Text.UTF8Encoding($false)
    foreach ($File in $Files) {
        $Source = Join-Path $PSScriptRoot $File
        if (!(Test-Path -LiteralPath $Source)) { throw "Missing: $File" }
        $Target = Join-Path $Stage $File
        New-Item -ItemType Directory -Force -Path (Split-Path $Target) | Out-Null
        if ($File.EndsWith('.gz')) {
            [IO.File]::WriteAllBytes($Target, [IO.File]::ReadAllBytes($Source))
            continue
        }
        $Text = [IO.File]::ReadAllText($Source).Replace("`r`n", "`n").Replace("`r", "`n")
        [IO.File]::WriteAllText($Target, $Text, $Utf8)
    }
    & ssh $Remote "umask 077; mkdir $RemoteStage"
    if ($LASTEXITCODE -ne 0) { throw 'Cannot create upload directory' }
    & scp -r "$Stage/." "${Remote}:$RemoteStage/"
    if ($LASTEXITCODE -ne 0) { throw 'Upload failed; installation not started' }
    # No PowerShell heredocs; Linux scripts arrive as UTF-8 without BOM and LF.
    $Command = 'bash ~/' + $RemoteStage + '/deploy-linux.sh'
    & ssh -tt $Remote $Command
    if ($LASTEXITCODE -ne 0) { throw 'Installation failed; see preceding output. Previous directory preserved in ~/slavik-backups.' }
    if ($Start) {
        & ssh -tt $Remote 'cd "$HOME/slavik" && bash start-gemini-assistant.sh'
        if ($LASTEXITCODE -ne 0) { throw 'Agent stopped with an error' }
    }
} finally {
    Remove-Item -LiteralPath $Stage -Recurse -Force
}
