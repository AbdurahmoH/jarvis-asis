param(
    [Parameter(Mandatory=$false)]
    [string]$Goal = ""
)

$token = $env:GITLAB_TOKEN
if (!$token) {
    try {
        $storage = Get-Content "$HOME\.gitlab\storage.json" | ConvertFrom-Json
        $token = $storage.'duo-cli-config'.gitlabAuthToken
    } catch {
        Write-Error "GITLAB_TOKEN not found in environment or storage.json"
        exit 1
    }
}

Write-Host "Creating GitLab Duo Workflow session for group xzczxc-group..." -ForegroundColor Cyan
$namespaceId = if ($env:GITLAB_DUO_NAMESPACE) { $env:GITLAB_DUO_NAMESPACE } else { "141666751" }
$body = @{
    namespace_id = $namespaceId
    workflow_definition = "developer"
    environment = "ide"
    allow_agent_to_request_user = $true
} | ConvertTo-Json

try {
    $res = Invoke-RestMethod -Uri "https://gitlab.com/api/v4/ai/duo_workflows/workflows" -Method Post -Headers @{ "PRIVATE-TOKEN" = $token } -ContentType "application/json" -Body $body
    $sessionId = $res.id
    Write-Host "Duo Session ID allocated: $sessionId" -ForegroundColor Green
} catch {
    Write-Error "Failed to allocate session: $($_.ErrorDetails.Message)"
    exit 1
}

$env:DUO_WORKFLOW_WORKFLOW_ID = "$sessionId"
if ($Goal) {
    duo run --existing-session-id $sessionId -g $Goal
} else {
    duo --existing-session-id $sessionId
}
