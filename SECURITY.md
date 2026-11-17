# Security policy

Smart Document Manager (SDM) is self-hosted software. There is no hosted
instance run by the project: every deployment belongs to whoever started it.
This policy covers the code in this repository.

## Supported versions

Fixes land on `main` and ship in the next release. Only the latest release
receives security fixes.

| Version | Supported |
|---|---|
| 1.0.x | Yes |
| Earlier | No |

## Reporting a vulnerability

**Please do not open a public issue, pull request or discussion for a security
problem.**

Report it privately through GitHub instead:
[**Report a vulnerability**](https://github.com/smart-models/Smart-Document-Manager-SDM/security/advisories/new)
(the *Security* tab, then *Report a vulnerability*). Only the maintainers can
read the report, and the fix can be discussed and prepared in the same private
advisory.

A useful report includes:

- the affected service (`ocr-api` and its web console, `vllm-paddleocr`,
  `ocr-mcp`, the `storage` profile, the launchers, the Compose/Docker setup or
  CI) and the release or commit;
- the relevant configuration: GPU and autotune profile, whether the `storage`
  profile or archive-only mode is active, and which settings in `.env` differ
  from `.env.example`. Leave real tokens and S3 keys out;
- the steps to reproduce, and what an attacker gains;
- a proof of concept, if you have one.

## What happens next

SDM is maintained by a small team, so these are targets, not guarantees:

- acknowledgement within **7 days**;
- a first assessment (confirmed or not, severity, next steps) within
  **14 days**;
- updates at least every two weeks until the report is closed.

For a confirmed issue we prepare the fix in the private advisory, release it,
and then publish a GitHub Security Advisory, requesting a CVE when one is
warranted. Reporters are credited in the advisory unless they prefer
otherwise.

Please keep the details confidential until the advisory is published, or for
90 days after your report if that comes first, unless we agree on another date
together.

## Scope

In scope: anything that breaks a security property the project documents, for
example:

- reaching an authenticated route without a valid token: an `ocr-api` route
  other than `/`, `/health`, `/assets/*` and the opt-in Swagger pages while
  `OCR_SERVER_API_TOKENS` is set, or `ocr-mcp` over HTTP while
  `MCP_API_TOKENS` is set;
- one credential opening a service it does not belong to: an MCP token
  accepted by `ocr-api`, an API token accepted by `ocr-mcp`, or an S3 call
  or storage-console login through `storage-gateway` succeeding without valid
  credentials;
- an S3 identity acting beyond its least-privilege role: the writer deleting
  objects, the reader writing or deleting, or the S3 administrator
  credentials reaching `ocr-api` or `ocr-mcp`;
- a destructive MCP storage tool acting without its exact `confirm` value
  (the archive job ID or the relative file name);
- the MCP server reading a `file_path` outside `MCP_INPUT_ROOTS`, for example
  through a symlink or path traversal;
- a published port bound to an address other than loopback, despite
  `scripts/verify_compose_security.py`, or the standalone MCP server binding
  every interface;
- a container escaping its hardening: every service drops all capabilities
  and sets `no-new-privileges`, and every service except `vllm-paddleocr`
  (whose upstream image still writes under `/root`) also runs as a non-root
  user with a read-only root filesystem;
- a token or S3 key appearing in a log, an error message, a response or the
  launchers' output (logs carry token labels, never token values);
- injection into the GitHub Actions workflows.

Out of scope:

- vulnerabilities in PaddleOCR, PaddlePaddle, vLLM, the model, SeaweedFS,
  LibreOffice or other third-party packages themselves: report them upstream.
  A report is welcome here when SDM makes such a flaw reachable, or when a
  shipped package carries an advisory that is neither fixed nor listed in
  [`requirements/audit-triage.json`](requirements/audit-triage.json). That
  file records each accepted advisory with its rationale and expiry date, and
  the weekly dependency audit checks the image inventories against OSV;
- setups that switch a documented safeguard off, such as running `ocr-api`
  with an empty `OCR_SERVER_API_TOKENS` or `ocr-mcp` with
  `MCP_ALLOW_UNAUTHENTICATED_LOCAL=true`, reusing one token across services,
  or a reverse proxy that exposes a service to the internet;
- instructions hidden in OCR'd documents that an AI agent chooses to follow:
  OCR text is returned as data, and what an MCP client does with it is up to
  that client;
- denial of service through request volume or oversized documents against
  your own instance.

## Testing

Test only against a deployment you run yourself, never against systems or
data that belong to someone else. Good-faith research that follows this
policy is welcome, and we will not take action against it.

## Where the security design is documented

- [`ocrserver/README.md`](ocrserver/README.md): API authentication, rate
  limiting, admission limits and Office conversion;
- [`ocr-mcp-server/README.md`](ocr-mcp-server/README.md): MCP authentication,
  Host/Origin allowlists, `MCP_INPUT_ROOTS` and the confirmation gates;
- [`storage/README.md`](storage/README.md): the four S3 identities and the
  storage gateway;
- [`scripts/README.md`](scripts/README.md): the launchers' token,
  credential and published-port checks;
- [`requirements/README.md`](requirements/README.md): dependency pins, image
  inventories, the OSV audit and the triage file;
- [`.github/AGENTS.md`](.github/AGENTS.md): workflow permissions and the
  release gate.
