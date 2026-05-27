# Security Policy

This repository contains optional local tools for Hermes Agent. Treat every
adapter and script as code you run on your own machine.

## Reporting a vulnerability

Please open a GitHub security advisory or a private report if available. If the
issue is not sensitive, open a normal issue with the `area:security` label.

## Secrets policy

Do not commit secrets. Use `.env.example`, environment variables, or local
Hermes configuration. If a secret appears in history, rotate it immediately and
remove it from the repository history before continuing public work.
