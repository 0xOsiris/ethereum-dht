#!/usr/bin/env python3
"""
Validator -> peer/IP correlation logic.

This script consumes:
- normalized discv5 crawl output from crawl_discv5.py
- expanded attestation observations from monitor_attestations.py

It produces two classes of output:
1. direct correlations
   - requires attestation observations that include peer metadata
     (e.g. Armiarma SSE / instrumented local beacon node)
2. heuristic candidate sets
   - when only public Beacon API attestation data is available, map each
     attestation's derived subnet(s) to crawled peers that advertise those
     attestation subnet subscriptions in ENR `attnets`
   - this is *not* a deanonymization result; it is only a low-confidence
     candidate set useful for narrowing investigation.
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

WORKSPACE = Path("/workspace")
DEFAULT_CRAWL = WORKSPACE / "results" / "discv5" / "nodes.json"
DEFAULT_ATTEST = WORKSPACE / "results" / "attestations" / "attestation_events_expanded.jsonl"
DEFAULT_OUTPUT = WORKSPACE / "results" / "correlations"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Correlate validator pubkeys to crawled peers/IPs")
    p.add_argument("--crawl-nodes", type=Path, default=DEFAULT_CRAWL)
    p.add_argument("--attestations", type=Path, default=DEFAULT_ATTEST)
    p.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    p.add_argument("--max-candidate-peers", type=int, default=20)
    return p.parse_args()


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def select_consensus_nodes(nodes: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    result = []
    for node in nodes:
        if node.get("node_kind") == "consensus" or node.get("attnets"):
            result.append(node)
    return result


def write_csv(path: Path, rows: List[Dict[str, Any]], fieldnames: List[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in fieldnames})


def main() -> int:
    args = parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    nodes = load_json(args.crawl_nodes)
    attestations = load_jsonl(args.attestations)
    consensus_nodes = select_consensus_nodes(nodes)
    nodes_by_peer_id = {n.get("peer_id"): n for n in nodes if n.get("peer_id")}

    direct_rows: List[Dict[str, Any]] = []
    heuristic_rows: List[Dict[str, Any]] = []
    validator_summary: Dict[str, Dict[str, Any]] = {}

    for att in attestations:
        source = att.get("source", {})
        validator_indices = att.get("participant_validator_indices", [])
        pubkeys = att.get("participant_pubkeys", [])
        slot = att.get("slot")
        subnet_ids = att.get("subnet_ids", [])

        peer_id = source.get("peer_id")
        peer_ip = source.get("peer_ip")
        if peer_id or peer_ip:
            crawled_node = nodes_by_peer_id.get(peer_id, {})
            for pos, validator_index in enumerate(validator_indices):
                pubkey = pubkeys[pos] if pos < len(pubkeys) else ""
                row = {
                    "validator_index": validator_index,
                    "validator_pubkey": pubkey,
                    "slot": slot,
                    "peer_id": peer_id,
                    "peer_ip": peer_ip,
                    "peer_user_agent": source.get("peer_user_agent", ""),
                    "peer_port": source.get("peer_port", ""),
                    "p2p_msg_id": source.get("p2p_msg_id", ""),
                    "method": "direct_observed_sender",
                    "confidence": "medium",
                    "crawled_node_kind": crawled_node.get("node_kind", ""),
                    "crawled_fork_digest": crawled_node.get("fork_digest", ""),
                    "observed_at": att.get("observed_at", ""),
                }
                direct_rows.append(row)
                key = pubkey or str(validator_index)
                summary = validator_summary.setdefault(key, {
                    "validator_index": validator_index,
                    "validator_pubkey": pubkey,
                    "direct_peer_ids": Counter(),
                    "direct_peer_ips": Counter(),
                    "direct_observations": 0,
                })
                summary["direct_peer_ids"][peer_id] += 1
                summary["direct_peer_ips"][peer_ip] += 1
                summary["direct_observations"] += 1
        else:
            candidate_peers = []
            subnet_set = set(subnet_ids)
            if subnet_set:
                for node in consensus_nodes:
                    advertised = set(node.get("subscribed_subnets", []))
                    overlap = sorted(subnet_set & advertised)
                    if overlap:
                        candidate_peers.append({
                            "peer_id": node.get("peer_id", ""),
                            "ip": node.get("ip", ""),
                            "agent_version": node.get("agent_version", ""),
                            "fork_digest": node.get("fork_digest", ""),
                            "matched_subnets": overlap,
                        })
            candidate_peers = sorted(candidate_peers, key=lambda x: (-len(x["matched_subnets"]), x["peer_id"]))
            heuristic_rows.append({
                "slot": slot,
                "subnet_ids": subnet_ids,
                "participant_validator_indices": validator_indices,
                "participant_pubkeys": pubkeys,
                "candidate_peer_count": len(candidate_peers),
                "candidate_peers": candidate_peers[: args.max_candidate_peers],
                "method": "heuristic_subnet_overlap_only",
                "confidence": "low",
                "note": "This is not a validator->IP mapping; it is only a candidate set of crawled peers advertising the relevant attestation subnet(s).",
                "observed_at": att.get("observed_at", ""),
            })
            for pos, validator_index in enumerate(validator_indices):
                pubkey = pubkeys[pos] if pos < len(pubkeys) else ""
                key = pubkey or str(validator_index)
                summary = validator_summary.setdefault(key, {
                    "validator_index": validator_index,
                    "validator_pubkey": pubkey,
                    "direct_peer_ids": Counter(),
                    "direct_peer_ips": Counter(),
                    "direct_observations": 0,
                })
                summary.setdefault("heuristic_slots", set()).add(slot)
                summary.setdefault("heuristic_subnets", set()).update(subnet_ids)
                summary["heuristic_candidate_observations"] = summary.get("heuristic_candidate_observations", 0) + 1

    direct_json = args.output_dir / "direct_validator_ip_correlations.json"
    heuristic_json = args.output_dir / "heuristic_candidate_events.json"
    summary_json = args.output_dir / "correlation_summary.json"
    direct_csv = args.output_dir / "direct_validator_ip_correlations.csv"

    direct_json.write_text(json.dumps(direct_rows, indent=2), encoding="utf-8")
    heuristic_json.write_text(json.dumps(heuristic_rows, indent=2), encoding="utf-8")

    write_csv(
        direct_csv,
        direct_rows,
        [
            "validator_index",
            "validator_pubkey",
            "slot",
            "peer_id",
            "peer_ip",
            "peer_user_agent",
            "peer_port",
            "p2p_msg_id",
            "method",
            "confidence",
            "crawled_node_kind",
            "crawled_fork_digest",
            "observed_at",
        ],
    )

    summarized_validators = []
    for key, item in validator_summary.items():
        summarized_validators.append({
            "validator_index": item.get("validator_index"),
            "validator_pubkey": item.get("validator_pubkey"),
            "direct_observations": item.get("direct_observations", 0),
            "top_direct_peer_ids": dict(item.get("direct_peer_ids", Counter()).most_common(10)),
            "top_direct_peer_ips": dict(item.get("direct_peer_ips", Counter()).most_common(10)),
            "heuristic_candidate_observations": item.get("heuristic_candidate_observations", 0),
            "heuristic_slots": sorted(item.get("heuristic_slots", set())),
            "heuristic_subnets": sorted(item.get("heuristic_subnets", set())),
        })

    summary = {
        "status": "ok",
        "crawl_nodes_file": str(args.crawl_nodes),
        "attestations_file": str(args.attestations),
        "crawl_nodes_loaded": len(nodes),
        "consensus_nodes_loaded": len(consensus_nodes),
        "attestation_events_loaded": len(attestations),
        "direct_correlations": len(direct_rows),
        "heuristic_candidate_events": len(heuristic_rows),
        "validators_summarized": len(summarized_validators),
        "limitations": [
            "Direct validator->IP mapping requires attestation events that include the first-forwarding peer or equivalent P2P sender metadata.",
            "When only Beacon API attestation SSE is available, subnet overlap is merely a low-confidence candidate reduction heuristic.",
            "Crawled discv5 inventories do not reveal exact live GossipSub mesh edges.",
        ],
        "validator_summary": summarized_validators,
        "output_files": {
            "direct_json": str(direct_json),
            "direct_csv": str(direct_csv),
            "heuristic_json": str(heuristic_json),
        },
    }
    summary_json.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
