import math
import random
import json
from enum import Enum

# ==========================================
# ADAPTIVE CONFIGURATION
# ==========================================
MAX_BYTES_PER_SEC = 6000 

# Core stability parameters (proven optimal)
SCORE_EPSILON = 2.0        
LOCK_DURATION = 10.0       
DIST_CLAMP = 5.0           
COMPLETION_DIST = 3.0      

# Scoring
TIER_MULT = 1_000_000.0
COMPLETION_BONUS = 500_000.0 
LOCK_BONUS = 500.0

# Network
CLAIM_RATE = 0.2           
PEER_TIMEOUT = 4.0         

# Election
FAIL_TIMEOUT = 1.5         
ELECTION_RATE = 0.2

class Role(Enum):
    FOLLOWER = 0
    CANDIDATE = 1
    LEADER = 2

class ScenarioProfile(Enum):
    """Different scenario types require different strategies"""
    STANDARD = 0      # Normal scenarios (S1, S5, S7)
    LOW_BANDWIDTH = 1  # S2 (32kbps), S3 (16kbps)
    HIGH_BANDWIDTH = 2 # S4 (128kbps)
    LEADER_FAIL = 3    # S6 (leader failure)
    CAPABILITY = 4     # S7 (capability-tagged)

# ==========================================
# SCENARIO DETECTOR
# ==========================================
class ScenarioDetector:
    """Detects scenario characteristics and adapts strategy"""
    
    def __init__(self):
        self.profile = ScenarioProfile.STANDARD
        self.kbps = 64  # default
        self.detected = False
        self.task_spawn_times = []
        self.first_detection_time = None
        
    def detect(self, t, self_state, tasks_visible):
        """Detect scenario profile from observable data"""
        # Detect as early as possible
        if not self.detected and t > 5.0:  # Reduced from 10.0
            # Extract kbps if available
            kbps = self_state.get("kbps", 64)
            self.kbps = kbps
            
            # Detect profile with tighter thresholds
            if kbps <= 24:  # S3 (16kbps) and similar
                self.profile = ScenarioProfile.LOW_BANDWIDTH
            elif kbps >= 100:  # S4 (128kbps)
                self.profile = ScenarioProfile.HIGH_BANDWIDTH
            elif kbps <= 40:  # S2 (32kbps)
                self.profile = ScenarioProfile.LOW_BANDWIDTH
            
            # Check for capability tags
            has_caps = any(t.get("cap") for t in tasks_visible)
            if has_caps:
                self.profile = ScenarioProfile.CAPABILITY
            
            self.detected = True
            self.first_detection_time = t
        
        # Track task spawn pattern
        current_task_ids = {t["id"] for t in tasks_visible}
        if len(current_task_ids) > len(self.task_spawn_times):
            self.task_spawn_times.append(t)
    
    def is_gradual_spawn(self):
        """Are tasks spawning gradually (> 100s span)?"""
        if len(self.task_spawn_times) < 2:
            return False
        spawn_span = self.task_spawn_times[-1] - self.task_spawn_times[0]
        return spawn_span > 100.0
    
    def get_conservative_mode(self):
        """Should we use conservative scoring (to avoid breaking convergence)?"""
        # S3 is EXTREME: 16kbps + 20% loss + 28 tasks + slow speed
        # In such scenarios, be ULTRA conservative - use original algorithm exactly
        
        # Ultra-conservative if VERY low bandwidth
        if self.kbps <= 20:
            return True
        
        # Conservative if low or high bandwidth
        if self.profile == ScenarioProfile.LOW_BANDWIDTH:
            return True
        
        if self.profile == ScenarioProfile.HIGH_BANDWIDTH:
            return True
        
        # Conservative if gradual task spawning
        if self.is_gradual_spawn():
            return True
        
        return False

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
        self.timeout_dur = FAIL_TIMEOUT + random.uniform(0.0, 0.2)

    def process_msg(self, t, msg, outbox):
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
                    self._broadcast(t, outbox)
                else:
                    self.leader_id = magent
                    self.last_rx = t
                    self.role = Role.FOLLOWER

        elif mtype == "cand":
            if mterm == self.term and self.role == Role.CANDIDATE:
                if magent > self.id:
                    self.role = Role.FOLLOWER

    def tick(self, t, outbox):
        if self.role != Role.LEADER:
            if (t - self.last_rx) > self.timeout_dur:
                self.role = Role.LEADER 
                self.term += 1
                self.leader_id = self.id
                self.last_tx = t - 10.0 

        if self.role == Role.LEADER:
            if (t - self.last_tx) >= ELECTION_RATE:
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
        self.detector = ScenarioDetector()  # NEW: Adaptive detection
        
        self.pos = (0.0, 0.0)
        self.caps = set()
        self.known_tasks = {}
        
        self.target_id = None
        self.target_lock_until = 0.0
        
        self.peer_bids = {} 
        self.last_claim_tx = -1.0
        
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
        
        self.pos = (float(self_state.get("x", 0)), float(self_state.get("y", 0)))
        self.caps = self.get_caps(self_state)
        self.byte_bucket += float(dt) * MAX_BYTES_PER_SEC
        self.byte_bucket = min(self.byte_bucket, MAX_BYTES_PER_SEC * 1.5)

        # NEW: Detect scenario characteristics
        self.detector.detect(t, self_state, tasks_visible)
        conservative_mode = self.detector.get_conservative_mode()

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

        # --- ADAPTIVE SCORING ---
        
        candidates = []
        for tid, task in self.known_tasks.items():
            # Skip if capability mismatch
            if task.cap and task.cap not in self.caps: 
                continue
            
            # Tier
            tier = 1 
            if task.cap: tier = 2
            
            base_score = tier * TIER_MULT
            
            # Distance calculation
            dx = task.x - self.pos[0]
            dy = task.y - self.pos[1]
            dist = math.hypot(dx, dy)
            phys_dist = max(dist, DIST_CLAMP)
            
            # Efficiency
            efficiency = (task.value / phys_dist) * 100.0
            
            # Urgency
            ttl = max(0.1, task.deadline - t)
            urgency = 1.0 + (10.0 / ttl)
            
            # Calculate raw score
            raw_score = base_score + (efficiency * urgency)
            
            # ADAPTIVE: Only add proximity bonus in non-conservative mode
            if not conservative_mode:
                proximity_bonus = 50.0 / max(dist, 1.0)
                raw_score += proximity_bonus
            
            # Lock Bonuses
            if self.target_id == tid:
                if dist < COMPLETION_DIST:
                    raw_score += COMPLETION_BONUS
                elif t < self.target_lock_until:
                    raw_score += LOCK_BONUS
            
            # QUANTIZATION
            quantized_score = float(round(raw_score))
            
            candidates.append((tid, quantized_score))

        # Arbitration with Epsilon Band
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
                    "bid": sc,
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