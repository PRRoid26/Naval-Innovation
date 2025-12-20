# Greedy CBBA-lite with simple leader role beacons and capability-aware selection.
import math, random

def dist(ax, ay, bx, by):
    return ((ax-bx)**2 + (ay-by)**2) ** 0.5

class Agent:
    def __init__(self, agent_id, world_bounds, speed, seed):
        self.id = agent_id
        self.speed = speed
        random.seed(seed)
        self.cur = None
        self.last_broadcast = -999
        self.term = 0
        self.is_leader = (agent_id == 0)  # default leader; will be lost on failure in S6

    def step(self, t, dt, self_state, tasks_visible, inbox):
        claims = {}
        for m in inbox:
            msg = m["msg"]
            if isinstance(msg, dict):
                if msg.get("type")=="claim":
                    claims[msg["task_id"]] = msg["agent"]
                if msg.get("type")=="role" and msg.get("role")=="leader":
                    # accept higher term
                    term = int(msg.get("term",0))
                    if term > self.term:
                        self.is_leader = False
                        self.term = term

        # If no leader beacon seen for ~3s, try to become leader by incrementing term
        if (t - self.last_broadcast) > 3.0 and not self.is_leader:
            # 10% chance to self-promote. tie broken by term reception
            if random.random() < 0.1:
                self.term += 1
                self.is_leader = True

        # plan task
        if self.cur is None or self.cur not in {ti["id"] for ti in tasks_visible}:
            scored = []
            for ti in tasks_visible:
                # if task requires capability (ti['cap']), we will still attempt (baseline doesn't know caps)
                d = dist(self_state["x"], self_state["y"], ti["x"], ti["y"])
                urgency = max(0.0, (ti["deadline"] - t))
                score = d + 0.001*(1.0/(urgency+1e-6))
                scored.append((score, ti["id"]))
            scored.sort()
            for _, tid in scored:
                if claims.get(tid) is None:
                    self.cur = tid; break
            if self.cur is None and scored:
                self.cur = scored[0][1]

        outbox = []
        # Broadcast claim
        if self.cur is not None and (t - self.last_broadcast >= 3.0):
            outbox.append({"type":"claim","agent":self.id,"task_id": self.cur})
            self.last_broadcast = t
        # Leader beacons
        if self.is_leader and (t - self.last_broadcast >= 1.0):
            outbox.append({"type":"role","agent": self.id, "role":"leader", "term": self.term})
            self.last_broadcast = t

        vx = vy = 0.0
        if self.cur is not None:
            tx=ty=None
            for ti in tasks_visible:
                if ti["id"]==self.cur:
                    tx,ty = ti["x"],ti["y"]; break
            if tx is not None:
                dx = tx - self_state["x"]
                dy = ty - self_state["y"]
                L = (dx*dx + dy*dy) ** 0.5 + 1e-9
                vx = (dx / L) * self.speed
                vy = (dy / L) * self.speed

        return {"vx": vx, "vy": vy}, outbox
