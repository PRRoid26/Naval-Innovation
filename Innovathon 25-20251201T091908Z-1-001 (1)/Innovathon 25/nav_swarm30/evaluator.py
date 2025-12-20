import argparse, importlib.util, json, math, os, random
from copy import deepcopy

def load_team(team_path):
    spec = importlib.util.spec_from_file_location("team_mod", team_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if not hasattr(mod, "Agent"):
        raise RuntimeError("Team file must define class Agent")
    return mod.Agent

def json_size_bytes(obj) -> int:
    return len(json.dumps(obj, separators=(",",":")).encode("utf-8"))

def dist(a, b):
    return math.hypot(a[0]-b[0], a[1]-b[1])

def clamp(v, lo, hi):
    return max(lo, min(hi, v))

def run(scn, agent_cls, log_path, trace_path=None):
    random.seed(scn["seed"])
    dt = scn.get("dt", 1.0)
    T = scn["sim_time"]
    speed = scn["agent_speed"]
    service_radius = scn.get("service_radius", 10.0)
    kbps = scn["comm"]["kbps"]
    loss = scn["comm"]["loss"]
    byte_budget_per_s = kbps * 125.0  # 1 kbps = 125 bytes/s

    roles_cfg = scn.get("roles", {"leaders_required": 0})
    leaders_required = int(roles_cfg.get("leaders_required", 0))
    fail_at = roles_cfg.get("fail_at", None)
    fail_mode = roles_cfg.get("fail_mode", "remove")

    # Agents initial positions on a ring
    agents = []
    ax0 = (scn["area"][0]+scn["area"][1])/2
    ay0 = (scn["area"][2]+scn["area"][3])/2
    r = 0.35 * min(scn["area"][1]-scn["area"][0], scn["area"][3]-scn["area"][2])

    for i in range(scn["num_agents"]):
        ang = (2*math.pi*i)/scn["num_agents"]
        x = ax0 + r*math.cos(ang)
        y = ay0 + r*math.sin(ang)
        agents.append({"id": i, "x": x, "y": y, "battery": 1.0, "bytes_used": 0.0, "dist": 0.0, "alive": True})

    # Capabilities (optional)
    agent_caps = scn.get("agent_caps", None)
    # Claimed ownership per task_id (from messages)
    claim_owner = {}
    claim_version = 0
    last_claim_version = 0

    # Tasks
    tasks = deepcopy(scn["tasks"])
    for t in tasks:
        t["remaining"] = t["service"]
        t["done"] = False
        t["started_at"] = None
        t["completed_at"] = None
        # t["cap"] may be None or a string like "thermal","lift1","sea3"
        t["cap"] = t.get("cap", None)

    # Instantiate team agents
    Agent = agent_cls
    agent_objs = [Agent(i, scn["area"], speed, scn["seed"]+i) for i in range(scn["num_agents"])]

    inbox = [[] for _ in range(scn["num_agents"])]
    convergence_tick = None
    last_change_tick = 0  # when the task set last changed (appear/disappear/done)

    # Leader tracking via role messages: {"type":"role","role":"leader","term": int}
    current_leader = None
    current_term = -1
    failed_agent = None
    leader_elected_after_fail = None

    # For convergence: ownership proxy
    assignment_hist = []
    def visible_tasks(time_t):
        return [task for task in tasks if (task["t0"]<=time_t and not task["done"] and time_t<=task["deadline"])]

    def current_assignment(time_t):
        mapping = {}
        for task in visible_tasks(time_t):
            tid = task["id"]
            owner = claim_owner.get(tid, None)
            if owner is not None and 0 <= owner < len(agents) and agents[owner]["alive"]:
                mapping[tid] = owner
        return mapping

    time_t = 0.0
    tick = 0
    bytes_sent_this_second = [0.0 for _ in agents]

    # For logs
    total_bytes = 0.0
    total_dist = 0.0
    trace = [] if trace_path else None

    while time_t < T:
        # Check failure injection
        if fail_at is not None and failed_agent is None and time_t >= fail_at:
            # by default, fail current leader if known, else agent 0
            candidate = current_leader if current_leader is not None else 0
            failed_agent = candidate
            if 0 <= failed_agent < len(agents):
                agents[failed_agent]["alive"] = False

        # Visible tasks
        visible = [ {k:task[k] for k in ("id","x","y","t0","deadline","service","value","remaining","cap")}
                    for task in visible_tasks(time_t) ]

        # Agents decide
        outboxes = [[] for _ in agents]
        desired = []
        for i,a in enumerate(agents):
            if not a["alive"]:
                desired.append((0.0,0.0))
                outboxes[i] = []
                continue
            state = {"x": a["x"], "y": a["y"], "battery": a["battery"], "speed": speed}
            act, out = agent_objs[i].step(time_t, dt, state, visible, inbox[i])
            if not isinstance(out, list): out = []
            outboxes[i] = out
            vx = float(act.get("vx", 0.0)); vy = float(act.get("vy", 0.0))
            vnorm = math.hypot(vx, vy)
            if vnorm > speed and vnorm>0:
                scale = speed / vnorm
                vx *= scale; vy *= scale
            desired.append((vx, vy))

        # Radio: broadcast with loss + per-second cap
        delivered = [[] for _ in agents]
        for i, msgs in enumerate(outboxes):
            if not agents[i]["alive"]:
                continue
            for m in msgs:
                sz = json_size_bytes(m)
                if bytes_sent_this_second[i] + sz > byte_budget_per_s:
                    continue
                bytes_sent_this_second[i] += sz
                if random.random() < loss:
                    continue
                for j in range(len(agents)):
                    if agents[j]["alive"]:
                        delivered[j].append({"from": i, "msg": m})
                agents[i]["bytes_used"] += sz
                total_bytes += sz
                # Track claimed ownership if provided
                if isinstance(m, dict) and m.get("type")=="claim":
                    tid = m.get("task_id", None)
                    if tid is not None:
                        tid = int(tid)
                        new_owner = int(m.get("agent", i))
                        prev_owner = claim_owner.get(tid, None)
                        if prev_owner != new_owner:
                            claim_owner[tid] = new_owner
                            claim_version += 1
                # Leader tracking
                if isinstance(m, dict) and m.get("type")=="role" and m.get("role")=="leader":
                    term = int(m.get("term", 0))
                    agent_id = int(m.get("agent", i))
                    # accept higher term or first leader if none
                    if term > current_term or current_leader is None:
                        # if we are after a failure and new leader different, record election time if not set
                        if failed_agent is not None and agent_id != failed_agent and leader_elected_after_fail is None:
                            leader_elected_after_fail = time_t - fail_at
                        current_term = term
                        current_leader = agent_id

        # Move
        for i,a in enumerate(agents):
            if not a["alive"]:
                continue
            oldx, oldy = a["x"], a["y"]
            a["x"] = clamp(a["x"] + desired[i][0]*dt, scn["area"][0], scn["area"][1])
            a["y"] = clamp(a["y"] + desired[i][1]*dt, scn["area"][2], scn["area"][3])
            d = math.hypot(a["x"]-oldx, a["y"]-oldy)
            a["dist"] += d
            total_dist += d

        # Service tasks (capability-aware)
        for task in tasks:
            if task["done"] or not (task["t0"]<=time_t<=task["deadline"]):
                continue
            in_count = 0
            for idx,a in enumerate(agents):
                if not a["alive"]:
                    continue
                if dist((a["x"],a["y"]), (task["x"],task["y"])) <= service_radius:
                    # capability check
                    if task["cap"] is None:
                        ok = True
                    else:
                        if agent_caps is None: 
                            ok = False
                        else:
                            ok = task["cap"] in agent_caps[idx]
                    if ok:
                        in_count += 1
            if in_count>0:
                if task["started_at"] is None: task["started_at"] = time_t
                task["remaining"] = max(0.0, task["remaining"] - dt)
                if task["remaining"] <= 0.0:
                    task["done"] = True
                    task["completed_at"] = time_t

        # Convergence check (relative to last task-set/claim change)
        current_visible_ids = {t["id"] for t in visible_tasks(time_t)}
        # reset window on visible task-set changes
        if len(assignment_hist)==0:
            assignment_hist.append(current_assignment(time_t))
        else:
            prev_visible_ids = {t["id"] for t in visible_tasks(time_t-dt)}
            if current_visible_ids != prev_visible_ids:
                assignment_hist = []
                last_change_tick = tick
            assignment_hist.append(current_assignment(time_t))

        # reset window on claim ownership changes
        if claim_version != last_claim_version:
            assignment_hist = [current_assignment(time_t)]
            last_change_tick = tick
            last_claim_version = claim_version

        window = int(60.0/dt)
        if len(assignment_hist) >= window and convergence_tick is None:
            stable = 0; total = 0
            last_map = assignment_hist[-1]
            for tid in last_map.keys():
                total += 1
                same = all((tid in m and m[tid]==last_map[tid]) for m in assignment_hist[-window:])
                if same: stable += 1
            if total>0 and stable/total>=0.95:
                convergence_tick = tick - last_change_tick  # ticks since last change

        if trace is not None:
            trace.append({
                "t": round(time_t, 3),
                "leader": {"id": current_leader, "term": current_term},
                "agents": [
                    {
                        "id": a["id"],
                        "alive": a["alive"],
                        "x": round(a["x"], 3),
                        "y": round(a["y"], 3),
                        "bytes": int(a["bytes_used"]),
                        "dist": round(a["dist"], 3)
                    } for a in agents
                ],
                "tasks": [
                    {
                        "id": task["id"],
                        "remaining": round(task["remaining"], 3),
                        "done": task["done"]
                    } for task in tasks
                ],
                "inbox_counts": [len(box) for box in inbox]
            })

        # step
        tick += 1
        time_t += dt
        if int(time_t) != int(time_t-dt):
            bytes_sent_this_second = [0.0 for _ in agents]
        inbox = delivered

    # Score
    total_value = sum(t["value"] for t in tasks)
    value_done = sum(t["value"] for t in tasks if t["done"])
    value_ratio = 0.0 if total_value==0 else value_done/total_value

    norm_distance = total_dist / (len(agents)*speed*T + 1e-9)
    norm_bytes = total_bytes / (len(agents)* (kbps*125.0) * T + 1e-9)

    score = 0.6*value_ratio - 0.2*norm_distance - 0.2*norm_bytes

    # Convergence
    conv_sec = None if convergence_tick is None else convergence_tick * dt
    penalty = 0.0
    if conv_sec is None or conv_sec>60.0:
        penalty += 0.05

    # Leader election gate if required
    leader_election_s = leader_elected_after_fail
    if leaders_required>0 and fail_at is not None:
        if leader_election_s is None or leader_election_s>10.0:
            penalty += 0.05

    score -= penalty

    # Capability stats
    cap_tasks_total = sum(1 for t in tasks if t.get("cap") is not None)
    cap_tasks_done = sum(1 for t in tasks if t.get("cap") is not None and t["done"])

    summary = {
        "scenario": scn["name"],
        "value_ratio": round(value_ratio,4),
        "norm_distance": round(norm_distance,4),
        "norm_bytes": round(norm_bytes,4),
        "score": round(score,4),
        "convergence_s": None if conv_sec is None else round(conv_sec,2),
        "leader_election_s": None if leader_election_s is None else round(leader_election_s,2),
        "penalty": round(penalty,3),
        "cap_tasks_done": cap_tasks_done,
        "cap_tasks_total": cap_tasks_total,
        "total_dist_m": round(total_dist,2),
        "total_bytes": int(total_bytes)
    }

    if log_path:
        with open(log_path,"w") as f: json.dump({"summary": summary}, f, separators=(",",":"))
    if trace_path and trace is not None:
        with open(trace_path,"w") as f: json.dump({"trace": trace}, f, separators=(",",":"))
    print(json.dumps(summary, indent=2))
    return summary

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", required=True)
    ap.add_argument("--team", required=True)
    ap.add_argument("--log", default=None, help="Write summary JSON to this path")
    ap.add_argument("--trace", default=None, help="Write per-tick trace JSON for debugging/plots")
    args = ap.parse_args()
    with open(args.scenario,"r") as f:
        scn = json.load(f)
    Agent = load_team(args.team)
    run(scn, Agent, args.log, trace_path=args.trace)

if __name__=="__main__":
    main()
