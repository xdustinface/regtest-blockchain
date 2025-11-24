#!/usr/bin/env python3
"""
Dash Regtest Test Data Generator

Efficient generation of comprehensive blockchain test data with:
- Automatic dashd startup in temporary directory
- Proactive UTXO management (prevents "Insufficient funds")
- Diverse transaction types (edge case coverage)
- Robust error handling
- Portable operation from any directory

Usage:
    python3 generate.py --blocks 100
    python3 generate.py --blocks 1000 --dashd-path /path/to/dashd
    python3 generate.py --blocks 500 --keep-temp
"""

import sys
import os
import json
import subprocess
import random
import struct
import shutil
from pathlib import Path
from typing import List, Dict, Any, Optional
from dataclasses import dataclass
import time
import datetime


# Add generator module to path
sys.path.insert(0, str(Path(__file__).parent))

from generator.errors import *
from generator.rpc_client import DashRPCClient
from generator.dashd_manager import DashdManager


@dataclass
class Config:
    """Configuration for test data generation"""
    target_blocks: int
    batch_size: int
    min_utxo_threshold: int
    target_utxo_count: int
    dashcli_path: str
    dashd_executable: str
    auto_start_dashd: bool
    dashd_datadir: Optional[str]
    dashd_wallet: str
    rpc_port: Optional[int]
    output_base: str
    # Transaction generation parameters
    tx_probability_none: float = 0.30    # Probability of 0 transactions per block
    tx_probability_low: float = 0.70     # Cumulative probability for 1-3 transactions
    tx_probability_medium: float = 0.90  # Cumulative probability for 4-10 transactions
    # else: 11-25 transactions (high)


class Generator:
    """Main test data generator with integrated UTXO management"""

    def __init__(self, config: Config, keep_temp: bool = False):
        self.config = config
        self.keep_temp = keep_temp
        self.dashd_manager: Optional[DashdManager] = None
        self.rpc: Optional[DashRPCClient] = None
        self.wallets = []  # List of wallet dictionaries with name, mnemonic, addresses, tier
        self.all_addresses = []  # Flat list of all addresses for easy access
        self.utxo_count = 0
        self.output_dir: Optional[Path] = None
        self.stats = {
            'blocks_generated': 0,
            'transactions_created': 0,
            'utxo_replenishments': 0
        }

    def generate(self):
        """Main generation workflow"""
        print("=" * 60)
        print("Dash Regtest Test Data Generator")
        print("=" * 60)
        print(f"Target blocks: {self.config.target_blocks}")
        print(f"UTXO threshold: {self.config.min_utxo_threshold}")
        print()

        generation_start_time = time.time()

        try:
            self._ensure_dashd_running()
            self._initialize_rpc_client()
            self._verify_dashd()
            self._load_addresses()
            self._initialize_utxo_pool()
            self._generate_blocks()
            self._export_data()

            generation_duration = time.time() - generation_start_time
            duration_str = str(datetime.timedelta(seconds=int(generation_duration)))

            print("\n" + "=" * 60)
            print("✓ Generation complete!")
            print("=" * 60)
            print(f"Blocks: {self.stats['blocks_generated']}")
            print(f"Transactions: {self.stats['transactions_created']}")
            print(f"UTXO replenishments: {self.stats['utxo_replenishments']}")
            print(f"Total duration: {duration_str}")

        except KeyboardInterrupt:
            print("\n\nGeneration interrupted by user")
            raise GeneratorError("User interrupted")
        finally:
            # Cleanup if not already done
            if self.dashd_manager:
                if self.dashd_manager.process:
                    self.dashd_manager.stop()
                elif self.dashd_manager.temp_dir and self.dashd_manager.should_cleanup:
                    # Process stopped but temp dir not cleaned up yet
                    try:
                        shutil.rmtree(self.dashd_manager.temp_dir, ignore_errors=True)
                    except:
                        pass

    def _ensure_dashd_running(self):
        """Start dashd if auto_start is enabled"""
        if not self.config.auto_start_dashd:
            return

        print("\n" + "=" * 60)
        print("DASHD AUTO-START")
        print("=" * 60)

        self.dashd_manager = DashdManager(
            dashd_executable=self.config.dashd_executable,
            rpc_port=self.config.rpc_port
        )

        rpc_port, temp_dir = self.dashd_manager.start(keep_temp=self.keep_temp)

        # Update config with actual values
        self.config.rpc_port = rpc_port
        self.config.dashd_datadir = str(temp_dir)

        print("=" * 60)
        print()

    def _initialize_rpc_client(self):
        """Initialize RPC client with appropriate settings"""
        self.rpc = DashRPCClient(
            dashcli_path=self.config.dashcli_path,
            datadir=self.config.dashd_datadir,
            rpc_port=self.config.rpc_port
        )

    def _verify_dashd(self):
        """Verify dashd is running and responsive, create wallet if needed"""
        print("Verifying dashd connection...")
        try:
            block_count = self.rpc.call('getblockcount')
            print(f"✓ Connected to dashd (current height: {block_count})")
        except DashdConnectionError as e:
            print(f"✗ Cannot connect to dashd: {e}")
            print("\nPlease ensure dashd is running in regtest mode:")
            print(f"  dashd -regtest -daemon")
            raise

        # Ensure wallet exists and is loaded
        try:
            # Try to load the wallet if it exists but isn't loaded
            self.rpc.call('loadwallet', self.config.dashd_wallet)
            print(f"✓ Loaded wallet: {self.config.dashd_wallet}")
        except RPCError as e:
            error_msg = str(e).lower()
            if "already loaded" in error_msg:
                print(f"✓ Wallet already loaded: {self.config.dashd_wallet}")
            elif "not found" in error_msg or "does not exist" in error_msg:
                # Wallet doesn't exist, create it
                print(f"Creating new wallet: {self.config.dashd_wallet}")
                self.rpc.call('createwallet', self.config.dashd_wallet)
                print(f"✓ Created wallet: {self.config.dashd_wallet}")
            else:
                print(f"✗ Unexpected wallet error: {e}")
                raise

    def _load_addresses(self):
        """Generate addresses using separate dashd wallets for each tier"""
        print("\nGenerating separate HD wallets via dashd...")

        # Define wallet configurations: (name, tier, num_addresses)
        wallet_configs = [
            ('light', 'light', 20),    # Light usage: few addresses
            ('normal', 'normal', 60),  # Normal usage: moderate addresses
            ('heavy', 'heavy', 120),   # Heavy usage: many addresses
        ]

        for wallet_name, tier, num_addresses in wallet_configs:
            print(f"  Creating {wallet_name} wallet ({tier} tier, {num_addresses} addresses)...")

            # Create a separate dashd wallet for this tier
            try:
                self.rpc.call('createwallet', wallet_name)
                print(f"    ✓ Created dashd wallet: {wallet_name}")
            except RPCError as e:
                error_msg = str(e).lower()
                if "already exists" in error_msg or "already loaded" in error_msg:
                    print(f"    ✓ Wallet already exists: {wallet_name}")
                else:
                    raise

            # Get HD wallet info from this specific wallet
            hd_info = self.rpc.call('dumphdinfo', wallet=wallet_name)
            mnemonic = hd_info.get('mnemonic', '')

            addresses = []
            for i in range(num_addresses):
                label = f"{wallet_name}_{i:03d}"

                # Generate new address in THIS wallet
                address = self.rpc.call('getnewaddress', label, wallet=wallet_name)

                # Get the private key from THIS wallet
                private_key = self.rpc.call('dumpprivkey', address, wallet=wallet_name)

                # Get address info to determine HD path
                addr_info = self.rpc.call('getaddressinfo', address, wallet=wallet_name)
                hd_path = addr_info.get('hdkeypath', f"m/44'/1'/0'/0/{i}")

                addr_dict = {
                    'address': address,
                    'label': label,
                    'private_key': private_key,
                    'hd_path': hd_path,
                    'tier': tier,
                    'wallet_name': wallet_name  # Track which dashd wallet this address belongs to
                }

                addresses.append(addr_dict)
                self.all_addresses.append(addr_dict)

                # Progress update every 10 addresses
                if (i + 1) % 10 == 0:
                    print(f"    Generated {i + 1}/{num_addresses} addresses...")

            # Store wallet info with empty transaction and UTXO lists (will be filled during generation)
            self.wallets.append({
                'wallet_name': wallet_name,
                'mnemonic': mnemonic,
                'addresses': addresses,
                'tier': tier,
                'transactions': [],  # Will be populated during block generation
                'utxos': [],  # Will be populated at the end
                'balance': 0  # Will be calculated at the end (in satoshis)
            })

            print(f"  ✓ {wallet_name} complete: {len(addresses)} addresses")

        print(f"\n✓ Generated {len(self.wallets)} wallets with {len(self.all_addresses)} total addresses")

    def _collect_wallet_statistics(self):
        """Collect transaction history, UTXOs, and balance for each wallet (including miner)"""
        print("\n  Collecting wallet statistics...")

        # Collect stats for faucet (default) wallet - it mines blocks and funds others
        faucet_wallet = self._collect_single_wallet_stats(self.config.dashd_wallet, 'faucet')

        # Add faucet wallet to beginning of wallets list so it's exported too
        self.wallets.insert(0, faucet_wallet)

        # Collect stats for tier wallets (light, normal, heavy)
        # Skip index 0 since that's the miner wallet we just added
        for wallet in self.wallets[1:]:
            wallet_name = wallet['wallet_name']
            stats = self._collect_single_wallet_stats(wallet_name, wallet.get('tier', 'unknown'))

            # Update the wallet dict with collected stats
            wallet['transactions'] = stats['transactions']
            wallet['utxos'] = stats['utxos']
            wallet['balance'] = stats['balance']

    def _collect_single_wallet_stats(self, wallet_name: str, tier: str) -> dict:
        """Collect statistics for a single wallet"""
        print(f"    Processing {wallet_name}...")

        # Get all transactions from THIS wallet's dashd wallet
        transactions = []
        try:
            addr_txs = self.rpc.call('listtransactions', '*', 10000, 0, True, wallet=wallet_name)
            for tx in addr_txs:
                transactions.append({
                    'txid': tx['txid'],
                    'address': tx.get('address', ''),
                    'amount': tx['amount'],
                    'confirmations': tx.get('confirmations', 0),
                    'blockhash': tx.get('blockhash', ''),
                    'time': tx.get('time', 0)
                })
        except RPCError as e:
            print(f"        Warning: Error getting transactions: {e}")

        # Get UTXOs from THIS wallet
        utxos_list = []
        balance = 0.0
        try:
            wallet_utxos = self.rpc.call('listunspent', 1, 9999999, [], wallet=wallet_name)

            utxos_list = [
                {
                    'txid': utxo['txid'],
                    'vout': utxo['vout'],
                    'address': utxo['address'],
                    'amount': utxo['amount'],
                    'confirmations': utxo['confirmations']
                }
                for utxo in wallet_utxos
            ]
            balance = sum(utxo['amount'] for utxo in wallet_utxos)
        except RPCError as e:
            print(f"        Warning: Error getting UTXOs: {e}")

        print(f"      {len(transactions)} txs, {len(utxos_list)} UTXOs, balance: {balance:.8f} DASH")

        # For miner wallet, get addresses if not already defined
        addresses = []
        if wallet_name == self.config.dashd_wallet:
            # Get HD info for miner wallet
            try:
                hd_info = self.rpc.call('dumphdinfo', wallet=wallet_name)
                mnemonic = hd_info.get('mnemonic', '')
            except RPCError:
                mnemonic = ''
        else:
            mnemonic = ''

        return {
            'wallet_name': wallet_name,
            'mnemonic': mnemonic,
            'addresses': addresses,  # Empty for miner, already set for tier wallets
            'tier': tier,
            'transactions': transactions,
            'utxos': utxos_list,
            'balance': balance
        }

    def _save_wallet_files(self):
        """Save each wallet to a separate JSON file in wallets/ directory"""
        print("\n  Saving wallet files...")

        # Create wallets directory
        wallets_dir = self.output_dir / "wallets"
        wallets_dir.mkdir(parents=True, exist_ok=True)

        for wallet in self.wallets:
            wallet_file = wallets_dir / f"{wallet['wallet_name']}.json"

            # Create wallet data structure
            # Note: Addresses are not included in export since they're derived from mnemonic
            wallet_data = {
                'wallet_name': wallet['wallet_name'],
                'mnemonic': wallet['mnemonic'],
                'balance': wallet['balance'],
                'transaction_count': len(wallet['transactions']),
                'utxo_count': len(wallet['utxos']),
                'transactions': wallet['transactions'],
                'utxos': wallet['utxos']
            }

            # Write to file
            with open(wallet_file, 'w') as f:
                json.dump(wallet_data, f, indent=2)

            print(f"    ✓ {wallet['wallet_name']}.json: "
                  f"{len(wallet['addresses'])} addrs, {len(wallet['transactions'])} txs, "
                  f"{len(wallet['utxos'])} UTXOs, balance: {wallet['balance']:.8f} DASH")

    def _initialize_utxo_pool(self):
        """
        Create initial UTXO pool and fund each wallet separately.

        This is critical for preventing "Insufficient funds" errors.
        """
        print("\nInitializing UTXO pool and funding wallets...")

        # Generate initial blocks to default wallet for mining rewards
        print(f"  Generating initial blocks to default wallet...")
        default_addr = self.rpc.call('getnewaddress', wallet=self.config.dashd_wallet)
        self.rpc.call('generatetoaddress', 200, default_addr)

        print(f"  Waiting for maturity (100 blocks)...")
        self.rpc.call('generatetoaddress', 100, default_addr)

        # Generate more blocks to have plenty of funds for both splitting and wallet funding
        print(f"  Generating additional blocks for funding...")
        self.rpc.call('generatetoaddress', 100, default_addr)

        print(f"  Splitting default wallet into {self.config.target_utxo_count} UTXOs...")
        self._split_utxos(self.config.target_utxo_count, self.config.dashd_wallet)

        self.utxo_count = len(self.rpc.call('listunspent', 1, wallet=self.config.dashd_wallet))
        print(f"  ✓ Default wallet UTXO pool: {self.utxo_count} UTXOs")

        # Now fund each tier wallet from the default wallet
        print(f"\n  Funding tier wallets from default wallet...")
        for wallet in self.wallets:
            wallet_name = wallet['wallet_name']
            tier = wallet['tier']

            if tier == 'light':
                num_funding_txs = 20
            elif tier == 'normal':
                num_funding_txs = 40
            else:
                num_funding_txs = 60

            print(f"    Funding {wallet_name} with {num_funding_txs} transactions...")

            for i in range(num_funding_txs):
                addr = wallet['addresses'][i % len(wallet['addresses'])]['address']
                amount = round(random.uniform(0.1, 5.0), 8)
                try:
                    self.rpc.call('sendtoaddress', addr, amount, wallet=self.config.dashd_wallet)
                except RPCError as e:
                    print(f"      Warning: Failed to fund {addr}: {e}")

            self.rpc.call('generatetoaddress', 1, default_addr)

            balance = self.rpc.call('getbalance', wallet=wallet_name)
            print(f"      ✓ {wallet_name} funded: {balance:.2f} DASH")

        print(f"\n✓ All wallets funded and initialized")

    def _split_utxos(self, target_count: int, wallet_name: str):
        """Split UTXOs to create enough for transactions"""
        print(f"    Creating UTXOs (target: {target_count})...")

        try:
            default_addr = self.rpc.call('getnewaddress', wallet=self.config.dashd_wallet)

            iteration = 0
            while iteration < 10:
                utxos = self.rpc.call('listunspent', 1, wallet=wallet_name)
                current_count = len(utxos)

                if iteration % 2 == 0:
                    print(f"      Current UTXOs: {current_count}/{target_count}")

                if current_count >= target_count:
                    break

                balance = self.rpc.call('getbalance', wallet=wallet_name)
                if balance < 10:
                    break

                recipients = {}
                for i in range(min(50, target_count - current_count)):
                    wallet_addr = self.rpc.call('getnewaddress', wallet=wallet_name)
                    recipients[wallet_addr] = 1.0

                try:
                    self.rpc.call('sendmany', '', json.dumps(recipients), wallet=wallet_name)
                except (RPCError, InsufficientFundsError):
                    break

                self.rpc.call('generatetoaddress', 1, default_addr)
                iteration += 1

            final_count = len(self.rpc.call('listunspent', 1, wallet=wallet_name))
            print(f"    ✓ UTXO creation complete: {final_count} UTXOs")
        except InsufficientFundsError:
            print(f"    ⚠ UTXO creation stopped early")
            print(f"      Will use existing UTXOs")

    def _generate_blocks(self):
        """Generate blocks with transactions to reach target height"""
        # Calculate how many blocks we need to generate to reach target
        current_height = self.rpc.call('getblockcount')
        blocks_to_generate = self.config.target_blocks - current_height

        if blocks_to_generate <= 0:
            print(f"\nAlready at or past target height ({current_height} >= {self.config.target_blocks})")
            return

        print(f"\nGenerating {blocks_to_generate} blocks to reach height {self.config.target_blocks}...")
        print(f"  Current height: {current_height}")

        start_time = time.time()
        tx_attempts = 0
        tx_successes = 0

        for block_num in range(blocks_to_generate):
            tx_count = self._determine_tx_count()

            # Create transactions (they will accumulate in mempool)
            if tx_count > 0:
                for _ in range(tx_count):
                    tx_attempts += 1
                    before_count = self.stats['transactions_created']
                    self._create_transaction()
                    if self.stats['transactions_created'] > before_count:
                        tx_successes += 1

            # Mine a block (this confirms all mempool transactions)
            addr = random.choice(self.all_addresses)['address']
            self.rpc.call('generatetoaddress', 1, addr)
            self.stats['blocks_generated'] += 1

            if (block_num + 1) % 100 == 0:
                elapsed = time.time() - start_time
                rate = (block_num + 1) / elapsed
                eta_seconds = (blocks_to_generate - block_num - 1) / rate if rate > 0 else 0
                eta = datetime.timedelta(seconds=int(eta_seconds))

                # Calculate actual current height (start height + blocks generated so far)
                actual_height = self.rpc.call('getblockcount')
                print(f"  Height {actual_height}/{self.config.target_blocks} "
                      f"({rate:.1f} blocks/sec, ETA: {eta}) "
                      f"[Tx: {tx_successes}/{tx_attempts} successful]")

        print(f"✓ Generated {self.stats['blocks_generated']} blocks")
        print(f"✓ Transaction success rate: {tx_successes}/{tx_attempts} ({100*tx_successes/tx_attempts if tx_attempts > 0 else 0:.1f}%)")

    def _determine_tx_count(self) -> int:
        """Determine how many transactions for this block based on configured probabilities"""
        rand = random.random()

        if rand < self.config.tx_probability_none:
            return 0
        elif rand < self.config.tx_probability_low:
            return random.randint(1, 3)
        elif rand < self.config.tx_probability_medium:
            return random.randint(4, 10)
        else:
            return random.randint(11, 25)

    def _refund_wallet(self, wallet: dict):
        """Refund a wallet from the faucet when it runs low on funds"""
        wallet_name = wallet['wallet_name']
        tier = wallet['tier']

        if tier == 'light':
            refund_amount = 20.0
        elif tier == 'normal':
            refund_amount = 40.0
        else:
            refund_amount = 60.0

        # Send funds from faucet to a random address in this wallet
        addr = random.choice(wallet['addresses'])['address']

        try:
            self.rpc.call('sendtoaddress', addr, refund_amount, wallet=self.config.dashd_wallet)
            # Refund will be confirmed in next block generated by main loop
            self.stats['utxo_replenishments'] += 1
        except RPCError as e:
            print(f"      Warning: Failed to refund {wallet_name}: {e}")

    def _create_transaction(self):
        """Create a single transaction using manual UTXO selection"""
        source_wallet = random.choice(self.wallets)
        source_wallet_name = source_wallet['wallet_name']

        try:
            # Get balance to check if wallet needs refunding
            balance = self.rpc.call('getbalance', wallet=source_wallet_name)

            # Check if wallet needs refunding
            if balance < 10.0:
                self._refund_wallet(source_wallet)
                balance = self.rpc.call('getbalance', wallet=source_wallet_name)
                if balance < 1.0:  # Still too low after refund
                    return

            # Decide transaction type
            # More multi-output transactions to create varied UTXO sets
            if random.random() < 0.4:
                tx_type = 'multi_output'
                num_outputs = random.randint(2, 5)
            else:
                tx_type = 'simple'
                num_outputs = 1

            # Use sendtoaddress or sendmany which automatically calculates proper fees
            if tx_type == 'simple':
                dest_addr = random.choice(self.all_addresses)['address']
                # Send a reasonable amount (1-5 DASH or 10-50% of balance, whichever is smaller)
                max_amount = min(balance * random.uniform(0.1, 0.5), random.uniform(1, 5))
                amount = round(max_amount, 8)

                if amount < 0.01:
                    return

                # Use sendtoaddress which handles fees automatically
                self.rpc.call('sendtoaddress', dest_addr, amount, wallet=source_wallet_name)
            else:
                # Multi-output: use sendmany
                outputs = {}
                # Split 20-50% of balance across multiple outputs
                total_to_send = min(balance * random.uniform(0.2, 0.5), random.uniform(5, 15))
                amount_per_output = round(total_to_send / num_outputs, 8)

                if amount_per_output < 0.01:
                    return

                for _ in range(num_outputs):
                    dest_addr = random.choice(self.all_addresses)['address']
                    outputs[dest_addr] = amount_per_output

                # Use sendmany which handles fees automatically
                self.rpc.call('sendmany', "", json.dumps(outputs), wallet=source_wallet_name)

            self.stats['transactions_created'] += 1

        except (TransactionCreationError, RPCError, InsufficientFundsError):
            # Transaction failed (insufficient funds, fee issues, etc.) - this is expected
            pass
        except Exception as e:
            # Catch any other unexpected errors
            print(f"      Unexpected error in transaction creation: {type(e).__name__}: {e}")
            pass

    def _export_data(self):
        """Export blockchain data"""
        print("\nExporting blockchain data...")

        # Verify we reached target height
        final_height = self.rpc.call('getblockcount')
        if final_height != self.config.target_blocks:
            print(f"  Warning: Final height ({final_height}) differs from target ({self.config.target_blocks})")

        self.output_dir = Path(self.config.output_base) / f"regtest-{self.config.target_blocks}"

        # Clean output directory if it exists to avoid leftover files
        if self.output_dir.exists():
            print(f"  Removing existing output directory: {self.output_dir}")
            shutil.rmtree(self.output_dir)

        self.output_dir.mkdir(parents=True, exist_ok=True)

        # Collect wallet statistics first (while dashd is still running)
        self._collect_wallet_statistics()
        self._save_wallet_files()

        # Copy datadir before stopping dashd (while temp dir still exists)
        if self.dashd_manager:
            print("\n  Stopping dashd to copy blockchain data...")
            # Stop dashd process but don't cleanup temp dir yet
            if self.dashd_manager.process:
                try:
                    self.dashd_manager.process.terminate()
                    self.dashd_manager.process.wait(timeout=10)
                except Exception as e:
                    print(f"  Warning: Error stopping dashd: {e}")
                finally:
                    self.dashd_manager.process = None

            # Wait a moment for clean shutdown
            time.sleep(2)

        # Copy the entire dashd datadir for direct use in tests
        self._copy_dashd_datadir(self.output_dir)

        # Now cleanup temp dir if needed
        if self.dashd_manager and self.dashd_manager.temp_dir and self.dashd_manager.should_cleanup:
            print(f"\n  Cleaning up temporary directory: {self.dashd_manager.temp_dir}")
            try:
                shutil.rmtree(self.dashd_manager.temp_dir, ignore_errors=True)
            except Exception as e:
                print(f"  Warning: Could not remove temp directory: {e}")
            self.dashd_manager.temp_dir = None

        print(f"\n✓ Exported to {self.output_dir}")

    def _copy_dashd_datadir(self, output_dir: Path):
        """Copy dashd datadir for direct use in tests

        Structure will be:
        regtest-N/
          regtest/           # Blockchain data (what dashd expects when using -datadir=regtest-N)
            blocks/
            chainstate/
            default/         # Wallet directories
            light/
            normal/
            heavy/
          wallets/           # JSON wallet files for SPV client
            default.json
            light.json
            normal.json
            heavy.json
        """
        if not self.config.dashd_datadir:
            print("  ⚠ No dashd datadir to copy (not using auto-start)")
            return

        source_dir = Path(self.config.dashd_datadir)
        if not source_dir.exists():
            print(f"  ⚠ Source datadir does not exist: {source_dir}")
            return

        print(f"  Copying dashd datadir from {source_dir}...")

        regtest_source = source_dir / "regtest"
        if regtest_source.exists():
            print(f"    Copying regtest directory...")

            # Copy regtest/ directory to output_dir/regtest/ (preserve directory structure)
            regtest_dest = output_dir / "regtest"
            if regtest_dest.exists():
                shutil.rmtree(regtest_dest)

            shutil.copytree(regtest_source, regtest_dest, symlinks=False)

            total_size = sum(f.stat().st_size for f in regtest_dest.rglob('*') if f.is_file())
            size_mb = total_size / 1024 / 1024

            print(f"    ✓ Copied regtest data ({size_mb:.1f} MB)")

            expected_wallets = ['default', 'light', 'normal', 'heavy']
            found_wallets = []
            for wallet_name in expected_wallets:
                wallet_dir = regtest_dest / wallet_name
                if wallet_dir.exists() and wallet_dir.is_dir():
                    found_wallets.append(wallet_name)

            if found_wallets:
                print(f"    ✓ Wallet directories copied ({len(found_wallets)} wallets: {', '.join(found_wallets)})")
            else:
                print(f"    ⚠ No wallet directories found in regtest")
        else:
            print(f"  ⚠ No regtest directory found in {source_dir}")

    def _export_blocks_dat(self, output_dir: Path):
        """Export blocks in binary format (legacy format, kept for backward compatibility)"""
        print("  Exporting blocks.dat...")

        block_count = self.rpc.call('getblockcount')
        blocks_file = output_dir / "blocks.dat"

        with open(blocks_file, 'wb') as f:
            f.write(struct.pack('<I', block_count))

            for height in range(block_count):
                # Progress update every 50 blocks
                if height > 0 and height % 50 == 0:
                    progress_pct = (height * 100) // block_count
                    print(f"    Exporting blocks: {height}/{block_count} ({progress_pct}%)")

                block_hash = self.rpc.call('getblockhash', height)
                raw_hex = self.rpc.call('getblock', block_hash, 0)
                raw_bytes = bytes.fromhex(raw_hex)

                f.write(struct.pack('<I', height))
                f.write(struct.pack('<I', len(raw_bytes)))
                f.write(raw_bytes)

        size_mb = blocks_file.stat().st_size / 1024 / 1024
        print(f"    ✓ blocks.dat ({size_mb:.1f} MB)")


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Generate Dash regtest test data")
    parser.add_argument('--blocks', type=int, default=100,
                        help="Number of blocks to generate (default: 100)")
    parser.add_argument('--dashd-path', type=str,
                        help="Path to dashd executable (default: dashd in PATH)")
    parser.add_argument('--no-auto-start', action='store_true',
                        help="Disable automatic dashd startup")
    parser.add_argument('--rpc-port', type=int,
                        help="RPC port to use (default: auto-detect)")
    parser.add_argument('--keep-temp', action='store_true',
                        help="Keep temporary directory after completion")
    parser.add_argument('--tx-density', type=str, choices=['minimal', 'light', 'normal', 'heavy'], default='normal',
                        help="Transaction density per block (default: normal)")

    args = parser.parse_args()

    # Get script directory for resolving relative paths
    script_dir = Path(__file__).parent.resolve()

    # Determine dashd and dash-cli paths
    if args.dashd_path:
        dashd_executable = args.dashd_path
        dashd_dir = Path(args.dashd_path).parent
        dashcli_path = str(dashd_dir / 'dash-cli')
    else:
        dashd_executable = 'dashd'
        dashcli_path = 'dash-cli'

    # Resolve paths relative to script directory
    # Output test data to test-data directory
    output_base = str(script_dir.resolve())

    # Configure transaction density based on --tx-density argument
    tx_density_configs = {
        'minimal': {
            'tx_probability_none': 0.90,  # 90% empty blocks
            'tx_probability_low': 0.98,   # 8% with 1-3 transactions
            'tx_probability_medium': 1.0  # 2% with 4-10 transactions
        },
        'light': {
            'tx_probability_none': 0.60,  # 60% empty blocks
            'tx_probability_low': 0.90,   # 30% with 1-3 transactions
            'tx_probability_medium': 0.98 # 8% with 4-10 transactions, 2% with 11-25
        },
        'normal': {
            'tx_probability_none': 0.30,  # 30% empty blocks
            'tx_probability_low': 0.70,   # 40% with 1-3 transactions
            'tx_probability_medium': 0.90 # 20% with 4-10 transactions, 10% with 11-25
        },
        'heavy': {
            'tx_probability_none': 0.10,  # 10% empty blocks
            'tx_probability_low': 0.40,   # 30% with 1-3 transactions
            'tx_probability_medium': 0.70 # 30% with 4-10 transactions, 30% with 11-25
        }
    }

    density_config = tx_density_configs[args.tx_density]

    # Create config with defaults
    config = Config(
        target_blocks=args.blocks,
        batch_size=50,  # Not used currently
        min_utxo_threshold=150,
        target_utxo_count=200,
        dashcli_path=dashcli_path,
        dashd_executable=dashd_executable,
        auto_start_dashd=not args.no_auto_start,
        dashd_datadir=None,
        dashd_wallet='default',
        rpc_port=args.rpc_port,
        output_base=output_base,
        tx_probability_none=density_config['tx_probability_none'],
        tx_probability_low=density_config['tx_probability_low'],
        tx_probability_medium=density_config['tx_probability_medium']
    )

    try:
        generator = Generator(config, keep_temp=args.keep_temp)
        generator.generate()

    except ConfigError as e:
        print(f"ERROR: Configuration problem: {e}")
        sys.exit(1)
    except DashdConnectionError as e:
        print(f"ERROR: Cannot connect to dashd: {e}")
        sys.exit(2)
    except InsufficientFundsError as e:
        # This can happen during UTXO splitting or transaction creation and is handled gracefully
        print(f"ERROR: Insufficient funds: {e}")
        print("Note: This typically happens during aggressive UTXO splitting and is expected")
        sys.exit(3)
    except GeneratorError as e:
        print(f"ERROR: {e}")
        sys.exit(4)
    except KeyboardInterrupt:
        print("\nInterrupted by user")
        sys.exit(130)


if __name__ == '__main__':
    main()
