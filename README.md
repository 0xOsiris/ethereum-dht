⚠️ This was 95% written by Agents as a research experiment. May contain innacurate information and bugs :warning

# Ethereum L1 DHT: Validator Deanonymization & P2P Topology Research

Passive toolkit for mapping Ethereum L1 consensus validators to network endpoints and building P2P topology maps.

## What This Does

1. **discv5 Crawling** — Enumerates Ethereum consensus peers, ENRs, IPs, and client metadata using [Nebula](https://github.com/dennis-tra/nebula)
2. **Attestation Monitoring** — Captures live attestation events from Beacon API SSE feeds and expands aggregates to validator indices/pubkeys
3. **Validator-IP Correlation** — Attempts validator→peer→IP mapping using attestation subnet forwarding heuristics (based on [Vonlanthen et al. 2024](https://arxiv.org/abs/2409.04366))

## Key Finding

Ethereum validators **can** be deanonymized to IP-level infrastructure via passive observation. The technique exploits attestation subnet design: if a peer repeatedly forwards attestations outside its expected broadcast responsibility, it is likely the local source. With 4 nodes over 3 days, prior work located >15% of validators.

## Structure

```
armiarma/          # Ethereum P2P gossip crawler (Go, libp2p/GossipSub)
nebula/            # discv5/discv4/libp2p DHT crawler (Go)
results/
  discv5/          # Crawl outputs (nodes, ENRs, IPs, client versions)
  attestations/    # Expanded attestation events with validator indices
  correlations/    # Validator→IP correlation attempts and candidates
  nebula/          # Raw Nebula crawl data
research_report.md # Full writeup of methodology, findings, and literature
```

## Quick Start

```bash
# Build tools
cd armiarma && make build
cd nebula && go build -o nebula ./cmd/nebula

# Run a discv5 crawl
./nebula/nebula crawl --network ETHEREUM_CONSENSUS

# Monitor attestations (requires Beacon API endpoint)
# See research_report.md for full instructions
```

## Limitations

- **Without true P2P sender metadata, direct validator→IP mapping is weak.** Public Beacon APIs expose attestation contents but not first-forwarding peers.
- A real P2P vantage point (connected beacon node or instrumented listener) is required for high-confidence correlations.
- discv5 crawls enumerate peers but do not reveal live GossipSub mesh topology.

## References

- Vonlanthen et al., *"Deanonymizing Ethereum Validators"* ([arXiv](https://arxiv.org/abs/2409.04366))
- [Nebula](https://github.com/dennis-tra/nebula) — P2P network crawler
- [Armiarma](https://github.com/migalabs/armiarma) — Ethereum gossip monitor
