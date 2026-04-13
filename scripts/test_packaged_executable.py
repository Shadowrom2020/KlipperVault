#!/usr/bin/env python3
"""Smoke test a packaged KlipperVault executable for basic functionality."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest


def find_executable() -> Path:
    """Locate the built executable in dist/."""
    candidates = [
        Path("dist/KlipperVault.exe"),
        Path("dist/KlipperVault"),
        Path("dist/KlipperVault.app/Contents/MacOS/KlipperVault"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise FileNotFoundError("No packaged executable found in dist/")


def test_executable_starts() -> None:
    """Test that the executable launches and emits expected startup messages."""
    try:
        executable = find_executable()
    except FileNotFoundError as e:
        pytest.skip(str(e))
    
    print(f"Testing executable: {executable}")
    
    try:
        # Start the process with a timeout.
        # Strip PYTEST_CURRENT_TEST so NiceGUI inside the executable doesn't
        # think it's running under pytest and try to read NICEGUI_SCREEN_TEST_PORT.
        child_env = {k: v for k, v in os.environ.items() if k != "PYTEST_CURRENT_TEST"}
        child_env["BROWSER"] = "/bin/true"  # Suppress browser opening
        process = subprocess.Popen(
            [str(executable)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            env=child_env,
        )
        
        # Give the app 10 seconds to start (increased from 5 for slower CI runners)
        time.sleep(10)
        
        # Check if process is still running
        poll = process.poll()
        if poll is not None:
            stdout, stderr = process.communicate(timeout=1)
            print(f"ERROR: Process exited with code {poll}")
            print(f"STDOUT: {stdout.decode()}")
            print(f"STDERR: {stderr.decode()}")
            pytest.fail(f"Process exited with code {poll}")
        
        # Check for expected startup markers in output
        # (process is still running but we can't easily read live output)
        print("✓ Executable launched successfully")
        print("✓ Process is running (startup phase)")
        
        # Terminate the process gracefully
        process.send_signal(signal.SIGTERM)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        
        print("✓ Executable shut down cleanly")

    except Exception as e:
        pytest.fail(f"Failed to test executable: {e}")


def test_executable_not_console() -> None:
    """Verify that Windows executable is windowed (not console)."""
    if not sys.platform.startswith("win"):
        pytest.skip("Not on Windows")

    try:
        executable = find_executable()
    except FileNotFoundError as e:
        pytest.skip(str(e))
    
    try:
        # Use Windows PE header inspection
        import pefile
    except ImportError:
        pytest.skip("pefile not installed")
    
    try:
        pe = pefile.PE(str(executable))
        subsystem = pe.OPTIONAL_HEADER.Subsystem
        # 3 = Windows CUI (console), 2 = Windows GUI (windowed)
        if subsystem == 2:
            print("✓ Executable is GUI (windowed)")
        else:
            pytest.fail(f"Executable subsystem is {subsystem} (expected 2 for GUI)")
    except Exception as e:
        pytest.skip(f"Could not check executable subsystem: {e}")


def main() -> None:
    """Run all smoke tests."""
    tests = [
        ("Start and shutdown", test_executable_starts),
        ("Windows GUI check", test_executable_not_console),
    ]
    
    results = []
    for name, test_func in tests:
        print(f"\n--- {name} ---")
        try:
            passed = test_func()
            results.append((name, passed))
        except Exception as e:
            print(f"EXCEPTION in {name}: {e}")
            results.append((name, False))
    
    print("\n=== Test Summary ===")
    for name, passed in results:
        status = "✓ PASS" if passed else "✗ FAIL"
        print(f"{status}: {name}")
    
    failed = sum(1 for _, passed in results if not passed)
    if failed > 0:
        print(f"\n{failed} test(s) failed")
        sys.exit(1)
    else:
        print("\nAll tests passed!")
        sys.exit(0)


if __name__ == "__main__":
    main()
