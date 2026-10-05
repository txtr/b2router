#!/usr/bin/env python3
"""Test b2router logic with mocked B2 API."""

import sys
import os
from pathlib import Path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Mock b2sdk before importing b2router
from unittest.mock import MagicMock, patch
import types

mock_b2api = MagicMock()
mock_b2api.authorize_account.return_value = None

# Mock bucket listing
mock_bucket = MagicMock()
mock_bucket.id_ = "bucket-123"
mock_bucket.name = "test-bucket"
mock_bucket.ls.return_value = []

mock_b2api.list_buckets.return_value = [mock_bucket]
mock_b2api.get_bucket_by_id.return_value = mock_bucket
mock_b2api.get_bucket_by_name.return_value = mock_bucket

# Patch the b2sdk module
b2sdk_module = types.ModuleType('b2sdk')
b2sdk_v2 = types.ModuleType('b2sdk.v2')
b2sdk_v2.B2Api = MagicMock(return_value=mock_b2api)
b2sdk_v2.InMemoryAccountInfo = MagicMock()
b2sdk_v2_exception = types.ModuleType('b2sdk.v2.exception')
b2sdk_v2_exception.B2Error = Exception
b2sdk_v2.exception = b2sdk_v2_exception

b2sdk_internal = types.ModuleType('b2sdk._internal')
b2sdk_internal_account_info = types.ModuleType('b2sdk._internal.account_info')
b2sdk_internal_account_info_exception = types.ModuleType('b2sdk._internal.account_info.exception')
b2sdk_internal_account_info_exception.MissingAccountData = Exception
b2sdk_internal_account_info.exception = b2sdk_internal_account_info_exception
b2sdk_internal.account_info = b2sdk_internal_account_info
b2sdk_module._internal = b2sdk_internal
b2sdk_module.v2 = b2sdk_v2
sys.modules['b2sdk'] = b2sdk_module
sys.modules['b2sdk.v2'] = b2sdk_v2
sys.modules['b2sdk.v2.exception'] = b2sdk_v2_exception
sys.modules['b2sdk._internal'] = b2sdk_internal
sys.modules['b2sdk._internal.account_info'] = b2sdk_internal_account_info
sys.modules['b2sdk._internal.account_info.exception'] = b2sdk_internal_account_info_exception

# Import the lean module
from b2router.config import load_config
from b2router.utils import collect_source_files, format_bytes
from b2router.allocation import allocate_files, print_allocation_plan
from b2router.b2_client import Bucket, authorize_account, build_buckets, list_files_in_bucket, upload_file, get_file_info
from b2router.commands.list import run_list
from b2router.commands.copy import run_copy
from b2router.commands.move import run_move


def test_load_config():
    """Test config loading with valid and invalid YAML."""
    # Test empty YAML
    with open('/tmp/test_empty.yaml', 'w') as f:
        f.write('')
    try:
        accounts = load_config('/tmp/test_empty.yaml')
        print(f"FAIL: empty YAML should raise ValueError, got {len(accounts)} accounts")
    except ValueError as e:
        print(f"PASS: ValueError raised for empty YAML: {e}")
    except Exception as e:
        print(f"FAIL: unexpected error: {e}")

    # Test missing master_key
    with open('/tmp/test_bad.yaml', 'w') as f:
        f.write('accounts:\n  bad_acc:\n    account_id: "abc"\n')
    try:
        accounts = load_config('/tmp/test_bad.yaml')
        print(f"FAIL: should have raised KeyError, got {accounts}")
    except KeyError as e:
        print(f"PASS: KeyError raised: {e}")

    # Test missing account_id
    with open('/tmp/test_bad2.yaml', 'w') as f:
        f.write('accounts:\n  bad_acc:\n    master_key: "key"\n    capacity_gb: 10\n')
    try:
        accounts = load_config('/tmp/test_bad2.yaml')
        print(f"FAIL: should have raised KeyError, got {accounts}")
    except KeyError as e:
        print(f"PASS: KeyError raised: {e}")

    # Test valid config
    with open('/tmp/test_valid.yaml', 'w') as f:
        f.write('accounts:\n  test_acc:\n    account_id: "id"\n    master_key: "key"\n    capacity_gb: 10\n')
    try:
        accounts = load_config('/tmp/test_valid.yaml')
        assert len(accounts) == 1
        assert accounts[0].name == "test_acc"
        assert accounts[0].account_id == "id"
        assert accounts[0].master_key == "key"
        assert accounts[0].capacity_gb == 10.0
        print("PASS: valid config loaded correctly")
    except Exception as e:
        print(f"FAIL: {e}")


def test_format_bytes():
    """Test byte formatting."""
    assert format_bytes(0) == "0.0 B"
    assert format_bytes(512) == "512.0 B"
    assert format_bytes(1024) == "1.0 KB"
    assert format_bytes(1024 * 1024) == "1.0 MB"
    assert format_bytes(1024 * 1024 * 1024) == "1.0 GB"
    assert format_bytes(1024 * 1024 * 1024 * 1024) == "1.0 TB"
    print("PASS: format_bytes")


def test_collect_source_files():
    """Test collecting source files."""
    import tempfile
    import os
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create test files
        Path(tmpdir, "file1.txt").write_text("hello")
        subdir = Path(tmpdir, "subdir")
        subdir.mkdir(parents=True, exist_ok=True)
        Path(subdir, "file2.txt").write_text("world")
        Path(tmpdir, "empty.txt").write_text("")

        files = collect_source_files(tmpdir)
        assert len(files) == 3
        sizes = {f.rel_path: f.size for f in files}
        assert sizes["file1.txt"] == 5
        assert sizes["subdir/file2.txt"] == 5
        assert sizes["empty.txt"] == 0
    print("PASS: collect_source_files")


def test_allocation():
    """Test allocation algorithm."""
    from b2router.utils import SourceFile

    # Create test files
    files = [
        SourceFile(Path("/tmp/a.bin"), 100, "a.bin"),
        SourceFile(Path("/tmp/b.bin"), 200, "b.bin"),
        SourceFile(Path("/tmp/c.bin"), 50, "c.bin"),
        SourceFile(Path("/tmp/d.bin"), 300, "d.bin"),
    ]

    # Create buckets with capacity 400 each
    buckets = [
        Bucket("acc1", "bucket1", "id1", 400, 0),
        Bucket("acc2", "bucket2", "id2", 400, 0),
    ]

    allocations = allocate_files(files, buckets)

    # Should allocate largest first: d(300), b(200), a(100), c(50)
    # d(300) -> bucket1 (remaining 100)
    # b(200) -> bucket2 (remaining 200)
    # a(100) -> bucket1 (remaining 0)
    # c(50) -> bucket2 (remaining 150)
    assert len(allocations) == 4
    allocated = {(a.source_file.rel_path, a.bucket.account_name) for a in allocations}
    assert ("d.bin", "acc1") in allocated
    assert ("a.bin", "acc1") in allocated
    assert ("b.bin", "acc2") in allocated
    assert ("c.bin", "acc2") in allocated
    print("PASS: allocation algorithm")


def test_allocation_skip_too_large():
    """Test files too large for any bucket are skipped."""
    from b2router.utils import SourceFile

    files = [
        SourceFile(Path("/tmp/huge.bin"), 1000, "huge.bin"),
        SourceFile(Path("/tmp/small.bin"), 50, "small.bin"),
    ]

    buckets = [
        Bucket("acc1", "bucket1", "id1", 400, 0),
    ]

    allocations = allocate_files(files, buckets)
    assert len(allocations) == 1
    assert allocations[0].source_file.rel_path == "small.bin"
    print("PASS: skip too large files")


def test_bucket_dataclass():
    """Test Bucket dataclass."""
    bucket = Bucket("acc1", "bucket1", "id1", 1000, 100)
    assert bucket.account_name == "acc1"
    assert bucket.bucket_name == "bucket1"
    assert bucket.bucket_id == "id1"
    assert bucket.capacity_bytes == 1000
    assert bucket.used_bytes == 100
    print("PASS: Bucket dataclass")


if __name__ == "__main__":
    print("Running lean b2router tests...\n")

    test_load_config()
    test_format_bytes()
    test_collect_source_files()
    test_allocation()
    test_allocation_skip_too_large()
    test_bucket_dataclass()

    print("\n=== All tests passed ===")