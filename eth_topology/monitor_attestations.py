#!/usr/bin/env python3
"""
Passive Ethereum attestation monitor.

Two supported modes:
1. beacon-api (default): subscribe to a public Beacon API SSE attestation feed.
   - Works in a passive, read-only way from public endpoints.
   - Lets us map aggregated attestation messages -> committee members ->
     validator indices/pubkeys.
   - Does NOT reveal which peer first forwarded the message.
2. armiarma-sse (optional): consume attestation events emitted by a local
   Armiarma instance instrumenting the P2P network.
   - If available, this adds peer_id / IP / arrival metadata and allows much
     stronger validator->peer correlation.

Outputs:
- attestation_events_raw.jsonl
- attestation_events_expanded.jsonl
- attestation_summary.json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

WORKSPACE = Path("/workspace")
DEFAULT_OUTPUT = WORKSPACE / "results" / "attestations"
SLOTS_PER_EPOCH = 32
ATTESTATION_SUBNET_COUNT = 64


def eprint(*args: Any) -> None:
    print(*args, file=sys.stderr)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Passive Ethereum attestation monitor")
    p.add_argument("--mode", choices=["beacon-api", "armiarma-sse"], default="beacon-api")
    p.add_argument("--beacon-api", default="https://lodestar-mainnet.chainsafe.io")
    p.add_argument("--armiarma-sse-url", default="http://127.0.0.1:9099/events")
    p.add_argument("--duration", type=int, default=10)
    p.add_argument("--max-events", type=int, default=12)
    p.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    p.add_argument("--include-pubkeys", action="store_true", default=True)
    return p.parse_args()


def http_get_json(base_url: str, path: str, params: Optional[Dict[str, Any]] = None, timeout: int = 20) -> Any:
    url = base_url.rstrip("/") + path
    if params:
        encoded = urllib.parse.urlencode(params, doseq=True)
        url = f"{url}?{encoded}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def parse_hex_bitfield(hex_value: Optional[str]) -> List[int]:
    if not hex_value:
        return []
    hv = hex_value[2:] if hex_value.startswith("0x") else hex_value
    if not hv:
        return []
    raw = bytes.fromhex(hv)
    out: List[int] = []
    for byte_index, byte in enumerate(raw):
        for bit in range(8):
            if byte & (1 << bit):
                out.append(byte_index * 8 + bit)
    return out


def compute_subnet_for_attestation(committees_per_slot: int, slot: int, committee_index: int) -> int:
    slots_since_epoch_start = slot % SLOTS_PER_EPOCH
    committees_since_epoch_start = committees_per_slot * slots_since_epoch_start
    return (committees_since_epoch_start + committee_index) % ATTESTATION_SUBNET_COUNT


class CommitteeCache:
    def __init__(self, beacon_api: str):
        self.beacon_api = beacon_api
        self.slot_committees: Dict[int, Dict[int, List[int]]] = {}
        self.slot_committees_per_slot: Dict[int, int] = {}

    def get_slot_committees(self, slot: int) -> Tuple[Dict[int, List[int]], int]:
        if slot in self.slot_committees:
            return self.slot_committees[slot], self.slot_committees_per_slot[slot]
        payload = http_get_json(self.beacon_api, "/eth/v1/beacon/states/head/committees", {"slot": str(slot)})
        entries = payload.get("data", [])
        committees: Dict[int, List[int]] = {}
        for entry in entries:
            idx = int(entry["index"])
            committees[idx] = [int(v) for v in entry.get("validators", [])]
        self.slot_committees[slot] = committees
        self.slot_committees_per_slot[slot] = len(committees)
        return committees, len(committees)


class ValidatorPubkeyCache:
    def __init__(self, beacon_api: str):
        self.beacon_api = beacon_api
        self.cache: Dict[int, str] = {}

    def fetch_many(self, validator_indices: Iterable[int], chunk_size: int = 10) -> None:
        missing = sorted(set(int(v) for v in validator_indices if int(v) not in self.cache))
        for i in range(0, len(missing), chunk_size):
            chunk = missing[i:i + chunk_size]
            params = [("id", str(idx)) for idx in chunk]
            query = urllib.parse.urlencode(params, doseq=True)
            path = f"/eth/v1/beacon/states/head/validators?{query}"
            url = self.beacon_api.rstrip("/") + path
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=20) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                for entry in payload.get("data", []):
                    self.cache[int(entry["index"])] = entry["validator"]["pubkey"]
            except urllib.error.HTTPError:
                # Fallback for public endpoints that dislike larger repeated-id query strings.
                for idx in chunk:
                    single_url = self.beacon_api.rstrip("/") + "/eth/v1/beacon/states/head/validators?id=" + str(idx)
                    single_req = urllib.request.Request(single_url, headers={"Accept": "application/json"})
                    with urllib.request.urlopen(single_req, timeout=20) as resp:
                        payload = json.loads(resp.read().decode("utf-8"))
                    for entry in payload.get("data", []):
                        self.cache[int(entry["index"])] = entry["validator"]["pubkey"]

    def get_many(self, validator_indices: Iterable[int]) -> List[str]:
        self.fetch_many(validator_indices)
        return [self.cache[idx] for idx in validator_indices if idx in self.cache]


def sse_messages(url: str, duration: int) -> Iterator[Tuple[str, str]]:
    req = urllib.request.Request(url, headers={"Accept": "text/event-stream"})
    with urllib.request.urlopen(req, timeout=duration + 10) as resp:
        start = time.time()
        current_event = "message"
        data_lines: List[str] = []
        while time.time() - start < duration:
            raw_line = resp.readline()
            if not raw_line:
                break
            line = raw_line.decode("utf-8", "ignore").rstrip("\r\n")
            if line == "":
                if data_lines:
                    yield current_event, "\n".join(data_lines)
                current_event = "message"
                data_lines = []
                continue
            if line.startswith(":"):
                continue
            if line.startswith("event:"):
                current_event = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                data_lines.append(line.split(":", 1)[1].strip())


def expand_beacon_api_attestation(
    observed_at: str,
    payload: Dict[str, Any],
    committee_cache: CommitteeCache,
    pubkey_cache: Optional[ValidatorPubkeyCache],
    beacon_api: str,
) -> Dict[str, Any]:
    att_data = payload["data"]
    slot = int(att_data["slot"])
    committee_bits = parse_hex_bitfield(payload.get("committee_bits"))
    if committee_bits:
        committee_indices = committee_bits
    else:
        committee_indices = [int(att_data.get("index", 0))]

    committees_by_index, committees_per_slot = committee_cache.get_slot_committees(slot)

    concatenated_validators: List[int] = []
    committee_lengths: Dict[int, int] = {}
    for committee_index in sorted(committee_indices):
        members = committees_by_index.get(committee_index, [])
        committee_lengths[committee_index] = len(members)
        concatenated_validators.extend(members)

    aggregation_positions = parse_hex_bitfield(payload.get("aggregation_bits"))
    participant_validator_indices = [
        concatenated_validators[pos]
        for pos in aggregation_positions
        if pos < len(concatenated_validators)
    ]

    participant_pubkeys: List[str] = []
    if pubkey_cache is not None and participant_validator_indices:
        participant_pubkeys = pubkey_cache.get_many(participant_validator_indices)

    subnet_ids = [
        compute_subnet_for_attestation(committees_per_slot, slot, committee_index)
        for committee_index in sorted(committee_indices)
    ]

    return {
        "observed_at": observed_at,
        "source": {
            "type": "beacon_api",
            "endpoint": beacon_api,
            "peer_id": None,
            "peer_ip": None,
        },
        "slot": slot,
        "beacon_block_root": att_data["beacon_block_root"],
        "source_checkpoint": att_data["source"],
        "target_checkpoint": att_data["target"],
        "committee_indices": sorted(committee_indices),
        "committee_lengths": committee_lengths,
        "committees_per_slot": committees_per_slot,
        "subnet_ids": subnet_ids,
        "aggregation_bit_positions": aggregation_positions,
        "participant_count": len(participant_validator_indices),
        "participant_validator_indices": participant_validator_indices,
        "participant_pubkeys": participant_pubkeys,
        "raw_attestation": payload,
        "confidence": {
            "validator_membership": "high",
            "validator_to_peer_ip": "none_without_p2p_sender_metadata",
        },
    }


def expand_armiarma_attestation(
    observed_at: str,
    payload: Dict[str, Any],
    committee_cache: CommitteeCache,
    pubkey_cache: Optional[ValidatorPubkeyCache],
) -> Dict[str, Any]:
    attestation = payload["attestation"]
    extra = payload.get("attestation_extra_data", {})
    peer = payload.get("peer_info", {})

    att_data = attestation["data"] if isinstance(attestation, dict) else attestation.get("data")
    slot = int(att_data["slot"])
    committee_bits = parse_hex_bitfield(attestation.get("committee_bits"))
    if committee_bits:
        committee_indices = committee_bits
    else:
        committee_indices = [int(att_data.get("index", 0))]

    committees_by_index, committees_per_slot = committee_cache.get_slot_committees(slot)
    concatenated_validators: List[int] = []
    committee_lengths: Dict[int, int] = {}
    for committee_index in sorted(committee_indices):
        members = committees_by_index.get(committee_index, [])
        committee_lengths[committee_index] = len(members)
        concatenated_validators.extend(members)

    aggregation_positions = parse_hex_bitfield(attestation.get("aggregation_bits"))
    participant_validator_indices = [
        concatenated_validators[pos]
        for pos in aggregation_positions
        if pos < len(concatenated_validators)
    ]
    participant_pubkeys: List[str] = []
    if pubkey_cache is not None and participant_validator_indices:
        participant_pubkeys = pubkey_cache.get_many(participant_validator_indices)

    return {
        "observed_at": observed_at,
        "source": {
            "type": "armiarma_sse",
            "endpoint": None,
            "peer_id": peer.get("id"),
            "peer_ip": peer.get("ip"),
            "peer_port": peer.get("port"),
            "peer_user_agent": peer.get("user_agent"),
            "p2p_msg_id": extra.get("peer_msg_id"),
            "time_in_slot": extra.get("time_in_slot"),
            "subnet": extra.get("subnet"),
        },
        "slot": slot,
        "beacon_block_root": att_data["beacon_block_root"],
        "source_checkpoint": att_data["source"],
        "target_checkpoint": att_data["target"],
        "committee_indices": sorted(committee_indices),
        "committee_lengths": committee_lengths,
        "committees_per_slot": committees_per_slot,
        "subnet_ids": [
            compute_subnet_for_attestation(committees_per_slot, slot, committee_index)
            for committee_index in sorted(committee_indices)
        ],
        "aggregation_bit_positions": aggregation_positions,
        "participant_count": len(participant_validator_indices),
        "participant_validator_indices": participant_validator_indices,
        "participant_pubkeys": participant_pubkeys,
        "raw_attestation": payload,
        "confidence": {
            "validator_membership": "high",
            "validator_to_peer_ip": "medium_without_multiple_observations; higher if repeated first-forward observations exist",
        },
    }


def main() -> int:
    args = parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    raw_path = args.output_dir / "attestation_events_raw.jsonl"
    expanded_path = args.output_dir / "attestation_events_expanded.jsonl"
    summary_path = args.output_dir / "attestation_summary.json"

    committee_cache = CommitteeCache(args.beacon_api)
    pubkey_cache = ValidatorPubkeyCache(args.beacon_api) if args.include_pubkeys else None

    raw_events: List[Dict[str, Any]] = []
    expanded_events: List[Dict[str, Any]] = []

    if args.mode == "beacon-api":
        url = args.beacon_api.rstrip("/") + "/eth/v1/events?topics=attestation"
        stream = sse_messages(url, args.duration)
    else:
        stream = sse_messages(args.armiarma_sse_url, args.duration)

    for event_name, data_str in stream:
        if len(expanded_events) >= args.max_events:
            break
        observed_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        try:
            payload = json.loads(data_str)
        except json.JSONDecodeError:
            continue

        raw_events.append({
            "observed_at": observed_at,
            "event": event_name,
            "payload": payload,
        })

        try:
            if args.mode == "beacon-api":
                expanded = expand_beacon_api_attestation(observed_at, payload, committee_cache, pubkey_cache, args.beacon_api)
            else:
                expanded = expand_armiarma_attestation(observed_at, payload, committee_cache, pubkey_cache)
        except Exception as exc:  # pragma: no cover - best-effort monitoring
            expanded = {
                "observed_at": observed_at,
                "error": str(exc),
                "raw_attestation": payload,
                "source": {"type": args.mode},
            }
        expanded_events.append(expanded)

    with raw_path.open("w", encoding="utf-8") as fh:
        for event in raw_events:
            fh.write(json.dumps(event) + "\n")
    with expanded_path.open("w", encoding="utf-8") as fh:
        for event in expanded_events:
            fh.write(json.dumps(event) + "\n")

    unique_validators = set()
    subnet_counter = Counter()
    for event in expanded_events:
        for idx in event.get("participant_validator_indices", []):
            unique_validators.add(idx)
        for subnet in event.get("subnet_ids", []):
            subnet_counter[subnet] += 1

    summary = {
        "status": "ok",
        "mode": args.mode,
        "beacon_api": args.beacon_api,
        "armiarma_sse_url": args.armiarma_sse_url if args.mode == "armiarma-sse" else None,
        "events_captured": len(expanded_events),
        "unique_validators_observed": len(unique_validators),
        "unique_slots_observed": len({e.get('slot') for e in expanded_events if 'slot' in e}),
        "top_subnets": dict(subnet_counter.most_common(20)),
        "output_files": {
            "raw": str(raw_path),
            "expanded": str(expanded_path),
        },
        "limitations": [
            "Beacon API SSE exposes attestation contents and node-local arrival, but not the peer that first forwarded the message.",
            "Direct validator->IP correlation requires P2P sender metadata (e.g. Armiarma or an instrumented local beacon node).",
        ] if args.mode == "beacon-api" else [
            "Armiarma SSE provides stronger peer metadata, but quality depends on the crawler's actual P2P connectivity and vantage point.",
        ],
    }
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
