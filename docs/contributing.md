# Contributing to b2router

## Quick Start

```bash
# 1. Fork & clone
git clone https://github.com/txtr/b2router.git
cd b2router

# 2. Create venv
python -m venv .venv
. .venv/bin/activate

# 3. Install deps
pip install -e '.[dev]'

# 4. Run tests
pytest -q

# 5. Lint & type-check
ruff check src/b2router
ruff format --check src/b2router
mypy src/b2router
```

---

## Development Workflow

### Branch Naming

| Type | Prefix | Example |
|------|--------|---------|
| Feature | `feat/` | `feat/rate-limit-retry` |
| Fix | `fix/` | `fix/size-mismatch-partial` |
| Docs | `docs/` | `docs/architecture` |
| Refactor | `refactor/` | `refactor/pool-lock` |
| Test | `test/` | `test/reconcile-apply` |

### Commit Messages

Follow [Conventional Commits](https://www.conventionalcommits.org/):

```
<type>(<scope>): <subject>

<body>

<footer>
```

Types: `feat`, `fix`, `docs`, `style`, `refactor`, `perf`, `test`, `chore`, `ci`

Examples:
```
feat(router): add rate-limit retry across accounts
fix(uploads): handle size mismatch as partial
docs(readme): add architecture diagram
```

### Pull Requests

1. **Title**: Conventional commit format
2. **Description**: What, why, testing done
3. **Checks**: All CI must pass (tests, ruff, mypy)
4. **Review**: At least one approval

---

## Code Standards

### Python

- **3.11+** only
- **Type hints**: All public functions, no `Any` in signatures
- **Docstrings**: Google style for public APIs
- **Imports**: `ruff` organized (`isort` compatible)
- **Formatting**: `ruff format` (single quotes, 100 cols)

### Architecture Rules

| Rule | Rationale |
|------|-----------|
| No `b2sdk` imports outside `services/account.py` | Testability, isolation |
| No global mutable state (except `flock`) | Concurrency safety |
| All mutations via `AccountPool` | Single accounting source |
| Errors as `Problem` (RFC 9457) | Consistent API |
| Context vars for request/job IDs | Log correlation |

### Testing

| Type | Location | Run |
|------|----------|-----|
| Unit | `src/b2router/tests/` | `pytest` |
| Integration | (future) | `pytest -m integration` |
| Fake backend | `conftest.py::FakeStorageBackend` | No B2 needed |

**Coverage target**: >90% on `services/`, `api/`

---

## Code Review Checklist

- [ ] Types: `mypy src/b2router` passes
- [ ] Lint: `ruff check src/b2router` passes
- [ ] Format: `ruff format --check src/b2router` passes
- [ ] Tests: `pytest -q` passes (109+)
- [ ] Security: `bandit -r src/b2router` no Medium/High
- [ ] Docs: README/architecture updated if behavior changed
- [ ] No `print()` / `pdb` / `breakpoint()` in code
- [ ] No `Any` in public signatures
- [ ] No `b2sdk` imports outside `account.py`
- [ ] Context vars cleaned in `finally`

---

## Security Reporting

**Do not file public issues** for security vulnerabilities.

Email: **security@txtr.dev** (or use GitHub Security Advisories)

Include:
- Description
- Reproduction steps
- Impact assessment
- Suggested fix (if any)

---

## Release Process

1. Update version in `pyproject.toml`
2. Update `CHANGELOG.md` (if exists)
3. Tag: `git tag v0.1.x`
4. Push: `git push origin v0.1.x`
5. GitHub Actions builds & publishes

---

## Getting Help

- **Discussions**: GitHub Discussions tab
- **Bugs**: GitHub Issues (use templates)
- **Security**: security@txtr.dev

---

## License

By contributing, you agree your contributions are licensed under the **MIT License** (same as project).
