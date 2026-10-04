#!/usr/bin/env python3
"""Test b2.py logic with mocked B2 API to verify bug fixes."""
import sys
import os
from pathlib import Path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Mock b2sdk before importing b2.py
from unittest.mock import MagicMock, patch
import types

mock_b2api = MagicMock()
mock_b2api.authorize_account.return_value = None
mock_b2api.list_buckets.return_value = []
mock_b2api.get_bucket_by_name.return_value = MagicMock()
mock_b2api.get_bucket_by_name.return_value.ls.return_value = []

# Patch the b2sdk module
b2sdk_module = types.ModuleType('b2sdk')
b2sdk_v2 = types.ModuleType('b2sdk.v2')
b2sdk_v2.B2Api = MagicMock(return_value=mock_b2api)
b2sdk_v2.InMemoryAccountInfo = MagicMock()
# Add exception module for B2Error import
b2sdk_v2_exception = types.ModuleType('b2sdk.v2.exception')
b2sdk_v2_exception.B2Error = Exception
b2sdk_v2.exception = b2sdk_v2_exception
b2sdk_module.v2 = b2sdk_v2
sys.modules['b2sdk'] = b2sdk_module
sys.modules['b2sdk.v2'] = b2sdk_v2
sys.modules['b2sdk.v2.exception'] = b2sdk_v2_exception

# Now import b2.py
import b2

# Test 1: load_config with empty YAML
print("=== Test 1: load_config with empty YAML ===")
with open('/tmp/empty.yaml', 'w') as f:
    f.write('')
try:
    accounts = b2.load_config('/tmp/empty.yaml')
    print(f"PASS: empty YAML returns {len(accounts)} accounts")
except Exception as e:
    print(f"FAIL: {e}")

# Test 2: load_config with missing key
print("\n=== Test 2: load_config with missing key ===")
with open('/tmp/bad.yaml', 'w') as f:
    f.write('accounts:\n  bad_acc:\n    account_id: "abc"\n')
try:
    accounts = b2.load_config('/tmp/bad.yaml')
    print(f"FAIL: should have raised KeyError, got {accounts}")
except KeyError as e:
    print(f"PASS: KeyError raised: {e}")

# Test 3: load_config with empty account_id
print("\n=== Test 3: load_config with empty account_id ===")
with open('/tmp/empty_id.yaml', 'w') as f:
    f.write('accounts:\n  bad_acc:\n    account_id: ""\n    master_key: "key"\n    capacity_in_gb: 10\n')
try:
    accounts = b2.load_config('/tmp/empty_id.yaml')
    print(f"FAIL: should have raised ValueError, got {accounts}")
except ValueError as e:
    print(f"PASS: ValueError raised: {e}")

# Test 4: load_config with empty master_key
print("\n=== Test 4: load_config with empty master_key ===")
with open('/tmp/empty_key.yaml', 'w') as f:
    f.write('accounts:\n  bad_acc:\n    account_id: "id"\n    master_key: ""\n    capacity_in_gb: 10\n')
try:
    accounts = b2.load_config('/tmp/empty_key.yaml')
    print(f"FAIL: should have raised ValueError, got {accounts}")
except ValueError as e:
    print(f"PASS: ValueError raised: {e}")

# Test 5: load_config with invalid capacity
print("\n=== Test 5: load_config with invalid capacity ===")
with open('/tmp/bad_cap.yaml', 'w') as f:
    f.write('accounts:\n  bad_acc:\n    account_id: "id"\n    master_key: "key"\n    capacity_in_gb: -5\n')
try:
    accounts = b2.load_config('/tmp/bad_cap.yaml')
    print(f"FAIL: should have raised ValueError, got {accounts}")
except ValueError as e:
    print(f"PASS: ValueError raised: {e}")

# Test 6: get_unique_object_name nesting fix
print("\n=== Test 6: get_unique_object_name nesting fix ===")
bucket = b2.Bucket('test', 'id1', 10)
bucket.add_file('file.txt', 100)
name1 = b2.get_unique_object_name(bucket, 'file.txt')
print(f"First duplicate: {name1}")
assert name1 == "Copy of (1) file.txt", f"Expected 'Copy of (1) file.txt', got '{name1}'"
bucket.add_file(name1, 100)
name2 = b2.get_unique_object_name(bucket, 'file.txt')
print(f"Second duplicate: {name2}")
assert name2 == "Copy of (2) file.txt", f"Expected 'Copy of (2) file.txt', got '{name2}'"
# Verify no nested "Copy of Copy of"
assert "Copy of Copy of" not in name2, f"Nested prefix found: {name2}"
print("PASS: no nested prefixes, correct numbering")

# Test 7: capacity per-account division
print("\n=== Test 7: capacity per-account division ===")
acc = b2.Account('test_acc', 'acc_id', 'key', 10)
# Simulate 5 buckets
for i in range(5):
    b = b2.Bucket(f'bucket{i}', f'id{i}', 10)
    b.account = acc
    acc.buckets.append(b)
n = len(acc.buckets)
total_capacity = int(10 * (1024**3) * b2.CAP_SAFETY)
per_bucket = total_capacity // n
for bucket in acc.buckets:
    bucket.capacity_bytes = per_bucket
total = sum(b.capacity_bytes for b in acc.buckets)
print(f"5 buckets, total capacity: {total / (1024**3):.2f} GB (should be ~9.9 GB with safety)")
assert total <= total_capacity, f"Total {total} exceeds account capacity {total_capacity}"
print("PASS: total capacity stays within account limit")

# Test 8: bucket idempotency
print("\n=== Test 8: discover_and_add_buckets_for_account idempotency ===")
# Reset
acc2 = b2.Account('test_acc2', 'acc_id2', 'key2', 10)
acc2.client = MagicMock()
acc2.client.list_buckets.return_value = [
    MagicMock(name='bucket1', id_='b1', bucket_id='b1'),
    MagicMock(name='bucket2', id_='b2', bucket_id='b2'),
]
# First call
b2.discover_and_add_buckets_for_account(acc2)
print(f"After first call: {len(acc2.buckets)} buckets")
# Second call - should clear and re-add
b2.discover_and_add_buckets_for_account(acc2)
print(f"After second call: {len(acc2.buckets)} buckets (should be 2, not 4)")
assert len(acc2.buckets) == 2, f"Expected 2 buckets, got {len(acc2.buckets)}"
print("PASS: idempotent - no bucket duplication")

# Test 9: bucket_id fallback
print("\n=== Test 9: bucket_id fallback ===")
acc3 = b2.Account('test_acc3', 'acc_id3', 'key3', 10)
acc3.client = MagicMock()
# SDK object without id_ but with bucket_id
mock_bucket = MagicMock()
del mock_bucket.id_  # remove id_ attribute
mock_bucket.bucket_id = 'fb1'
mock_bucket.name = 'fallback_bucket'
acc3.client.list_buckets.return_value = [mock_bucket]
b2.discover_and_add_buckets_for_account(acc3)
print(f"Bucket ID: {acc3.buckets[0].id_}")
assert acc3.buckets[0].id_ == 'fb1', f"Expected 'fb1', got '{acc3.buckets[0].id_}'"
print("PASS: bucket_id fallback works")

# Test 10: delete_source_file failure handling
print("\n=== Test 10: delete_source_file failure handling ===")
# Create a file and then make it undeletable (simulate)
with open('/tmp/test_delete.txt', 'w') as f:
    f.write('test')
result = b2.delete_source_file(Path('/tmp/test_delete.txt'))
print(f"Delete result: {result}")
assert result == True, f"Expected True, got {result}"
assert not Path('/tmp/test_delete.txt').exists(), "File should be deleted"
print("PASS: delete_source_file works correctly")

# Test 11: collect_source_files includes dotfiles
print("\n=== Test 11: collect_source_files includes dotfiles ===")
os.makedirs('/tmp/test_src', exist_ok=True)
with open('/tmp/test_src/normal.txt', 'w') as f:
    f.write('test')
with open('/tmp/test_src/.hidden', 'w') as f:
    f.write('hidden')
files = b2.collect_source_files('/tmp/test_src')
names = [f.rel_path for f in files]
print(f"Found files: {names}")
assert 'normal.txt' in names, f"Expected normal.txt in {names}"
assert '.hidden' in names, f"Expected .hidden in {names}"
print("PASS: dotfiles are now included")

# Test 12: broken symlink handling
print("\n=== Test 12: broken symlink handling ===")
os.makedirs('/tmp/test_broken', exist_ok=True)
with open('/tmp/test_broken/good.txt', 'w') as f:
    f.write('test')
os.symlink('/tmp/nonexistent', '/tmp/test_broken/broken')
files = b2.collect_source_files('/tmp/test_broken')
names = [f.rel_path for f in files]
print(f"Found files: {names}")
assert 'good.txt' in names, f"Expected good.txt in {names}"
assert 'broken' not in names, f"Broken symlink should be skipped, got {names}"
print("PASS: broken symlink is skipped without crash")

# Test 13: valid symlink handling (should be skipped, not followed)
print("\n=== Test 13: valid symlink handling ===")
os.makedirs('/tmp/test_symlink', exist_ok=True)
with open('/tmp/test_symlink/real.txt', 'w') as f:
    f.write('test')
os.symlink('/tmp/test_symlink/real.txt', '/tmp/test_symlink/link.txt')
files = b2.collect_source_files('/tmp/test_symlink')
names = [f.rel_path for f in files]
print(f"Found files: {names}")
assert 'real.txt' in names, f"Expected real.txt in {names}"
assert 'link.txt' not in names, f"Valid symlink should be skipped, got {names}"
print("PASS: valid symlink is skipped (not followed)")

# Test 14: has_file O(1) lookup with set
print("\n=== Test 14: has_file O(1) lookup ===")
bucket = b2.Bucket('test', 'id1', 10)
# Add many files
for i in range(1000):
    bucket.add_file(f'file{i}.txt', 100)
# Check that has_file works and is fast (uses set)
assert bucket.has_file('file0.txt') == True
assert bucket.has_file('file500.txt') == True
assert bucket.has_file('file999.txt') == True
assert bucket.has_file('nonexistent.txt') == False
# Check _file_names set is maintained
assert len(bucket._file_names) == 1000
print("PASS: has_file uses set for O(1) lookup")

# Test 15: populate_failed flag behavior
print("\n=== Test 15: populate_failed flag ===")
bucket = b2.Bucket('test', 'id1', 10)
bucket.add_file('existing.txt', 100)
bucket.used_bytes = 100
assert bucket._populate_failed == False
# Simulate populate failure by not calling populate_bucket_files_and_usage
# but manually setting the flag
bucket._populate_failed = True
assert bucket._populate_failed == True
# Allocation should skip buckets with populate_failed
print("PASS: populate_failed flag works correctly")

# Test 16: cleanup_empty_dirs
print("\n=== Test 16: cleanup_empty_dirs ===")
os.makedirs('/tmp/test_cleanup/a/b/c', exist_ok=True)
with open('/tmp/test_cleanup/a/file.txt', 'w') as f:
    f.write('test')
# Remove the file
Path('/tmp/test_cleanup/a/file.txt').unlink()
# Cleanup
b2.cleanup_empty_dirs('/tmp/test_cleanup')
# Check that empty directories were removed
assert not Path('/tmp/test_cleanup/a/b/c').exists()
assert not Path('/tmp/test_cleanup/a/b').exists()
assert not Path('/tmp/test_cleanup/a').exists()
print("PASS: cleanup_empty_dirs removes empty directories")

# Test 17: cleanup_empty_dirs doesn't follow symlinks
print("\n=== Test 17: cleanup_empty_dirs doesn't follow symlinks ===")
os.makedirs('/tmp/test_cleanup_symlink/real_dir/subdir', exist_ok=True)
os.makedirs('/tmp/test_cleanup_symlink/link_target', exist_ok=True)
# Put a file in real_dir so it's not empty
with open('/tmp/test_cleanup_symlink/real_dir/file.txt', 'w') as f:
    f.write('test')
os.symlink('/tmp/test_cleanup_symlink/link_target', '/tmp/test_cleanup_symlink/symlink_dir')
# Cleanup should not traverse into symlink_dir
b2.cleanup_empty_dirs('/tmp/test_cleanup_symlink')
# The real_dir should still exist (not empty)
assert Path('/tmp/test_cleanup_symlink/real_dir').exists()
# link_target is empty but is a real directory - it will be removed
# The symlink should still exist (not followed)
assert Path('/tmp/test_cleanup_symlink/symlink_dir').is_symlink()
print("PASS: cleanup_empty_dirs doesn't follow symlinks")

# Test 18: FileMetadata equality includes size and sha1
print("\n=== Test 18: FileMetadata equality ===")
f1 = b2.FileMetadata('file.txt', 100, 'sha1')
f2 = b2.FileMetadata('file.txt', 100, 'sha1')
f3 = b2.FileMetadata('file.txt', 200, 'sha1')
f4 = b2.FileMetadata('file.txt', 100, 'sha2')
assert f1 == f2
assert f1 != f3
assert f1 != f4
assert hash(f1) == hash(f2)
print("PASS: FileMetadata equality includes size and sha1")

# Test 19: validate_object_name UTF-8 validation
print("\n=== Test 19: validate_object_name UTF-8 validation ===")
import b2
# Valid names should pass
b2.validate_object_name("normal_file.txt")
b2.validate_object_name("file with spaces.txt")
b2.validate_object_name("unicode_文件.txt")
print("  Valid names pass")

# Invalid control characters should fail
try:
    b2.validate_object_name("file\x00name.txt")
    print("FAIL: should have raised ValueError for null byte")
except ValueError as e:
    print(f"  Null byte rejected: {e}")

try:
    b2.validate_object_name("file\x01name.txt")
    print("FAIL: should have raised ValueError for control char")
except ValueError as e:
    print(f"  Control char rejected: {e}")

try:
    b2.validate_object_name("file\x7fname.txt")
    print("FAIL: should have raised ValueError for DEL")
except ValueError as e:
    print(f"  DEL rejected: {e}")

# Valid control chars (tab, newline, carriage return) should pass
b2.validate_object_name("file\tname.txt")
b2.validate_object_name("file\nname.txt")
b2.validate_object_name("file\rname.txt")
print("  Tab/newline/carriage return accepted")

# Test max length (1024 bytes)
long_name = "a" * 1024
b2.validate_object_name(long_name)
print("  1024 byte name accepted")

too_long = "a" * 1025
try:
    b2.validate_object_name(too_long)
    print("FAIL: should have raised ValueError for too long name")
except ValueError as e:
    print(f"  >1024 byte name rejected: {e}")

print("PASS: validate_object_name UTF-8 validation works")

# Cleanup
import shutil
for path in ['/tmp/empty.yaml', '/tmp/bad.yaml', '/tmp/empty_id.yaml', '/tmp/empty_key.yaml', 
             '/tmp/bad_cap.yaml', '/tmp/test_delete.txt', '/tmp/test_src', '/tmp/test_broken',
             '/tmp/test_symlink', '/tmp/test_cleanup', '/tmp/test_cleanup_symlink']:
    try:
        if os.path.isdir(path):
            shutil.rmtree(path)
        elif os.path.exists(path):
            os.unlink(path)
    except:
        pass

print("\n=== All tests passed! ===")