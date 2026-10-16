# AI Watch runtime and handoff contract

This feature is a Windows x64, local-first updater. Source collection requires an internet connection; summary selection runs locally and does not call GPT, Codex, a remote model API, or a model downloader.

## Data flow and protection

1. While Cubic is running (including in the tray), the Tauri scheduler checks at startup and then on a daily cadence. A failed or partial run is retried after a delay. Exiting Cubic stops the scheduler; there is no service, auto-start, or background process left behind.
2. The packaged Python 3.13 standard-library runtime reads the bundled 91-entry public source registry and reuses the original rules-first `ai_radar` collector. Its SQLite snapshots and update status live only in Tauri's per-user `app_local_data_dir`; the collector does not read `accounts.json`, the source project's databases, backups, logs, or user files.
3. Deterministic rules detect changes, retain source URLs/evidence and dates, filter stale dated changes, flag missing dates as undated/observe, and deduplicate exact normalized evidence. Explicit future effective dates are retained even when the announcement itself is older.
4. Qwen is used only to select a short, verbatim excerpt from the evidence for the three items the existing home card displays. The model does not create events, labels, dates, amounts, priorities, or sources. The result must parse against the JSON schema, refer to the exact input keys, and be an exact substring of the matching evidence; uncertain evidence must remain visibly uncertain. Instruction-like source text is not sent to the model. Any failed validation leaves the last good feed intact.
5. A validated version-1 feed is written with a same-directory temporary file and atomic replacement. If all sources fail or model validation fails, the previous `ai-intelligence.json` is not replaced. Partial source failures, total failure, a successful no-new-items check, stale data, and an update in progress have separate status states.

The app-data directory is keyed by the unchanged Tauri identifier `com.tradedesk.app`, so moving or renaming the EXE for the same Windows user does not move the feed, SQLite snapshots, or model cache. First launch seeds `ai-intelligence.json` only when it is absent; an existing valid feed is never replaced by the packaged seed. Notes, calculator history, theme, and other WebView storage are not migrated to a different computer. The collector's primary HTTP transport is Python's bundled standard library; the reused optional Schannel/TLS fallback calls Windows `curl.exe` when present. If that OS fallback is unavailable, affected sources are reported as failed/partial rather than as “no news.”

## Single-file Windows package

The final Windows x64 `Cubic.exe` is a normal Tauri PE executable with a verified local-runtime payload appended to it. `tauri build --no-bundle` by itself is only the base application; `scripts/build_ai_runtime.ps1` verifies the locked resources, runs the synthetic model check, and appends the payload. The end-user package is one EXE: it contains the Qwen weights, Python worker/runtime, llama.cpp CPU inference files, public source registry, licenses, schema, and optional initial feed seed. On first run, Cubic validates the payload and releases the managed runtime to `app_local_data_dir/ai-runtime`; on later starts it verifies and reuses that cache. Corrupt or interrupted component extraction is rebuilt through a staging directory; feed and user storage are outside that managed component cache.

The package does not contain a third-party model application, and the end user does not download the model or install Python. Windows WebView2 remains a platform prerequisite. The initial feed seed is optional at build time and is never committed to this repository.

### Locked build resources

The resource manifest is [`resources/ai-watch/resource-lock.json`](resources/ai-watch/resource-lock.json). It pins:

| Resource | Pinned source | SHA-256 | License |
| --- | --- | --- | --- |
| Qwen3-0.6B Q4_0 GGUF (428,970,080 bytes) | `ggml-org/Qwen3-0.6B-GGUF`, revision `b5f37287796e5be0ea3dab2e7430873fb3f73e49` | `da2572f16c06133561ce56accaa822216f2391ef4d37fba427801cd6736417d4` | Apache-2.0 |
| llama.cpp Windows x64 CPU runtime, release `b11669`, commit `620d2225cc436ab0b001e3200e590e3b286ef9ab` | official `llama-b11669-bin-win-cpu-x64.zip` release asset | `37b3efcf962847ca869745fd4ccf7f5e7e8dacfeff9fa3523b4ca8cdc66d632b` | MIT; bundled OpenMP notice included |
| CPython 3.13.16 embeddable AMD64 | python.org official embeddable archive | `97dae5274cc54867065e8d5a3226e48c35017ed332a0fdb0e27d5b5821961297` | PSF-2.0; upstream SPDX SBOM included |
| VC145 x64 runtime files, package directory `14.51.36231` | licensed Visual Studio Redistributable `Microsoft.VC145.CRT` directory; exact file hashes in the lock | pinned per-file | Microsoft Software License Terms; see bundled notice |

The model, runtime archives, and EXEs are not stored in Git. The build machine obtains any missing public resource from its pinned source and verifies both byte size and SHA-256 before use; the VC runtime is taken from a licensed Visual Studio redist directory and verified the same way. This is build-time acquisition; the application itself only reads its local package payload and never downloads a model. Third-party notices are under `resources/ai-watch/licenses/` and included in the final package. Windows 10/11's Universal CRT remains an OS component, and WebView2 Runtime is still required.

### Reproducible build steps

1. Create a registered, ignored `work/<task>/TASK.md`, a clean `work/<task>/build/frontend` copied from the current tracked frontend resources, and a separate Tauri merge config containing only `build.frontendDist: "../../../work/<task>/build/frontend"`. Resolve it from `development/desktop/src-tauri` and verify `index.html`, logos, fonts, and `pantone-data.js`. Use a task-specific Cargo target directory so no prior candidate is overwritten:

   ```powershell
   New-Item -ItemType Directory .\work\<task>\build\target | Out-Null
   $env:CARGO_TARGET_DIR = (Resolve-Path .\work\<task>\build\target).Path
   Set-Location .\development\desktop
   node .\node_modules\@tauri-apps\cli\tauri.js build --no-bundle --ci --config "..\..\work\<task>\build\tauri.frontend.json"
   Set-Location ..\..
   ```

2. Append the locked runtime. Use new ignored `work/` directories; do not point these paths at `outputs/`:

   ```powershell
   .\development\desktop\scripts\build_ai_runtime.ps1 `
     -ResourceCache .\work\ai-runtime-resources `
     -BaseExe .\work\<task>\build\target\release\Cubic.exe `
     -OutputExe .\work\ai-runtime-build\Cubic.exe `
     -WorkDir .\work\ai-runtime-build\stage `
     -VCRedistDir "$env:VCINSTALLDIR\Redist\MSVC\14.51.36231\x64\Microsoft.VC145.CRT"
   ```

   Also pass `-VCRedistDir` pointing to the licensed build host's matching `Microsoft.VC145.CRT` directory. For a private local seed only, the builder may pass `-InitialFeedPath` pointing to a validated version-1 feed. Do not add that file or its path to the commit. The script refuses to overwrite existing staging or output paths, rechecks hashes, tests the embedded Python worker/model against synthetic inputs, and uses `pack_ai_runtime.py` to verify the final payload and PE architecture.

## Frontend contract for Claude Code

- Tauri `read_ai_feed` returns the existing version-1 JSON schema from app-local data. Existing `events`, source URLs, evidence, `published_at`, priorities, and the current three-items-per-local-day behavior remain authoritative.
- `read_ai_status` returns `state`, `message`, source counts, and `last_success_at`/`last_attempt_at`. States are `preparing`, `updating`, `updated`, `no_new_items`, `partial`, `failed`, `interrupted`, and `unsupported`. A read/collection failure must not be presented as “no news”; `partial` and `failed` retain the last successful feed.
- Only `summary` with `summary_mode: "extractive-v1"` is a model-selected excerpt, and it is guaranteed by the worker to be a literal substring of `evidence`. Legacy or unvalidated summaries must fall back to evidence text.
- The existing home-card hooks `.home-ai-card`, `.home-ai-day`, `.home-ai-events`, `.home-ai-item`, `.home-ai-priority`, `.home-ai-source`, and `.home-ai-time` are adjustable. Keep new styling within `.home-ai-*`, preserve the existing theme variables and responsive layout, render feed text with `textContent`, and validate links with the current HTTP/HTTPS allowlist.
- Frontend polling is read-only. Do not start collection from a timer in JavaScript, write feed/status files from the browser, change the three-item display limit, or relabel a failure as an empty result.

## Known limits

Qwen3-0.6B is only an extractive selector for the displayed evidence, not a fact checker or a replacement for GPT. The rules decide freshness, category, priority, and deduplication. News collection still needs network access; only inference is offline. Hardware-specific inference speed, WebView2 presence, native display, tray behavior, and full exit behavior must be reported separately from source and packaging checks.
