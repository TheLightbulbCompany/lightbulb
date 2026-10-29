# Security policy

This repository is the part of Lightbulb that runs on your computer: the macOS desktop app
and the `lightbulb` command. The hub it connects to (`thelightbulb.company`) is hosted and
is not in this repository, but reports about it are welcome at the same address.

## Reporting a vulnerability

Email **security@thelightbulb.company**. Please do not open a public issue, pull request
or discussion for a vulnerability.

You can also use GitHub's private reporting: **Security → Report a vulnerability** on
this repository.

Include what you can of:

- the version (Lightbulb → About, or `lightbulb --version`) and macOS version;
- what an attacker needs (a malicious web page, a teammate in the same workspace, a
  local process under the same macOS user, network position);
- steps to reproduce, and what you saw happen.

We acknowledge a report within 3 business days and send a first assessment within 10.
We fix confirmed issues in a signed release before we publish details, and we credit
reporters who want credit.

## Scope

In scope:

- anything that lets a web page, a teammate, an agent or another local user make this
  app run a command, read or write a file, type into a session, open a browser session
  or share a screen without the Mac owner's consent;
- leaks of local credentials, session transcripts or file contents to the hub beyond
  what the README says is sent;
- bypasses of the pairing, device-token or session-sharing checks;
- the update path (manifest handling, signature verification).

Out of scope: findings that need root or full control of the same macOS user account
already; social engineering of our staff; denial of service by volume against the
hosted service; reports from automated scanners with no demonstrated impact.

## Safe harbour

We will not take legal action against good-faith research that follows this policy,
avoids other people's data and accounts, and gives us reasonable time to fix the issue
before disclosure. Test against your own workspace and your own Mac.
