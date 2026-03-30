# eth_topology

Passive Ethereum mainnet topology and validator-correlation toolkit.

## What this toolkit does

This directory contains a small Python 3.10+ toolkit that combines:

1. **Discovery-layer crawling** of the Ethereum consensus `discv5` network
2. **Attestation monitoring** from a public Beacon API SSE stream
3. **Validator expansion/correlation logic** that maps aggregated attestations to validator indices and BLS pubkeys, and then attempts to correlate them to crawled peers

## Files

- `crawl_discv5.py` — runs a passive `discv5` crawl using [Nebula](https://github.com/dennis-tra/nebula) as a subprocess, then normalizes the results to JSON/CSV
- `monitor_attestations.py` — subscribes to attestation events from a public Beacon API SSE endpoint; optionally supports Armiarma SSE if you have a local P2P vantage point
- `correlate_validators.py` — expands attestation committee membership to validator indices/pubkeys and attempts peer/IP correlation
- `run_all.sh` — orchestrates the full workflow
- `requirements.txt` — no third-party Python dependencies required

## Why it is built this way

A direct, pure-Python implementation of Ethereum consensus networking is possible in theory, but the most battle-tested public crawlers today are in Go. The task allowed subprocess integration with tools like **Armiarma** and **Nebula**, so this toolkit uses:

- **Nebula** for live `discv5` crawling
- **Beacon API** for passive attestation observation
- optional **Armiarma SSE** for stronger validator->peer/IP correlation when available

## What is actually possible passively

### High-confidence, passive capabilities

- `peer_id` / ENR / IP / TCP / UDP / agent/version discovery from the live `discv5` layer
- extracting consensus-specific ENR fields such as fork digest and `attnets`
- mapping aggregated attestations to:
  - committee indices
  - validator indices
  - validator BLS pubkeys
  using public Beacon API data

### Medium-confidence capabilities

- validator -> peer/IP correlation **when you have a P2P vantage point that records sender metadata**, such as:
  - Armiarma SSE
  - a modified local beacon node
  - custom libp2p instrumentation

### Low-confidence / heuristic-only capabilities

- candidate peer narrowing via `attnets` subnet overlap between crawled ENRs and observed attestation subnet(s)
- this is **not** a deanonymization result by itself

## Important limitations

1. **Discovery is not the gossip mesh**
   - `discv5` tells you who is discoverable and what they advertise
   - it does **not** reveal the exact live GossipSub mesh edges for a topic

2. **Public Beacon API SSE does not expose first-forwarding peers**
   - it gives you attestation contents and the node-local arrival time
   - it does **not** tell you which libp2p peer first forwarded the message

3. **Direct validator -> IP mapping needs P2P sender metadata**
   - without that, correlation stops at validator membership and low-confidence candidate sets

4. **Ethereum discovery is a shared DHT**
   - crawls may include non-mainnet-consensus nodes (execution, OP Stack, other chains, or mixed deployments)

## Usage

### Run the whole pipeline

```bash
bash /workspace/eth_topology/run_all.sh
```

### Run the crawler only

```bash
python3 /workspace/eth_topology/crawl_discv5.py --limit 50
```

### Monitor attestations from a public Beacon API

```bash
python3 /workspace/eth_topology/monitor_attestations.py \
  --beacon-api https://lodestar-mainnet.chainsafe.io \
  --duration 10 \
  --max-events 12
```

### Use Armiarma SSE instead of Beacon API SSE

If you are running a local Armiarma instance that publishes SSE attestation events:

```bash
python3 /workspace/eth_topology/monitor_attestations.py \
  --mode armiarma-sse \
  --armiarma-sse-url http://127.0.0.1:9099/events
```

### Run correlation

```bash
python3 /workspace/eth_topology/correlate_validators.py \
  --crawl-nodes /workspace/results/discv5/nodes.json \
  --attestations /workspace/results/attestations/attestation_events_expanded.jsonl
```

## Outputs

The scripts write under `/workspace/results/` by default:

- `/workspace/results/discv5/`
  - `crawl_summary.json`
  - `nodes.json`
  - `nodes.csv`
  - `raw/` Nebula raw output
- `/workspace/results/attestations/`
  - `attestation_events_raw.jsonl`
  - `attestation_events_expanded.jsonl`
  - `attestation_summary.json`
- `/workspace/results/correlations/`
  - `direct_validator_ip_correlations.json`
  - `direct_validator_ip_correlations.csv`
  - `heuristic_candidate_events.json`
  - `correlation_summary.json`

## Live results collected in this workspace

This workspace includes a successful live `discv5` crawl using Nebula and passive attestation captures from a public Beacon API SSE stream. In this environment, the discovery crawl worked well, but direct validator->IP correlation remained limited because the available attestation feed did not include the first-forwarding libp2p peer.

## Research basis

This toolkit was built against the research summarized in `/workspace/research_report.md`, especially:

- `discv5` and ENR specs
- Ethereum consensus networking spec
- Nebula and Armiarma prior work
- ETH Zurich / IMDEA validator deanonymization work
- prior Ethereum topology and eclipse-attack literature

## Ethics

This toolkit is intentionally **passive/read-only**:

- no packet injection attacks
- no eclipse or DoS behavior
- no disruption of network participants
- only public network data and public Beacon API endpoints
