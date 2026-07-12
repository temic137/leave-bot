$env:OPENAI_API_KEY = $env:GROQ_API_KEY
$env:CONDUCTOR_AI_OPENAI_BASE_URL = "https://api.groq.com/openai/v1"

$jar = Join-Path $env:USERPROFILE ".agentspan\server\agentspan-runtime.jar"
$stdout = Join-Path $env:TEMP "agentspan-java.out.log"
$stderr = Join-Path $env:TEMP "agentspan-java.err.log"

Start-Process `
    -FilePath "java" `
    -ArgumentList @(
        "-jar",
        $jar,
        "--server.port=6767",
        "--conductor.ai.openai.api-key=$env:GROQ_API_KEY",
        "--conductor.ai.openai.baseURL=https://api.groq.com/openai/v1"
    ) `
    -WorkingDirectory (Split-Path $jar) `
    -WindowStyle Hidden `
    -RedirectStandardOutput $stdout `
    -RedirectStandardError $stderr
