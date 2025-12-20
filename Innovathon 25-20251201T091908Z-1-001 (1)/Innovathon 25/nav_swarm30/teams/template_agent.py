# Minimal template that follows a naive "nearest-task" policy with light messaging.
import math, random

def dist(ax, ay, bx, by):
    return ((ax-bx)**2 + (ay-by)**2) ** 0.5

class Agent:
    def __init__(self, agent_id, world_bounds, speed, seed):
        self.id = agent_id
        self.bounds = world_bounds
        self.speed = speed
        random.seed(seed)
        self.target = None
        self.last_broadcast = -999

    def step(self, t, dt, self_state, tasks_visible, inbox):
        # Listen to broadcasts to avoid duplicate selection.
        claimed = set()
        for m in inbox:
            msg = m["msg"]
            if isinstance(msg, dict) and msg.get("type")=="claim":
                claimed.add(msg["task_id"])

        # pick nearest task that's not claimed
        if (self.target is None) or (self.target not in [ti["id"] for ti in tasks_visible]) or (self.target in claimed):
            best = None; best_d = 1e9
            for ti in tasks_visible:
                if ti["id"] in claimed: 
                    continue
                d = dist(self_state["x"], self_state["y"], ti["x"], ti["y"])
                if d < best_d:
                    best_d = d; best = ti["id"]
            self.target = best

        outbox = []
        if self.target is not None and (t - self.last_broadcast >= 5.0):
            outbox.append({"type":"claim","agent":self.id,"task_id": self.target})
            self.last_broadcast = t

        vx = vy = 0.0
        if self.target is not None:
            for ti in tasks_visible:
                if ti["id"]==self.target:
                    dx = ti["x"] - self_state["x"]
                    dy = ti["y"] - self_state["y"]
                    L = (dx*dx + dy*dy) ** 0.5 + 1e-9
                    vx = (dx / L) * self.speed
                    vy = (dy / L) * self.speed
                    break

        return {"vx": vx, "vy": vy}, outbox
