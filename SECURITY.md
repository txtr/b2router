# Security Policy

## Supported Versions

We release security patches for the latest version only.

| Version | Supported          |
| ------- | ------------------ |
| Latest  | :white_check_mark: |
| Older   | :x:                |

## Reporting a Vulnerability

Please report security vulnerabilities **privately** by emailing [security@txtr.dev](mailto:security@txtr.dev).

Do **not** open a public issue for security vulnerabilities.

### What to Include

- Description of the vulnerability
- Steps to reproduce
- Potential impact
- Any suggested fixes

## Response Timeline

- **Acknowledgment**: Within 48 hours
- **Initial Assessment**: Within 1 week
- **Fix Development**: Within 30 days (depending on severity)
- **Release**: Coordinated with reporter

## Disclosure Policy

- We follow responsible disclosure practices
- Credit will be given to reporters who follow this process
- We will not take legal action against researchers acting in good faith

## Security Best Practices for Users

- Store credentials in `accounts.yaml` (gitignored)
- Use application keys with minimal required capabilities
- Rotate keys periodically
- Monitor B2 usage for unexpected activity
- Keep b2sdk updated: `pip install --upgrade b2sdk`

## Known Considerations

- This tool requires B2 master keys or application keys with broad capabilities
- Keys are stored in plaintext in config file (ensure file permissions are restrictive)
- Network traffic uses HTTPS (B2 API requirement)
- No encryption at rest for local state file (`.b2router_state.json`)