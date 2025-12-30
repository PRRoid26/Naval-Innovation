import math
import random
import json
from enum import Enum

# ==========================================
# CONFIGURATION: RELIABILITY MODE
# ==========================================
MAX_BYTES_PER_SEC = 6000 

# --- STABILITY TUNING ---
# We prioritize holding a decision over finding a marginally better one.
LOCK_DURATION = 30.0       # Effectively "Commit until done"
DIST_CLAMP = 5.0           # Ignore micro-movements < 5m
COMPLETION_DIST = 5.0      # Wide berth for completion locking

# --- SCORING & PRIORITY ---
# Pure logic tiers, no mixing metrics.
TIER_CAP = 2
TIER_STD = 1

# --- NETWORK ---
# High redundancy to prevent state desync
CLAIM_RATE = 0.2           # 5 Hz
PEER_TIMEOUT = 5.0         # Very tolerant of packet loss

# --- ELECTION ---
FAIL_TIMEOUT = 2.0         
ELECTION_RATE = 0.25       

class Role(Enum):
    FOLLOWER = 0
    CANDIDATE = 1
    LEADER = 2

# ==========================================
# ELECTION: AGGRESSIVE BULLY
# ==========================================
class ElectionManager:
    def __init__(self, agent_id):
        self.id = agent_id
        self.role = Role.FOLLOWER
        self.term = 0
        self.leader_id = -1
        self.last_rx = -1.0
        self.last_tx = -1.0
        self.timeout_dur = FAIL_TIMEOUT + random.uniform(0.0, 0.3)

    def process_msg(self, t, msg):
        mtype = msg.get("type")
        mterm = int(msg.get("term", 0))
        magent = int(msg.get("agent", -1))

        if mterm > self.term:
            self.term = mterm
            self.role = Role.FOLLOWER
            self.leader_id = -1

        if mtype == "role" and msg.get("role") == "leader":
            if mterm >= self.term:
                if magent < self.id:
                    pass # Coup
                else:
                    self.leader_id = magent
                    self.last_rx = t
                    self.role = Role.FOLLOWER

        elif mtype == "cand":
            if mterm == self.term and self.role == Role.CANDIDATE:
                if magent > self.id:
                    self.role = Role.FOLLOWER

    def tick(self, t, outbox):
        # 1. Failure Detection
        if self.role != Role.LEADER:
            if (t - self.last_rx) > self.timeout_dur:
                self.role = Role.LEADER 
                self.term += 1
                self.leader_id = self.id
                self.last_tx = t - 10.0 

        # 2. Broadcast
        if self.role == Role.LEADER:
            if (t - self.last_tx) >= ELECTION_RATE:
                self._broadcast(t, outbox)
        elif self.role == Role.CANDIDATE:
            if (t - self.last_tx) >= ELECTION_RATE:
                outbox.append({
                    "type": "cand", "term": self.term, "agent": self.id
                })
                self.last_tx = t

    def _broadcast(self, t, outbox):
        outbox.append({
            "type": "role", "role": "leader", 
            "term": self.term, "agent": self.id
        })
        self.last_tx = t

# ==========================================
# MAIN AGENT
# ==========================================
class Task:
    __slots__ = ("id", "x", "y", "value", "deadline", "cap", "t_visible")
    def __init__(self, data, t):
        self.id = int(data["id"])
        self.x = float(data["x"])
        self.y = float(data["y"])
        self.value = float(data.get("value", 1.0))
        self.deadline = float(data.get("deadline", t + 60.0))
        self.cap = data.get("cap", "")
        self.t_visible = t

class Agent:
    def __init__(self, agent_id, world_bounds, speed, seed):
        self.id = int(agent_id)
        self.speed = float(speed)
        random.seed(seed)
        
        self.elec = ElectionManager(self.id)
        
        self.pos = (0.0, 0.0)
        self.caps = set()
        self.known_tasks = {}
        
        # State
        self.target_id = None
        self.target_tier = 0
        self.target_lock_until = 0.0
        
        # Memory
        self.peer_bids = {} 
        self.last_claim_tx = -1.0
        
        # Bandwidth
        self.byte_bucket = MAX_BYTES_PER_SEC
        self.last_time = 0.0

    def get_caps(self, state):
        c = state.get("capabilities", [])
        if not c:
            m = self.id % 3
            if m == 0: return {"thermal"}
            elif m == 1: return {"lift1"}
            else: return {"sea3"}
        return set(c) if isinstance(c, (list, tuple)) else {c}

    def step(self, t, dt, self_state, tasks_visible, inbox):
        t = float(t)
        
        # 1. Update
        self.pos = (float(self_state.get("x", 0)), float(self_state.get("y", 0)))
        self.caps = self.get_caps(self_state)
        self.byte_bucket += float(dt) * MAX_BYTES_PER_SEC
        self.byte_bucket = min(self.byte_bucket, MAX_BYTES_PER_SEC * 1.5)

        # 2. Inbox (Pass Outbox)
        outbox = []
        for pkt in inbox:
            msg = pkt.get("msg", {})
            mtype = msg.get("type")
            
            if mtype in ["role", "cand"]:
                self.elec.process_msg(t, msg)
            elif mtype == "claim":
                pid = int(msg.get("agent", -1))
                if pid != self.id:
                    tid = int(msg.get("task_id", -1))
                    score = float(msg.get("bid", 0.0))
                    self.peer_bids[pid] = (tid, score, t)

        # 3. Cleanup
        if int(t * 10) % 50 == 0: 
            cutoff = t - PEER_TIMEOUT
            self.peer_bids = {p: v for p, v in self.peer_bids.items() if v[2] > cutoff}

        # 4. Perception
        current_ids = set()
        for td in tasks_visible:
            tid = int(td["id"])
            current_ids.add(tid)
            self.known_tasks[tid] = Task(td, t)
        
        to_del = []
        for tid, task in self.known_tasks.items():
            if tid not in current_ids and (t - task.t_visible > 2.0):
                to_del.append(tid)
            elif t > task.deadline:
                to_del.append(tid)
        for tid in to_del:
            del self.known_tasks[tid]
            if self.target_id == tid:
                self.target_id = None

        # 5. Robust Allocation
        
        # A. Calculate Priorities
        candidates = []
        for tid, task in self.known_tasks.items():
            if task.cap and task.cap not in self.caps: continue
            
            # Tier Logic
            tier = TIER_STD
            if task.cap: tier = TIER_CAP
            
            # Physics
            dx = task.x - self.pos[0]
            dy = task.y - self.pos[1]
            dist = math.hypot(dx, dy)
            phys_dist = max(dist, DIST_CLAMP)
            
            # Score (Stratified: Tier 1000 + Efficiency)
            # No multipliers. Simple addition ensures Tier separation.
            score = (tier * 1000.0) + ((task.value / phys_dist) * 100.0)
            
            # Urgency (S7 Fix): Deadline affects score WITHIN tier
            ttl = max(0.1, task.deadline - t)
            urgency = 1.0 + (10.0 / ttl)
            score += urgency # Additive boost to prevent breaking tier logic
            
            candidates.append((tid, score, tier))

        # B. Check Current Target (Lock Logic)
        # If we have a target, check if we've been evicted by a peer
        if self.target_id is not None:
            # Check for eviction
            evicted = False
            my_curr_score = 0.0
            # Re-find current task score
            for (tid, s, tier) in candidates:
                if tid == self.target_id:
                    my_curr_score = s
                    break
            
            # Check peers
            for pid, (p_tid, p_score, p_time) in self.peer_bids.items():
                if (t - p_time) > PEER_TIMEOUT: continue
                if p_tid == self.target_id:
                    # Strict Arbitration: Higher Score Wins
                    if p_score > my_curr_score:
                        evicted = True
                    elif abs(p_score - my_curr_score) < 1e-5 and pid > self.id:
                        evicted = True
            
            if evicted:
                self.target_id = None
                self.target_lock_until = 0.0

        # C. Selection
        best_tid = None
        best_score = -1.0
        best_tier = 0
        
        # 1. Filter out tasks claimed by better peers
        valid_candidates = []
        for (tid, score, tier) in candidates:
            is_blocked = False
            for pid, (p_tid, p_score, p_time) in self.peer_bids.items():
                if (t - p_time) > PEER_TIMEOUT: continue
                if p_tid == tid:
                    if p_score > score: 
                        is_blocked = True
                    elif abs(p_score - score) < 1e-5 and pid > self.id:
                        is_blocked = True
            
            if not is_blocked:
                valid_candidates.append((tid, score, tier))
                if score > best_score:
                    best_score = score
                    best_tid = tid
                    best_tier = tier

        # D. State Transition
        if self.target_id is None:
            # IDLE -> PICK
            if best_tid is not None:
                self.target_id = best_tid
                self.target_tier = best_tier
                self.target_lock_until = t + LOCK_DURATION
        else:
            # BUSY -> CHECK UPGRADE
            # Only upgrade if:
            # 1. New Tier > Current Tier (S7 Fix)
            # 2. Lock expired AND New Score > Current Score (S4 Fix)
            
            if best_tid is not None and best_tid != self.target_id:
                if best_tier > self.target_tier:
                    # Immediate Upgrade
                    self.target_id = best_tid
                    self.target_tier = best_tier
                    self.target_lock_until = t + LOCK_DURATION
                elif t > self.target_lock_until:
                    # Gentle Upgrade
                    self.target_id = best_tid
                    self.target_tier = best_tier
                    self.target_lock_until = t + LOCK_DURATION

        # 6. Actuation
        vx, vy = 0.0, 0.0
        if self.target_id is not None and self.target_id in self.known_tasks:
            task = self.known_tasks[self.target_id]
            dx = task.x - self.pos[0]
            dy = task.y - self.pos[1]
            dist = math.hypot(dx, dy)
            if dist > 0.1:
                vx = (dx / dist) * self.speed
                vy = (dy / dist) * self.speed

        # 7. Communications
        self.elec.tick(t, outbox)
        
        if self.target_id is not None:
            if (t - self.last_claim_tx) >= CLAIM_RATE:
                # Re-calculate score to broadcast
                sc = 0.0
                for (tid, s, tr) in candidates:
                    if tid == self.target_id:
                        sc = s
                        break
                
                msg = {
                    "type": "claim",
                    "agent": self.id,
                    "task_id": self.target_id,
                    "bid": round(sc, 2),
                    "t": round(t, 2)
                }
                if self._send(msg, outbox):
                    self.last_claim_tx = t

        return {"vx": vx, "vy": vy}, outbox

    def _send(self, msg, outbox):
        s = json.dumps(msg, separators=(',', ':'))
        b = len(s)
        if self.byte_bucket >= b:
            self.byte_bucket -= b
            outbox.append(msg)
            return True
        return False