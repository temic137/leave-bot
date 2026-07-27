$jar = Join-Path $env:USERPROFILE ".agentspan\server\agentspan-runtime.jar"
$stdout = Join-Path $env:TEMP "agentspan-java.out.log"
$stderr = Join-Path $env:TEMP "agentspan-java.err.log"

Start-Process `
    -FilePath "java" `
    -ArgumentList @(
        "-jar",
        $jar,
        "--server.port=6767"
    ) `
    -WorkingDirectory (Split-Path $jar) `
    -WindowStyle Hidden `
    -RedirectStandardOutput $stdout `
    -RedirectStandardError $stderr
