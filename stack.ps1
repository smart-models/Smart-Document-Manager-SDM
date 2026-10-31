#Requires -Version 5.1

<#
    .SYNOPSIS
        Build, start, inspect and stop the Smart Document Manager stack.

    .DESCRIPTION
        One of the two supported entry points; stack.sh is the other,
        and the two are kept at parity. See scripts/README.md for the operator
        reference.

        This script parses arguments and runs processes. It does not decide
        what Compose is asked to do -- scripts/stack_core.py does, once, for
        both launchers. The one deliberate exception is the operational
        recovery commands (down, logs, status, help), which never call the
        core: stopping a stack has to keep working with no Python interpreter
        on PATH and with .env absent or malformed.

        No parameter uses [ValidateSet], deliberately. A ValidateSet violation
        is a parameter *binding* failure: it exits 1 before the script body
        runs, which would make this script's own error messages unreachable
        and would answer `up bogus` with 1 on Windows against 2 on Unix --
        exactly the asymmetry the $Extra parameter closes.
        [ArgumentCompleter] gives the same tab-completion without ever
        rejecting a value; validation stays manual, in the body, through
        Exit-Usage.
#>
[CmdletBinding()]
param(
    # An ArgumentCompleter scriptblock is handed five positional arguments and
    # only the third, the word being completed, matters here. It is read
    # through $args so that the other four are never declared and then left
    # unused.
    [Parameter(Position = 0)]
    [ArgumentCompleter({
        @(
            "up", "verify", "config", "build", "pull", "restart", "urls",
            "logs", "status", "down", "version", "help"
        ) | Where-Object { $_ -like "$($args[2])*" }
    })]
    [string]$Command = "help",

    [switch]$NoBuild,
    [switch]$Wait,
    [switch]$SkipVerify,
    [switch]$ShowSecrets,
    [switch]$DryRun,
    [switch]$Frontend,
    [switch]$NoFrontend,

    # The launcher switch has a pair of opposed flags. Passing both is a usage
    # error rather than last-one-wins, and the planner is what decides that --
    # putting it here would put the same decision in two languages again.
    [switch]$NoMcp,
    [switch]$Mcp,

    [switch]$Follow,
    [switch]$Volumes,
    [switch]$RemoveOrphans,
    [switch]$Yes,

    [Nullable[int]]$WaitTimeout,
    [string]$EnvFile,
    [string]$ProjectName,
    # A string, not an int: the planner validates it for both launchers.
    [string]$GpuDevice,
    [string[]]$Service,
    [string]$Since,
    [Nullable[int]]$Tail,

    # Without this, anything the binder cannot place -- a second positional
    # or an unknown -Bogus -- is a parameter binding
    # failure. Those happen before the script body runs, exit 1, and never
    # reach Exit-Usage, so Windows answered a usage error with 1 where Unix
    # answered 2. Collecting the leftovers here keeps every argument error
    # inside the body, where the exit-code contract is enforced. It catches
    # unknown named parameters as well as excess positionals.
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Extra
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$root = $PSScriptRoot
$compose = Join-Path $root "compose.yml"
$defaultEnvFile = Join-Path $root ".env"
$envExample = Join-Path $root ".env.example"
$scriptsDir = Join-Path $root "scripts"

$minimumComposeMajor = 2
$minimumComposeMinor = 24
$minimumComposePatch = 4

# Date-based, and identical in stack.sh. It answers "are these two launchers
# the same generation", which is the question worth asking when a Windows and
# a Unix operator disagree about what the stack does; a parity test asserts
# the two strings match.
$launcherVersion = "1.0.0"

function Show-Usage {
    <#
        Print the usage text on the requested stream.

        Usage printed because the operator asked for it is primary output and
        belongs on stdout; usage printed because a command was wrong is an
        error message and belongs on stderr. bash has always drawn that line
        -- its fail_usage does `usage >&2` while `help` prints to stdout --
        and this parameter is what makes PowerShell agree. It also replaces
        Write-Host, which reaches neither stream: `& stack.ps1 help` captured
        zero lines.

        The text is emitted one line per object rather than as one blob, so
        that capturing the output yields a line count instead of a single
        string.
    #>
    param([switch]$ToStandardError)

    $text = @"
Smart Document Manager stack launcher $launcherVersion

Examples:
  pwsh stack.ps1 up                        build and start the GPU OCR stack
  pwsh stack.ps1 up -Wait                  start the GPU OCR stack, wait for health
  pwsh stack.ps1 status                    show what is running
  pwsh stack.ps1 logs -Service ocr-api -Tail 200
  pwsh stack.ps1 urls                          print the endpoints and their authentication state
  pwsh stack.ps1 down                          stop the stack

Commands:
  up                    verify the configuration, build the images, start the stack
  verify                          check the configuration against the cross-service invariants only
  build                 build the images and start nothing
  pull                  refresh the vLLM security image and storage images
  restart               restart the running services without rebuilding
  config                print the resolved Compose configuration, credentials redacted
  urls                            print the published endpoints and each service's auth state
  status                show container status
  logs                  show container logs
  down                            stop and remove the containers
  version                         report the launcher, Docker and Compose versions
  help                            print this text

Options, listed under the command that accepts them:
  up      -NoBuild  -Wait  -WaitTimeout <seconds>  -SkipVerify
  up/config  -Frontend / -NoFrontend  -GpuDevice <index>
  config  -ShowSecrets
  logs    -Tail <lines>  -Follow  -Since <time>  -Service <name>
  down    -Volumes  -RemoveOrphans  -Yes
  every   -DryRun  -EnvFile <path>  -ProjectName <name>

Launcher switches, each overriding its configured value for this run:
  -NoMcp / -Mcp                   MCP_SERVER_ENABLED
  Passing both halves of a pair is a usage error, not last one wins.

Precedence, highest first: the command line, the shell environment, the
environment file, the built-in default.

Exit codes:
  0    success
  1    unmet prerequisite: Docker missing, daemon down, Compose too old, no environment file
  2    usage error, or a failed configuration preflight
       PowerShell native binding errors (missing/duplicate values) exit 1 before the script
  *    anything else is Docker's own exit code, passed through unchanged

scripts/README.md is the full operator reference for both launchers.
"@
    $lines = $text -split "`r?`n"
    if ($ToStandardError) {
        foreach ($line in $lines) { [Console]::Error.WriteLine($line) }
    } else {
        foreach ($line in $lines) { Write-Output $line }
    }
}

function Exit-Usage {
    param([string]$Message)

    [Console]::Error.WriteLine("[ERROR] $Message")
    Show-Usage -ToStandardError
    exit 2
}

function Find-PythonInterpreter {
    foreach ($candidate in @("py", "python3", "python")) {
        $found = Get-Command $candidate -ErrorAction SilentlyContinue
        if ($found) { return $found.Source }
    }
    return $null
}

function Test-ComposeVersionSupported {
    <#
        Compare a Compose version string against the required minimum
        numerically. Lexical comparison gets 2.100.1 wrong -- "100" sorts
        before "24" as text -- and that is exactly the range Compose is in
        now.
    #>
    param([string]$Version)

    $trimmed = $Version.Trim().TrimStart("v")
    if ($trimmed -notmatch '^([0-9]+)(\.([0-9]+))?(\.([0-9]+))?([-+].*)?$') {
        return $false
    }
    $major = 0
    $minor = 0
    $patch = 0
    if (-not [int]::TryParse($Matches[1], [ref]$major)) { return $false }
    if ($Matches.ContainsKey(3) -and -not [int]::TryParse($Matches[3], [ref]$minor)) {
        return $false
    }
    if ($Matches.ContainsKey(5) -and -not [int]::TryParse($Matches[5], [ref]$patch)) {
        return $false
    }
    if ($major -gt $minimumComposeMajor) { return $true }
    if ($major -lt $minimumComposeMajor) { return $false }
    if ($minor -gt $minimumComposeMinor) { return $true }
    if ($minor -lt $minimumComposeMinor) { return $false }
    return $patch -ge $minimumComposePatch
}

# --- argument surface ------------------------------------------------------
#
# One table says which options each command accepts, walked by one loop. The
# alternative -- a per-command validation block -- was five near-identical
# blocks that drifted apart. Parameter sets cannot do this job: an
# unresolvable parameter set is a binder failure, exit 1, body never reached.
# A table shaped like the bash one also keeps the two implementations
# diffable, which is the point.
# -DryRun, -EnvFile and -ProjectName are global, so every command lists them;
# the switch overrides are listed by the commands whose plan they change,
# which includes verify because they change what its token preflight checks.
$globalOptions = @("dry-run", "env-file", "project-name")
$switchOptions = @("no-mcp", "mcp")
$allowedOptions = @{
    "up"     = @(
        "no-build", "wait", "wait-timeout",
        "skip-verify", "frontend", "no-frontend", "gpu-device"
    ) + $switchOptions + $globalOptions
    "verify" = $switchOptions + $globalOptions
    "config" = @(
        "show-secrets", "frontend", "no-frontend", "gpu-device"
    ) +
        $switchOptions + $globalOptions
    "build"   = @() + $switchOptions + $globalOptions
    "pull"    = @() + $switchOptions + $globalOptions
    "restart" = @() + $switchOptions + $globalOptions
    "urls"    = @() + $switchOptions + $globalOptions
    "logs"   = @("tail", "follow", "service", "since") + $globalOptions
    "status" = @() + $globalOptions
    "down"   = @("volumes", "remove-orphans", "yes") + $globalOptions
    "version" = @()
    "help"   = @()
}
$optionDisplayNames = @{
    "frontend"          = "-Frontend"
    "no-frontend"       = "-NoFrontend"
    "no-build"          = "-NoBuild"
    "wait"              = "-Wait"
    "wait-timeout"      = "-WaitTimeout"
    "skip-verify"       = "-SkipVerify"
    "show-secrets"      = "-ShowSecrets"
    "dry-run"           = "-DryRun"
    "tail"              = "-Tail"
    "env-file"          = "-EnvFile"
    "project-name"      = "-ProjectName"
    "gpu-device"        = "-GpuDevice"
    "follow"           = "-Follow"
    "service"           = "-Service"
    "since"             = "-Since"
    "volumes"           = "-Volumes"
    "remove-orphans"    = "-RemoveOrphans"
    "yes"               = "-Yes"
    "no-mcp"            = "-NoMcp"
    "mcp"               = "-Mcp"
}

$script:dryRunIndex = 0

function Write-DryRunStep {
    <#
        Print one step the launcher would run. One argument per line, so an
        argument containing a space is unambiguous, and so the two launchers
        can be compared byte for byte.
    #>
    param([string]$Description, [string[]]$Argv)

    $script:dryRunIndex += 1
    Write-Output "step $($script:dryRunIndex): $Description"
    foreach ($argument in $Argv) {
        Write-Output "  $argument"
    }
}

if ($Extra -and $Extra.Count -gt 0) {
    Exit-Usage "Too many positional arguments."
}

$Command = $Command.Trim().ToLowerInvariant()

$allProfileArguments = @("--profile", "storage")

$tailProvided = $PSBoundParameters.ContainsKey("Tail")
$waitTimeoutProvided = $PSBoundParameters.ContainsKey("WaitTimeout")
$envFileProvided = -not [string]::IsNullOrWhiteSpace($EnvFile)
$projectNameProvided = -not [string]::IsNullOrWhiteSpace($ProjectName)
$gpuDeviceProvided = -not [string]::IsNullOrEmpty($GpuDevice)
# A comma-separated value is split here rather than left to the binder.
# Invoked as `pwsh -File stack.ps1 ... -Service a,b` -- which is how the
# launcher is documented and how the tests drive it -- PowerShell passes every
# argument as a plain string, so `a,b` binds to [string[]] as one element and
# would reach Compose as a service literally named "a,b". Splitting covers
# both call styles; `-Service a,b` from inside a session already arrives as
# two elements and is unaffected. bash spells the same intent as a repeatable
# --service.
$Service = @(
    @($Service) |
        Where-Object { $null -ne $_ } |
        ForEach-Object { $_ -split "," } |
        ForEach-Object { $_.Trim() } |
        Where-Object { $_ }
)
$serviceProvided = @($Service).Count -gt 0
$sinceProvided = -not [string]::IsNullOrWhiteSpace($Since)

if ($tailProvided -and $Tail -lt 1) {
    Exit-Usage "Tail must be a positive integer."
}

if ($waitTimeoutProvided -and $WaitTimeout -lt 1) {
    Exit-Usage "-WaitTimeout requires a positive integer."
}

if (-not $allowedOptions.ContainsKey($Command)) {
    Exit-Usage "Unknown command '$Command'."
}

$providedOptions = @()
if ($Frontend) { $providedOptions += 'frontend' }
if ($NoFrontend) { $providedOptions += 'no-frontend' }
if ($NoBuild) { $providedOptions += "no-build" }
if ($Wait) { $providedOptions += "wait" }
if ($SkipVerify) { $providedOptions += "skip-verify" }
if ($ShowSecrets) { $providedOptions += "show-secrets" }
if ($DryRun) { $providedOptions += "dry-run" }
if ($NoMcp) { $providedOptions += "no-mcp" }
if ($Mcp) { $providedOptions += "mcp" }
if ($Follow) { $providedOptions += "follow" }
if ($Volumes) { $providedOptions += "volumes" }
if ($RemoveOrphans) { $providedOptions += "remove-orphans" }
if ($Yes) { $providedOptions += "yes" }
if ($serviceProvided) { $providedOptions += "service" }
if ($sinceProvided) { $providedOptions += "since" }
if ($tailProvided) { $providedOptions += "tail" }
if ($waitTimeoutProvided) { $providedOptions += "wait-timeout" }
if ($envFileProvided) { $providedOptions += "env-file" }
if ($projectNameProvided) { $providedOptions += "project-name" }
if ($gpuDeviceProvided) { $providedOptions += "gpu-device" }

foreach ($option in $providedOptions) {
    if ($option -notin $allowedOptions[$Command]) {
        Exit-Usage (
            "Command '$Command' does not accept " +
            "$($optionDisplayNames[$option])."
        )
    }
}

if ($Command -eq "help") {
    Show-Usage
    exit 0
}

# `version` reports what is installed, so it runs before -- and instead of --
# the checks that refuse to continue when something is missing. Being told
# "Docker Compose: not available" is the answer an operator came for; exiting
# 1 with the same information on stderr is not.
if ($Command -eq "version") {
    Write-Output "Launcher: $launcherVersion (stack.ps1)"
    if (Get-Command docker -ErrorAction SilentlyContinue) {
        $engine = (
            & docker version --format "{{.Server.Version}}" 2>$null | Out-String
        ).Trim()
        if ($LASTEXITCODE -eq 0 -and $engine) {
            Write-Output "Docker Engine: $engine"
        } else {
            Write-Output (
                "Docker Engine: not reachable (the CLI is installed; the " +
                "daemon did not answer)"
            )
        }
        $composeReported = (& docker compose version --short 2>$null | Out-String).Trim()
        if ($LASTEXITCODE -eq 0 -and $composeReported) {
            Write-Output "Docker Compose: $composeReported"
        } else {
            Write-Output "Docker Compose: not available"
        }
    } else {
        Write-Output "Docker Engine: not installed"
        Write-Output "Docker Compose: not available"
    }
    Write-Output (
        "Required: Docker Compose $minimumComposeMajor.$minimumComposeMinor.$minimumComposePatch or newer"
    )
    exit 0
}

# --- prerequisites ---------------------------------------------------------

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    [Console]::Error.WriteLine("[ERROR] Docker is not available on PATH.")
    exit 1
}

# $LASTEXITCODE has to be read before anything else runs: piping the output
# through Out-String first loses the native exit code, which silently turned
# a missing compose plugin into a confusing "version too old" message.
$composeVersionOutput = & docker compose version --short 2>$null
$composeVersionExitCode = $LASTEXITCODE
$composeVersion = ($composeVersionOutput | Out-String).Trim()
if ($composeVersionExitCode -ne 0 -or -not $composeVersion) {
    [Console]::Error.WriteLine("[ERROR] Docker Compose v2 is not available.")
    exit 1
}
if (-not (Test-ComposeVersionSupported -Version $composeVersion)) {
    [Console]::Error.WriteLine(
        "[ERROR] Docker Compose $minimumComposeMajor.$minimumComposeMinor.$minimumComposePatch or " +
        "newer is required, found '$composeVersion'."
    )
    [Console]::Error.WriteLine(
        "        This stack uses 'docker compose config --environment'."
    )
    exit 1
}
# `docker compose version` never contacts the daemon, so without this probe
# the first real command failed with Docker's own connect error instead of a
# message an operator can act on.
#
# Only the commands that actually reach the daemon are gated on it. `config`
# and `verify` resolve Compose entirely client-side, and being able to check a
# configuration before starting Docker Desktop is exactly when an operator
# wants them.
#
# -DryRun executes nothing, so it does not need a daemon either.
$daemonCommands = @("up", "down", "logs", "status", "build", "pull", "restart")
if ($Command -in $daemonCommands -and -not $DryRun) {
    & docker info *> $null
    if ($LASTEXITCODE -ne 0) {
        [Console]::Error.WriteLine(
            "[ERROR] Docker daemon not reachable -- start Docker Desktop / the " +
            "docker service."
        )
        exit 1
    }
}

# 'verify' answers "is *my* configuration sound", so it deliberately does not
# fall back to .env.example: verifying the versioned template instead would
# silently answer a different question.
# The same holds for `urls`: it reports where *this* stack is published.
$requiresConfiguredEnvironment = $Command -in @(
    "up", "config", "verify", "build", "pull", "restart", "urls"
)
if ($envFileProvided) {
    # An explicit -EnvFile is the operator's own choice of configuration, so
    # it is required to exist even for the recovery commands: silently falling
    # back to .env.example would answer a question they did not ask.
    if (-not (Test-Path -LiteralPath $EnvFile -PathType Leaf)) {
        [Console]::Error.WriteLine("[ERROR] Missing environment file $EnvFile.")
        exit 1
    }
    $runtimeEnvFile = (Resolve-Path -LiteralPath $EnvFile).Path
} elseif ($requiresConfiguredEnvironment) {
    if (-not (Test-Path -LiteralPath $defaultEnvFile -PathType Leaf)) {
        [Console]::Error.WriteLine(
            "[ERROR] Missing $defaultEnvFile. Copy .env.example to .env and configure it first."
        )
        exit 1
    }
    $runtimeEnvFile = $defaultEnvFile
} else {
    if (-not (Test-Path -LiteralPath $envExample -PathType Leaf)) {
        [Console]::Error.WriteLine("[ERROR] Missing repository template $envExample.")
        exit 1
    }
    # Operational recovery commands use stable defaults and remain available
    # even when .env is missing or malformed.
    $runtimeEnvFile = $envExample
}

$base = @("compose")
if ($projectNameProvided) {
    $base += @("--project-name", $ProjectName)
}
$base += @("--file", $compose, "--env-file", $runtimeEnvFile)

# --- recovery commands: pure shell, no planner, no Python ------------------

switch ($Command) {
    "down" {
        $downArguments = $base + $allProfileArguments + "down"
        if ($Volumes) { $downArguments += "--volumes" }
        if ($RemoveOrphans) { $downArguments += "--remove-orphans" }
        if ($DryRun) {
            Write-DryRunStep "stop the stack" (@("docker") + $downArguments)
            exit 0
        }
        # -Volumes destroys named volumes, which can carry data that takes a
        # long time to rebuild. It is confirmed interactively, and refused
        # outright when there is nobody to ask.
        if ($Volumes -and -not $Yes) {
            if ([Console]::IsInputRedirected) {
                [Console]::Error.WriteLine(
                    "[ERROR] -Volumes destroys every named volume of the stack."
                )
                [Console]::Error.WriteLine(
                    "        Standard input is not a terminal, so it cannot " +
                    "be confirmed here; re-run with -Yes."
                )
                exit 2
            }
            [Console]::Error.WriteLine(
                "This removes every named volume of the stack."
            )
            [Console]::Error.Write("Type yes to continue: ")
            $confirmation = [Console]::In.ReadLine()
            if ("$confirmation".Trim().ToLowerInvariant() -ne "yes") {
                [Console]::Error.WriteLine("[ERROR] Aborted; nothing was removed.")
                exit 2
            }
        }
        & docker @downArguments
        exit $LASTEXITCODE
    }
    "logs" {
        $composeArguments = @($base)
        $composeArguments += $allProfileArguments + "logs"
        if ($tailProvided) {
            $composeArguments += @("--tail", [string]$Tail)
        }
        if ($sinceProvided) {
            $composeArguments += @("--since", $Since)
        }
        # -Follow and -Tail are independent in Compose; they were mutually
        # exclusive here only because following was the implicit default.
        # Following stays the default when neither is asked for.
        if ($Follow -or (-not $tailProvided -and -not $sinceProvided)) {
            $composeArguments += "--follow"
        }
        if ($serviceProvided) {
            $composeArguments += @($Service)
        }
        if ($DryRun) {
            Write-DryRunStep "show container logs" (@("docker") + $composeArguments)
            exit 0
        }
        & docker @composeArguments
        exit $LASTEXITCODE
    }
    "status" {
        $composeArguments = @($base)
        $composeArguments += $allProfileArguments + "ps"
        if ($DryRun) {
            Write-DryRunStep "show container status" (@("docker") + $composeArguments)
            exit 0
        }
        & docker @composeArguments
        exit $LASTEXITCODE
    }
}

# --- planned commands: up, config, verify, build, pull, restart, urls ------

$preflightPython = Find-PythonInterpreter
if (-not $preflightPython) {
    [Console]::Error.WriteLine(
        "[ERROR] No Python interpreter found; '$Command' needs one."
    )
    exit 2
}

$originalPlanEnvironment = @{}
$commandExitCode = 0

try {
    $composeEnvironment = & docker @base config --environment 2>$null
    if ($LASTEXITCODE -ne 0) {
        [Console]::Error.WriteLine("[ERROR] Cannot resolve Compose environment; check .env syntax. Sensitive output withheld.")
        exit 2
    }

    # The planner receives the resolved environment and the parsed CLI intent
    # on stdin, never on argv: the environment carries bearer tokens and
    # arguments are world-readable.
    $plannerLines = @($composeEnvironment)
    $plannerLines += "--cli-command=$Command"
    $plannerLines += "--cli-no-build=$($NoBuild.IsPresent.ToString().ToLowerInvariant())"
    $plannerLines += "--cli-wait=$($Wait.IsPresent.ToString().ToLowerInvariant())"
    if ($waitTimeoutProvided) {
        $plannerLines += "--cli-wait-timeout=$WaitTimeout"
    }
    if ($projectNameProvided) {
        $plannerLines += "--cli-project-name=$ProjectName"
    }
    if ($gpuDeviceProvided) {
        $plannerLines += "--cli-gpu-device=$GpuDevice"
    }
    # The opposed flags are passed through raw. Folding them into one value,
    # and refusing a pair that contradicts itself, is the planner's job --
    # doing it here would put the same decision in two languages again.
    foreach ($pair in @(
        @("flag-mcp", $Mcp),
        @("flag-no-mcp", $NoMcp),
        @("flag-frontend", $Frontend),
        @("flag-no-frontend", $NoFrontend)
    )) {
        $value = ([bool]$pair[1]).ToString().ToLowerInvariant()
        $plannerLines += "--cli-$($pair[0])=$value"
    }
    $plannerLines += "--cli-compose-file=$compose"
    $plannerLines += "--cli-env-file=$runtimeEnvFile"

    $planText = $plannerLines -join "`n" |
        & $preflightPython (Join-Path $scriptsDir "stack_core.py")
    if ($LASTEXITCODE -ne 0) { exit 2 }
    $plan = ($planText | Out-String) | ConvertFrom-Json

    foreach ($property in $plan.environment.PSObject.Properties) {
        if (-not $originalPlanEnvironment.ContainsKey($property.Name)) {
            $originalPlanEnvironment[$property.Name] = [Environment]::GetEnvironmentVariable(
                $property.Name, 'Process'
            )
        }
        [Environment]::SetEnvironmentVariable(
            $property.Name, [string]$property.Value, "Process"
        )
    }

    $resolvedMcpServerEnabled = $plan.resolved.mcp_server_enabled

    function Invoke-Verification {
        <#
            Both verifiers signal a violation with exit 1. The launcher's own
            contract reserves 1 for an unmet prerequisite and 2 for a failed
            preflight, so every non-zero result from either one is mapped to
            2 here. Their own contracts are unchanged; only the caller
            reinterprets them.

            The POSIX .env permission warning has no counterpart on this
            side: a Windows file mode says nothing about the real ACL.
        #>

        $lines = @($composeEnvironment)
        $lines += "MCP_SERVER_ENABLED=$resolvedMcpServerEnabled"
        $lines -join "`n" |
            & $preflightPython (Join-Path $scriptsDir "verify_token_config.py")
        if ($LASTEXITCODE -ne 0) { exit 2 }

        # The compose hardening invariants used to run against .env.example
        # only, by hand. Running them against the operator's own env file is
        # the point.
        & $preflightPython (
            Join-Path $scriptsDir "verify_compose_security.py"
        ) --env-file $runtimeEnvFile
        if ($LASTEXITCODE -ne 0) { exit 2 }
    }

    # -DryRun skips verifiers, so it reports what verification would do
    # instead of doing it. That is what makes "-SkipVerify really skips it"
    # checkable without starting anything.
    $verificationWouldRun = (
        $Command -eq "verify" -or ($Command -eq "up" -and -not $SkipVerify)
    )
    if ($DryRun) {
        # Only the two commands verification applies to say anything about
        # it. Reporting "verification would be skipped" under `urls` would
        # describe a decision nobody made.
        if ($Command -in @("up", "verify")) {
            if ($verificationWouldRun) {
                [Console]::Error.WriteLine(
                    "[dry-run] configuration verification would run."
                )
            } else {
                [Console]::Error.WriteLine(
                    "[dry-run] configuration verification would be skipped."
                )
            }
        }
    } else {
        if ($verificationWouldRun) {
            Invoke-Verification
        }
        if ($Command -eq "verify") {
            exit 0
        }
    }

    foreach ($warning in @($plan.warnings)) {
        if ($warning) { [Console]::Error.WriteLine("[WARN] $warning") }
    }
    foreach ($message in @($plan.messages)) {
        if ($message) { [Console]::Error.WriteLine($message) }
    }

    foreach ($step in @($plan.steps)) {
        $stepArgv = @($step.argv)
        if ($DryRun) {
            Write-DryRunStep $step.description $stepArgv
            continue
        }
        $executable = $stepArgv[0]
        $stepArguments = @($stepArgv | Select-Object -Skip 1)
        if ($Command -eq "config" -and -not $ShowSecrets) {
            $configOutput = & $executable @stepArguments 2>$null
            $dockerConfigExitCode = $LASTEXITCODE
            if ($dockerConfigExitCode -ne 0) {
                [Console]::Error.WriteLine(
                    "[ERROR] Docker config failed; sensitive output withheld."
                )
                exit $dockerConfigExitCode
            }
            $configOutput |
                & $preflightPython (Join-Path $scriptsDir "stack_core.py") --redact-config
        } else {
            if ($Command -eq "config") {
                [Console]::Error.WriteLine(
                    "[WARN] -ShowSecrets: every bearer token in the stack is " +
                    "printed in cleartext below."
                )
            }
            & $executable @stepArguments
        }
        $commandExitCode = $LASTEXITCODE
        if ($commandExitCode -ne 0) {
            exit $commandExitCode
        }
    }

    # The summary is primary output, so it goes to stdout, unlike the
    # messages and warnings above. It is printed after every step has
    # succeeded -- the loop above exits on the first failure -- which is what
    # makes it a report of what is now true rather than of what was intended.
    # `urls` has no steps at all and is nothing but this.
    foreach ($summaryLine in @($plan.summary)) {
        if ($summaryLine) { Write-Output $summaryLine }
    }
} finally {
    foreach ($name in $originalPlanEnvironment.Keys) {
        if ($null -eq $originalPlanEnvironment[$name]) {
            [Environment]::SetEnvironmentVariable($name, [NullString]::Value, 'Process')
        } else {
            [Environment]::SetEnvironmentVariable($name, $originalPlanEnvironment[$name], 'Process')
        }
    }
}

exit $commandExitCode
