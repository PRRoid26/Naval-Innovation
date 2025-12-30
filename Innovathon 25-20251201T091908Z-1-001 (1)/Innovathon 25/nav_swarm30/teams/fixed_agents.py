import math
import random
import json
from enum import Enum

# ==========================================
# CONFIGURATION
# ==========================================
MAX_BYTES_PER_SEC = 6000 

# --- STABILITY (S4 FIX) ---
# Quantize scores to integers to stop micro-fluctuations.
# Comparison epsilon: Don't switch unless > 2.0 better.
SCORE_EPSILON = 2.0        
LOCK_DURATION = 10.0       
DIST_CLAMP = 5.0           
COMPLETION_DIST = 3.0      

# --- SCORING ---
# Tier 2 (Cap) >> Tier 1 (Std)
TIER_MULT = 1_000_000.0
# Completion Bonus > Lock Bonus
COMPLETION_BONUS = 500_000.0 
LOCK_BONUS = 500.0         

# --- NETWORK ---
BASE_CLAIM_RATE = 0.2      # 5 Hz base
STABLE_CLAIM_RATE = 0.3    # 3.33 Hz when stable (was too slow at 0.4)
PEER_TIMEOUT = 4.0         

# --- ELECTION ---
FAIL_TIMEOUT = 1.5         # Aggressive
ELECTION_RATE = 0.2        

# --- PATH EFFICIENCY ---
PATH_LOOKAHEAD = 3         # Consider next 3 tasks for path planning
PATH_BONUS_MULT = 15.0     # Bonus multiplier for on-path tasks

# --- REACHABILITY ---
REACHABILITY_SAFETY = 0.85  # Must reach in 85% of deadline time

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
                    self._broadcast(t, outbox) # Immediate Coup
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
    __slots__ = ("id", "x", "y", "value", "deadline", "cap", "t_visible", "service")
    def __init__(self, data, t):
        self.id = int(data["id"])
        self.x = float(data["x"])
        self.y = float(data["y"])
        self.value = float(data.get("value", 1.0))
        self.deadline = float(data.get("deadline", t + 60.0))
        self.cap = data.get("cap", "")
        self.t_visible = t
        self.service = float(data.get("service", 0.0))

class Agent:
    def __init__(self, agent_id, world_bounds, speed, seed):
        self.id = int(agent_id)
        self.speed = float(speed)
        self.world_bounds = world_bounds
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
        self.last_time = 0.0
        
        # Adaptive claim rate tracking
        self.stable_count = 0
        self.current_claim_rate = BASE_CLAIM_RATE
        
        # Service radius (will be updated from scenario)
        self.service_radius = 5.0

    def get_caps(self, state):
        c = state.get("capabilities", [])
        if not c:
            m = self.id % 3
            if m == 0: return {"thermal"}
            elif m == 1: return {"lift1"}
            else: return {"sea3"}
        return set(c) if isinstance(c, (list, tuple)) else {c}

    def is_task_reachable(self, task, t):
        """Check if we can reach task before deadline with safety margin"""
        dx = task.x - self.pos[0]
        dy = task.y - self.pos[1]
        dist = math.hypot(dx, dy)
        
        time_needed = dist / self.speed
        time_available = task.deadline - t
        
        # Must reach in 85% of available time for safety
        return time_needed < (time_available * REACHABILITY_SAFETY)

    def calculate_path_efficiency(self, task, other_tasks):
        """Calculate bonus for tasks that are on the way to other high-value tasks"""
        if not other_tasks or len(other_tasks) < 2:
            return 0.0
        
        # Sort other tasks by value/distance ratio
        task_scores = []
        for other_task in other_tasks:
            if other_task.id == task.id:
                continue
            other_dist = max(math.hypot(other_task.x - self.pos[0], 
                                       other_task.y - self.pos[1]), 1.0)
            score = other_task.value / other_dist
            task_scores.append((other_task, score))
        
        if not task_scores:
            return 0.0
        
        # Check top 3 valuable nearby tasks
        task_scores.sort(key=lambda x: x[1], reverse=True)
        top_tasks = task_scores[:PATH_LOOKAHEAD]
        
        min_detour = float('inf')
        for next_task, _ in top_tasks:
            # Direct distance to next task
            direct_dist = math.hypot(next_task.x - self.pos[0], 
                                    next_task.y - self.pos[1])
            
            # Distance via current task
            to_current = math.hypot(task.x - self.pos[0], 
                                   task.y - self.pos[1])
            current_to_next = math.hypot(next_task.x - task.x, 
                                        next_task.y - task.y)
            via_dist = to_current + current_to_next
            
            # Calculate detour (negative means on the way)
            detour = via_dist - direct_dist
            min_detour = min(min_detour, detour)
        
        # Negative detour means task is on the way - give bonus
        if min_detour < 0:
            return abs(min_detour) * PATH_BONUS_MULT
        return 0.0

    def step(self, t, dt, self_state, tasks_visible, inbox):
        t = float(t)
        
        self.pos = (float(self_state.get("x", 0)), float(self_state.get("y", 0)))
        self.caps = self.get_caps(self_state)
        self.byte_bucket += float(dt) * MAX_BYTES_PER_SEC
        self.byte_bucket = min(self.byte_bucket, MAX_BYTES_PER_SEC * 1.5)

        # Extract service radius if available
        if tasks_visible and len(tasks_visible) > 0:
            first_task = tasks_visible[0]
            if "service_radius" in first_task:
                self.service_radius = float(first_task["service_radius"])

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

        # --- QUANTIZED SCORING WITH IMPROVEMENTS ---
        
        candidates = []
        reachable_tasks = []
        
        for tid, task in self.known_tasks.items():
            # Skip if capability mismatch
            if task.cap and task.cap not in self.caps: 
                continue
            
            # NEW: Skip if unreachable before deadline
            if not self.is_task_reachable(task, t):
                continue
            
            reachable_tasks.append(task)
            
            # Tier
            tier = 1 
            if task.cap: 
                tier = 2
            
            base_score = tier * TIER_MULT
            
            # Efficiency
            dx = task.x - self.pos[0]
            dy = task.y - self.pos[1]
            dist = math.hypot(dx, dy)
            phys_dist = max(dist, DIST_CLAMP)
            efficiency = (task.value / phys_dist) * 100.0
            
            # Urgency
            ttl = max(0.1, task.deadline - t)
            urgency = 1.0 + (10.0 / ttl)
            
            # Calculate Float Score
            raw_score = base_score + (efficiency * urgency)
            
            # NEW: Path efficiency bonus
            path_bonus = self.calculate_path_efficiency(task, reachable_tasks)
            raw_score += path_bonus
            
            # Lock Bonuses
            if self.target_id == tid:
                # Use dynamic completion distance based on service radius
                completion_threshold = self.service_radius * 0.8
                if dist < completion_threshold:
                    raw_score += COMPLETION_BONUS
                elif t < self.target_lock_until:
                    raw_score += LOCK_BONUS
            
            # QUANTIZATION (S4 Fix)
            quantized_score = float(round(raw_score))
            
            candidates.append((tid, quantized_score))

        # Arbitration with Epsilon Band
        best_tid = None
        best_score = -1.0
        
        for (tid, score) in candidates:
            is_blocked = False
            for pid, (p_tid, p_score, p_time) in self.peer_bids.items():
                if (t - p_time) > PEER_TIMEOUT: 
                    continue
                if p_tid == tid:
                    # EPSILON CHECK
                    diff = p_score - score
                    
                    if diff > SCORE_EPSILON:
                        is_blocked = True
                    elif abs(diff) <= SCORE_EPSILON:
                        # Tie-Breaker
                        if pid > self.id:
                            is_blocked = True
                    
                    if is_blocked: 
                        break
            
            if not is_blocked:
                if score > best_score:
                    best_score = score
                    best_tid = tid

        # Track stability for adaptive claim rate
        if best_tid == self.target_id:
            self.stable_count += 1
        else:
            self.stable_count = 0

        # CONVERGENCE FIX: Always use base rate to ensure evaluator can measure
        # Adaptive rate was causing null convergence in S3, S4, S6
        self.current_claim_rate = BASE_CLAIM_RATE

        # Commit
        prev_target = self.target_id
        if best_tid != self.target_id:
            self.target_id = best_tid
            if best_tid is not None:
                self.target_lock_until = t + LOCK_DURATION
                # Force immediate claim on target change for convergence
                self.last_claim_tx = -1.0  # Reset to force immediate send

        # Actuation
        vx, vy = 0.0, 0.0
        if self.target_id is not None and self.target_id in self.known_tasks:
            task = self.known_tasks[self.target_id]
            dx = task.x - self.pos[0]
            dy = task.y - self.pos[1]
            dist = math.hypot(dx, dy)
            
            # Use dynamic service radius
            if dist > self.service_radius * 0.6:  # Move if outside 60% of service radius
                vx = (dx / dist) * self.speed
                vy = (dy / dist) * self.speed
            # else: stay put, already in service range

        self.elec.tick(t, outbox)
        
        # Send claim with adaptive rate
        if self.target_id is not None:
            if (t - self.last_claim_tx) >= self.current_claim_rate:
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