param(
  [string]$BaseUrl = "http://127.0.0.1:8000",
  [int]$ConcurrentDebits = 50,
  [decimal]$DebitAmount = 10,
  [decimal]$InitialCredit = 100,
  [string]$HealthPath = "/health/live",
  [int]$TimeoutSec = 10,
  [int]$JobsTimeoutSec = 120
)

$ErrorActionPreference = "Stop"

function Normalize-BaseUrl {
  param([string]$Url)
  if ($Url.EndsWith("/")) { return $Url.TrimEnd("/") }
  return $Url
}

function Assert-ApiReachable {
  param([string]$Url, [string]$Path)
  $healthUrl = (Normalize-BaseUrl $Url) + $Path
  try {
    Invoke-RestMethod -Method Get -Uri $healthUrl -TimeoutSec 3 | Out-Null
  } catch {
    Write-Host "API not reachable at $Url"
    Write-Host "Expected health endpoint: $healthUrl"
    Write-Host "Start the server in another terminal, for example:"
    Write-Host "  uvicorn app.main:app --reload --host 127.0.0.1 --port $(([uri]$Url).Port)"
    throw
  }
}

function New-RandomUser {
  $suffix = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
  return "phase2_$suffix"
}

function Invoke-Json {
  param(
    [Parameter(Mandatory=$true)][string]$Method,
    [Parameter(Mandatory=$true)][string]$Uri,
    [hashtable]$Headers = @{},
    $Body = $null
  )

  if ($null -eq $Body) {
    return Invoke-RestMethod -Method $Method -Uri $Uri -Headers $Headers -TimeoutSec $TimeoutSec
  }

  $json = $Body | ConvertTo-Json -Depth 10
  return Invoke-RestMethod -Method $Method -Uri $Uri -Headers $Headers -ContentType "application/json" -Body $json -TimeoutSec $TimeoutSec
}

$BaseUrl = Normalize-BaseUrl $BaseUrl
Assert-ApiReachable -Url $BaseUrl -Path $HealthPath

$user = New-RandomUser
Write-Host "base=$BaseUrl user=$user"

Write-Host "Step: register"
Invoke-Json -Method Post -Uri "$BaseUrl/auth/register" -Body @{ username = $user; password = "Passw0rd!" } | Out-Null

# The API supports /auth/token (alias) and /auth/login; prefer token.
Write-Host "Step: token"
$tokenResp = Invoke-Json -Method Post -Uri "$BaseUrl/auth/token" -Body @{ username = $user; password = "Passw0rd!" }
$token = $tokenResp.access_token
$headers = @{ Authorization = "Bearer $token" }

Write-Host "Step: create wallet"
$wallet = Invoke-Json -Method Post -Uri "$BaseUrl/wallets" -Headers $headers
$walletId = [int]$wallet.wallet_id
Write-Host "wallet_id=$walletId"

Write-Host "Step: credit initial balance"
Invoke-Json -Method Post -Uri "$BaseUrl/wallets/$walletId/credit" -Headers $headers -Body @{ amount = $InitialCredit } | Out-Null

$debitUri = "$BaseUrl/wallets/$walletId/debit"
Write-Host "Step: start $ConcurrentDebits concurrent debits of $DebitAmount"

$jobs = 1..$ConcurrentDebits | ForEach-Object {
  Start-Job -ScriptBlock {
    param($Uri, $Tok, $Amt, $TimeoutSec)
    $h = @{ Authorization = "Bearer $Tok" }
    $body = @{ amount = $Amt } | ConvertTo-Json

    # Client-side retry on 409 just in case; server already retries optimistically.
    for ($i = 1; $i -le 3; $i++) {
      try {
        Invoke-RestMethod -Method Post -Uri $Uri -Headers $h -ContentType "application/json" -Body $body -TimeoutSec $TimeoutSec | Out-Null
        return "success"
      } catch {
        $raw = $_.ErrorDetails.Message
        if ($raw) {
          try {
            $detail = (ConvertFrom-Json $raw).detail
            if ($detail -eq "Concurrent update, please retry") { continue }
            return $detail
          } catch {
            return "error"
          }
        }
        return "error"
      }
    }
    return "Concurrent update, please retry"
  } -ArgumentList $debitUri, $token, $DebitAmount, $TimeoutSec
}

$null = Wait-Job -Job $jobs -Timeout $JobsTimeoutSec
$running = $jobs | Where-Object { $_.State -eq "Running" }
if ($running) {
  Write-Host "WARNING: $($running.Count) jobs still running after ${JobsTimeoutSec}s, stopping them"
  $running | Stop-Job | Out-Null
}

$results = $jobs | Receive-Job -Keep -ErrorAction SilentlyContinue
$jobs | Remove-Job -Force | Out-Null

$results | Group-Object | Sort-Object Count -Descending | Select-Object Name, Count | Format-Table -AutoSize

Write-Host "Step: fetch balance and ledger"
$balance = Invoke-Json -Method Get -Uri "$BaseUrl/wallets/$walletId/balance" -Headers $headers
$ledger = Invoke-Json -Method Get -Uri "$BaseUrl/wallets/$walletId/ledger?limit=200" -Headers $headers

$debits = @($ledger | Where-Object { $_.type -eq "debit" }).Count
$credits = @($ledger | Where-Object { $_.type -eq "credit" }).Count

$successCount = @($results | Where-Object { $_ -eq "success" }).Count
$insufficientCount = @($results | Where-Object { $_ -eq "Insufficient balance" }).Count

# Expected results for the standard scenario.
$expectedSuccess = [int]([math]::Min([math]::Floor([double]($InitialCredit / $DebitAmount)), $ConcurrentDebits))
$expectedInsufficient = $ConcurrentDebits - $expectedSuccess
$expectedFinal = $InitialCredit - ($expectedSuccess * $DebitAmount)

$pass =
  ($successCount -eq $expectedSuccess) -and
  ($insufficientCount -eq $expectedInsufficient) -and
  ([decimal]$balance.balance -eq [decimal]$expectedFinal) -and
  ($debits -eq $expectedSuccess) -and
  ($credits -eq 1)

Write-Host ""
Write-Host "=== Concurrency Summary ==="
Write-Host ("base_url          : {0}" -f $BaseUrl)
Write-Host ("wallet_id         : {0}" -f $walletId)
Write-Host ("initial_credit    : {0}" -f $InitialCredit)
Write-Host ("debit_amount      : {0}" -f $DebitAmount)
Write-Host ("concurrent_debits : {0}" -f $ConcurrentDebits)
Write-Host ""
Write-Host ("success           : {0} (expected {1})" -f $successCount, $expectedSuccess)
Write-Host ("insufficient      : {0} (expected {1})" -f $insufficientCount, $expectedInsufficient)
Write-Host ("final_balance     : {0} (expected {1})" -f $balance.balance, $expectedFinal)
Write-Host ("ledger_debits     : {0} (expected {1})" -f $debits, $expectedSuccess)
Write-Host ("ledger_credits    : {0} (expected 1)" -f $credits)
Write-Host ""
Write-Host ("RESULT            : {0}" -f ($(if ($pass) { "PASS" } else { "FAIL" })))

if (-not $pass) {
  Write-Host ""
  Write-Host "Raw result counts:"
  $results | Group-Object | Sort-Object Count -Descending | Select-Object Name, Count | Format-Table -AutoSize
}
