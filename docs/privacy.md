# Privacy and contribution policy

This repository is a reusable implementation, not a copy of any organization's internal workspace.

Never commit or attach:

- Customer forms, agreements, scans, or exported PDFs.
- Signatures, seals, identity numbers, contact information, or business records.
- Passwords, tokens, private keys, environment files, or NAS configuration exports.
- Private hostnames, internal IP addresses, production logs, or deployment backups.
- Proprietary printer/scanner driver binaries or fonts copied from commercial applications.

Use generated fixtures with fictitious content when reporting layout defects. Replace network addresses with documentation addresses such as `192.0.2.10`, and remove filenames or paths that identify a person or company. If an actual document is necessary to reproduce a defect, obtain its owner's permission and share it privately through an appropriate channel; do not upload it to a public issue by default.

A useful public bug report includes the application and converter versions, selected renderer, document structure involved, sanitized error code, and a minimal synthetic reproduction. Page count alone is not sufficient evidence of layout fidelity; a sanitized expected/actual page image can help when you own the content.

The local station keeps temporary scan and print files in its data volume. The operator is responsible for network access controls, retention policy, backups, and physical access to the printer. Password protection applies to the web interface; a TLS reverse proxy is needed when transport encryption is required.
