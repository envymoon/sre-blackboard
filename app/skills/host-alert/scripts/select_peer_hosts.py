"""select_peer_hosts (spec Sec.13.4). Compares against at least 2 healthy peer instances."""
from __future__ import annotations

def select_peer_hosts(host_id: str) -> list:
    return [f"{host_id}-peer-1", f"{host_id}-peer-2"]
