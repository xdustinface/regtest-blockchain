"""
Dashd Process Manager

Handles automatic startup and lifecycle management of dashd for test data generation.
"""

import subprocess
import socket
import time
import tempfile
import shutil
import atexit
from pathlib import Path
from typing import Optional, Tuple
from .errors import DashdConnectionError


class DashdManager:
    """Manages dashd process lifecycle with automatic port detection and cleanup"""

    def __init__(self, dashd_executable: str = "dashd", rpc_port: Optional[int] = None):
        self.dashd_executable = dashd_executable
        self.requested_port = rpc_port
        self.actual_port: Optional[int] = None
        self.p2p_port: Optional[int] = None
        self.temp_dir: Optional[Path] = None
        self.process: Optional[subprocess.Popen] = None
        self.should_cleanup = True

    def is_port_available(self, port: int) -> bool:
        """Check if a port is available for binding"""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind(('127.0.0.1', port))
                return True
        except OSError:
            return False

    def find_free_port(self, start_port: int = 19998, max_attempts: int = 20) -> int:
        """Find first available port starting from start_port"""
        for port in range(start_port, start_port + max_attempts):
            if self.is_port_available(port):
                return port
        raise DashdConnectionError(
            f"No free RPC port found in range {start_port}-{start_port + max_attempts - 1}"
        )

    def verify_dashd_executable(self) -> bool:
        """Check if dashd executable exists and is runnable"""
        try:
            result = subprocess.run(
                [self.dashd_executable, '--version'],
                capture_output=True,
                text=True,
                timeout=5
            )
            return result.returncode == 0
        except (subprocess.TimeoutExpired, FileNotFoundError, PermissionError):
            return False

    def start(self, keep_temp: bool = False) -> Tuple[int, Path]:
        """
        Start dashd in a temporary directory with auto-detected port.

        Returns:
            Tuple of (rpc_port, temp_dir_path)

        Raises:
            DashdConnectionError: If dashd cannot be started
        """
        print("Starting dashd...")

        # Verify executable exists
        if not self.verify_dashd_executable():
            raise DashdConnectionError(
                f"dashd executable not found or not runnable: {self.dashd_executable}\n"
                f"Please ensure dashd is installed and accessible, or use --dashd-path to specify location"
            )

        # Determine RPC port
        if self.requested_port:
            if not self.is_port_available(self.requested_port):
                raise DashdConnectionError(
                    f"Requested RPC port {self.requested_port} is not available"
                )
            self.actual_port = self.requested_port
        else:
            self.actual_port = self.find_free_port(19998)

        # Determine P2P port
        self.p2p_port = self.find_free_port(self.actual_port + 1)

        # Create temporary directory
        self.temp_dir = Path(tempfile.mkdtemp(prefix='dash-testdata-'))
        self.should_cleanup = not keep_temp

        # Create regtest subdirectory (dashd requires it to exist)
        regtest_dir = self.temp_dir / 'regtest'
        regtest_dir.mkdir(exist_ok=True)

        print(f"  Using temporary directory: {self.temp_dir}")
        print(f"  RPC port: {self.actual_port}")
        print(f"  P2P port: {self.p2p_port}")

        # Build dashd command
        cmd = [
            self.dashd_executable,
            '-regtest',
            f'-datadir={self.temp_dir}',
            f'-port={self.p2p_port}',
            f'-rpcport={self.actual_port}',
            '-server=1',
            '-daemon=0',  # Run in foreground (we manage the process)
            '-fallbackfee=0.00001',
            '-rpcbind=127.0.0.1',
            '-rpcallowip=127.0.0.1',
            '-listen=1',
            '-txindex=0',
            '-addressindex=0',
            '-spentindex=0',
            '-timestampindex=0',
        ]

        # Start dashd process with increased file descriptor limit
        try:
            import resource

            def preexec_fn():
                """Set file descriptor limit before starting dashd"""
                try:
                    # Increase file descriptor limit to 10000
                    resource.setrlimit(resource.RLIMIT_NOFILE, (10000, 10000))
                except:
                    pass  # Ignore if we can't set it

            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                cwd=str(self.temp_dir),
                preexec_fn=preexec_fn
            )
        except FileNotFoundError:
            raise DashdConnectionError(
                f"Failed to execute dashd: {self.dashd_executable}\n"
                f"Please check the path or install Dash Core"
            )

        # Register cleanup handler
        atexit.register(self.stop)

        # Wait for dashd to be ready
        print(f"  Waiting for dashd to be ready (PID: {self.process.pid})...")
        if not self._wait_for_ready(timeout=30):
            self.stop()
            raise DashdConnectionError(
                "dashd failed to start within 30 seconds. Check dashd installation."
            )

        print(f"✓ dashd started successfully")
        return self.actual_port, self.temp_dir

    def _wait_for_ready(self, timeout: int = 30) -> bool:
        """Wait for dashd to become ready to accept RPC calls"""
        from .rpc_client import DashRPCClient

        # Determine dash-cli path from dashd path
        dashd_path = Path(self.dashd_executable)
        if dashd_path.is_absolute():
            dashcli_path = str(dashd_path.parent / 'dash-cli')
        else:
            dashcli_path = 'dash-cli'

        # Create RPC client for this instance
        rpc = DashRPCClient(
            dashcli_path=dashcli_path,
            datadir=str(self.temp_dir),
            rpc_port=self.actual_port
        )

        start_time = time.time()
        last_error = None

        while time.time() - start_time < timeout:
            # Check if process died
            if self.process and self.process.poll() is not None:
                return False

            try:
                # Try to get block count
                rpc.call('getblockcount')
                return True
            except Exception as e:
                last_error = str(e)
                time.sleep(0.5)

        print(f"  Warning: Timeout waiting for dashd. Last error: {last_error}")
        return False

    def stop(self):
        """Stop dashd and cleanup temporary directory"""
        if self.process:
            print("\nStopping dashd...")
            try:
                self.process.terminate()
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                print("  dashd didn't stop gracefully, forcing...")
                self.process.kill()
                self.process.wait()
            except Exception as e:
                print(f"  Warning: Error stopping dashd: {e}")
            finally:
                self.process = None

        if self.temp_dir and self.should_cleanup:
            print(f"  Cleaning up temporary directory: {self.temp_dir}")
            try:
                shutil.rmtree(self.temp_dir, ignore_errors=True)
            except Exception as e:
                print(f"  Warning: Could not remove temp directory: {e}")
            self.temp_dir = None

    def get_port(self) -> int:
        """Get the RPC port being used"""
        if not self.actual_port:
            raise DashdConnectionError("dashd not started yet")
        return self.actual_port

    def get_temp_dir(self) -> Path:
        """Get the temporary directory path"""
        if not self.temp_dir:
            raise DashdConnectionError("dashd not started yet")
        return self.temp_dir
