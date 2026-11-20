# Security

This tool places orders in a brokerage account, so please report problems that could send an unintended order privately. That includes a wrong direction, a crossing price, the wrong quantity, the wrong account, or a duplicate order.

Use GitHub's **Report a vulnerability** (Security tab) on this repository rather than a public issue.

What it does and doesn't do:
- It connects only to the TWS or IB Gateway socket you point it at (default `127.0.0.1:7496`).
- It makes no other network calls and stores no credentials.
- It writes no files.
- It reads an optional `roll.toml` from the working directory (or `--config`) and prints its path on every run.
