# Security policy

lmon reads a lot about the machine it runs on and can serve it over HTTP, so security reports are taken seriously.

## Supported versions

Only the latest release (and `main`) gets fixes while the project is in 0.x.

## Reporting a vulnerability

Please **do not open a public issue**. Instead:

- use GitHub's **"Report a vulnerability"** button on the repository's Security tab, or
- email **arunlearn13@gmail.com** with "lmon security" in the subject.

Include what you found, how to reproduce it, and the impact you expect. You should get an answer within 7 days.
Once a fix is released you will be credited in the changelog, unless you prefer not to be.

## In scope

- The web dashboard / `/api/metrics` exposing data without the configured token, or token bypasses.
- Terminal escape-sequence injection through process names, command lines, log lines or hostnames.
- Any way to make lmon run commands, write files or change the system beyond what is documented in the README.
- Problems that appear only under `sudo lmon` (file ownership, privilege use).

Out of scope: the information lmon is designed to show to the user running it.
