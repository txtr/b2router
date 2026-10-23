# Migration & Upgrade Guide

## Versioning Policy

| Version | Policy |
|---------|--------|
| **Major** | Breaking API/config changes; config migration required |
| **Minor** | New features, backwards-compatible |
| **Patch** | Bug fixes, security, no config changes |

Current: `0.1.x` (pre-1.0, breaking changes possible)

---

## Upgrade Checklist

### Pre-Upgrade

1. **Stop gateway** (drain jobs first):
   ```bash
   docker compose stop b2router
   # or: systemctl stop b2router
   ```

2. **Backup config**:
   ```bash
   cp accounts.yaml accounts.yaml.bak
   ```

3. **Run reconcile** (clean state):
   ```bash
   B2ROUTER_ACCOUNTS=accounts.yaml b2router-reconcile --apply --pretty
   ```

### Upgrade

```bash
git pull origin main
pip install -e '.[dev]'   # or: docker compose build
```

### Post-Upgrade

1. **Validate config**:
   ```bash
   b2router  # dry-run: loads config, builds pool, exits on error
   ```

2. **Start gateway**:
   ```bash
   docker compose up -d
   ```

3. **Verify health**:
   ```bash
   curl -s http://127.0.0.1:8000/ready
   ```

4. **Run reconcile** (idempotent):
   ```bash
   b2router-reconcile --apply --pretty
   ```

---

## Breaking Changes by Version

### 0.1.0 → 0.1.1 (Hypothetical)

| Change | Migration |
|--------|-----------|
| New required env var | Add to `.env` / compose |
| Config schema change | Update `accounts.yaml` |
| API response field added | Client ignores unknown fields |

### Config Schema Changes

| Version | Change | Action |
|---------|--------|--------|
| 0.1.0 | Initial | — |

---

## Zero-Downtime Strategy

**Not supported in 0.x** — single-writer design requires stop.

### Future (1.0+) Target

| Phase | Description |
|-------|-------------|
| 1 | Add `/api/admin/drain` endpoint |
| 2 | Graceful job drain + config reload |
| 3 | Blue/green via separate lock files |

---

## Config Migration Examples

### Adding Capacity to Existing Account

```yaml
# Before
accounts:
  - account_id: 0011aabb22ccdd33
    account_key: K001...

# After
accounts:
  - account_id: 0011aabb22ccdd33
    account_key: K001...
    capacity_in_mb: 20480   # Add capacity
```

### Changing `max_files_per_account`

```yaml
# Before (uses default 1700)
accounts:
  - ...

# After
max_files_per_account: 2000
accounts:
  - ...
```

> ⚠️ Must not exceed B2's 1700 limit per bucket.

### Rotating Application Key

```yaml
# Rotate key (B2: create new, deactivate old)
accounts:
  - account_id: 0011aabb22ccdd33
    account_key: K002...NEW_KEY_HERE...
    capacity_in_mb: 10240
```

1. Update `accounts.yaml`
2. Restart gateway (re-authorizes)

---

## Data Migration

### Moving Files Between Accounts

Not supported via API. Use `b2router-reconcile --apply` after manual B2 copy.

### Changing Bucket

Not supported — each account key maps to exactly one bucket.

---

## Rollback Procedure

1. Stop new version
2. Restore `accounts.yaml.bak`
3. Run `b2router-reconcile --apply` (if schema changed)
4. Start previous version

---

## Compatibility Matrix

| Gateway | UI | API Clients |
|---------|-----|-------------|
| 0.1.x | 0.1.x | RFC 9457 + JSON |
| 0.1.x | 0.2.x | ❌ (UI separate repo) |

> UI is now separate repo (`b2router-ui`). Gateway 0.1.x has no `/ui` routes.
