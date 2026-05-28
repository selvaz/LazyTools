# Security Policy

## Reporting a Vulnerability

**Please do not report security vulnerabilities through public GitHub issues,
pull requests, or discussions.**

Report them privately through **GitHub Private Vulnerability Reporting**:

1. Go to <https://github.com/selvaz/LazyTools/security/advisories/new>
   (or the repository's **Security → Advisories → Report a vulnerability**
   button).
2. Fill in the affected version, a description, and reproduction steps.

GitHub delivers the report to the maintainers privately and gives us a place to
collaborate on a fix and coordinate disclosure.

> **Maintainer contact.** If Private Vulnerability Reporting is unavailable to
> you, contact the maintainer (`selvaz`) via the email on the GitHub profile and
> mark the subject `[SECURITY] LazyTools`. Replace this with a dedicated
> security mailbox once one is established.

Please include, where possible:

- The affected version (`pip show lazytoolkit`) and Python version.
- A minimal reproduction and the impact you observed.
- Any suggested remediation.

We aim to acknowledge a report within a few business days and to keep you
updated as we investigate and ship a fix. Please give us a reasonable window to
remediate before any public disclosure.

## Supported Versions

`lazytoolkit` is pre-1.0; security fixes land on the latest `0.x` release.
Pin a version and watch the [CHANGELOG](CHANGELOG.md) before upgrading.

## Scope and Known Considerations

`lazytools` provides tool providers and connector clients that talk to external
services. The most relevant security surfaces:

### Tools are a code-execution / data-egress surface

Anything you place in `Agent(tools=[...])` can be invoked by the LLM with
LLM-chosen arguments. Treat every tool you expose as attacker-influenced input.

- **Gated dangerous actions.** `gmail_send` and `telegram_send_message` are
  gated by `lazytools.safety` (`Allowlist` + one-shot `ConfirmationGate`). The
  harmless companion (`gmail_create_draft`) is never gated — dry-run first.
- **OAuth token files.** `GmailClient.from_credentials` persists the cached
  OAuth token (including the long-lived refresh token) with owner-only
  (`0600`) permissions. Store credential and token files outside any
  world-readable or version-controlled location.

### MCP and external gateway connectors are deny-by-default

- **MCP.** `MCP.stdio()` and `MCP.http()` require an explicit `allow=` (or
  `deny=`) filter — omitting both raises `ValueError`. Each advertised tool the
  LLM can call runs inside your process or with the server's permissions, so
  audit the tool surface before passing `allow=["*"]`.
- **Same-origin redirects.** The default gateway HTTP client refuses redirects
  to a different host or an HTTPS→HTTP downgrade, so the `Authorization` header
  is not silently forwarded to an attacker-controlled host.
- **No result sanitisation.** `lazytools.connectors.gateway` forwards each
  tool's JSON response to the agent **unmodified**. Sanitising results
  (stripping secrets, PII, internal identifiers) is the remote gateway's
  responsibility — do it server-side.

### Document reading is sandboxed

When exposed as a tool, `read_docs_tools` requires a `base_dir`; paths that
resolve outside it are refused and oversized files are skipped rather than read.
The `skills` indexer skips symlinks so an in-tree symlink can't widen the read
surface to unrelated files on disk.

### Email authentication results

`parse_authentication_results` only treats DKIM/SPF/DMARC as verified when the
authoritative result says so, ignoring `pass` substrings inside comments and
extension fields. Pass `trusted_authserv_id=` to pin the authenticating relay
and reject forged headers from other hosts.

## Out of Scope

- Vulnerabilities in third-party dependencies (`google-api-python-client`,
  `httpx`, the `mcp` SDK, `trafilatura`, etc.) — report those upstream, though
  we're happy to help coordinate.
- Secret redaction in agent logs/sessions and at-rest store encryption are
  provided by **LazyBridge**, not `lazytools`; see the LazyBridge security
  policy for those.
