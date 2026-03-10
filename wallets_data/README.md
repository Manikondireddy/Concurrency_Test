# Wallet Ledger API (Phase 1 + Phase 2 + Phase 3)

## Delivered Requirements
- Phase 1: wallet + ledger APIs (create wallet, credit, debit, balance, history)
- Phase 2: concurrency-safe behavior under simultaneous requests
- Phase 3: JWT authentication + strict per-user wallet authorization

## Production-Oriented Features
- Async FastAPI + async SQLAlchemy + PostgreSQL (`asyncpg`)
- Atomic debit/credit updates with in-DB checks (prevents negative balance)
- Ledger writes in same transaction as wallet mutation
- JWT auth with hashed passwords (`passlib` + `bcrypt`)
- Ownership enforcement (`403` when accessing another user's wallet)
- Request logging with request ID and security response headers
- Health endpoints: `/health/live`, `/health/ready`
- Config via environment variables (`.env.example` provided)
- Alembic dependency included for migration-based schema management

## Setup
```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

Copy `.env.example` to `.env` and update values.

## Important Environment Variables
- `APP_ENV` (`development` or `production`)
- `DATABASE_URL`
- `JWT_SECRET_KEY` (must be strong, 32+ chars; mandatory in production)
- `AUTO_CREATE_TABLES` (`false` in production)
- `CORS_ALLOW_ORIGINS`
- `ALLOWED_HOSTS`

## Run
```powershell
uvicorn app.main:app --reload --port 8010
```
Swagger: `http://127.0.0.1:8010/docs`

## Database Migrations (Production)
Use Alembic migrations instead of runtime schema creation:
```powershell
$env:AUTO_CREATE_TABLES = "false"
alembic upgrade head
```

## API Endpoints
Auth:
- `POST /auth/register`
- `POST /auth/token` (alias: `POST /auth/login`)

Wallet (JWT required):
- `POST /wallets`
- `POST /wallets/{wallet_id}/credit` (also available as `POST /wallets/me/credit`)
- `POST /wallets/{wallet_id}/debit` (also available as `POST /wallets/me/debit`)
- `GET /wallets/{wallet_id}/balance` (also available as `GET /wallets/me/balance`)
- `GET /wallets/{wallet_id}/ledger` (also available as `GET /wallets/me/ledger`)

Health:
- `GET /health/live`
- `GET /health/ready`

## Swagger Quick Test
1. `POST /auth/register`
```json
{"username":"alice","password":"Passw0rd!"}
```
2. `POST /auth/token`
```json
{"username":"alice","password":"Passw0rd!"}
```
3. Click **Authorize** in Swagger and paste:
```text
Bearer <access_token>
```
4. Create wallet: `POST /wallets` (no body)
5. Credit:
```json
{"amount":100}
```
6. Debit:
```json
{"amount":10}
```
7. Check balance and ledger

## Phase 2 Concurrency Validation (50 concurrent debits)
Use PowerShell while API is running:

```powershell
$ErrorActionPreference='Stop'
$base='http://127.0.0.1:8010'

$u='phase2_' + [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
Invoke-RestMethod -Method Post -Uri "$base/auth/register" -ContentType 'application/json' -Body (@{username=$u;password='Passw0rd!'}|ConvertTo-Json) | Out-Null
$token=(Invoke-RestMethod -Method Post -Uri "$base/auth/token" -ContentType 'application/json' -Body (@{username=$u;password='Passw0rd!'}|ConvertTo-Json)).access_token
$h=@{Authorization="Bearer $token"}

$wallet=(Invoke-RestMethod -Method Post -Uri "$base/wallets" -Headers $h)
$id=[int]$wallet.wallet_id
Invoke-RestMethod -Method Post -Uri "$base/wallets/$id/credit" -Headers $h -ContentType 'application/json' -Body (@{amount=100}|ConvertTo-Json) | Out-Null

$uri="$base/wallets/$id/debit"
$body='{"amount":10}'
$jobs=1..50 | ForEach-Object {
  Start-Job -ScriptBlock {
    param($u,$b,$tok)
    try { Invoke-RestMethod -Method Post -Uri $u -Headers @{Authorization="Bearer $tok"} -ContentType 'application/json' -Body $b | Out-Null; 'success' }
    catch { $raw=$_.ErrorDetails.Message; if($raw){try{(ConvertFrom-Json $raw).detail}catch{'error'}} else {'error'} }
  } -ArgumentList $uri,$body,$token
}
$results=$jobs | Receive-Job -Wait -AutoRemoveJob
$results | Group-Object | Sort-Object Name | Select Name,Count

$balance=Invoke-RestMethod -Method Get -Uri "$base/wallets/$id/balance" -Headers $h
$ledger=Invoke-RestMethod -Method Get -Uri "$base/wallets/$id/ledger?limit=200" -Headers $h
"final_balance=$($balance.balance)"
"debit_entries=$(($ledger|?{$_.type -eq 'debit'}).Count)"
"credit_entries=$(($ledger|?{$_.type -eq 'credit'}).Count)"
```

Expected:
- `success = 10`
- `Insufficient balance = 40`
- `final_balance = 0.00`
- `debit_entries = 10`
- `credit_entries = 1`

## Phase 3 Authorization Validation
- Register/login two users.
- Create wallet with user A token.
- Access user A wallet with user B token.
- Expected: `403 Not authorized for this wallet`.

## Submission Checklist
- [ ] `.env` configured with production-safe values:
  - `DATABASE_URL`
  - `JWT_SECRET_KEY` (32+ chars, strong random)
  - `APP_ENV=production` (for production deploy)
- [ ] Dependencies installed:
```powershell
pip install -r requirements.txt
```
- [ ] Migrations applied:
```powershell
$env:AUTO_CREATE_TABLES = "false"
alembic upgrade head
```
- [ ] Service starts successfully:
```powershell
uvicorn app.main:app --host 127.0.0.1 --port 8010
```
- [ ] Swagger reachable: `http://127.0.0.1:8010/docs`
- [ ] Phase 1 evidence captured:
  - wallet created
  - credit/debit succeed
  - balance endpoint correct
  - ledger shows matching entries
- [ ] Phase 2 evidence captured:
  - 50 concurrent debits from balance 100
  - `success=10`, `Insufficient balance=40`
  - final balance `0.00`
  - ledger debit entries `10`
- [ ] Phase 3 evidence captured:
  - valid JWT required for wallet APIs
  - user B blocked (`403`) from user A wallet

## What to Submit
- Source code
- `README.md`
- `.env.example` (do not submit real `.env` secrets)
- Output/screenshots of:
  - Phase 2 concurrency result summary
  - Phase 3 authorization block (`403`) proof
