# Shared task board: executed validation

Base: `d2ee0cdea60825608c2f8906eb54e1d4d264fb42` (v1.58.2).
Environment: Windows x64, Python 3.13.14, locked workspace dependencies,
LiteLLM 1.93.0 with its native bridge loaded. No model calls or paid services.

## Dependency setup

The initial standard install failed because `link.exe` was missing. Microsoft
Visual Studio Build Tools 2022 17.14.42, MSVC 14.44, and Windows SDK 10.0.26100
were installed with the user's authorization. `uv sync --frozen --all-packages
--group dev` then built and installed the pinned LiteLLM successfully.
`native_bridge_available()` returned true; package metadata had no local source
override. The temporary Python-only fallback used during early development was
replaced before the results below were recorded. No dependency or lockfile changes
are part of this PR.

## Passing checks

```sh
uv run --frozen pytest tests/agent_server/test_task_board.py \
  tests/tools/task_board tests/tools/task_tracker \
  tests/agent_server/test_openapi_contract.py \
  tests/agent_server/test_openapi_discriminator.py \
  tests/agent_server/docker_runtime/test_hybrid.py \
  tests/cross/test_task_board_live_server.py -q
```

Result: **62 passed**, one existing Starlette test-client deprecation warning.
After the final corrupt-tree error-classification adjustment, the 14 task-board
tests were rerun and passed. Changed-file pre-commit checks passed: Ruff format,
Ruff lint, pycodestyle, Pyright, dynamic-attribute check, import rules and tool
registration. Agent Server and tools wheels built successfully.

The exported actual-source OpenAPI passed the quality checker with the unchanged
63-entry weak-type allowlist. Docstring checking, with UTF-8 enabled on Windows,
passed core API checks and reported 39 existing warnings in other SDK files.

From `clients/typescript`:

```sh
npm ci --ignore-scripts
npm run build
npx vitest run --config vitest.config.ts --maxWorkers=2 --reporter=dot
npm run check:public-type-budget
```

Result: **397 passed** across 28 files; build and public weak-type budget passed
(103 sites, unchanged). The test run used a writable workspace temporary directory
after the default Windows temporary-directory run encountered module-cache access
errors. Changed TypeScript files passed ESLint and Prettier.

## Live proof

The cross test starts the actual Agent Server entry point, uses the real tool over
HTTP with no browser/WebSocket or model request, persists an active claim and a
completion, kills the process, and starts a new process on the same directory.
The board reads back identically and root deletion makes the former member's board
404. The test configures a 1-second runtime lease so a dead owner's Windows lease
does not delay the separate conversation-deletion operation. An initial run with
the default lease restored the board successfully but deletion returned 404 before
lease expiry; that run is not counted as a pass.

F36's two REST recipes were actually executed through `control-agent-server`,
attached to a fresh local server. `doctor` passed. Eight redacted exchanges were
saved and remained after the verifier detached/stopped. See `evidence/cli/`.

`.pr/verify-task-board-client.mjs` was run with the built TypeScript client against
the real server. Two concurrent claims produced one winner and one typed conflict
with identical current state. All four client operations worked, completion read
back from the root, outside-tree read returned 403 and root deletion caused 404.
See `evidence/typescript-client.json`.

The map format check passed (35 files, 938 sub-features). Route coverage gained
four owned/driven routes (194 → 198); it still reports the same three unowned
routes as an actual run on the unchanged baseline: child launch, skills catalog
and plugin fetch. No new unowned or multiply owned route was introduced.

## Existing failures and limits

- Full `test_conversation_service.py`: **137 passed, 1 failed**. The failure is
  `test_restart_resumes_conversations_after_non_graceful_shutdown`; the same test
  was executed in a separate, unmodified checkout at the base commit and failed
  there too. The deletion tests pass after adapting their file-removal patch target
  to the newly shared transaction boundary.
- Full TypeScript lint reports three errors in unchanged integration-test code:
  an unused `waitFor`, and unused/non-const `connectionCount`. The full format check
  also reports existing files; changed-file checks pass. These checks are not
  reported as globally passing.
- No full Python repository suite, real-model integration run, or benchmark was
  performed. No live Docker daemon was available. Docker/hybrid dispatch was
  checked with the repository's deterministic tests, not a container deployment.
- Isolated container tools only mount their own conversation and cannot resolve
  an outer multi-runtime tree through loopback. A separate scoped bridge is needed
  for that host topology; this PR keeps the outer REST store canonical and does
  not inject a broad outer session key into a container. This is not a claim that
  every deployment variant of #6132 is complete.
- Upstream CI/review and the template's human-author note remain separate gates.
  Local passing results do not assert CI success, adoption, or merge.
