import assert from 'node:assert/strict'
import { execFileSync } from 'node:child_process'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { simpleGit } from 'simple-git'
import { afterEach, test, vi } from 'vitest'

import {
  gitFor,
  repoStatus,
  resolveRenamePath,
  REVIEW_FILE_CAP,
  reviewCommit,
  reviewCreatePr,
  reviewList,
  SIMPLE_GIT_BINARY_ALLOWANCE,
  SIMPLE_GIT_BINARY_REFUSED
} from './git-review-ops'
import type * as NoConsoleGit from './no-console-git'

// `runGh` shells to the `gh` CLI via execFile. Mock it so reviewCreatePr's gh
// invocation is controllable (real `gh` may be absent or slow in CI) while the
// repo setup below still uses the real execFileSync.
vi.mock('node:child_process', async importOriginal => {
  const actual = await importOriginal<{ execFile: unknown; execFileSync: unknown }>()

  return { ...actual, execFile: vi.fn() }
})

const refused = (err: unknown): boolean =>
  err instanceof Error && err.message === SIMPLE_GIT_BINARY_REFUSED

const tempDirs: string[] = []

afterEach(() => {
  for (const dir of tempDirs.splice(0)) {
    fs.rmSync(dir, { force: true, recursive: true })
  }
})

function makeRepo() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-desktop-git-status-'))

  tempDirs.push(dir)
  execFileSync('git', ['init', '-q'], { cwd: dir })
  execFileSync('git', ['config', 'user.email', 'hermes-test@example.com'], { cwd: dir })
  execFileSync('git', ['config', 'user.name', 'Hermes Test'], { cwd: dir })
  fs.writeFileSync(path.join(dir, 'tracked.txt'), 'tracked\n')
  execFileSync('git', ['add', 'tracked.txt'], { cwd: dir })
  execFileSync('git', ['commit', '-qm', 'initial'], { cwd: dir })

  return dir
}

test('resolveRenamePath: plain path is unchanged', () => {
  assert.equal(resolveRenamePath('src/a.ts'), 'src/a.ts')
})

test('gitFor fails closed on a restricted custom binary path (no unsafe hatch)', () => {
  // simple-git 4.x refuses a binary path outside its ASCII whitelist unless the
  // unsafe custom-binary hatch is set — and the product does not set it (owner
  // decision 2026-10-18: no unsafe opt-outs). On POSIX there is no short-path
  // conversion, so the spaced `Program Files` form surfaces as the refusal.
  assert.throws(() => gitFor(process.cwd(), 'C:\\Program Files\\Git\\cmd\\git.exe'), refused)
})

test('gitFor accepts a whitelist-clean custom binary path', () => {
  assert.doesNotThrow(() => gitFor(process.cwd(), '/usr/local/bin/git'))
  assert.doesNotThrow(() => gitFor(process.cwd(), 'C:/PROGRA~1/Git/cmd/git.exe'))
  assert.match('/usr/local/bin/git', SIMPLE_GIT_BINARY_ALLOWANCE)
})

test('gitFor fails closed on restricted non-space characters (POSIX has no short path)', () => {
  // simple-git's whitelist is `/^([a-z]:)?([a-z0-9/.\_~-]+)$/i`, so parentheses
  // (`Program Files (x86)`), `+`, and accented profile dirs (`C:\Users\João\...`)
  // are rejected exactly like a space. simple-git 3.x passed all of these through
  // the `unsafe.allowUnsafeCustomBinary` hatch; the migration to 4.0.2 removes the
  // hatch (owner decision 2026-10-18: no unsafe opt-outs), and on POSIX there is no
  // short-path conversion — so these paths now FAIL CLOSED instead of silently
  // downgrading to a PATH lookup (which could resolve a repo-local git.exe).
  const restrictedBinaries = [
    String.raw`C:\Git(x86)\cmd\git.exe`,
    String.raw`C:\tools\git+portable\cmd\git.exe`,
    String.raw`C:\Users\João\AppData\Local\hermes\git\cmd\git.exe`
  ]

  for (const binary of restrictedBinaries) {
    // strict assert compares the thrown message against the second argument.
    assert.throws(() => gitFor(process.cwd(), binary), refused)
  }
})

// SYNTHETIC WINDOWS CONTROL — no real Windows host runs on this test runner:
// `windowsGitHost` and the 8.3 conversion are mocked and `process.platform` is
// stubbed to `win32` so the conversion branch executes. This asserts the
// conversion contract (argv-shaped calls, fail-closed refusals), NOT a real
// Windows target acceptance.
test('gitFor converts a restricted Windows host tuple to short paths (synthetic)', async () => {
  const pythonBin = String.raw`C:\Tools\python-3.14+build\python.exe`
  const scriptPath = String.raw`C:\Users\Hermes Dev\AppData\Local\Temp\hermes-no-console-git.py`
  const calls: Array<[string, string]> = []

  vi.resetModules()
  vi.doMock('./no-console-git', async importOriginal => {
    const actual = await importOriginal<typeof NoConsoleGit>()

    return {
      ...actual,
      windowsGitHost: () => ({ isWindows: true, pythonBin, scriptPath }),
      windowsShortPath: (target: string, host: string) => {
        // argv-shaped conversion contract: the raw target arrives as its own
        // argument — never embedded in a cmd.exe command string — together
        // with the python host that runs it.
        calls.push([target, host])
        return target.toUpperCase().replace(/ /g, '~').replace(/[()+]/g, '')
      }
    }
  })

  const platformSpy = vi.spyOn(process, 'platform', 'get').mockReturnValue('win32')

  try {
    const { gitFor: gitForWithHostTuple } = await import('./git-review-ops')

    assert.doesNotThrow(() => gitForWithHostTuple(process.cwd(), 'git'), 'a converted tuple should be accepted')
    assert.deepEqual(calls, [
      [pythonBin, pythonBin],
      [scriptPath, pythonBin]
    ])
  } finally {
    platformSpy.mockRestore()
    vi.doUnmock('./no-console-git')
    vi.resetModules()
  }
})

test('gitFor fails closed when the 8.3 short-path conversion is unavailable (synthetic)', async () => {
  const pythonBin = String.raw`C:\Tools\python-3.14+build\python.exe`
  const scriptPath = String.raw`C:\Users\Hermes Dev\AppData\Local\Temp\hermes-no-console-git.py`

  vi.resetModules()
  vi.doMock('./no-console-git', async importOriginal => {
    const actual = await importOriginal<typeof NoConsoleGit>()

    return {
      ...actual,
      windowsGitHost: () => ({ isWindows: true, pythonBin, scriptPath }),
      // A volume with 8dot3 names disabled: GetShortPathNameW returns the long
      // path unchanged, so the conversion cannot produce a whitelist-clean
      // element and the caller must refuse.
      windowsShortPath: (target: string) => target
    }
  })

  const platformSpy = vi.spyOn(process, 'platform', 'get').mockReturnValue('win32')

  try {
    const { gitFor: gitForWithHostTuple } = await import('./git-review-ops')

    assert.throws(
      () => gitForWithHostTuple(process.cwd(), 'git'),
      (err: unknown): boolean => err instanceof Error && err.message === SIMPLE_GIT_BINARY_REFUSED
    )
  } finally {
    platformSpy.mockRestore()
    vi.doUnmock('./no-console-git')
    vi.resetModules()
  }
})

test('gitFor no longer patches console.warn (4.0.2 has no custom-binary warning to filter)', () => {
  const originalWarn = console.warn
  const warnings: unknown[][] = []

  const recordingWarn = (...args: unknown[]) => {
    warnings.push(args)
  }

  console.warn = recordingWarn

  try {
    gitFor(process.cwd(), 'git')

    assert.equal(console.warn, recordingWarn)
  } finally {
    console.warn = originalWarn
  }

  assert.deepEqual(warnings, [])
})

test('a product git spawn keeps the function env and strips the advisory vectors (real)', async () => {
  const dir = makeRepo()
  const dump = path.join(dir, 'hook-env.txt')

  fs.mkdirSync(path.join(dir, '.git', 'hooks'), { recursive: true })
  fs.writeFileSync(
    path.join(dir, '.git', 'hooks', 'pre-commit'),
    `#!/bin/sh\nenv | sort > ${JSON.stringify(dump)}\nexit 0\n`,
    { mode: 0o755 }
  )

  const previous: Record<string, string | undefined> = {
    GIT_PAGER: process.env.GIT_PAGER,
    GIT_EDITOR: process.env.GIT_EDITOR,
    GIT_AUTHOR_NAME: process.env.GIT_AUTHOR_NAME,
    GIT_CONFIG_COUNT: process.env.GIT_CONFIG_COUNT,
    GIT_TERMINAL_PROMPT: process.env.GIT_TERMINAL_PROMPT
  }
  const added: string[] = []

  const setEnv = (name: string, value: string) => {
    if (process.env[name] === undefined) {
      added.push(name)
    }
    process.env[name] = value
  }

  setEnv('GIT_PAGER', 'evil-pager')
  setEnv('GIT_EDITOR', 'evil-editor')
  setEnv('GIT_AUTHOR_NAME', 'ambient-evil')
  setEnv('GIT_CONFIG_COUNT', '1')
  setEnv('GIT_CONFIG_KEY_0', 'core.pager')
  setEnv('GIT_CONFIG_VALUE_0', 'evil')
  setEnv('GIT_TERMINAL_PROMPT', '0')

  try {
    const { ok } = await reviewCommit(dir, 'hook env probe', false, 'git')
    assert.equal(ok, true)

    const env = Object.fromEntries(
      fs
        .readFileSync(dump, 'utf8')
        .trim()
        .split('\n')
        // `env` emits continuation lines without '=' for multi-line values;
        // those are values, not keys.
        .filter(line => line.includes('='))
        .map(line => [line.slice(0, line.indexOf('=')), line.slice(line.indexOf('=') + 1)])
    )

    // Function preserved: executables and identity resolution still reach git.
    assert.ok(env.PATH, 'PATH must reach the spawned git')
    assert.ok(env.HOME, 'HOME must reach the spawned git')
    // The one allowlisted GIT-class name flows.
    assert.equal(env.GIT_TERMINAL_PROMPT, '0')
    // simple-git 4.0.2's native guard strips ambient GIT-class names outside
    // the allowlist — including the advisory vectors and the env-config
    // injection path — before the spawn.
    assert.equal(env.GIT_PAGER, undefined)
    assert.equal(env.GIT_CONFIG_COUNT, undefined)
    // The abbreviation-options guard marker reaches every spawn.
    assert.equal(env.GIT_TEST_DISALLOW_ABBREVIATED_OPTIONS, 'true')
    // With the ambient author injection stripped, the commit identity is the
    // repo config's — the existing function keeps working.
    const author = execFileSync('git', ['log', '-1', '--pretty=%an <%ae>'], { cwd: dir, encoding: 'utf8' }).trim()
    assert.equal(author, 'Hermes Test <hermes-test@example.com>')
  } finally {
    for (const name of added) {
      delete process.env[name]
    }
    for (const [name, value] of Object.entries(previous)) {
      if (value === undefined) {
        delete process.env[name]
      } else {
        process.env[name] = value
      }
    }
  }
})

test('the product git instance refuses the advisory vectors at the 4.0.2 guard', async () => {
  const fresh = () => gitFor(process.cwd(), 'git')

  // argv vectors — caught by the native parser before git ever spawns
  await assert.rejects(() => fresh().raw(['-c', 'alias.x=evil']), /allowUnsafeAlias/)
  await assert.rejects(() => fresh().raw(['-c', 'include.path=/evil']), /allowUnsafeInclude/)
  await assert.rejects(() => fresh().raw(['-c', 'trailer.command=evil']), /allowUnsafeCommandBinaries/)
  await assert.rejects(() => fresh().raw(['-c', 'core.editor=evil']), /allowUnsafeEditor/)
  await assert.rejects(() => fresh().raw(['-C', '/etc']), /allowUnsafeConfigPaths/)
  await assert.rejects(() => fresh().raw(['--exec-path=/evil']), /allowUnsafeExec/)
  // executor-env vectors
  await assert.rejects(() => fresh().env({ GIT_EDITOR: 'evil' }).raw(['--version']), /allowUnsafeEditor/)
  await assert.rejects(() => fresh().env({ GIT_SSH_COMMAND: 'evil' }).raw(['--version']), /allowUnsafeSshCommand/)
  await assert.rejects(() =>
    fresh().env({ GIT_CONFIG_PARAMETERS: "'core.editor=evil'" }).raw(['--version']), /allowUnsafeConfigEnvCount/
  )
  // an allowlisted executor name still passes (the product's prompt guard)
  await assert.doesNotReject(() => fresh().env({ GIT_TERMINAL_PROMPT: '0' }).raw(['--version']))
  // the wrapper itself refuses a restricted binary with no hatch available
  assert.throws(() => simpleGit({ binary: String.raw`C:\Program Files\Git\cmd\git.exe` }), /restricted characters/)
})

test('resolveRenamePath: simple rename resolves to the new path', () => {
  assert.equal(resolveRenamePath('old.ts => new.ts'), 'new.ts')
})

test('resolveRenamePath: brace rename resolves to the new path', () => {
  assert.equal(resolveRenamePath('src/{old => new}/file.ts'), 'src/new/file.ts')
})

test('resolveRenamePath: brace rename collapsing a segment', () => {
  assert.equal(resolveRenamePath('src/{lib => }/file.ts'), 'src/file.ts')
})

test('repoStatus reports an untracked directory without recursively listing its contents', async () => {
  const dir = makeRepo()
  const nested = path.join(dir, 'generated', 'deep')

  fs.mkdirSync(nested, { recursive: true })
  fs.writeFileSync(path.join(nested, 'large-output.txt'), 'generated\n')

  const status = await repoStatus(dir, 'git')

  assert.ok(status)
  assert.equal(status.untracked, 1)
  assert.equal(status.changed, 1)
  assert.deepEqual(
    status.files.map(file => file.path),
    ['generated/']
  )
})

test('reviewList reports an untracked directory without recursively listing its contents', async () => {
  const dir = makeRepo()
  const nested = path.join(dir, 'browser-profile', 'Default', 'Cache')

  fs.mkdirSync(nested, { recursive: true })

  for (let i = 0; i < 20; i++) {
    fs.writeFileSync(path.join(nested, `cache-${i}.bin`), 'generated\n')
  }

  const result = await reviewList(dir, 'uncommitted', null, 'git')

  assert.deepEqual(
    result.files.map(file => file.path),
    ['browser-profile/']
  )
})

test('reviewList caps the file payload returned to the renderer', async () => {
  const dir = makeRepo()

  for (let i = 0; i < REVIEW_FILE_CAP + 10; i++) {
    fs.writeFileSync(path.join(dir, `untracked-${String(i).padStart(4, '0')}.txt`), 'generated\n')
  }

  const result = await reviewList(dir, 'uncommitted', null, 'git')

  assert.equal(result.files.length, REVIEW_FILE_CAP)
})

const mockExecFile = vi.mocked(await import('node:child_process')).execFile

type ExecFileCallback = (error: Error | null, stdout?: string, stderr?: string) => void

// `execFile` has overloaded declarations returning ChildProcess; the mock
// implementation only needs to drive its callback, so view it as a plain
// callable and set the implementation through the Mock typing.
function failGh(stderr: string): void {
  ;(
    mockExecFile as unknown as {
      mockImplementation: (
        impl: (file: string, args: string[], options: object, callback: ExecFileCallback) => void
      ) => unknown
    }
  ).mockImplementation((_bin: string, _args: string[], _opts: object, callback: ExecFileCallback) => {
    const error = new Error('command failed')

    if (stderr) {
      ;(error as Error & { stderr?: string }).stderr = stderr
    }

    callback(error, '', stderr)
  })
}

test('reviewCreatePr surfaces gh stderr when pr create fails', async () => {
  const dir = makeRepo()

  failGh('no commits between main and feature')

  await assert.rejects(reviewCreatePr(dir, 'git', 'gh'), /no commits between main and feature/)
})

test('reviewCreatePr falls back to the generic message when gh reports no stderr', async () => {
  const dir = makeRepo()

  failGh('')

  await assert.rejects(reviewCreatePr(dir, 'git', 'gh'), /is gh installed and authenticated\?/)
})
