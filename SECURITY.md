# Security Policy

Hermes Fleet handles authenticated cross-node communication and optional remote AI execution. Security reports are taken seriously.

## Supported versions

Hermes Fleet is currently pre-1.0 and under active development.

| Version | Security updates |
| --- | --- |
| Current `main` | Supported on a best-effort basis |
| Older commits and unreleased forks | Not supported |

## Reporting a vulnerability

Do not publish credentials, private topology, peer IDs, exploit details, or proof-of-concept code in a public issue.

Preferred reporting path:

1. Use GitHub's **Report a vulnerability** or private security-advisory feature for this repository when available.
2. If private reporting is unavailable, open a minimal public issue asking maintainers to establish a private channel. Do not include sensitive details in that issue.

Please include, when safe:

- affected revision or release;
- affected component;
- expected and observed behavior;
- reproducible steps using redacted or synthetic identities;
- security impact;
- suggested mitigation, if known.

## High-priority areas

Reports are especially valuable when they affect:

- Keryx sender authentication;
- Fleet operation authorization;
- exact-peer routing and route receipts;
- deadline enforcement;
- task reclaim and duplicate execution;
- execution-binding durability;
- cancellation truthfulness;
- secret or credential exposure;
- unsafe parsing of peer-produced content;
- systemd or deployment privilege boundaries.

## Safe defaults

Until the project reaches a production-ready release:

- deploy only on controlled private networks;
- keep Keryx daemons and local Hermes APIs on loopback where possible;
- keep worker policy default-deny;
- do not expose Fleet or Keryx services directly to the public internet;
- do not commit keys, tokens, TLS private keys, API keys, or model credentials;
- treat all peer-produced JSON and model text as untrusted;
- keep granular execution permissions narrowly scoped.

## Disclosure

Maintainers will acknowledge credible reports, investigate the affected revisions, and coordinate a fix and disclosure timeline appropriate to the severity and project maturity.
