# Eliminating per-repository Codex Cloud environments (internal, 2026-11)

**Scope.** Read-only investigation: no environment or ChatGPT API was called and no
credential file was read. The installed container has no `codex` binary, so its help
could not be rechecked here. “Legacy” below means the surface currently driven by
`codex cloud`, not the newer published-environment experience.

## Root cause

| Layer | Finding |
|---|---|
| Bridge selection | **VERIFIED (repo `src/lazytools/code_bridge/_cloud.py:370-378,755-762,1198-1203`):** configuration is a lowercase `owner/name -> env_id` map; submission fails before admission if the key is absent. There is no discovery/provision path. |
| CLI contract | **VERIFIED (repo `src/lazytools/code_bridge/_cloud.py:788-819`; `docs/code-bridge-cloud-setup.md:91-95`):** the bridge runs `codex cloud exec --env ID --branch BRANCH`, retains the returned task handle, then uses `status`, `diff`, and `apply`. There is no repository argument. **VERIFIED (this container: `codex cloud --help`):** not testable because `codex` is not installed. |
| Environment/repository binding | **VERIFIED (repo `docs/code-bridge-cloud-setup.md:59-64` and operator test dated 2026-11-09 in this task):** the CLI lists usable legacy single-repository environments; UI multi-repo tasks produced no CLI-retrievable diff. **VERIFIED ([OpenAI, Using Codex Cloud](https://help.openai.com/en/articles/20001545-using-codex-cloud)):** a repository must be included in the selected environment; new published environments are created in desktop/web, while GitHub integrations still use Legacy during the transition. |
| Result transport | **VERIFIED (repo `src/lazytools/code_bridge/_cloud.py:815-819,1318-1337`):** Codex result/report capture depends on `codex cloud diff`; application delegates to `codex cloud apply`. A task that runs but cannot expose that diff breaks the bridge contract. |
| Claude path | **VERIFIED (repo `docs/code-bridge-cloud-setup.md:167-181,256-285,313-318`):** unattended use fires a saved routine whose repository, branch, environment, prompt and bearer token are preconfigured. There is no status API; the bridge detects a new `claude/*` branch and diffs Git. This replaces diff transport, but not per-repository setup. |

## Candidate designs

### 1. Auto-provision a legacy environment on first use — **no-go unless supported API appears**

**VERIFIED (operator-supplied protocol):** legacy creation is an undocumented
`POST /backend-api/wham/environments` with `{label,machine_id,repos:["github-<id>"],
setup,agent_network_access}` and needs a ChatGPT bearer token. **VERIFIED ([OpenAI
help](https://help.openai.com/en/articles/20001545-using-codex-cloud)):** the documented
creation surface is desktop/web. **ASSUMED:** CLI login credentials might technically
authorize that private endpoint, but consuming its private token store or endpoint is
unsupported, brittle, and expressly outside this task. Effort medium, operational risk
critical (credential handling/API churn). Do not implement until an official CLI/API
can create and enumerate repository bindings without exposing tokens; even then it
still mints one environment per repository and only hides the registration.

### 2. One generic legacy environment that clones the target — **not compatible with current CLI**

**VERIFIED (repo `src/lazytools/code_bridge/_cloud.py:788-794`):** task input can carry
a repo slug, but `exec` selects only environment and branch. **VERIFIED (OpenAI help
above):** the selected environment must include the repository. **VERIFIED (operator
test):** multi-repo UI environments have no retrievable CLI diff. **ASSUMED:** a setup
script could clone an extra public repo, or a private repo if a readable credential
were injected, but the legacy task/diff would remain anchored to the environment’s
bound repo and setup runs before a per-task prompt is available. Effort medium, risk
critical (private token exposed to agent/setup; wrong or missing diff). Reject.

### 3. Route arbitrary repositories to Claude web routines — **not generic today**

**VERIFIED (repo `src/lazytools/code_bridge/_cloud.py:434-475`; setup doc cited above):**
each bridge routine declares exactly one repo and branch; its API fire body is only
`{"text": brief}`. **VERIFIED:** the Claude GitHub App clones and pushes the configured
repository, allowing Git-based result recovery without a provider diff. **ASSUMED:** a
normal interactive web session may let a user select any App-authorized repo, but no
verified unattended API field dynamically attaches one. Effort low per configured
repo, high fleet-wide; risk medium (branch polling ambiguity and no status API). Keep
as an already-supported secondary engine, not the structural fix.

### 4. GitHub Actions dispatch + reusable agent workflow — **recommended spike**

**VERIFIED ([GitHub dispatch API](https://docs.github.com/en/rest/actions/workflows#create-a-workflow-dispatch-event)):**
an App installation token with Actions:write can dispatch a repository workflow and
the response identifies the run. **VERIFIED ([reusable workflows](https://docs.github.com/en/actions/how-tos/reuse-automations/reuse-workflows)):
**a thin caller can invoke centrally maintained `workflow_call` logic and pass declared
inputs/secrets. **VERIFIED ([openai/codex-action](https://github.com/openai/codex-action)):
**the official action runs `codex exec`, accepts prompt/model/effort/working directory,
and protects an API key behind a proxy/safety strategy. Claude’s action can be assessed
as an alternative in the spike. **ASSUMED:** an organization App can install/dispatch
the same thin workflow across all selected/new repositories; GitHub cannot dispatch a
workflow file that does not exist on the target ref. Effort medium (bridge provider,
run polling, artifact/branch convention), risk medium (API billing instead of ChatGPT
cloud quota, secrets/permissions, runner cost). This removes Codex Cloud environments;
standardize output as a patch artifact or bot branch, then reuse existing landing logic.

### 5. Local worktrees, with an Actions “burst” fallback — **safe baseline**

**VERIFIED (repo `src/lazytools/code_bridge/_cloud_place.py:79-106`):** `--where auto`
already chooses local outside busy windows when a local engine slot is free and cloud
when all eligible local slots are occupied. **VERIFIED (operator statement):** D: Git
worktrees already work. **ASSUMED:** changing the busy/no-slot destination to the
Actions provider yields a useful burst tier without altering local behavior. Effort
low after candidate 4; risk low. It does not itself supply cloud capacity, but is the
best failure fallback and rollout guard.

### 6. New Codex environments / self-hosted Agents API — **strategic, not drop-in**

**VERIFIED ([OpenAI help](https://help.openai.com/en/articles/20001545-using-codex-cloud)):
**published environments are the newer experience and Legacy remains for existing
GitHub workflows. **VERIFIED ([self-hosted sandboxes](https://developers.openai.com/api/docs/guides/agents-api/environments/self-hosted)):
**the Agents API supports an operator-controlled workspace/executor, but requires
separate API and restricted environment keys and warns that shared environments share
files/credentials. **ASSUMED:** a pool that checks out the requested repo could replace
both legacy cloud and diff retrieval, returning a Git patch/branch, but it is a new
API-billed provider and isolation system rather than a `codex cloud` adjustment. Effort
high, risk medium-high; revisit if Actions latency/cost is unacceptable.

## Recommendation and minimal verified spike

Adopt **local-first + GitHub Actions burst** (4+5), initially in one private sandbox
repo. It meets zero Codex-environment registration, uses documented APIs, scopes GitHub
access per installation, and returns auditable Git/artifact output. Do not scrape CLI
auth, call ChatGPT private endpoints, or depend on unretrievable multi-repo diffs.

Run these non-secret-bearing checks first (the spike may create only a test branch/PR):

```bash
gh api repos/OWNER/SPIKE/actions/workflows/agent.yml
gh workflow run agent.yml -R OWNER/SPIKE -f task='Add a sentinel file' -f base=DEFAULT_BRANCH
gh run list -R OWNER/SPIKE --workflow agent.yml --limit 1 --json databaseId,status,conclusion,url
gh run watch RUN_ID -R OWNER/SPIKE --exit-status
gh run download RUN_ID -R OWNER/SPIKE -n agent-patch -D .spike-result
git apply --check .spike-result/result.patch
```

Verify both `openai/codex-action` and Claude Code action in separate runs; confirm a
private repo checkout, no secret in logs/artifacts, deterministic base SHA, patch/branch
retrieval, cancellation, and concurrent runs. Then prototype a provider behind the
bridge interface (`submit/status/diff/apply`) before changing routing.

**Owner decisions:** (1) API-billed Codex, Claude, or both; (2) GitHub-hosted versus
self-hosted runners and monthly cap; (3) organization-wide App installation and whether
new repos receive the thin caller automatically; (4) patch artifact versus bot branch/
PR as the result contract and retention; (5) permitted trigger users, repo permissions,
network and secret policy; (6) default local concurrency/busy windows and when burst is
allowed; (7) whether zero-touch means “all App-authorized repos” or only repos carrying
the centrally managed caller; and (8) whether to fund the longer Agents API pool spike.
