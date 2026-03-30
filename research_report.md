# Ethereum validator/IP deanonymization and P2P topology research report

## Scope

This report covers two related questions:

1. **What is known about mapping Ethereum L1 validator / attestor identities to network endpoints?**
   - BLS validator pubkey -> peer -> IP
   - validator index -> pubkey -> peer/IP
   - timing analysis and first-forward / sender-observation approaches

2. **What is known about building a topology map of Ethereum’s P2P network?**
   - discovery (`discv5`, ENR, peer IDs, IPs, client identification)
   - gossip (`libp2p`, GossipSub, attestation subnets, aggregate propagation)
   - practical tools and prior measurement work

It also documents the passive tooling built in `/workspace/eth_topology/` and the live data collected in `/workspace/results/`.

---

## Executive summary

### Bottom line

- **Yes, Ethereum consensus validators can be deanonymized to IP-level infrastructure with passive observation under the right vantage.** The strongest public result is **Vonlanthen, Villacis, Kiffer, Wattenhofer, “Deanonymizing Ethereum Validators: The P2P Network Has a Privacy Issue”** (arXiv 2024, later 2025 version / USENIX-style revision). They show that by observing **attestation messages received from directly connected peers**, an adversary can infer which validators are hosted behind those peers. Using four vantage nodes over three days, they report locating **more than 15% of Ethereum validators**.
- The key idea is **not** generic “network timing” alone. It relies on a **specific interaction between Ethereum’s attestation subnet design and GossipSub propagation responsibilities**: if a peer sends an attestation that it was **not expected to relay as part of its assigned broadcast responsibility**, repeated observations strongly suggest that the validator producing that attestation is local to that peer.
- **Peer/IP mapping is straightforward once the peer is known**, because Ethereum discovery and ENRs are intentionally endpoint-carrying records. A consensus ENR normally exposes IP, UDP/TCP ports, a secp256k1 networking public key, fork data, and the `attnets` bitfield for subnet subscriptions.
- **Validator index -> pubkey** is easy via the Beacon API. The hard step is **pubkey -> peer/IP**.
- **Public Beacon APIs alone are insufficient for strong pubkey -> IP deanonymization**, because they expose attestation contents but not the **first-forwarding libp2p peer**.
- To get high-confidence validator/IP correlations **passively**, you need a **real P2P vantage point**: a listening / connected beacon node, or an instrumented listener such as **Armiarma** or **Hermes**, or a modified consensus client.
- **`discv5` crawls are useful but do not reveal the live GossipSub mesh.** They enumerate discoverable peers, ENRs, IPs, client-like metadata, and some advertised capabilities/subnets. They do **not** directly expose who is connected to whom in the topic meshes.

### What I built

I built a passive toolkit in `/workspace/eth_topology/`:

- `crawl_discv5.py` — crawls live Ethereum mainnet consensus discovery using **Nebula** as a subprocess and normalizes results to JSON/CSV
- `monitor_attestations.py` — monitors live attestations from a public Beacon API SSE feed, and optionally supports **Armiarma SSE** if available
- `correlate_validators.py` — expands attestation aggregates into validator indices/pubkeys and attempts validator->peer/IP correlation when sender metadata exists; otherwise produces **low-confidence subnet-overlap candidate sets**
- `run_all.sh` — orchestration wrapper
- `README.md` and `requirements.txt`

### What I was able to verify live

- I successfully ran a **live Ethereum consensus discovery crawl** with **Nebula** and wrote normalized results under `/workspace/results/discv5/`.
- I successfully monitored **live attestation events** from a public Beacon API SSE feed and expanded them to validator indices/pubkeys under `/workspace/results/attestations/`.
- I built and ran correlation logic under `/workspace/results/correlations/`.
- I also built **Armiarma** successfully, but in this environment it did not achieve useful live peer connectivity for deanonymization; discovery/gossip collection from that vantage was limited. This is important because it illustrates the core limitation: **without true P2P sender metadata, direct validator->IP mapping remains weak**.

---

## 1. Key result: validator deanonymization is real

## 1.1 Vonlanthen et al.: the most important paper for this question

The most directly relevant work is:

- **Yann Vonlanthen, Juan Villacis, Lucianna Kiffer, Roger Wattenhofer**
- **“Deanonymizing Ethereum Validators: The P2P Network Has a Privacy Issue”**
- arXiv preprint 2024; revised 2025 HTML/PDF versions publicly available
- Example public copies:
  - https://arxiv.org/html/2409.04366v2
  - https://dspace.networks.imdea.org/bitstream/handle/20.500.12761/1899/_USENIX25_Revision__Deanonymizing_Ethereum_Validators-4.pdf?sequence=1

### Their claim

They show that a passive node connected to Ethereum consensus peers can infer which validators are hosted on those peers by observing **attestation dissemination behavior**.

### Why the attack works

Ethereum consensus does **not** flood all attestations uniformly to all peers. Instead:

- unaggregated attestations are published on **attestation subnet topics** (`beacon_attestation_{subnet_id}`)
- aggregate attestations are later propagated globally on `beacon_aggregate_and_proof`
- nodes maintain **meshes** and **fanout sets** rather than relaying everything to everyone
- nodes are effectively responsible for disseminating only a subset of the message space

Vonlanthen et al. exploit the fact that if peer `p` repeatedly sends attestations from validator `v` that are **outside** `p`’s expected forwarding responsibility, that is evidence `p` is not merely relaying them opportunistically — it is likely the local source of those attestations.

### What they achieved

From the abstract and introduction:

- with **four nodes** over **three days**
- they located **more than 15% of Ethereum validators**
- they derived infrastructure concentration findings, including very large validator concentrations on single peers and cloud/hosting concentration
- they received an **Ethereum Foundation bug bounty**

### Why this matters

Once an attacker knows which validators sit behind which IPs or peers, they gain a target list for:

- DoS / DDoS against proposers or dense validator clusters
- targeted attacks around high-value slots / MEV opportunities
- geolocation and hosting-provider concentration analysis
- operator clustering across pools / infrastructure reuse

### Important nuance

This is **not** just generic internet timing analysis. The strongest published technique is specific to Ethereum’s **attestation subnet + broadcast responsibility design**.

---

## 1.2 What this means for the validator->IP chain

The deanonymization chain conceptually looks like:

1. **validator index -> BLS pubkey**
   - trivial via Beacon API / state queries
2. **observe attestation message containing validator participation**
   - via gossip or Beacon API feed
3. **infer likely source peer**
   - strongest if you are directly connected to the peer and observe first-forward behavior or non-backbone forwarding patterns
4. **peer / ENR / peer ID -> IP**
   - via ENR, discovery, or peer metadata

The hardest step is **(3)**. If you can do (3), step (4) is usually easy.

---

## 2. Ethereum networking architecture relevant to deanonymization

Ethereum post-Merge has **two distinct networking stacks**:

- **Execution layer (EL)**: devp2p / RLPx / discv4 historically, evolving toward discv5 in some contexts
- **Consensus layer (CL)**: libp2p + GossipSub + Req/Resp + discv5 + ENRs

For validator deanonymization, the **consensus layer** is the main target.

---

## 2.1 Discovery layer: discv5 and ENRs

### Main references

- Ethereum devp2p discv5 spec:
  - https://github.com/ethereum/devp2p/blob/master/discv5/discv5.md
  - https://github.com/ethereum/devp2p/blob/master/discv5/discv5-wire.md
  - https://github.com/ethereum/devp2p/blob/master/discv5/discv5-rationale.md
- ENR spec:
  - https://github.com/ethereum/devp2p/blob/master/enr.md
- Ethereum consensus networking spec:
  - https://ethereum.github.io/consensus-specs/specs/phase0/p2p-interface/
- Ethereum networking overview:
  - https://ethereum.org/developers/docs/networking-layer/

### What discv5 is

`discv5` is a UDP-based, Kademlia-like node discovery system that stores and exchanges **Ethereum Node Records (ENRs)**.

It supports:

- node discovery by node ID distance
- retrieval of signed node records
- topic advertisement / topic-oriented discovery concepts
- encrypted request/response messaging

### What an ENR contains

An ENR is a signed, versioned record containing a node’s network reachability information and arbitrary protocol-specific fields.

For Ethereum consensus nodes, relevant ENR fields include:

- `ip` and/or `ip6`
- `udp`, `tcp` (and in some contexts `tcp6`, `udp6`)
- secp256k1 networking public key / identity
- `eth2` field containing fork digest / next fork metadata
- `attnets` bitfield: attestation subnet subscriptions
- later forks add further fields like sync committee info or custody/data-column related fields

### Why ENR matters for deanonymization

Because ENR is an explicit peer advertisement:

- **peer/network identity -> IP is usually direct**
- once you know the peer or node identity behind a validator, the endpoint mapping often falls out of discovery data or live peer metadata

### Important limitation

Discovery tells you:

- who exists
- who is reachable
- what subnet/fork metadata they advertise

It does **not** tell you:

- exact current libp2p peer connections
- current GossipSub mesh neighbors
- which validator keys are attached to that node

So `discv5` is necessary for **peer/IP inventory**, but insufficient on its own for validator deanonymization.

---

## 2.2 Peer identity forms

For Ethereum consensus networking, the same node can be referred to in multiple ways:

- **Node ID** — discv5/DHT identity derived from the secp256k1 key
- **Peer ID** — libp2p identity representation
- **ENR** — signed record carrying endpoint and protocol metadata
- **multiaddr** — libp2p dial address format

In practice, topology tooling often wants to normalize among:

- `peer_id`
- `enr`
- `ip:port`
- `fork_digest`
- `agent/version`

That is exactly why tools like Nebula and Armiarma are useful: they do the normalization work.

---

## 3. Gossip layer: GossipSub, attestation subnets, and why they leak information

## 3.1 Consensus-layer gossip topics

The consensus spec defines topic families including:

- `beacon_block`
- `beacon_aggregate_and_proof`
- `voluntary_exit`
- `proposer_slashing`
- `attester_slashing`
- **attestation subnet topics**: `beacon_attestation_{subnet_id}`

Reference:
- https://ethereum.github.io/consensus-specs/specs/phase0/p2p-interface/

### Attestation subnet mechanism

Unaggregated attestations are sent to one of **64 attestation subnets**.

The subnet is computed by:

```python
SubnetID((committees_since_epoch_start + committee_index) % ATTESTATION_SUBNET_COUNT)
```

From the validator spec:
- `ATTESTATION_SUBNET_COUNT = 64`
- validators and nodes use the subnet corresponding to committee assignment

This is crucial because it partitions attestation traffic and creates **observable dissemination structure**.

---

## 3.2 GossipSub mesh parameters relevant to Ethereum

Ethereum’s consensus GossipSub profile uses parameters documented in the consensus networking spec. Important defaults include:

- `D` (target mesh degree): **8**
- `D_low`: **6**
- `D_high`: **12**
- `D_lazy`: **6**
- `heartbeat_interval`: **0.7 seconds**

Reference snippet from the consensus spec:
- `D = 8`, `D_low = 6`, `D_high = 12`, `D_lazy = 6`, `heartbeat_interval = 0.7`

These parameters matter because message propagation is not arbitrary; it is structured by topic meshes, gossip control traffic, mesh repair, and fanout behavior.

---

## 3.3 Mesh vs fanout vs flood publishing

Ethereum consensus clients use libp2p GossipSub. A node’s message dissemination behavior depends on whether:

- it is subscribed to the topic and has a **mesh**
- it is publishing to a topic without stable mesh membership and therefore uses a **fanout** set
- it is doing some form of **flood publish** to all topic peers, depending on implementation details and topic handling

This matters because deanonymization exploits often hinge on the difference between:

- messages a peer is expected to relay via mesh/fanout
- messages that appear to originate from the peer’s own local validator set

---

## 3.4 Why attestation subnets are privacy-sensitive

Attestations are unusually deanonymization-friendly because:

- each validator has predictable committee/subnet assignments
- subnet placement is deterministic from slot/committee information
- attestation traffic volume is high and repetitive
- a beacon node can host many validators, creating a dense signature pattern
- the same peer may repeatedly be first or anomalous source for many validators

This creates a much stronger statistical fingerprint than, say, rare block proposals alone.

---

## 4. Known correlation techniques

## 4.1 Strongest passive technique: direct peer observation of attestation dissemination

### Mechanism

Run one or more beacon nodes / listeners and connect to many peers. For each attestation received:

- record who sent it first
- record topic/subnet, slot, and arrival time
- compare that against expected forwarding behavior
- accumulate repeated evidence by validator / peer

### What it gives

Potentially:

- peer -> validator cluster mapping
- validator -> peer mapping
- then peer -> IP via ENR / identify metadata

### Confidence

- **High**, when done from real P2P vantage points and repeated over time
- strongest published evidence: **Vonlanthen et al.**

---

## 4.2 First-forward / earliest-arrival timing analysis

A weaker but common idea is:

- whichever directly connected peer first sends an attestation may be closer to its source
- repeated earliest-arrival observations may identify likely origin peers

### Caveats

- network jitter and local scheduling noise are significant
- mesh position influences first-arrival heavily
- first arrival does not imply origin
- without many repetitions and careful controls, this is noisy

This is useful as supporting evidence, but weaker than the broadcast-responsibility method in the deanonymization paper.

---

## 4.3 ENR / attnets-based candidate narrowing

If you observe an attestation on subnet `s`, and you have a live discv5 crawl, you can narrow to nodes that advertise subscription to subnet `s` in their `attnets` bitfield.

### What this gives

- a **candidate set** of peers that plausibly participate in that subnet

### What it does not give

- origin peer
- validator host certainty
- validator -> IP attribution

This is useful for **search-space reduction**, but not deanonymization by itself.

---

## 4.4 Validator index -> pubkey -> participant set expansion

Even if you only see an **aggregated attestation**, you can still recover the participating validator indices by:

1. fetching committee membership for `(slot, committee_index)` from Beacon API
2. interpreting `aggregation_bits`
3. mapping bit positions to validator indices
4. fetching pubkeys for those indices from Beacon API

This part is straightforward and I implemented it in `monitor_attestations.py`.

### Limitation

This only gives **which validators are represented in the attestation**, not which peer created or forwarded it first.

---

## 4.5 Multi-vantage correlation

A more powerful operator can run multiple geographically distributed observers. Benefits:

- more peers connected overall
- better first-arrival triangulation
- more robust evidence against local jitter
- greater validator coverage

This is consistent with both the deanonymization paper and general network measurement practice.

---

## 4.6 Direct execution/consensus host co-location inference

Once validator-hosting consensus peers are mapped to IPs, additional inferences are often possible:

- same IP hosting many validators
- same ASN / cloud provider hosting many peers
- likely co-located EL + CL stacks
- pool / operator clustering using external validator labels

Vonlanthen et al. explicitly discuss cloud and operator concentration findings of this type.

---

## 5. What is not enough on its own

## 5.1 Public Beacon API only

Public Beacon APIs expose:

- validator registry and pubkeys
- committees
- blocks and attestations
- SSE event feeds

But they generally do **not** expose:

- the remote peer that forwarded the attestation first
- peer IDs for gossip senders
- local mesh edges

So they enable **validator expansion**, but not strong validator->IP deanonymization.

---

## 5.2 Discovery crawl only

A pure `discv5` crawl gives:

- peer IDs / ENRs / IPs / ports
- some protocol/client/fork/subnet metadata

But it does not give:

- which validator keys live behind those peers
- who forwarded what first
- topic-level mesh edges

Discovery is inventory, not attribution.

---

## 6. Prior tools and practical ecosystems

## 6.1 Armiarma

### What it is

- GitHub: https://github.com/migalabs/armiarma
- Paper: **“Discovering the Ethereum2 P2P Network”** (Bautista-Gomez et al.)
  - https://ar5iv.labs.arxiv.org/html/2012.14728

Armiarma is a **libp2p-based open-network crawler** focused on Ethereum consensus.

### What it does well

- joins the CL P2P network
- crawls peers and metadata
- can subscribe to gossip topics and subnets
- can persist message metadata and peer observations
- has explicit support for validator pubkey tracking hooks (experimental)
- is much closer to a deanonymization-capable vantage than a discovery-only crawler

### Why it matters here

Armiarma is the most natural open-source base if the goal is:

- real gossip observation
- sender peer metadata
- attestation arrival timing
- eventual validator->peer correlation

### My use of it

I cloned and built Armiarma successfully in this workspace. The binary ran, but in this environment it did not produce useful live peer connectivity for deanonymization. That itself is instructive: this style of research depends heavily on having a **routable, useful P2P vantage point**, not just code.

---

## 6.2 Nebula

### What it is

- GitHub: https://github.com/dennis-tra/nebula
- Ethresear.ch announcement: “Nebula - A novel discv5 DHT crawler”

Nebula is a **network-agnostic crawler** that supports Ethereum consensus (`discv5`) and execution (`discv4`) among many other networks.

### What it does well

- discovery-layer crawling at scale
- exports JSON/Postgres/ClickHouse
- captures ENRs, IPs, peer IDs, protocols, agent strings where available
- excellent for topology inventory and monitoring

### What it does not do in the open-source edition

- full live GossipSub mesh reconstruction
- full validator deanonymization
- detailed topic tracing beyond the crawl/monitor surface

### My use of it

I used Nebula for the live discovery crawl in this job. It worked reliably and produced normalized data under `/workspace/results/discv5/`.

---

## 6.3 Hermes

### What it is

- GitHub: https://github.com/probe-lab/hermes

Hermes is a **GossipSub listener and tracer**. For Ethereum it is designed to discover/connect to network participants, subscribe to relevant topics, and trace protocol interactions such as:

- GRAFT
- PRUNE
- RPC exchanges
- topic activity

### Why it matters

Hermes is closer than Nebula to the kind of vantage required for:

- observing topic-level behavior
- tracing mesh dynamics
- studying dissemination paths
- possibly supporting first-forward or topic-control analysis

In short:

- **Nebula** is discovery-centric
- **Hermes** is gossip-observation-centric
- **Armiarma** spans discovery + gossip + measurement in a more Ethereum-specialized way

---

## 6.4 Xatu / ethPandaOps

### What it is

- GitHub: https://github.com/ethpandaops/xatu

Xatu is an Ethereum monitoring system with multiple collection modes, including:

- `sentry` — Beacon API collection next to a local consensus client
- `discovery` — discv4/discv5 discovery observation
- `mimicry` — execution-layer P2P collection
- server-side data pipeline and publishing via `xatu-data`

### Why it matters

Xatu is not primarily a deanonymization tool, but it is very relevant operationally because it demonstrates how Ethereum network telemetry can be collected at scale from:

- discovery
- Beacon API
- client-side instrumentation

For passive research, it provides a real-world example of how to collect and publish ecosystem-scale data.

---

## 6.5 Ethereum execution node-crawler / devp2p crawl

For execution-layer topology and endpoint inventories, relevant tools include:

- geth `devp2p crawl`
- Ethereum `node-crawler`: https://github.com/ethereum/node-crawler

These are mainly EL-focused, but they are conceptually important because they show how crawlers work in Ethereum generally:

- crawl discovery network
- establish sessions
- validate peers
- store metadata over time

---

## 7. Academic and prior measurement literature

Below are the most relevant works I found for this problem space.

## 7.1 Deanonymization / validator privacy

### 1) Deanonymizing Ethereum Validators: The P2P Network Has a Privacy Issue
- **Vonlanthen, Villacis, Kiffer, Wattenhofer**
- Main result: passive deanonymization of validators from attestation dissemination behavior
- Strongest known public paper directly addressing BLS pubkey / validator -> IP
- URL: https://arxiv.org/html/2409.04366v2

This is the central paper for the user’s question.

---

## 7.2 Ethereum consensus topology measurement

### 2) Discovering the Ethereum2 P2P Network
- **Leonardo Bautista-Gomez et al.**
- Introduces **Armiarma** and performs one of the first detailed Ethereum consensus network analyses
- Focus: topology, client distribution, geography, hazards
- URL: https://ar5iv.labs.arxiv.org/html/2012.14728

This is foundational for the topology/tooling side.

---

## 7.3 Ethereum gossip / propagation analysis

### 3) Under the Hood of the Ethereum Gossip Protocol
- **Lucianna Kiffer, Asad Salman, Dave Levin, Alan Mislove, Cristina Nita-Rotaru**
- FC 2021
- Focused on Ethereum gossip / connectivity / propagation behavior
- Public landing pages:
  - Springer: https://link.springer.com/chapter/10.1007/978-3-662-64331-0_23
  - PDF mirror reference surfaced via Northeastern CCS page

This is more EL-leaning historically, but highly relevant for propagation methodology and network reasoning.

---

## 7.4 Ethereum topology / decentralization measurement

### 4) Decentralization in Bitcoin and Ethereum Networks
- **Gencer et al.**
- FC 2018
- Canonical measurement work on node distribution and decentralization
- Link referenced in Springer references and widely cited

### 5) Topology Measurement and Analysis on Ethereum P2P Network
- **Gao et al.**
- IEEE ISCC 2019
- Early topology-focused Ethereum measurement work

These are important background for network inventory and concentration analysis.

---

## 7.5 Eclipse-attack literature

### 6) Low-Resource Eclipse Attacks on Ethereum’s Peer-to-Peer Network
- IACR ePrint 2018/236
- Focused on eclipse attacks against Ethereum’s P2P network
- Useful background on peer management and attack surfaces

### 7) Eclipse Attacks on Ethereum’s Peer-to-Peer Network
- 2026 paper / preprint surfaced in search results
- Post-Merge / modernized perspective, including discovery poisoning and bootstrapping considerations
- Not necessary to rely on for current tooling, but relevant to risk analysis

These works are useful because deanonymization and topology mapping are often stepping stones toward eclipse, partition, or DoS attacks.

---

## 7.6 Chain/client diversity and network health

### 8) Unveiling Ethereum’s P2P Network: The Role of Chain and Client Diversity
- 2025 preprint surfaced in search results
- Focus: discovery inefficiency, chain/client diversity, low-level P2P message analysis
- Relevant especially for interpreting noisy crawl data, because Ethereum discovery can contain non-mainnet or incompatible peers

---

## 7.7 GossipSub itself

### 9) GossipSub: Attack-Resilient Message Propagation in the Filecoin and Eth2.0 Networks
- Protocol Labs / Research
- Reference for why Ethereum consensus uses GossipSub and what attack-resilience assumptions it makes

### 10) The Hitchhiker’s Guide to P2P Overlays in Ethereum Consensus
- HackMD by Louis Thibault and Dan Marzec
- Not an academic paper, but one of the best practical explainers of Ethereum consensus overlay structure
- Link: https://hackmd.io/@dmarz/ethereum_overlays

These sources are valuable for understanding why exact mesh reconstruction is difficult and why topic behavior matters so much.

---

## 8. Practical correlation chain: from validator to IP

## 8.1 Validator index -> BLS pubkey

This is easy via Beacon API:

- `GET /eth/v1/beacon/states/{state_id}/validators/{validator_id}`
- `GET /eth/v1/beacon/states/{state_id}/validators?id=...`

Public endpoints like Lodestar / PublicNode expose this.

### Confidence
- **High**
- canonical chain data

---

## 8.2 Attestation -> participant validator indices

Given an attestation:

- use its `slot`
- derive or read committee index / committee bits
- fetch committee membership for that slot
- decode `aggregation_bits`
- map bit positions to validator indices

### Confidence
- **High**, if committee interpretation is correct
- one caveat: some API event formats may surface very large aggregate forms that span multiple committees, so tooling must handle both simple and large aggregates carefully

---

## 8.3 Participant validator(s) -> likely source peer

This is the difficult step.

### Strong methods

- direct connected-peer observation
- repeated first-forward measurements
- Vonlanthen-style non-responsibility forwarding detection
- topic tracer / local client instrumentation

### Weak methods

- subnet overlap with ENR `attnets`
- rough timing from public APIs
- geolocation heuristics alone

### Confidence
- **Medium to high** only with local P2P observation
- **Low** with discovery + public APIs only

---

## 8.4 Peer -> IP

Once the peer is identified, IP mapping is often available via:

- ENR `ip`/`ip6`
- live identify / peer metadata
- Nebula / Armiarma records

### Confidence
- **High**, subject to normal caveats (NATs, proxies, relays, rotating addresses, fronting)

---

## 9. What I built in `/workspace/eth_topology/`

## 9.1 `crawl_discv5.py`

Purpose:
- crawl live Ethereum mainnet consensus discovery passively
- normalize peer inventory to structured outputs

Design:
- uses **Nebula** as a subprocess rather than implementing discv5 from scratch in Python
- parses Nebula output into:
  - `crawl_summary.json`
  - `nodes.json`
  - `nodes.csv`
  - `raw/` files

Normalized fields include:
- `peer_id`
- `ip`, `tcp`, `udp`
- `dialable`
- `agent_version`
- `protocols`
- `enr`
- inferred `node_kind` (`consensus`, `execution`, `opstack`, `unknown`)
- `fork_digest`
- `attnets` and decoded `subscribed_subnets`

### Live result

I ran it successfully. One live run produced approximately:

- **50 peers crawled**
- **23 dialable**
- inferred mix roughly:
  - **15 consensus**
  - **17 execution**
  - **10 opstack**
  - **8 unknown**

This itself is a useful empirical reminder that Ethereum discovery is **shared / noisy / heterogeneous**.

---

## 9.2 `monitor_attestations.py`

Purpose:
- observe live attestation messages passively
- expand aggregate attestations to validator indices and pubkeys
- optionally use Armiarma SSE if a real P2P vantage is available

Modes:
- `beacon-api` (default): uses public Beacon API SSE
- `armiarma-sse`: consumes local Armiarma event stream if available

Outputs:
- `attestation_events_raw.jsonl`
- `attestation_events_expanded.jsonl`
- `attestation_summary.json`

What it computes:
- slot
- block root / checkpoints
- committee indices
- committees per slot
- derived subnet IDs
- participating validator indices
- optional participating pubkeys
- source metadata if provided by Armiarma

### Important limitation

In `beacon-api` mode, source peer ID and IP are **unknown**. So this mode is excellent for:

- validator set expansion
- subnet inference
- attestation content analysis

but insufficient for strong validator->IP mapping.

---

## 9.3 `correlate_validators.py`

Purpose:
- join discovery-layer crawl output with attestation observations
- produce either:
  - **direct correlations**, if peer metadata exists
  - **heuristic candidate sets**, if only subnet overlap is available

Outputs:
- `direct_validator_ip_correlations.json`
- `direct_validator_ip_correlations.csv`
- `heuristic_candidate_events.json`
- `correlation_summary.json`

Correlation modes:

### A. Direct observed sender
If the attestation event includes:
- `peer_id`
- `peer_ip`
- optional user-agent / port / P2P message ID

then the script records a **medium-confidence direct observed sender** mapping.

### B. Heuristic subnet overlap only
If no peer metadata exists, the script:
- takes the attestation subnet(s)
- finds crawled consensus peers advertising those subnets in `attnets`
- outputs candidate peer sets

This is explicitly labeled **low confidence** and **not deanonymization**.

---

## 9.4 `run_all.sh`

Simple wrapper that runs:
1. discovery crawl
2. attestation monitoring
3. correlation

---

## 9.5 `README.md`

Documents:
- purpose of each script
- what is feasible vs not feasible passively
- how to run the toolkit
- output layout
- ethics / limitations

---

## 10. Live data collected in this workspace

## 10.1 Discovery results

Stored under:
- `/workspace/results/discv5/`

Key files:
- `crawl_summary.json`
- `nodes.json`
- `nodes.csv`
- `raw/`

Notable observations from the live crawl:
- a mixed peer set of consensus, execution, and OP Stack nodes
- multiple recognizable Ethereum client fingerprints (Teku, Lighthouse, Lodestar, Prysm, SSV, etc.)
- advertised `attnets` and fork metadata on consensus nodes

---

## 10.2 Attestation monitoring results

Stored under:
- `/workspace/results/attestations/`

Key files:
- `attestation_events_raw.jsonl`
- `attestation_events_expanded.jsonl`
- `attestation_summary.json`

What I verified:
- public Beacon API SSE provides real-time attestation payloads
- those payloads can be expanded into validator indices and pubkeys using committee lookups
- some events are simple committee-specific aggregates; some public feeds may also emit very large aggregate forms spanning many committees, which requires careful handling in downstream tooling

---

## 10.3 Correlation results

Stored under:
- `/workspace/results/correlations/`

Key files:
- `direct_validator_ip_correlations.json`
- `direct_validator_ip_correlations.csv`
- `heuristic_candidate_events.json`
- `correlation_summary.json`

What I observed:
- with public Beacon API input only, **direct correlations are empty or near-empty by design**, because no sender peer is exposed
- heuristic subnet-overlap candidate sets can still be produced, but these are **not reliable validator->IP attributions**

This matches the core research conclusion: **the missing ingredient is a true P2P sender vantage**.

---

## 11. What worked, what didn’t, and why

## 11.1 What worked well

- Nebula-based discv5 crawl
- public Beacon API committee/validator lookups
- public Beacon API SSE subscription for attestation events
- expansion from attestation aggregates to validator indices/pubkeys
- heuristic correlation framework

## 11.2 What did not fully work in this environment

I built and ran **Armiarma**, but the live environment did not yield useful active peer/gossip observations for deanonymization.

Why that matters:
- the core deanonymization step requires observing **who forwarded which attestation**
- discovery-only and Beacon-API-only vantage points cannot provide that
- this is not a flaw in the research question; it is an inherent requirement of the methodology

## 11.3 Practical implication

If the goal is real validator deanonymization research rather than just topology inventory, the next step is not more Beacon API querying — it is a **better consensus P2P vantage point**, ideally:

- routable public IP
- stable peer connectivity
- instrumentation of inbound gossip sender metadata
- long-lived measurement across many slots/epochs
- multi-vantage deployment if possible

---

## 12. Practical advice for future work

## 12.1 If you want topology only

Use:
- **Nebula** for discovery inventory and peer metadata
- optionally **Xatu discovery** for operational telemetry

This is enough for:
- ENR/IP inventories
- client mix
- fork diversity
- subnet advertisement inventories
- general network health views

## 12.2 If you want gossip / mesh insights

Use:
- **Hermes** for GossipSub listening/tracing
- **Armiarma** for Ethereum-specific libp2p/gossip measurements
- instrumented local beacon clients if you control them

This is where mesh dynamics become visible.

## 12.3 If you want validator->IP deanonymization

You need:
- a real P2P vantage with connected peers
- per-message sender metadata
- repeated observation across many slots
- robust committee expansion logic
- peer/IP normalization from ENRs / identify metadata
- ideally multi-vantage deployment

Vonlanthen et al. strongly suggests this is feasible and impactful.

---

## 13. Security and ethics implications

This area is security-sensitive.

### Main risks of successful deanonymization

- targeted DoS of proposers / validator clusters
- pre-slot targeting around MEV
- infrastructure concentration mapping
- pool/operator co-location inference
- reduced practical anonymity for validators

### Why passive work is still sensitive

Even passive measurement can produce actionable target lists if published carelessly.

### How the tooling built here stays conservative

- no packet injection attacks
- no eclipse, spoofing, or disruption
- only passive observation / public data
- direct correlation is only attempted where sender metadata exists
- otherwise the tool emits **heuristic candidate sets with explicit low-confidence labels**

---

## 14. Final conclusions

1. **The literature now clearly supports the claim that Ethereum consensus validators can be deanonymized to infrastructure endpoints under passive observation from the right vantage.**
2. The most important public result is **Vonlanthen et al.**, which shows a concrete passive deanonymization method using attestation dissemination behavior.
3. **`discv5` / ENR crawling is necessary but not sufficient.** It gives peer/IP inventory, not validator attribution.
4. **Beacon API data is necessary but not sufficient.** It gives validator registry and attestation contents, not sender peers.
5. The key missing link for strong pubkey->IP attribution is **P2P sender observation**.
6. Among existing tools:
   - **Nebula** is best for passive discovery inventory
   - **Armiarma** is the most promising open-source base for Ethereum-specific gossip measurement and potential validator correlation
   - **Hermes** is strong for topic/gossip tracing
   - **Xatu** is strong for operational telemetry pipelines
7. The Python toolkit built in `/workspace/eth_topology/` is a useful passive prototype that:
   - crawls discovery
   - monitors attestations
   - expands validator participation
   - performs direct or heuristic correlation depending on vantage quality
8. To turn this into a high-confidence validator deanonymization system, the next step is **not** more chain API work; it is **better live consensus P2P instrumentation**.

---

## Built artifacts written to disk

### Report
- `/workspace/research_report.md`

### Tool directory
- `/workspace/eth_topology/README.md`
- `/workspace/eth_topology/requirements.txt`
- `/workspace/eth_topology/crawl_discv5.py`
- `/workspace/eth_topology/monitor_attestations.py`
- `/workspace/eth_topology/correlate_validators.py`
- `/workspace/eth_topology/run_all.sh`

### Live results
- `/workspace/results/discv5/`
- `/workspace/results/attestations/`
- `/workspace/results/correlations/`

---

## Reference links

### Core specs and docs
- Ethereum devp2p discovery overview: https://github.com/ethereum/devp2p/wiki/Discovery-Overview
- discv5 spec: https://github.com/ethereum/devp2p/blob/master/discv5/discv5.md
- discv5 wire spec: https://github.com/ethereum/devp2p/blob/master/discv5/discv5-wire.md
- discv5 rationale: https://github.com/ethereum/devp2p/blob/master/discv5/discv5-rationale.md
- ENR spec: https://github.com/ethereum/devp2p/blob/master/enr.md
- Consensus networking spec: https://ethereum.github.io/consensus-specs/specs/phase0/p2p-interface/
- Validator spec: https://ethereum.github.io/consensus-specs/specs/phase0/validator/
- Ethereum networking layer overview: https://ethereum.org/developers/docs/networking-layer/

### Key papers / writeups
- Deanonymizing Ethereum Validators: https://arxiv.org/html/2409.04366v2
- Armiarma / Discovering the Ethereum2 P2P Network: https://ar5iv.labs.arxiv.org/html/2012.14728
- Under the Hood of the Ethereum Gossip Protocol: https://link.springer.com/chapter/10.1007/978-3-662-64331-0_23
- Low-Resource Eclipse Attacks on Ethereum’s Peer-to-Peer Network: https://eprint.iacr.org/2018/236.pdf
- The Hitchhiker’s Guide to P2P Overlays in Ethereum Consensus: https://hackmd.io/@dmarz/ethereum_overlays

### Tooling
- Armiarma: https://github.com/migalabs/armiarma
- Nebula: https://github.com/dennis-tra/nebula
- Hermes: https://github.com/probe-lab/hermes
- Xatu: https://github.com/ethpandaops/xatu
- Ethereum node-crawler: https://github.com/ethereum/node-crawler
