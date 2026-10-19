"""Stable row keys of the claims domain.

Row ids are UUIDv5(dataset, entity, seed, key), so a key must depend only on what the row belongs to, never on
the shard or the order rows are produced in. Keys are packed integers:

  claim            member index x 64 + claim number      (claim numbers 0-19 are generated)
  standalone auth  member index x 64 + 32 + auth number  (auths for a claim reuse the claim key)
  replacement      original claim key + 32               (a corrected or reprocessed claim)
  line             claim key x 8 + line number
  adjustment       line key x 8 + adjustment slot

Growing a dataset (more members, more claims per member) adds keys but never changes existing ones.
"""

from __future__ import annotations

import numpy as np

CLAIM_SLOTS = 64  # key space per member: 0-19 claims (capped at 20), 32-63 replacements and standalone auths
REPLACEMENT_OFFSET = 32
STANDALONE_AUTH_OFFSET = 32
LINE_SLOTS = 8  # claim lines per claim (at most 6 are generated)
ADJUSTMENT_SLOTS = 8  # adjustments per line: 0 CO-45, 1 PR-1, 2 PR-2, 3 PR-3, 4 the denial's CO code
DENIAL_ADJUSTMENT_SLOT = 4


def claim_key(member: np.ndarray, n: np.ndarray) -> np.ndarray:
    return member * CLAIM_SLOTS + n


def standalone_auth_key(member: np.ndarray, n: np.ndarray) -> np.ndarray:
    return member * CLAIM_SLOTS + STANDALONE_AUTH_OFFSET + n


def replacement_key(claim: np.ndarray) -> np.ndarray:
    return claim + REPLACEMENT_OFFSET


def line_key(claim: np.ndarray, n: np.ndarray) -> np.ndarray:
    return claim * LINE_SLOTS + n


def adjustment_key(line: np.ndarray, slot: np.ndarray | int) -> np.ndarray:
    return line * ADJUSTMENT_SLOTS + slot


def claim_numbers(keys) -> list[str]:
    """Business claim numbers CLM-<member number, 7 digits>-<claim slot, 2 digits>; the key can be read back."""
    return [f"CLM-{int(k // CLAIM_SLOTS) + 1:07d}-{int(k % CLAIM_SLOTS):02d}" for k in keys]


def claim_key_from_number(number: str) -> int:
    """Inverse of claim_numbers: 'CLM-0000267-03' -> 266 x 64 + 3."""
    return (int(number[4:11]) - 1) * CLAIM_SLOTS + int(number[12:])
