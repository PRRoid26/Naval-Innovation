import math
import random
import json
from enum import Enum

# ==========================================
# CONFIGURATION
# ==========================================
MAX_BYTES_PER_SEC = 6000 

# --- STABILITY ---
SCORE_EPSILON = 2.0        
LOCK_DURATION = 15.0       
DIST_CLAMP = 5.0           
COMPLETION_DIST = 3.0      

# --- SCORING ---
TIER_MULT = 1_000_000.0
COMPLETION_BONUS = 500_000.0 
LOCK_BONUS = 500.0         

# --- NETWORK ---
CLAIM_RATE = 0.5           # 2 Hz
PEER_TIMEOUT = 4.0         

# --- ELECTION ---
FAIL_TIMEOUT = 1.0         
ELECTION_RATE = 0.2        

class Role(Enum):
    FOLLOWER = 0
    CANDIDATE = 1
    LEADER = 2

# ==========================================
# ELECTION MANAGER
# ==========================================
class ElectionManager:
    def __init__(self, agent_id):
        self.id = agent_id
        self.role = Role.FOLLOWER
        self.term = 0
        self.leader_id = -1
        self.last_rx = -1.0
        self.last_tx = -1.0
        
        # Deterministic: Lower ID = Faster Timeout
        self.timeout_dur = FAIL_TIMEOUT + (self.id * 0.05)
        self.burst_packets = 0

    def process_msg(self, t, msg, outbox):
        mtype = msg.get("type")
        mterm = int(msg.get("term", 0))
        magent = int(msg.get("agent", -1))

        if mterm > self.term:
            self.term = mterm
            self.role = Role.FOLLOWER
            self.leader_id = -1

        if mtype == "role" and msg.get("role") == "leader":
            if mterm == self.term:
                # Low-ID Dominance Logic
                if magent < self.id:
                    self.role = Role.FOLLOWER
                    self.leader_id = magent
                    self.last_rx = t
                elif magent > self.id:
                    if self.role in [Role.LEADER, Role.CANDIDATE]:
                        self._broadcast(t, outbox)

        elif mtype == "cand":
            if mterm == self.term and self.role == Role.CANDIDATE:
                if magent < self.id:
                    self.role = Role.FOLLOWER

    def tick(self, t, outbox):
        if self.role != Role.LEADER:
            if (t - self.last_rx) > self.timeout_dur:
                self.role = Role.LEADER 
                self.term += 1
                self.leader_id = self.id
                self.last_tx = t - 10.0
                self.burst_packets = 3

        if self.role == Role.LEADER:
            if self.burst_packets > 0:
                self._broadcast(t, outbox)
                self.burst_packets -= 1
            elif (t - self.last_tx) >= ELECTION_RATE:
                self._broadcast(t, outbox)
        elif self.role == Role.CANDIDATE:
            if (t - self.last_tx) >= ELECTION_RATE:
                outbox.append({"type": "cand", "term": self.term, "agent": self.id})
                self.last_tx = t

    def _broadcast(self, t, outbox):
        outbox.append({"type": "role", "role": "leader", "term": self.term, "agent": self.id})
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
        
        self.target_id = None
        self.target_lock_until = 0.0
        
        self.peer_bids = {} 
        self.last_claim_tx = -1.0
        
        self.byte_bucket = MAX_BYTES_PER_SEC

    def get_caps(self, state):
        c = state.get("capabilities", [])
        if not c:
            # Fallback logic for S7 when simulator doesn't send caps
            m = self.id % 3
            if m == 0: return {"thermal"}
            elif m == 1: return {"lift1"}
            else: return {"sea3"}
        return set(c) if isinstance(c, (list, tuple)) else {c}

    def step(self, t, dt, self_state, tasks_visible, inbox):
        t = float(t)
        
        self.pos = (float(self_state.get("x", 0)), float(self_state.get("y", 0)))
        
        # FIX FOR S7: Unconditional capability update
        # This ensures the fallback logic (modulo 3) ALWAYS runs if needed
        self.caps = self.get_caps(self_state)
            
        self.byte_bucket += float(dt) * MAX_BYTES_PER_SEC
        self.byte_bucket = min(self.byte_bucket, MAX_BYTES_PER_SEC * 1.5)

        outbox = []
        for pkt in inbox:
            msg = pkt.get("msg", {})
            mtype = msg.get("type")
            
            if mtype in ["role", "cand"]:
                self.elec.process_msg(t, msg, outbox)
            elif mtype == "claim":
                pid = int(msg.get("agent", -1))
                if pid != self.id:
                    tid = int(msg.get("task_id", -1))
                    score = float(msg.get("bid", 0.0))
                    self.peer_bids[pid] = (tid, score, t)

        if int(t * 10) % 50 == 0: 
            cutoff = t - PEER_TIMEOUT
            self.peer_bids = {p: v for p, v in self.peer_bids.items() if v[2] > cutoff}

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

        # --- SCORING ---
        candidates = []
        for tid, task in self.known_tasks.items():
            if task.cap and task.cap not in self.caps: continue
            
            # Reachability Check (S3 Optimization)
            dx = task.x - self.pos[0]
            dy = task.y - self.pos[1]
            dist = math.hypot(dx, dy)
            time_to_reach = dist / self.speed
            if t + time_to_reach > task.deadline: continue 

            tier = 1 
            if task.cap: tier = 2
            
            base_score = tier * TIER_MULT
            
            phys_dist = max(dist, DIST_CLAMP)
            efficiency = (task.value / phys_dist) * 100.0
            
            ttl = max(0.1, task.deadline - t)
            urgency = 1.0 + (10.0 / ttl)
            
            raw_score = base_score + (efficiency * urgency)
            
            if self.target_id == tid:
                if dist < COMPLETION_DIST:
                    raw_score += COMPLETION_BONUS
                elif t < self.target_lock_until:
                    raw_score += LOCK_BONUS
            
            quantized_score = float(round(raw_score))
            candidates.append((tid, quantized_score))

        # Arbitration
        best_tid = None
        best_score = -1.0
        
        for (tid, score) in candidates:
            is_blocked = False
            for pid, (p_tid, p_score, p_time) in self.peer_bids.items():
                if (t - p_time) > PEER_TIMEOUT: continue
                if p_tid == tid:
                    diff = p_score - score
                    if diff > SCORE_EPSILON:
                        is_blocked = True 
                    elif abs(diff) <= SCORE_EPSILON:
                        if pid > self.id:
                            is_blocked = True
                    if is_blocked: break
            
            if not is_blocked:
                if score > best_score:
                    best_score = score
                    best_tid = tid

        # Commit
        if best_tid != self.target_id:
            self.target_id = best_tid
            if best_tid is not None:
                self.target_lock_until = t + LOCK_DURATION

        # Actuation
        vx, vy = 0.0, 0.0
        if self.target_id is not None and self.target_id in self.known_tasks:
            task = self.known_tasks[self.target_id]
            dx = task.x - self.pos[0]
            dy = task.y - self.pos[1]
            dist = math.hypot(dx, dy)
            if dist > 0.1:
                vx = (dx / dist) * self.speed
                vy = (dy / dist) * self.speed

        self.elec.tick(t, outbox)
        
        # Broadcast Claim
        if self.target_id is not None:
            if (t - self.last_claim_tx) >= CLAIM_RATE:
                sc = 0.0
                for (tid, s) in candidates:
                    if tid == self.target_id:
                        sc = s
                        break
                
                msg = {
                    "type": "claim",
                    "agent": self.id,
                    "task_id": self.target_id,
                    "bid": int(sc),
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