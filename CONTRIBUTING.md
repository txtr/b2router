# Contributing to B2 Router

Thank you for your interest in contributing! This document provides guidelines for contributing to the project.

## Code of Conduct

This project adheres to the [Contributor Covenant Code of Conduct](CODE_OF_CONDUCT.md). By participating, you are expected to uphold this code.

## How to Contribute

### Reporting Bugs
- Use the [Bug Report template](.github/ISSUE_TEMPLATE/bug_report.yml)
- Include steps to reproduce, expected vs actual behavior
- Provide environment details (OS, Python version, b2sdk version)

### Suggesting Features
- Use the [Feature Request template](.github/ISSUE_TEMPLATE/feature_request.yml)
- Describe the problem and proposed solution
- Explain why this feature would be useful

### Pull Requests
1. Fork the repository
2. Create a feature branch: `git checkout -b feature/your-feature-name`
3. Make your changes
4. Run tests: `python test_bugs.py`
5. Run linter: `ruff check b2.py`
6. Commit with descriptive messages
7. Push to your fork and open a PR

## Development Setup

```bash
# Clone the repo
git clone https://github.com/txtr/b2router.git
cd b2router

# Create virtual environment
python -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install b2sdk tqdm pyyaml ruff

# Run tests
python test_bugs.py

# Run linter
ruff check b2.py
```

## Code Style

- Follow PEP 8
- Use type hints (PEP 484)
- Use dataclasses with `slots=True` for data models
- Keep functions small and focused
- Add docstrings for public functions
- Use structured logging (not print statements)

## Testing

- All PRs must pass existing tests: `python test_bugs.py`
- Add tests for new functionality
- Test with real B2 accounts when possible (use test accounts)

## Security

- Never commit credentials, API keys, or tokens
- Use `.gitignore` patterns for config files
- Report security vulnerabilities privately (see SECURITY.md)

## Release Process

Releases are automated via GitHub Actions when a version tag is pushed:
```bash
git tag v1.0.0
git push origin v1.0.0
```

## Questions?

Open a [discussion](https://github.com/txtr/b2router/discussions) or [issue](https://github.com/txtr/b2router/issues).