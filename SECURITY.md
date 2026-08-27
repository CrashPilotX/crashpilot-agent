# Security Policy

## Reporting a vulnerability

Email **saqibkhan@crashpilotx.com** with the details. Please do not open a public issue for a security problem.

Include what you need to make the issue reproducible: the version or commit, the platform, and the steps. A proof of concept helps but is not required to report something.

You should get an acknowledgement within 3 business days. If you do not, assume the mail went astray and send a follow-up.

Please give a reasonable window to ship a fix before disclosing publicly. If a report is valid and you want credit in the release notes, say so and you will get it.

## Scope

This repository contains the agent that runs on your machine. In-scope issues include:

- Anything that causes the agent to send data it documents as never leaving the host.
- Gaps in secret redaction (`agent/crashpilot/redaction.py`): a credential format that reaches disk, the AI analyzer, or the cloud unredacted.
- Local privilege escalation through the agent, its systemd units, its file permissions, or its install script.
- Authentication bypass on the local REST API (`crashpilot serve`), which binds to `127.0.0.1` and requires a bearer token on every `/api/v1/*` route.
- Command injection, path traversal, or unsafe deserialization reachable from collected system data. The agent parses attacker-influenceable text (kernel logs, journal entries), so parser issues are in scope.
- Supply chain problems in packaging, the container image, or the install script.

The hosted dashboard and backend at crashpilotx.com are not in this repository, but findings there go to the same address and are equally welcome.

## Why the agent is privileged

The agent needs host PID namespace, `/dev`, `/sys`, the kernel journal, and SMART data because node crash forensics cannot be done without them. That access is exactly why this code is public: you can audit what it does with it before installing.

If you find that the agent takes more access than it needs for a documented feature, that is a legitimate report even without a working exploit.
