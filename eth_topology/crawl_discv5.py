#!/usr/bin/env python3
"""
Passive Ethereum mainnet discovery crawler.

This script uses ProbeLab's Nebula crawler as a subprocess to enumerate the
Ethereum consensus-layer discv5 DHT, then normalizes the results into JSON/CSV.

Why this design:
- Pure-Python discv5 + libp2p crawling is possible in principle, but the most
  battle-tested public tooling for live ETH mainnet enumeration today is in Go.
- The task explicitly allows subprocess-based integration with existing tools.

Outputs:
- crawl_summary.json
- nodes.json
- nodes.csv
- raw/                (Nebula raw output files)
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


WORKSPACE = Path("/workspace")
DEFAULT_OUTPUT = WORKSPACE / "results" / "discv5"
DEFAULT_NEBULA_CANDIDATES = [
    Path(os.environ.get("NEBULA_BIN", "")) if os.environ.get("NEBULA_BIN") else None,
    WORKSPACE / "nebula" / "nebula",
    Path("/usr/local/bin/nebula"),
    Path("/usr/bin/nebula"),
]


def eprint(*args: Any) -> None:
    print(*args, file=sys.stderr)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Crawl Ethereum consensus discv5 via Nebula")
    p.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    p.add_argument("--network", default="ETHEREUM_CONSENSUS")
    p.add_argument("--limit", type=int, default=50, help="Number of peers to crawl")
    p.add_argument("--workers", type=int, default=200)
    p.add_argument("--timeout", type=int, default=180, help="Timeout in seconds")
    p.add_argument("--reuse-existing-raw", action="store_true", help="Reuse latest raw output if present")
    p.add_argument("--keep-enr", action="store_true", default=True)
    return p.parse_args()


def which_existing(paths: Iterable[Optional[Path]]) -> Optional[Path]:
    for path in paths:
        if path and path.exists():
            return path
    return None


def run(cmd: List[str], cwd: Optional[Path] = None, timeout: Optional[int] = None) -> subprocess.CompletedProcess:
    eprint("[crawl_discv5] $", " ".join(cmd))
    return subprocess.run(cmd, cwd=str(cwd) if cwd else None, timeout=timeout, check=True)


def ensure_nebula() -> Path:
    existing = which_existing(DEFAULT_NEBULA_CANDIDATES)
    if existing:
        return existing

    repo_dir = WORKSPACE / "nebula"
    if not repo_dir.exists():
        run(["git", "clone", "--depth", "1", "https://github.com/dennis-tra/nebula.git", str(repo_dir)])

    nebula_bin = repo_dir / "nebula"
    env = os.environ.copy()
    go_bin = "/usr/local/go/bin/go"
    if Path(go_bin).exists():
        env["PATH"] = f"/usr/local/go/bin:{env.get('PATH', '')}"
    build_cmd = [go_bin if Path(go_bin).exists() else "go", "build", "-o", str(nebula_bin), "./cmd/nebula"]
    eprint("[crawl_discv5] building Nebula...")
    subprocess.run(build_cmd, cwd=str(repo_dir), env=env, check=True)
    if not nebula_bin.exists():
        raise RuntimeError("Nebula build completed but binary was not found")
    return nebula_bin


def parse_hex_bitfield(hex_value: Optional[str], max_bits: int = 64) -> List[int]:
    if not hex_value:
        return []
    hv = hex_value[2:] if hex_value.startswith("0x") else hex_value
    if not hv:
        return []
    raw = bytes.fromhex(hv)
    bits: List[int] = []
    for byte_index, byte in enumerate(raw):
        for bit in range(8):
            if byte & (1 << bit):
                idx = byte_index * 8 + bit
                if idx < max_bits:
                    bits.append(idx)
    return bits


def infer_node_kind(record: Dict[str, Any]) -> str:
    props = record.get("Properties") or {}
    protocols = record.get("Protocols") or []

    if props.get("opstack_chain_id") is not None or any(p.startswith("/opstack/") for p in protocols):
        return "opstack"
    if props.get("fork_digest") or props.get("attnets") or any(p.startswith("/eth2/beacon_chain/") for p in protocols):
        return "consensus"
    if props.get("eth_fork_digest") or props.get("snap") is not None:
        return "execution"
    return "unknown"


def flatten_visit(record: Dict[str, Any]) -> Dict[str, Any]:
    props = record.get("Properties") or {}
    kind = infer_node_kind(record)
    attnets = props.get("attnets")
    subscribed_subnets = parse_hex_bitfield(attnets, max_bits=64)
    protocols = record.get("Protocols") or []

    return {
        "peer_id": record.get("PeerID", ""),
        "ip": props.get("ip", ""),
        "tcp": props.get("tcp", ""),
        "udp": props.get("udp", ""),
        "dialable": record.get("ConnectErrorStr", "") == "",
        "connect_error": record.get("ConnectErrorStr", ""),
        "crawl_error": record.get("CrawlErrorStr", ""),
        "agent_version": record.get("AgentVersion", ""),
        "protocols": protocols,
        "protocols_joined": ";".join(protocols),
        "connect_maddr": record.get("ConnectMaddr", ""),
        "listen_maddrs": record.get("ListenMaddrs") or [],
        "maddrs": record.get("Maddrs") or [],
        "filtered_maddrs": record.get("FilteredMaddrs") or [],
        "connect_duration": record.get("ConnectDuration", ""),
        "crawl_duration": record.get("CrawlDuration", ""),
        "visit_started_at": record.get("VisitStartedAt", ""),
        "visit_ended_at": record.get("VisitEndedAt", ""),
        "enr": props.get("enr", ""),
        "node_kind": kind,
        "fork_digest": props.get("fork_digest") or props.get("eth_fork_digest") or "",
        "next_fork_version": props.get("next_fork_version", ""),
        "next_fork_epoch": props.get("next_fork_epoch", ""),
        "attnets": attnets or "",
        "attnets_num": props.get("attnets_num", 0),
        "subscribed_subnets": subscribed_subnets,
        "syncnets": props.get("syncnets", ""),
        "opstack_chain_id": props.get("opstack_chain_id", ""),
        "seq": props.get("seq", ""),
        "raw_properties": props,
    }


def load_ndjson(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def latest_matching(path: Path, suffix: str) -> Optional[Path]:
    matches = sorted(path.glob(f"*{suffix}"))
    return matches[-1] if matches else None


def copy_raw_outputs(raw_source: Path, raw_dest: Path) -> Dict[str, Path]:
    raw_dest.mkdir(parents=True, exist_ok=True)
    copied: Dict[str, Path] = {}
    for src in raw_source.iterdir():
        if src.is_file():
            dst = raw_dest / src.name
            shutil.copy2(src, dst)
            copied[src.name] = dst
    return copied


def main() -> int:
    args = parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = args.output_dir / "raw"

    if args.reuse_existing_raw and raw_dir.exists():
        eprint("[crawl_discv5] reusing existing raw outputs")
    else:
        nebula_bin = ensure_nebula()
        with tempfile.TemporaryDirectory(prefix="nebula-crawl-") as tmp:
            tmp_path = Path(tmp)
            cmd = [
                str(nebula_bin),
                "--json-out",
                str(tmp_path),
                "crawl",
                "--network",
                args.network,
                "--limit",
                str(args.limit),
                "--workers",
                str(args.workers),
            ]
            if args.keep_enr:
                cmd.append("--keep-enr")
            subprocess.run(cmd, timeout=args.timeout, check=True)
            copy_raw_outputs(tmp_path, raw_dir)

    visits_path = latest_matching(raw_dir, "_visits.ndjson")
    crawl_path = latest_matching(raw_dir, "_crawl.json")
    props_path = latest_matching(raw_dir, "_crawl_properties.json")
    if not visits_path or not crawl_path or not props_path:
        raise SystemExit("Nebula raw output files were not found")

    visits = load_ndjson(visits_path)
    crawl_summary = json.loads(crawl_path.read_text(encoding="utf-8"))
    crawl_properties = json.loads(props_path.read_text(encoding="utf-8"))

    nodes = [flatten_visit(v) for v in visits]

    node_kind_counts = Counter(n["node_kind"] for n in nodes)
    agent_counts = Counter(n["agent_version"] or "<empty>" for n in nodes)
    dialable_count = sum(1 for n in nodes if n["dialable"])
    consensus_nodes = [n for n in nodes if n["node_kind"] == "consensus"]

    normalized_summary = {
        "source": "nebula",
        "network": args.network,
        "raw_crawl_summary": crawl_summary,
        "raw_crawl_properties": crawl_properties,
        "normalized": {
            "nodes_seen": len(nodes),
            "dialable_nodes": dialable_count,
            "undialable_nodes": len(nodes) - dialable_count,
            "node_kind_counts": dict(node_kind_counts),
            "top_agent_versions": dict(agent_counts.most_common(20)),
            "consensus_nodes_with_attnets": sum(1 for n in consensus_nodes if n["attnets"]),
        },
        "notes": [
            "Ethereum discv5 is a shared DHT; crawls can include non-mainnet-consensus peers (e.g. OP Stack, execution-layer, or other networks).",
            "Nebula reveals peer inventory, ENRs, IPs, ports, and identify/req-resp metadata; it does not reveal exact live GossipSub mesh edges.",
        ],
    }

    (args.output_dir / "crawl_summary.json").write_text(json.dumps(normalized_summary, indent=2), encoding="utf-8")
    (args.output_dir / "nodes.json").write_text(json.dumps(nodes, indent=2), encoding="utf-8")

    csv_fields = [
        "peer_id",
        "ip",
        "tcp",
        "udp",
        "dialable",
        "connect_error",
        "crawl_error",
        "agent_version",
        "node_kind",
        "fork_digest",
        "attnets",
        "attnets_num",
        "subscribed_subnets",
        "syncnets",
        "opstack_chain_id",
        "protocols_joined",
        "connect_maddr",
        "connect_duration",
        "crawl_duration",
        "visit_started_at",
        "visit_ended_at",
        "seq",
        "enr",
    ]
    with (args.output_dir / "nodes.csv").open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=csv_fields)
        writer.writeheader()
        for node in nodes:
            row = dict(node)
            row["subscribed_subnets"] = ";".join(str(x) for x in node.get("subscribed_subnets", []))
            writer.writerow({k: row.get(k, "") for k in csv_fields})

    print(json.dumps({
        "status": "ok",
        "output_dir": str(args.output_dir),
        "nodes_seen": len(nodes),
        "dialable_nodes": dialable_count,
        "node_kind_counts": dict(node_kind_counts),
        "top_agent_versions": dict(agent_counts.most_common(10)),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
