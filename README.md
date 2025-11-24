# regtest-blockchain

Pre-generated Dash regtest blockchain data for integration testing.

Contains blockchain state and wallet files with known addresses and transaction history, useful for testing wallet sync, transaction parsing, and block validation without running a live network.

## Quick Start

Download the latest release:

```bash
curl -LO https://github.com/xdustinface/regtest-blockchain/releases/latest/download/regtest-1000.tar.gz
tar -xzf regtest-1000.tar.gz
```

Start dashd with this data:

```bash
dashd -datadir=./regtest-1000 -regtest -daemon
```

## What's Included

```
regtest-1000/
├── dash.conf
├── regtest/          # blockchain data
│   ├── blocks/
│   ├── chainstate/
│   ├── default/      # wallet data
│   ├── light/
│   ├── normal/
│   └── heavy/
└── wallets/          # JSON wallet files with mnemonics & keys
    ├── default.json
    ├── light.json
    ├── normal.json
    └── heavy.json
```

The wallet JSON files contain HD mnemonics and derived addresses for verifying sync against the blockchain.

## Regenerating

Generate fresh test data with the included script:

```bash
# Generate 5000 blocks (outputs to data/regtest-5000/)
./generate.py --blocks 5000
```

The generator creates realistic transaction patterns across the wallet tiers - single outputs, multi-output transactions, dust amounts, and inter-wallet transfers. Each wallet maintains independent UTXOs with automatic refunding to ensure continuous activity.

Requires `dashd` and `dash-cli` in PATH (or specify with `--dashd-path`).
