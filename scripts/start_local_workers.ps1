# Spawn native Windows Celery worker subprocesses that connect to the
# Postgres + Redis exposed by docker compose (5432 / 6379 on localhost).
#
# Windows cannot use the prefork pool (no fork()), so each worker uses
# --pool=solo. To get parallelism on a queue, spawn MULTIPLE solo workers,
# each pinned to that queue. Each solo worker handles exactly one task at a
# time in its main thread, so DB pool sizing is trivial: 1 + small overflow.
#
# Usage:
#   pwsh scripts/start_local_workers.ps1            # start workers
#   pwsh scripts/start_local_workers.ps1 -Stop      # kill any started worker subprocesses

param(
    [switch]$Stop
)

$ErrorActionPreference = "Stop"
$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Split-Path -Parent $ScriptRoot
$LogDir = Join-Path $RepoRoot "local_worker_logs"
$PidFile = Join-Path $LogDir "winworker.pids"
$CertPath = Join-Path $RepoRoot "local_ssl\server.crt"

if ($Stop) {
    if (Test-Path $PidFile) {
        Get-Content $PidFile | ForEach-Object {
            $procId = [int]$_
            try {
                Stop-Process -Id $procId -Force -ErrorAction Stop
                Write-Host "killed pid $procId"
            } catch {
                Write-Host "pid $procId already gone"
            }
        }
        Remove-Item $PidFile -Force
    } else {
        Write-Host "no pidfile at $PidFile"
    }
    exit 0
}

if (-not (Test-Path $CertPath)) {
    Write-Error "Cert not found at $CertPath. Run 'docker compose cp postgres:/var/lib/postgresql/ssl/server.crt local_ssl/server.crt' first."
}

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

# verify-ca trusts the chain without checking hostname (cert CN=postgres but we
# connect via localhost when running outside Docker).
# Note: Postgres is published on host port 55432 (not 5432) and Redis on 56379
# to avoid any collision with native Windows Postgres / Redis installs that may
# already be bound to the default ports.
$CertPathFwd = $CertPath -replace '\\','/'
$env:APP_SYNC_DATABASE_URL  = "postgresql+psycopg://app:app@localhost:55432/appdb?sslmode=verify-ca&sslrootcert=$CertPathFwd"
$env:APP_ASYNC_DATABASE_URL = "postgresql+asyncpg://app:app@localhost:55432/appdb"
$env:APP_ASYNC_PG_SSL_CA_FILE = $CertPath
$env:APP_CELERY_BROKER_URL = "redis://localhost:56379/0"
$env:APP_CELERY_RESULT_BACKEND = "redis://localhost:56379/1"
# Each --pool=solo worker handles 1 task at a time so a tiny pool is enough.
$env:APP_SYNC_DB_POOL_SIZE = "1"
$env:APP_SYNC_DB_MAX_OVERFLOW = "2"

# Layout: 6 solo workers on the heavy execution queue, 2 on the aggregate queues.
#
# IMPORTANT: NO Windows workers are placed on `sweep.sweep.finalize`.
# That queue carries `dispatch_next_chunk_task`, which is on the critical path of
# every multi-chunk sweep. A single solo worker that stalls inside `apply_async()`
# (e.g. broker socket_timeout under heavy backpressure) wedges an entire sweep for
# up to 120s. Validation Test #7 reproduced exactly this — see VALIDATION.md.
# Keep dispatcher work on the high-throughput Docker prefork tier only.
$workers = @(
    @{ Hostname = "winexec1@%h";       Queues = "sweep.execution" },
    @{ Hostname = "winexec2@%h";       Queues = "sweep.execution" },
    @{ Hostname = "winexec3@%h";       Queues = "sweep.execution" },
    @{ Hostname = "winexec4@%h";       Queues = "sweep.execution" },
    @{ Hostname = "winexec5@%h";       Queues = "sweep.execution" },
    @{ Hostname = "winexec6@%h";       Queues = "sweep.execution" },
    @{ Hostname = "winaggregate1@%h";  Queues = "sweep.job.finalize,sweep.chunk.finalize" },
    @{ Hostname = "winaggregate2@%h";  Queues = "sweep.job.finalize,sweep.chunk.finalize" }
)

$pids = @()
$procs = @()
foreach ($w in $workers) {
    $logFile = Join-Path $LogDir ("{0}.log" -f ($w.Hostname -replace '[@%]', '_'))
    Write-Host "starting $($w.Hostname) on queues=$($w.Queues) (pool=solo) -> $logFile"
    $proc = Start-Process -FilePath "uv" `
        -ArgumentList @(
            "run", "celery", "-A", "app.celery_app:celery_app", "worker",
            "--loglevel=INFO",
            "--pool=solo",
            "--hostname=$($w.Hostname)",
            "--queues=$($w.Queues)"
        ) `
        -WorkingDirectory $RepoRoot `
        -RedirectStandardOutput $logFile `
        -RedirectStandardError ($logFile + ".err") `
        -PassThru `
        -NoNewWindow
    $pids += $proc.Id
    $procs += $proc
}

$pids | Out-File -FilePath $PidFile -Encoding ASCII
Write-Host "spawned pids: $($pids -join ', ')  (saved to $PidFile)"
Write-Host "tail logs:    Get-Content -Wait $LogDir\winexec1__h.log.err"
Write-Host "stop:         pwsh $($MyInvocation.MyCommand.Path) -Stop"

# Detach from spawned subprocesses so this script returns immediately.
foreach ($p in $procs) { $p.Dispose() }
