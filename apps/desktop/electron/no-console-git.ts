// Electron is a GUI-subsystem process, so a direct git.exe spawn allocates its
// own console even when Node's windowsHide is set. windowsHide is SW_HIDE and,
// from a console-less parent, does not stop that flash (hermes_cli/_subprocess_compat.py).
//
// The flag that does is CREATE_NO_WINDOW (0x08000000). A console-subsystem
// python.exe started with that flag owns one hidden console; git is then
// spawned from that host with the same flag, and the git argv is forwarded
// unchanged. simple-git cannot take a creation flag, so its binary tuple is
// [python, this host script].

import { type ChildProcess, execFile, execFileSync, spawn, type SpawnOptions } from 'node:child_process'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

export const CREATE_NO_WINDOW = 0x08000000

export const NO_CONSOLE_GIT_SCRIPT = `import json, os, subprocess, sys
git = json.loads(os.environ["HERMES_GIT_ARGV0"])
argv = [git, *sys.argv[1:]]
kwargs = {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE}
if sys.platform == "win32" or os.environ.get("HERMES_GIT_NO_CONSOLE") == "1":
    kwargs["creationflags"] = 0x08000000
if os.environ.get("HERMES_GIT_DRY_RUN") == "1":
    sys.stdout.write(json.dumps({"argv": argv, "creationflags": kwargs.get("creationflags", 0)}))
    raise SystemExit(0)
proc = subprocess.run(argv, **kwargs)
sys.stdout.buffer.write(proc.stdout or b"")
sys.stderr.buffer.write(proc.stderr or b"")
code = proc.returncode
raise SystemExit(code if isinstance(code, int) and code >= 0 else 1)
`

const PIPED_STDIO = ['ignore', 'pipe', 'pipe'] as const

export interface NoConsoleGitPlan {
  command: string
  args: string[]
  env: NodeJS.ProcessEnv
  windowsHide: boolean
  stdio: ['ignore', 'pipe', 'pipe']
  creationFlags: number
}

export interface NoConsoleGitHost {
  isWindows: true
  pythonBin: string
  scriptPath: string
}

let configuredRoots: string[] = []

export function setNoConsoleGitRoots(roots: Array<string | null | undefined | false>) {
  configuredRoots = roots.filter((root): root is string => Boolean(root))
}

function isPythonW(candidate: string) {
  return path.win32.basename(candidate).toLowerCase() === 'pythonw.exe'
}

function isWindowsAppsStub(candidate: string) {
  return candidate.replace(/\\/g, '/').toLowerCase().includes('/windowsapps/')
}

export function resolveNoConsolePython({
  isWindows,
  env = process.env,
  roots = configuredRoots,
  fileExists = fs.existsSync
}: {
  isWindows: boolean
  env?: NodeJS.ProcessEnv
  roots?: string[]
  fileExists?: (candidate: string) => boolean
}): string | null {
  if (!isWindows) {
    return null
  }

  const candidates: string[] = []
  const override = env.HERMES_DESKTOP_PYTHON

  if (override) {
    candidates.push(override)
  }

  const hermesRoot = env.HERMES_DESKTOP_HERMES_ROOT
  const searchRoots = hermesRoot ? [hermesRoot, ...roots] : roots

  for (const root of searchRoots) {
    if (!root) {
      continue
    }

    candidates.push(path.win32.join(root, '.venv', 'Scripts', 'python.exe'))
    candidates.push(path.win32.join(root, 'venv', 'Scripts', 'python.exe'))
  }

  for (const candidate of candidates) {
    if (!candidate || isPythonW(candidate) || isWindowsAppsStub(candidate)) {
      continue
    }

    try {
      if (fileExists(candidate)) {
        return candidate
      }
    } catch {
      continue
    }
  }

  return null
}

export function ensureNoConsoleGitScript(dir = os.tmpdir()) {
  const scriptPath = path.join(dir, 'hermes-no-console-git.py')

  try {
    if (fs.readFileSync(scriptPath, 'utf8') === NO_CONSOLE_GIT_SCRIPT) {
      return scriptPath
    }
  } catch {
    // Missing or unreadable: rewrite below.
  }

  fs.writeFileSync(scriptPath, NO_CONSOLE_GIT_SCRIPT, { encoding: 'utf8', mode: 0o600 })

  return scriptPath
}

export function windowsGitHost(isWindows = process.platform === 'win32'): NoConsoleGitHost | null {
  if (!isWindows) {
    return null
  }

  const pythonBin = resolveNoConsolePython({ isWindows: true })

  if (!pythonBin) {
    return null
  }

  try {
    return { isWindows: true, pythonBin, scriptPath: ensureNoConsoleGitScript() }
  } catch {
    return null
  }
}

export function noConsoleGitEnv(base: NodeJS.ProcessEnv | undefined, gitBin: string): NodeJS.ProcessEnv {
  const env: NodeJS.ProcessEnv = {}

  for (const [key, value] of Object.entries(base || {})) {
    if (value !== undefined) {
      env[key] = value
    }
  }

  env.HERMES_GIT_ARGV0 = JSON.stringify(gitBin || 'git')

  if (!env.GIT_TERMINAL_PROMPT) {
    env.GIT_TERMINAL_PROMPT = '0'
  }

  return env
}

export function planNoConsoleGitSpawn({
  gitBin,
  args,
  isWindows = false,
  pythonBin = null,
  scriptPath = null,
  env
}: {
  gitBin: string
  args: string[]
  isWindows?: boolean
  pythonBin?: string | null
  scriptPath?: string | null
  env?: NodeJS.ProcessEnv
}): NoConsoleGitPlan {
  if (isWindows && pythonBin && scriptPath) {
    return {
      command: pythonBin,
      args: [scriptPath, ...args],
      env: noConsoleGitEnv(env, gitBin),
      windowsHide: true,
      stdio: [...PIPED_STDIO],
      creationFlags: CREATE_NO_WINDOW
    }
  }

  return {
    command: gitBin || 'git',
    args,
    env: { ...(env || {}) },
    windowsHide: Boolean(isWindows),
    stdio: [...PIPED_STDIO],
    creationFlags: 0
  }
}

// simple-git 4.0.2's environment guard (`allowEnvironment`, measured against
// the exact installed package) governs only GIT-class names — `GIT_*` prefixed
// plus its built-in danger list (EDITOR, VISUAL, PAGER, GIT_EDITOR,
// GIT_SSH_COMMAND, GIT_CONFIG*, SSH_ASKPASS, ...): ambient GIT-class names NOT
// in the option's list are stripped from every spawn, and a GIT-class name set
// through the executor env (`.env()`) must be listed or the wrapper THROWS.
// Non-GIT ambient names pass through that guard untouched on the ambient path.
// The product lists exactly one name there (`GIT_TERMINAL_PROMPT`, see
// git-review-ops); the list below is a second, separate mechanism: it is the
// executor-env replacement set for the Windows no-console host tuple, where the
// executor env REPLACES the whole inherited environment (measured: with an
// executor env set, the child receives only the executor values). Every entry
// is a name python.exe and the hosted git need — every entry is named and
// justified; adding a name here is a product decision. Deliberately NOT copied
// from the caller's environment: ambient GIT_* names (the guard strips the
// dangerous ones on the ambient path anyway), credentials, tokens, secrets.
export const GIT_ENV_ALLOWLIST: readonly string[] = [
  // identity + config discovery (global .gitconfig, ~/.config/git)
  'home',
  // executables: ssh, credential helpers, hooks resolved through PATH
  'path',
  // ssh agent socket (Windows pageant too) for pushed/fetched remotes
  'ssh_auth_sock',
  // XDG config location when it is not the default under HOME
  'xdg_config_home',
  // commit/log date formatting
  'tz',
  // git's own message/locale handling
  'lang', 'lc_all', 'lc_ctype', 'lc_messages',
  // TLS trust overrides (CI runners), git https verification
  'ssl_cert_file', 'ssl_cert_dir',
  // corporate proxies for https remotes (git/curl read all four spellings)
  'http_proxy', 'https_proxy', 'all_proxy', 'no_proxy',
  // Windows: the Win32 CRT requires SYSTEMROOT; identity/GCM resolves through
  // USERPROFILE/HOMEDRIVE+HOMEPATH/APPDATA; PROGRAMFILES/ComSpec/PATHEXT are
  // needed to spawn credential managers and helpers.
  'systemroot', 'systemdrive', 'userprofile', 'homedrive', 'homepath',
  'appdata', 'localappdata', 'programdata', 'programfiles', 'comspec',
  'pathext'
]

// Windows short (8.3) path conversion for simple-git's ASCII binary whitelist,
// memoized per target. GetShortPathNameW is the only whitelist-clean
// representation of a restricted path (e.g. `C:\Program Files\...` becomes
// `C:\PROGRA~1\...`) that does NOT need `unsafe.allowUnsafeCustomBinary`
// (owner decision 2026-10-18: no unsafe opt-outs). Called with the resolved
// python host binary's path and the target as separate argv elements —
// `cmd.exe` string interpolation would open a new shell-injection path, so the
// conversion runs through python's ctypes instead. When the volume has 8dot3
// names disabled GetShortPathNameW returns the long path unchanged (or 0 when
// it cannot), so the caller keeps the restricted path and FAILS CLOSED; the
// conversion never upgrades a refused path to a PATH lookup (which could
// resolve a repo-local git.exe).
const WINDOWS_SHORT_PATH_CACHE = new Map<string, string>()
const WINDOWS_SHORT_PATH_CTYPE = `import ctypes, sys
buf = ctypes.create_unicode_buffer(4096)
rc = ctypes.windll.kernel32.GetShortPathNameW(sys.argv[1], buf, 4096)
sys.stdout.write(buf.value if rc else "")
`

export function windowsShortPath(target: string, pythonBin: string): string {
  const cached = WINDOWS_SHORT_PATH_CACHE.get(target)
  if (cached !== undefined) {
    return cached
  }

  const short = execFileSync(pythonBin, ['-c', WINDOWS_SHORT_PATH_CTYPE, target], {
    windowsHide: true,
    encoding: 'utf8'
  }).trim()

  if (short !== target) {
    WINDOWS_SHORT_PATH_CACHE.set(target, short)
  }

  return short
}

// The executor env a spawned git receives on the Windows no-console host
// tuple: exactly the allowlist names valued from the caller's environment,
// plus the argv0 marker and the terminal prompt guard. A set executor env
// REPLACES the whole inherited environment (measured against simple-git
// 4.0.2), so this is where the no-console path stops forwarding secrets —
// copying the full process.env here would both leak ambient
// GIT_EDITOR/GIT_PAGER-class variables into the host script's throw path and
// pass every secret in the process to each spawned git. GIT_TERMINAL_PROMPT
// is allowlisted for simple-git's guard (a GIT-class name in an executor env
// must be listed or the wrapper throws) and keeps its caller-set value, only
// defaulting to the no-prompt guard when unset — matching the old
// noConsoleGitEnv behavior.
export function gitSpawnEnv(base: NodeJS.ProcessEnv | undefined, gitBin: string): NodeJS.ProcessEnv {
  const env: NodeJS.ProcessEnv = {}
  for (const name of GIT_ENV_ALLOWLIST) {
    const value = base?.[name] ?? base?.[name.toUpperCase()]
    if (value !== undefined) {
      env[name.toUpperCase()] = value
    }
  }
  env.GIT_TERMINAL_PROMPT = base?.GIT_TERMINAL_PROMPT ?? base?.git_terminal_prompt ?? '0'
  env.HERMES_GIT_ARGV0 = JSON.stringify(gitBin || 'git')
  return env
}

export function simpleGitBinary(
  gitBin: string | undefined,
  host?: { isWindows: boolean; pythonBin?: string | null; scriptPath?: string | null } | null
): string | [string, string] {
  if (host?.isWindows && host.pythonBin && host.scriptPath) {
    return [host.pythonBin, host.scriptPath]
  }

  return gitBin || 'git'
}

export function hiddenGitSpawnSpec(
  gitBin: string,
  args: string[],
  options: SpawnOptions & { isWindows?: boolean } = {}
) {
  const isWindows = options.isWindows ?? process.platform === 'win32'
  const host = isWindows ? windowsGitHost(true) : null

  const plan = planNoConsoleGitSpawn({
    gitBin,
    args,
    isWindows,
    pythonBin: host?.pythonBin ?? null,
    scriptPath: host?.scriptPath ?? null,
    env: (options.env as NodeJS.ProcessEnv | undefined) || process.env
  })

  const { isWindows: _ignored, ...rest } = options

  return {
    command: plan.command,
    args: plan.args,
    creationFlags: plan.creationFlags,
    options: {
      ...rest,
      env: plan.env,
      windowsHide: plan.windowsHide || rest.windowsHide,
      stdio: rest.stdio ?? plan.stdio
    }
  }
}

// Timed commands still running. On POSIX they lead their own process group,
// so nothing reaches them when the app exits unless we do it here (#125243:
// probes and their promisor fetches outliving a restart, reparented to PID 1).
const timedChildren = new Set<ChildProcess>()

const TASKKILL_OPTIONS = { windowsHide: true, timeout: 5000 }

// On Windows a root that already exited has no tree left for taskkill /T to
// walk, and its pid may already belong to someone else. A POSIX group
// outlives its leader, so it is always signaled.
function hasTreeToKill(child: ChildProcess): child is ChildProcess & { pid: number } {
  if (!child.pid) {
    return false
  }

  return process.platform !== 'win32' || (child.exitCode === null && child.signalCode === null)
}

function killGroup(child: ChildProcess & { pid: number }): void {
  try {
    process.kill(-child.pid, 'SIGKILL')
  } catch {
    child.kill('SIGKILL')
  }
}

/** Kill every timed git command still running, with its descendants. For app quit. */
export function killTimedGitChildren(): void {
  const live = [...timedChildren].filter(hasTreeToKill)
  timedChildren.clear()

  if (process.platform !== 'win32') {
    live.forEach(killGroup)

    return
  }

  if (live.length === 0) {
    return
  }

  // One synchronous call for every tree: will-quit cannot wait on N serial ones.
  try {
    execFileSync('taskkill', [...live.flatMap(child => ['/PID', String(child.pid)]), '/T', '/F'], {
      ...TASKKILL_OPTIONS,
      stdio: 'ignore'
    })
  } catch {
    live.forEach(child => child.kill('SIGKILL'))
  }
}

export function execGit(
  gitBin: string,
  args: string[],
  options: { cwd?: string; env?: NodeJS.ProcessEnv; timeoutMs?: number } = {}
): Promise<{ code: number | null; stdout: string; stderr: string }> {
  const spec = hiddenGitSpawnSpec(gitBin, args, {
    cwd: options.cwd,
    env: options.env,
    // Timed commands own a POSIX group so a promisor fetch cannot outlive
    // the git process. On Windows taskkill follows the Python host's tree.
    detached: Boolean(options.timeoutMs) && process.platform !== 'win32',
    stdio: ['ignore', 'pipe', 'pipe']
  })

  return new Promise((resolve, reject) => {
    const child = spawn(spec.command, spec.args, spec.options)
    let stdout = ''
    let stderr = ''
    let settled = false
    let timeoutError: (NodeJS.ErrnoException & { stderr?: string }) | undefined

    const finish = (error?: Error, code: number | null = child.exitCode) => {
      if (settled) {
        return
      }

      settled = true

      if (timer) {
        clearTimeout(timer)
      }

      if (error) {
        reject(error)

        return
      }

      resolve({ code, stdout, stderr })
    }

    const timer = options.timeoutMs
      ? setTimeout(() => {
          timeoutError = Object.assign(new Error('git timed out'), { code: 'ETIMEDOUT', stderr })

          const done = () => {
            // Pipes held by descendants must not keep Electron alive after a
            // failed tree kill. The timeout remains a failure, never exit 0.
            child.stdout?.destroy()
            child.stderr?.destroy()
            timedChildren.delete(child)
            finish(timeoutError)
          }

          if (!hasTreeToKill(child)) {
            done()
          } else if (process.platform === 'win32') {
            execFile('taskkill', ['/PID', String(child.pid), '/T', '/F'], TASKKILL_OPTIONS, error => {
              if (error) {
                child.kill('SIGKILL')
              }

              done()
            })
          } else {
            killGroup(child)
            done()
          }
        }, options.timeoutMs)
      : null

    child.stdout?.on('data', chunk => {
      stdout += chunk.toString()
    })
    child.stderr?.on('data', chunk => {
      stderr += chunk.toString()
    })

    if (timer) {
      timedChildren.add(child)
      child.once('close', () => timedChildren.delete(child))
    }

    child.once('error', error => {
      timedChildren.delete(child)
      finish(error)
    })
    child.once('close', code => {
      // On Windows wait for taskkill's completion, not just the root's exit.
      if (!timeoutError) {
        finish(undefined, code)
      }
    })
  })
}
