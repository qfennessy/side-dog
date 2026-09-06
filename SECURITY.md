# Security Policy

## Supported versions

Security fixes are made on `main` and released from the current release line.
Please upgrade to the newest Side Dog release before reporting a problem that
may already have been fixed.

| Version | Supported |
| --- | --- |
| 1.1.x | Yes |
| Earlier versions | No |

## Report a vulnerability privately

Do not describe a suspected vulnerability in a public issue, discussion, pull
request, or commit. Use GitHub's private
[Report a vulnerability](https://github.com/qfennessy/side-dog/security/advisories/new)
form instead. If that form is unavailable, open a public issue containing only
a request for a maintainer to contact you; do not include security details.

A useful report explains:

- the affected Side Dog version or commit and operating system;
- the affected surface and the conditions needed to reach it;
- a minimal, sanitized way to reproduce the behavior; and
- the likely impact and any suggested mitigation.

Side Dog observes sensitive local development activity. Remove secrets,
tokens, private repository names and paths, prompts, responses, source or file
contents, command output, logs, databases, session files, and identifying
screenshots from a report. Use placeholders or a small synthetic repository
where possible. Do not send data that belongs to another person or
organization.

## What belongs in a security report

Please report behavior that could realistically break a security or privacy
boundary, including:

- capturing, retaining, displaying, or transmitting private local activity
  that Side Dog promises to discard;
- exposing the local browser panel beyond its intended loopback boundary;
- using untrusted local agent data to execute code, traverse paths, or write to
  unintended files;
- exposing credentials or weakening the safety of Side Dog's local state,
  configuration, hooks, dependencies, or release artifacts.

Normal bugs, documentation corrections, and feature requests belong in the
public issue tracker. When you are unsure whether a bug has security impact,
use the private reporting form.

## What to expect

A maintainer will acknowledge and assess a private report as soon as practical.
There is no guaranteed response or remediation time. Complexity, affected
versions, and release coordination can all change the schedule. Please keep the
report private while it is being assessed and allow reasonable time for a fix
before discussing it publicly.

Maintainers will use GitHub's private security advisory to discuss and triage
the report, prepare a fix in a private security fork when needed, coordinate
credit and disclosure with the reporter, and publish an advisory after a fix or
mitigation is available. Maintainers may close reports that cannot be
reproduced or do not cross a security boundary, with an explanation when it is
safe to provide one.

## Safe research

Test only systems, repositories, accounts, and data you own or are authorized
to use. Do not access other people's data, disrupt services, persist access, or
use a vulnerability beyond what is necessary to demonstrate it safely.
