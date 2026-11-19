<#
Install the skills in .\skills into one or more agent skill directories (Windows).

  .\install.ps1                          symlink every skill into ~\.agents\skills
  .\install.ps1 ~\.claude\skills DIR2    ...or into the given directories instead
  .\install.ps1 -Skill openalex DIR      only the named skill(s); -Skill a,b for several
  .\install.ps1 -Copy DIR                copy instead of symlink (no Developer Mode, or the harness doesn't follow links)
  .\install.ps1 -Force DIR               move an existing, different install to the backup folder and replace it
  .\install.ps1 -Uninstall DIR           remove symlinks that point into this repo
  .\install.ps1 -Check                   only check prerequisites
  .\install.ps1 -Index                   print a skill index for harnesses without native skill support

Same behaviour as install.sh. Symlinks are the default so that `git pull` here updates every
installed copy; creating them needs Windows Developer Mode (Settings > System > For developers)
or an elevated shell. Nothing is ever deleted except symlinks into this repo; -Force moves
things aside. If scripts are blocked: powershell -ExecutionPolicy Bypass -File install.ps1 ...
#>
[CmdletBinding(PositionalBinding = $false)]
param(
    [string[]]$Skill = @(),
    [switch]$Copy,
    [switch]$Force,
    [switch]$Uninstall,
    [switch]$Check,
    [switch]$Index,
    [switch]$Help,
    [Parameter(ValueFromRemainingArguments = $true)][string[]]$Targets = @()
)
$ErrorActionPreference = 'Stop'

$Repo = $PSScriptRoot
$Src = Join-Path $Repo 'skills'
$DefaultTarget = Join-Path $HOME '.agents\skills'
$LocalAppData = if ($env:LOCALAPPDATA) { $env:LOCALAPPDATA } else { Join-Path $HOME 'AppData\Local' }
$AppData = if ($env:APPDATA) { $env:APPDATA } else { Join-Path $HOME 'AppData\Roaming' }
# Backups live outside every skills dir: a backup left beside the original would still
# hold a SKILL.md and be loaded as a duplicate skill.
$BackupDir = Join-Path $LocalAppData 'assistant_skills\backups'

if ($Help) { Get-Help $PSCommandPath -Full | Out-String | Write-Output; exit 0 }
function Fail($m) { [Console]::Error.WriteLine($m); exit 1 }
foreach ($t in $Targets) { if ($t -like '-*') { Fail "unknown option: $t (see: .\install.ps1 -Help)" } }

$all = @(Get-ChildItem -Directory $Src | Where-Object { Test-Path (Join-Path $_.FullName 'SKILL.md') } | ForEach-Object Name)
$skills = @($Skill | ForEach-Object { $_ -split ',' } | Where-Object { $_ })
if ($skills.Count -eq 0) { $skills = $all }
foreach ($s in $skills) {
    if ($all -notcontains $s) { Fail "no such skill: $s (have: $($all -join ' '))" }
}
if ($Targets.Count -eq 0) { $Targets = @($DefaultTarget) }
$Targets = @($Targets | ForEach-Object { if ($_ -match '^~(?=$|[\\/])') { $HOME + $_.Substring(1) } else { $_ } })

# --- prerequisites -----------------------------------------------------------

function Ok($m)   { Write-Output "  [ok]   $m" }
function Warn($m) { Write-Output "  [warn] $m" }
function Miss($m) { Write-Output "  [miss] $m" }
function Have($cmd) { [bool](Get-Command $cmd -ErrorAction SilentlyContinue) }

# Returns the version of the first working interpreter as [version], or $null. The
# WindowsApps python3.exe is a Microsoft Store stub that exits non-zero, so run each one.
function Find-Python {
    foreach ($c in @(@('py', '-3'), @('python'), @('python3'))) {
        if (-not (Have $c[0])) { continue }
        $exe = $c[0]; $pre = @($c | Select-Object -Skip 1)
        try { $v = & $exe @pre -c 'import platform; print(platform.python_version())' 2>$null } catch { continue }
        if ($LASTEXITCODE -eq 0 -and $v) { return [pscustomobject]@{ Cmd = ($c -join ' '); Version = [version]"$v".Trim() } }
    }
    return $null
}

function Test-NonEmpty($path) { (Test-Path -LiteralPath $path -PathType Leaf) -and ((Get-Item -LiteralPath $path).Length -gt 0) }

function Invoke-Check {
    $ErrorActionPreference = 'Continue'  # 5.1 turns a native program's stderr into an error under Stop
    Write-Output 'Prerequisites:'
    $py = Find-Python
    if ($py -and $py.Version -ge [version]'3.10') { Ok "python $($py.Version) (run scripts with: $($py.Cmd), or uv run)" }
    else { Miss 'python >= 3.10 (needed by openalex); install from python.org, which adds the py launcher' }
    $py3 = Get-Command python3 -ErrorAction SilentlyContinue
    if ($py3 -and $py3.Source -like '*\WindowsApps\*') {
        cmd /c 'python3 --version >nul 2>&1'
        if ($LASTEXITCODE -ne 0) { Warn "python3 is the Microsoft Store stub: use 'py -3' or 'uv run' where a skill says python3" }
    }
    $uv = Have 'uv'
    foreach ($s in $skills) {
        switch ($s) {
            'openalex' {
                if ($uv) { Ok 'uv (openalex: provides duckdb for `oa.py sql`)' }
                else { Warn 'uv not found: openalex works except `oa.py sql` (install: https://docs.astral.sh/uv/)' }
                if ($env:OPENALEX_API_KEY -or (Test-NonEmpty (Join-Path $HOME '.config\openalex\api_key'))) { Ok 'OpenAlex API key' }
                else {
                    Warn 'no OpenAlex API key: works, but 1,000 credits/day instead of 10,000.'
                    Warn "  get a free key from an openalex.org account and save it as $HOME\.config\openalex\api_key"
                }
            }
            'pdf-convert' {
                if ($uv) { Ok 'uv (pdf-convert: provides pypdf and keyring)' }
                else { Warn 'uv not found: pdf-convert works, but without page counts before upload or the OS keyring' }
                if (Have 'pandoc') { Ok 'pandoc (pdf-convert: LaTeX output)' }
                else { Warn 'pandoc not found: pdf-convert works except --format latex' }
                $pc = Join-Path $AppData 'pdf-convert'
                $cfg = Join-Path $pc 'config.json'
                $keyring = (Test-Path -LiteralPath $cfg) -and ((Get-Content -Raw -LiteralPath $cfg) -match '"key_store":\s*"keyring"')
                if ($env:DATALAB_API_KEY) { Ok 'Datalab API key ($env:DATALAB_API_KEY)' }
                elseif ((Test-NonEmpty (Join-Path $pc 'api_key')) -or $keyring) { Ok 'Datalab API key stored (`pdfc.py doctor` verifies it)' }
                else {
                    Warn "no Datalab API key: run ``uv run $Src\pdf-convert\pdfc.py setup`` in your own terminal,"
                    Warn '  and give the key a 30-day spend cap at https://www.datalab.to/app/keys'
                }
            }
            'zotero' {
                $zc = if ($env:ZOTERO_ROBOT_CONFIG) { $env:ZOTERO_ROBOT_CONFIG } else { Join-Path $AppData 'zotero-robot\config.toml' }
                if (($py -and $py.Version -ge [version]'3.11') -or $uv) { Ok 'zotero: TOML reader (python >= 3.11, or uv)' }
                else { Miss 'zotero needs python >= 3.11, or uv to supply tomli on 3.10' }
                if (Test-NonEmpty $zc) { Ok "zotero config: $zc (``zot.py doctor`` checks Zotero itself)" }
                else { Warn "no zotero config: run ``py -3 $Src\zotero\zot.py config init``, then edit scope and vault path" }
                if ($env:ZOTERO_API_KEY -or (Test-NonEmpty (Join-Path $AppData 'zotero\api_key'))) { Ok 'Zotero web API key stored' }
                else { Warn 'no Zotero web API key: reads work; write commands will need `zot.py setup`' }
            }
            'papis' {
                if (Have 'papis') {
                    Ok "papis $(((papis --version 2>$null) -split ' ')[-1])"
                    $n = @(papis list --libraries 2>$null | Where-Object { $_ }).Count
                    if ($n -gt 0) { Ok "papis libraries configured: $n" }
                    else { Warn 'papis has no libraries configured (see papis config)' }
                } else { Miss 'papis (install: uv tool install papis, or pipx install papis)' }
            }
        }
    }
}

# --- index for harnesses without native skill support -------------------------

function Get-Frontmatter($field, $file) {
    $lines = Get-Content -LiteralPath $file -Encoding UTF8
    if ($lines.Count -eq 0 -or $lines[0] -ne '---') { return '' }
    for ($i = 1; $i -lt $lines.Count -and $lines[$i] -ne '---'; $i++) {
        if ($lines[$i].StartsWith("${field}: ")) { return $lines[$i].Substring($field.Length + 2) }
    }
    return ''
}

function Invoke-Index {
    Write-Output '## Skills'
    Write-Output ''
    Write-Output 'Before starting a task that matches one of these, read its SKILL.md in full and follow it.'
    Write-Output 'Paths inside a skill are relative to the directory holding its SKILL.md.'
    Write-Output ''
    foreach ($s in $skills) {
        $f = Join-Path $Src "$s\SKILL.md"
        Write-Output "- **$(Get-Frontmatter 'name' $f)** (${f}): $(Get-Frontmatter 'description' $f)"
    }
}

# --- install / uninstall -------------------------------------------------------

function Get-LinkTarget($path) {  # target of a symlink or junction, else $null
    $item = Get-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
    if (-not $item -or -not $item.LinkType) { return $null }
    $t = @($item.Target)[0]
    if (-not [IO.Path]::IsPathRooted($t)) { $t = Join-Path (Split-Path $path) $t }
    return [IO.Path]::GetFullPath($t).TrimEnd('\')
}

# Removes a link only. Remove-Item on a directory link can recurse into the target on
# Windows PowerShell 5.1; Directory.Delete without recursion never does.
function Remove-Link($path) {
    $item = Get-Item -LiteralPath $path -Force
    if ($item.PSIsContainer) { [IO.Directory]::Delete($path, $false) } else { [IO.File]::Delete($path) }
}

function Install-One($s, $dir) {
    $srcPath = [IO.Path]::GetFullPath((Join-Path $Src $s)).TrimEnd('\')
    $dst = Join-Path $dir $s
    $target = Get-LinkTarget $dst
    if ($target -and $target -eq $srcPath -and -not $Copy) { Write-Output "  = $dst (already linked)"; return }
    if (Test-Path -LiteralPath $dst) {
        if (-not $Force) {
            Write-Output "  ! $dst exists and is not a link to this repo; skipped (use -Force to move it aside)"
            return
        }
        if ($target) { Remove-Link $dst }     # a link: removing it touches no data
        else {
            $bak = Join-Path $BackupDir "$s-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
            New-Item -ItemType Directory -Force $BackupDir | Out-Null
            Move-Item -LiteralPath $dst $bak
            Write-Output "  ~ moved existing $dst -> $bak"
        }
    }
    if ($Copy) {
        Copy-Item -Recurse -LiteralPath $srcPath $dst
        Get-ChildItem -LiteralPath $dst -Recurse -Directory -Filter __pycache__ | Remove-Item -Recurse -Force
        Write-Output "  + $dst (copy)"
    } else {
        # mklink, not New-Item: Windows PowerShell 5.1 does not ask for the unprivileged
        # symlink that Developer Mode allows. A junction needs no privilege at all.
        cmd /c "mklink /D `"$dst`" `"$srcPath`" >nul 2>&1"
        if ($LASTEXITCODE -eq 0) { Write-Output "  + $dst -> $srcPath"; return }
        try { New-Item -ItemType Junction -Path $dst -Target $srcPath | Out-Null }
        catch { Write-Output "  ! cannot link $dst ($($_.Exception.Message.Trim())); rerun with -Copy"; return }
        Write-Output "  + $dst -> $srcPath (junction: no Developer Mode)"
    }
}

function Uninstall-One($s, $dir) {
    $srcPath = [IO.Path]::GetFullPath((Join-Path $Src $s)).TrimEnd('\')
    $dst = Join-Path $dir $s
    if ((Get-LinkTarget $dst) -eq $srcPath) { Remove-Link $dst; Write-Output "  - $dst" }
    elseif (Test-Path -LiteralPath $dst) { Write-Output "  ! $dst is not a link to this repo; left alone" }
}

if ($Check) { Invoke-Check; exit 0 }
if ($Index) { Invoke-Index; exit 0 }
foreach ($t in $Targets) {
    Write-Output "${t}:"
    if (-not $Uninstall) { New-Item -ItemType Directory -Force $t | Out-Null }
    if (-not (Test-Path -LiteralPath $t -PathType Container)) { Write-Output '  (does not exist)'; continue }
    foreach ($s in $skills) { if ($Uninstall) { Uninstall-One $s $t } else { Install-One $s $t } }
}
if (-not $Uninstall) { Write-Output ''; Invoke-Check }
exit 0
