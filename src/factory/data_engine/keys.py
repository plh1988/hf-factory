"""Stable row keys of the claims and member domains.

Row ids are UUIDv5(dataset, entity, seed, key), so a key must depend only on what the row belongs to, never on
the shard or the order rows are produced in. Keys are packed integers:

  claim            member index x 64 + claim number      (claim numbers 0-19 are generated)
  standalone auth  member index x 64 + 32 + auth number  (auths for a claim reuse the claim key)
  replacement      original claim key + 32               (a corrected or reprocessed claim)
  line             claim key x 8 + line number
  adjustment       line key x 8 + adjustment slot
  auth line        auth key x 4 + line number
  accumulator      member index x 2 + (0 in network | 1 out of network)

Member-anchored rows of s4 use member_key(member, slot, slots): enrollment and PCP spans and household rows
(4 slots: current 0, prior 1), risk scores (2: HCC RAF 0, LACE 1), care gaps (8: one per gap).

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
AUTH_LINE_SLOTS = 4
ACCUMULATOR_SLOTS = 2  # one accumulator per member and network (IN, OON)


SPAN_SLOTS = 4  # enrollment, PCP assignment and household rows per member: 0 current, 1 prior/caregiver
RISK_SLOTS = 2  # 0 HCC RAF, 1 LACE
GAP_SLOTS = 8  # care gaps per member (at most 3 are generated)


def member_key(member: np.ndarray, slot: np.ndarray | int, slots: int) -> np.ndarray:
    """Key of the slot-th row of a member-anchored entity with `slots` rows reserved per member."""
    return member * slots + slot


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


def auth_line_key(auth: np.ndarray, n: np.ndarray) -> np.ndarray:
    return auth * AUTH_LINE_SLOTS + n


def accumulator_key(member: np.ndarray, out_of_network: np.ndarray) -> np.ndarray:
    return member * ACCUMULATOR_SLOTS + out_of_network


def business_numbers(prefix: str, keys) -> list[str]:
    """<prefix>-<member number, 7 digits>-<slot, 2 digits> for claim-space keys (claims, auths, appeals)."""
    return [f"{prefix}-{int(k // CLAIM_SLOTS) + 1:07d}-{int(k % CLAIM_SLOTS):02d}" for k in keys]


def claim_numbers(keys) -> list[str]:
    """Business claim numbers CLM-<member number>-<claim slot>; the key can be read back (claim_key_from_number)."""
    return business_numbers("CLM", keys)


def claim_key_from_number(number: str) -> int:
    """Inverse of claim_numbers: 'CLM-0000267-03' -> 266 x 64 + 3."""
    return (int(number[4:11]) - 1) * CLAIM_SLOTS + int(number[12:])
