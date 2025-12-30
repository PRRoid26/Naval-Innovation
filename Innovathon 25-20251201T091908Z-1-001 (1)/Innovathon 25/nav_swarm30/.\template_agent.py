# SWARMBENCH-30+ template_agent.py
# Single-file agent: leader election (epoch/quality/id) + capability-aware task allocation (bids/bundles)
# Exposes required API:
#   class Agent:
#     __init__(agent_id, world_bounds, speed, seed)
#     step(t, dt, self_state, tasks_visible, inbox) -> (action, outbox)
#
# Messages are JSON-serializable dicts. Keep them small.
#
# External expected messages:
#   Leader beacon (S6): {"type":"role","role":"leader","term":INT,"agent":ID}
#   Claim (convergence gate): {"type":"claim","agent":ID,"task_id":INT,"bid":FLOAT}

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple


# =============================================================================
# Parameters (doc defaults; tuned to meet S6 ≤10s re-election)
# =============================================================================

# Election
T_HB = 1.0
T_FAIL = 3.5
T_GOSSIP = 2.5
T_HOLD = 12.0
HYSTERESIS = 0.04

WINDOW_SIZE = 6.0
ALPHA = 0.6
BETA = 0.4
LAMBDA_EMA = 0.8
GOSSIP_TTL = 3

# Allocation
BUNDLE_SIZE = 5
SCORE_THRESHOLD = 0.25

W_V = 1.0
W_D = 0.3
W_T = 0.5
W_C = 0.2

DELTA_BID = 0.10
T_FREEZE = 30.0
T_STALE = 3.5
D_0 = 10.0
DECAY_RATE = 0.1
GRACE_PERIOD = 30.0

# Messaging cadence (bandwidth-friendly)
ROLE_BEACON_PERIOD = 1.0
CLAIM_REFRESH_PERIOD = 5.0
BID_MIN_PERIOD = 0.5   # throttle bid update spam under loss/dense tasks
TASK_ANNOUNCE_PERIOD = 1.0  # leader announces newly seen tasks (low freq)


# =============================================================================
# Helpers
# =============================================================================

def _dist(ax: float, ay: float, bx: float, by: float) -> float:
    return math.hypot(ax - bx, ay - by)

def _clamp01(x: float) -> float:
    return 0.0 if x < 0.0 else 1.0 if x > 1.0 else x


# =============================================================================
# Data models
# =============================================================================

@dataclass
class Task:
    id: int
    x: float
    y: float
    value: float
    t0: float
    deadline: float
    service: float
    cap: Any  # could be None/""/int/str/list depending on scenario
    remaining: float

    @property
    def pos(self) -> Tuple[float, float]:
        return (self.x, self.y)

@dataclass
class Bid:
    task_id: int
    agent_id: int
    score: float
    agent_quality: float
    t: float

    def key(self) -> Tuple[float, float, int]:
        # score, quality, id
        return (self.score, self.agent_quality, self.agent_id)

@dataclass
class Candidate:
    epoch: int
    agent_id: int
    quality: float
    t: float

    def key(self) -> Tuple[int, float, int]:
        return (self.epoch, self.quality, self.agent_id)


class Role(Enum):
    FOLLOWER = 0
    CANDIDATE = 1
    LEADER = 2


# =============================================================================
# Link quality tracking (heartbeat PDR estimate)
# =============================================================================

class Neighbor:
    __slots__ = ("id", "discovery_t", "last_seen", "hb_times")

    def __init__(self, neighbor_id: int, t: float):
        self.id = int(neighbor_id)
        self.discovery_t = float(t)
        self.last_seen = float(t)
        self.hb_times: List[float] = []

    def record_hb(self, t: float):
        self.last_seen = float(t)
        self.hb_times.append(float(t))

    def link_quality(self, t: float) -> float:
        cutoff = t - WINDOW_SIZE
        # prune
        i = 0
        while i < len(self.hb_times) and self.hb_times[i] < cutoff:
            i += 1
        if i:
            self.hb_times = self.hb_times[i:]

        known = min(WINDOW_SIZE, max(0.0, t - self.discovery_t))
        expected = known / T_HB if T_HB > 0 else 0.0
        if expected <= 0:
            return 0.0
        return _clamp01(len(self.hb_times) / expected)


# =============================================================================
# Election Manager (gossip-max; candidate key = (epoch, quality, id))
# =============================================================================

class ElectionManager:
    def __init__(self, agent_id: int):
        self.id = int(agent_id)
        self.epoch = 0
        self.role = Role.FOLLOWER

        self.leader_id: int = -1
        self.leader_quality: float = 0.0

        self.neighbors: Dict[int, Neighbor] = {}
        self.raw_quality: float = 0.0
        self.smoothed_quality: float = 0.0

        self.best_candidate: Optional[Candidate] = None

        self.last_hb_rx: float = -1e9
        self.last_hb_tx: float = -1e9
        self.candidate_start: float = -1e9
        self.last_election: float = -1e9

    def _compute_quality(self, t: float, swarm_size_hint: int = 30) -> float:
        deg = len(self.neighbors)
        d_hat = min(1.0, deg / max(1, (swarm_size_hint - 1)))

        if self.neighbors:
            q_avg = sum(n.link_quality(t) for n in self.neighbors.values()) / len(self.neighbors)
        else:
            q_avg = 0.0

        return _clamp01(ALPHA * d_hat + BETA * q_avg)

    def _ema(self, q: float) -> float:
        self.smoothed_quality = LAMBDA_EMA * self.smoothed_quality + (1.0 - LAMBDA_EMA) * q
        return self.smoothed_quality

    # ---- inbox handlers (dict messages) ----
    def on_heartbeat(self, msg: dict, t: float):
        epoch = int(msg.get("e", 0))
        leader = int(msg.get("l", -1))
        q = float(msg.get("q", 0.0))

        # neighbor tracking
        if leader >= 0:
            nb = self.neighbors.get(leader)
            if nb is None:
                nb = Neighbor(leader, t)
                self.neighbors[leader] = nb
            nb.record_hb(t)

        # Golden rule
        if epoch < self.epoch:
            return

        if epoch > self.epoch:
            self.epoch = epoch
            self.role = Role.FOLLOWER
            self.best_candidate = None

        self.leader_id = leader
        self.leader_quality = q
        self.last_hb_rx = t

    def on_candidate(self, msg: dict, t: float) -> Optional[dict]:
        epoch = int(msg.get("e", 0))
        aid = int(msg.get("a", -1))
        q = float(msg.get("q", 0.0))
        ttl = int(msg.get("ttl", 0))

        if epoch < self.epoch:
            return None

        if epoch > self.epoch:
            self.epoch = epoch
            self.role = Role.FOLLOWER
            self.best_candidate = None

        inc = Candidate(epoch, aid, q, t)
        if self.best_candidate is None or inc.key() > self.best_candidate.key():
            self.best_candidate = inc

        if ttl > 0:
            # forward (gossip)
            return {"type": "cand", "e": epoch, "a": aid, "q": q, "ttl": ttl - 1}
        return None

    def on_leader_announce(self, msg: dict, t: float):
        epoch = int(msg.get("e", 0))
        leader = int(msg.get("l", -1))
        q = float(msg.get("q", 0.0))

        if epoch < self.epoch:
            return
        if epoch > self.epoch:
            self.epoch = epoch

        self.role = Role.FOLLOWER
        self.leader_id = leader
        self.leader_quality = q
        self.last_hb_rx = t
        self.best_candidate = None

    def update(self, t: float, swarm_size_hint: int = 30) -> List[dict]:
        out: List[dict] = []

        # quality update
        self.raw_quality = self._compute_quality(t, swarm_size_hint)
        q_bar = self._ema(self.raw_quality)

        # failure detection
        if self.role != Role.LEADER:
            if (t - self.last_hb_rx) > T_FAIL and (t - self.last_election) > T_HOLD:
                # become candidate
                self.role = Role.CANDIDATE
                self.epoch += 1
                self.candidate_start = t
                self.leader_id = self.id
                self.best_candidate = Candidate(self.epoch, self.id, q_bar, t)
                out.append({"type": "cand", "e": self.epoch, "a": self.id, "q": q_bar, "ttl": GOSSIP_TTL})

        # leader heartbeat
        if self.role == Role.LEADER and (t - self.last_hb_tx) >= T_HB:
            out.append({"type": "hb", "e": self.epoch, "l": self.id, "q": q_bar})
            self.last_hb_tx = t

        # candidate convergence
        if self.role == Role.CANDIDATE and (t - self.candidate_start) >= T_GOSSIP:
            my_key = (self.epoch, q_bar, self.id)
            best_key = self.best_candidate.key() if self.best_candidate else (-1, -1.0, -1)
            if my_key >= best_key:
                self.role = Role.LEADER
                self.leader_id = self.id
                self.last_election = t
                self.last_hb_tx = t
                out.append({"type": "lead", "e": self.epoch, "l": self.id, "q": q_bar})

        return out


# =============================================================================
# Task Manager (bundle + bids; tie-break score>quality>id)
# =============================================================================

class TaskManager:
    def __init__(self, agent_id: int, election: ElectionManager, agent_caps: Optional[Set[Any]] = None):
        self.id = int(agent_id)
        self.elec = election
        self.caps: Set[Any] = set(agent_caps) if agent_caps else set()

        self.known: Dict[int, Task] = {}
        self.bundle: List[int] = []
        self.winners: Dict[int, Bid] = {}  # current best bid per task
        self.my_bids: Dict[int, Bid] = {}

        self.last_bid_sent_t: float = -1e9
        self._last_bundle_sig: Tuple[int, ...] = tuple()

        self.pos = (0.0, 0.0)
        self.speed = 1.0

    def set_state(self, x: float, y: float, speed: float):
        self.pos = (float(x), float(y))
        self.speed = max(0.1, float(speed))

    def set_caps(self, caps: Set[Any]):
        self.caps = set(caps)

    def add_visible_tasks(self, tasks_visible: List[dict], t: float):
        # Tasks are given as pending tasks in view
        for ti in tasks_visible:
            tid = int(ti["id"])
            if tid not in self.known:
                self.known[tid] = Task(
                    id=tid,
                    x=float(ti["x"]),
                    y=float(ti["y"]),
                    value=float(ti.get("value", 1.0)),
                    t0=float(ti.get("t0", t)),
                    deadline=float(ti.get("deadline", t + 60.0)),
                    service=float(ti.get("service", 0.0)),
                    cap=ti.get("cap", None),
                    remaining=float(ti.get("remaining", 0.0)),
                )
            else:
                # update moving fields
                task = self.known[tid]
                task.x = float(ti["x"]); task.y = float(ti["y"])
                task.remaining = float(ti.get("remaining", task.remaining))

    def _cap_ok(self, task: Task) -> bool:
        # S7: task.cap is required; if empty/None -> anyone can do it.
        if task.cap is None:
            return True
        if task.cap == "" or task.cap == 0:
            return True
        if not self.caps:
            # if we don't know our caps, be conservative: only accept "no-cap" tasks
            return False
        # task.cap may be scalar or list
        if isinstance(task.cap, (list, tuple, set)):
            return set(task.cap).issubset(self.caps)
        return task.cap in self.caps

    def score(self, task: Task, t: float) -> float:
        if not self._cap_ok(task):
            return -1.0

        ax, ay = self.pos
        dist = _dist(ax, ay, task.x, task.y)
        D_ik = dist / (dist + D_0)

        time_to_arrive = dist / self.speed
        arrival = t + time_to_arrive
        if arrival > task.deadline + GRACE_PERIOD:
            return -1.0
        tau = max(0.0, arrival - task.deadline)

        age = max(0.0, t - task.t0)
        V_t = task.value * math.exp(-DECAY_RATE * age)

        if self.caps and task.cap not in (None, "", 0):
            # small bonus if we have capability; if multiple caps, bonus is fraction
            if isinstance(task.cap, (list, tuple, set)):
                C_ik = len(set(task.cap) & self.caps) / max(1, len(self.caps))
            else:
                C_ik = 1.0 / max(1, len(self.caps))
        else:
            C_ik = 0.0

        s = (W_V * V_t) - (W_D * D_ik) - (W_T * tau) + (W_C * C_ik)
        return max(0.0, s)

    def _cleanup(self, t: float):
        expired: List[int] = []
        for tid, task in self.known.items():
            if t > task.deadline + GRACE_PERIOD:
                expired.append(tid)
        for tid in expired:
            self.known.pop(tid, None)
            if tid in self.bundle:
                self.bundle.remove(tid)
            self.winners.pop(tid, None)
            self.my_bids.pop(tid, None)

    def _resolve(self, cur: Optional[Bid], inc: Bid) -> Bid:
        if cur is None:
            return inc
        eps = 1e-6
        if inc.score > cur.score + eps:
            return inc
        if cur.score > inc.score + eps:
            return cur
        if inc.agent_quality > cur.agent_quality + eps:
            return inc
        if cur.agent_quality > inc.agent_quality + eps:
            return cur
        return inc if inc.agent_id > cur.agent_id else cur

    def on_bid(self, msg: dict, t: float):
        tid = int(msg.get("task", -1))
        aid = int(msg.get("agent", -1))
        score = float(msg.get("bid", 0.0))
        q = float(msg.get("q", 0.0))
        bt = float(msg.get("t", t))

        if tid < 0 or aid < 0:
            return
        inc = Bid(tid, aid, score, q, bt)
        cur = self.winners.get(tid)
        win = self._resolve(cur, inc)
        self.winners[tid] = win

        # if we lost, drop from bundle
        if win.agent_id != self.id and tid in self.bundle:
            self.bundle.remove(tid)
            self.my_bids.pop(tid, None)

    def update_bids(self, t: float) -> List[dict]:
        self._cleanup(t)
        out: List[dict] = []

        # orphan detection: drop stale external winners
        for tid, bid in list(self.winners.items()):
            if bid.agent_id != self.id and (t - bid.t) > T_STALE:
                self.winners.pop(tid, None)

        # fill bundle slots
        slots = BUNDLE_SIZE - len(self.bundle)
        if slots <= 0:
            return out

        candidates: List[Tuple[float, int]] = []
        for tid, task in self.known.items():
            if tid in self.bundle:
                continue
            cur = self.winners.get(tid)
            if cur and (t - cur.t) > T_FREEZE and cur.agent_id != self.id:
                continue
            s = self.score(task, t)
            if s > SCORE_THRESHOLD:
                candidates.append((s, tid))

        candidates.sort(reverse=True, key=lambda x: x[0])

        # throttle sending
        can_send = (t - self.last_bid_sent_t) >= BID_MIN_PERIOD

        for s, tid in candidates[:slots]:
            cur = self.winners.get(tid)
            should = False
            if cur is None:
                should = True
            elif s > cur.score + DELTA_BID:
                should = True
            elif cur.agent_id == self.id and abs(s - cur.score) > 0.05:
                should = True

            if should:
                myq = float(self.elec.smoothed_quality)
                b = Bid(tid, self.id, s, myq, t)
                self.winners[tid] = b
                self.my_bids[tid] = b
                if tid not in self.bundle:
                    self.bundle.append(tid)
                if can_send:
                    out.append({"type": "bid", "task": tid, "agent": self.id, "bid": float(s), "q": float(myq), "t": float(t)})
                    self.last_bid_sent_t = t
                    can_send = False  # one per window

        return out

    def voluntary_release(self, t: float) -> Optional[dict]:
        # release tasks that became infeasible/low-value
        release: List[int] = []
        ax, ay = self.pos
        for tid in list(self.bundle):
            task = self.known.get(tid)
            if task is None:
                release.append(tid)
                continue

            dist = _dist(ax, ay, task.x, task.y)
            arrival = t + (dist / self.speed)
            if arrival > task.deadline + GRACE_PERIOD:
                release.append(tid)
                continue

            age = max(0.0, t - task.t0)
            val = task.value * math.exp(-DECAY_RATE * age)
            if val < SCORE_THRESHOLD:
                release.append(tid)

        if not release:
            return None

        for tid in release:
            if tid in self.bundle:
                self.bundle.remove(tid)
            self.my_bids.pop(tid, None)
            if tid in self.winners and self.winners[tid].agent_id == self.id:
                self.winners.pop(tid, None)

        return {"type": "rel", "agent": self.id, "tasks": release}

    def next_target(self) -> Optional[int]:
        return self.bundle[0] if self.bundle else None


# =============================================================================
# Main Agent wrapper for SWARMBENCH
# =============================================================================

class Agent:
    def __init__(self, agent_id, world_bounds, speed, seed):
        self.id = int(agent_id)
        self.bounds = world_bounds
        self.speed = float(speed)
        random.seed(seed)

        # Core components
        self.elec = ElectionManager(self.id)

        # Capability handling: evaluator may inject via tasks_visible or scenario; default unknown.
        self._caps: Set[Any] = set()

        self.tasks = TaskManager(self.id, self.elec, agent_caps=self._caps)

        # External-message state
        self._last_role_beacon = -1e9
        self._last_claim = -1
        self._last_claim_t = -1e9
        self._last_task_announce = -1e9

        # Convergence-friendly: remember claimed tasks seen in inbox
        self._claimed_by_other: Dict[int, int] = {}  # task_id -> agent_id

        # Conservative bytes budget (optional). If you can access kbps, set this.
        self._bytes_budget_per_sec: Optional[int] = None
        self._budget_window_start = 0.0
        self._budget_used = 0

        # Cached swarm size hint (not provided by API; keep default)
        self._swarm_size_hint = 30

    # ---- Byte budget helpers ----
    def _budget_reset_if_needed(self, t: float):
        if self._bytes_budget_per_sec is None:
            return
        if t - self._budget_window_start >= 1.0:
            self._budget_window_start = t
            self._budget_used = 0

    def _budget_allow(self, t: float, msg: dict) -> bool:
        if self._bytes_budget_per_sec is None:
            return True
        import json
        self._budget_reset_if_needed(t)
        size = len(json.dumps(msg, separators=(",", ":")).encode("utf-8"))
        if self._budget_used + size <= self._bytes_budget_per_sec:
            self._budget_used += size
            return True
        return False

    # ---- External message ingestion ----
    def _process_inbox(self, t: float, inbox: List[dict]) -> List[dict]:
        out_fwd: List[dict] = []
        for m in inbox:
            msg = m.get("msg")
            if not isinstance(msg, dict):
                continue
            mtype = msg.get("type")

            # External expected messages
            if mtype == "role":
                # leader beacon from sim manual
                if msg.get("role") == "leader":
                    # treat as heartbeat for failure detection
                    self.elec.on_heartbeat({"type": "hb", "e": int(msg.get("term", 0)), "l": int(msg.get("agent", -1)), "q": float(msg.get("q", 0.0))}, t)

            elif mtype == "claim":
                tid = int(msg.get("task_id", -1))
                aid = int(msg.get("agent", -1))
                if tid >= 0 and aid >= 0:
                    self._claimed_by_other[tid] = aid
                    # also treat as a weak bid signal (optional)
                    bidv = float(msg.get("bid", 0.0))
                    self.tasks.on_bid({"type": "bid", "task": tid, "agent": aid, "bid": bidv, "q": float(msg.get("q", 0.0)), "t": float(msg.get("t", t))}, t)

            # Internal doc-protocol messages (compact)
            elif mtype == "hb":
                self.elec.on_heartbeat(msg, t)
            elif mtype == "cand":
                fwd = self.elec.on_candidate(msg, t)
                if fwd:
                    out_fwd.append(fwd)
            elif mtype == "lead":
                self.elec.on_leader_announce(msg, t)
            elif mtype == "bid":
                self.tasks.on_bid(msg, t)
            elif mtype == "rel":
                # release: drop winners if that agent was winner
                agent = int(msg.get("agent", -1))
                tids = msg.get("tasks", [])
                if agent >= 0 and isinstance(tids, list):
                    for tid in tids:
                        tid = int(tid)
                        cur = self.tasks.winners.get(tid)
                        if cur and cur.agent_id == agent:
                            self.tasks.winners.pop(tid, None)
            elif mtype == "task":
                # leader task announce: allow others to learn tasks outside view (if simulator supports)
                # In SWARMBENCH, tasks_visible is the primary, so this is optional.
                pass

        return out_fwd

    def step(self, t, dt, self_state, tasks_visible, inbox):
        # Optional: if environment provides kbps in state, honor it
        if self._bytes_budget_per_sec is None:
            kbps = self_state.get("kbps", None)
            if isinstance(kbps, (int, float)) and kbps > 0:
                self._bytes_budget_per_sec = int(float(kbps) * 125.0)

        x = float(self_state.get("x", 0.0))
        y = float(self_state.get("y", 0.0))
        speed = float(self_state.get("speed", self.speed)) or self.speed

        # Update internal state
        self.tasks.set_state(x, y, speed)

        # Capabilities (S7): evaluator often provides per-agent caps via scenario; sometimes injected elsewhere.
        # If self_state contains caps, consume them (safe fallback).
        caps = self_state.get("capabilities", None)
        if isinstance(caps, (list, tuple, set)):
            self._caps = set(caps)
            self.tasks.set_caps(self._caps)

        outbox: List[dict] = []

        # 1) Process inbox
        fwd_msgs = self._process_inbox(float(t), inbox)
        for fm in fwd_msgs:
            if self._budget_allow(float(t), fm):
                outbox.append(fm)

        # 2) Update election
        elec_out = self.elec.update(float(t), self._swarm_size_hint)
        for m in elec_out:
            if self._budget_allow(float(t), m):
                outbox.append(m)

        # 3) Add visible tasks
        self.tasks.add_visible_tasks(tasks_visible, float(t))

        # 4) Allocation: release then bid
        rel = self.tasks.voluntary_release(float(t))
        if rel and self._budget_allow(float(t), rel):
            outbox.append(rel)

        bid_msgs = self.tasks.update_bids(float(t))
        for bm in bid_msgs:
            if self._budget_allow(float(t), bm):
                outbox.append(bm)

        # //5) Required external leader beacon (S6)
        if self.elec.role == Role.LEADER and (float(t) - self._last_role_beacon) >= ROLE_BEACON_PERIOD:
            beacon = {"type": "role", "role": "leader", "term": int(self.elec.epoch), "agent": int(self.id)}
            # Optional include q to help others' tie-breaks without extra messages
            beacon["q"] = float(self.elec.smoothed_quality)
            if self._budget_allow(float(t), beacon):
                outbox.append(beacon)
                self._last_role_beacon = float(t)

        # 6) Required external claim messages (convergence gate)
        # Claim only the current next target; refresh slowly; send on change.
        target = self.tasks.next_target()
        if target is not None:
            changed = (target != self._last_claim)
            due = (float(t) - self._last_claim_t) >= CLAIM_REFRESH_PERIOD
            if changed or due:
                # Use our current score as "bid" (bounded)
                task = self.tasks.known.get(int(target))
                bidv = 0.0
                if task:
                    s = self.tasks.score(task, float(t))
                    bidv = float(max(0.0, min(1.0, s)))  # keep compact/ranged
                claim = {"type": "claim", "agent": int(self.id), "task_id": int(target), "bid": bidv, "t": float(t)}
                if self._budget_allow(float(t), claim):
                    outbox.append(claim)
                    self._last_claim = int(target)
                    self._last_claim_t = float(t)

        # 7) Movement action towards current target
        vx = vy = 0.0
        if target is not None and int(target) in self.tasks.known:
            task = self.tasks.known[int(target)]
            dx = task.x - x
            dy = task.y - y
            L = math.hypot(dx, dy)
            if L > 1e-9:
                vx = (dx / L) * speed
                vy = (dy / L) * speed

        action = {"vx": float(vx), "vy": float(vy)}
        return action, outbox
