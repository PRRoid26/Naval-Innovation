# SWARMBENCH-30 (DTAR-Lite) — Distributed Task Allocation under Radio Loss

This is a **single-file, CPU-only** evaluation harness for Swavlamban Innovathon challenge.
It simulates a 2D maritime area with **tasks** (locations, time windows, service times, values),
**agents** with speed limits, and a **lossy radio** with bandwidth caps.

- Deterministic by seed
- Pure Python 3 (no external deps)
- Reproducible scoring akin to an ORB‑SLAM benchmark suite: fixed **scenarios S1..S5** with ground‑truth.
- See `USER_MANUAL.md` for full usage, logging, and scenario details.

## Quick start
python evaluator.py --scenario scenarios/S1.json --team teams/template_agent.py --log out_S1.json
python evaluator.py --scenario scenarios/S1.json --team baselines/greedy_cbba.py --log out_baseline_S1.json
# Optional per-tick trace for plotting/debugging:
python evaluator.py --scenario scenarios/S1.json --team teams/template_agent.py --trace out_trace.json

- Convergence gate: 95% stable task ownership must be achieved within 60 s **after the last task-set or claim change**. Ownership is defined by `{"type":"claim","task_id":...,"agent":...}` messages; unclaimed tasks are ignored for the gate. The evaluator resets the stability window on each task-set/claim change and penalizes if stability is not met within that window.

## Interface for teams (`Agent` API)
Implement a file with a class `Agent`:

class Agent:
    def __init__(self, agent_id, world_bounds, speed, seed):
        pass

    def step(self, t, dt, self_state, tasks_visible, inbox):
        """
        Return:
          action: dict with {'vx': float, 'vy': float}  # desired velocity (clamped by speed)
          outbox: list of messages (each a JSON-serializable object)
        """
        return action, outbox

- `self_state = {'x':..., 'y':..., 'battery':..., 'speed':...}`
- `tasks_visible` is a list of pending tasks (public info) with fields: `id,x,y,t0,deadline,service,value,remaining`
- `inbox` is a list of messages delivered **this tick** (subject to radio loss)

Messages are **broadcast** (all-to-all). Each message size is computed via JSON, counted toward bytes.
Radio loss is applied per-message with probability `loss` from the scenario.

## Output
The evaluator prints the **composite score** and writes a JSON log with detailed time series and breakdown:
- task value achieved ratio, km per task, bytes per node per minute, convergence time

## Scoring (mirrors the briefing)
score = 0.6 * value_ratio - 0.2 * norm_distance - 0.2 * norm_bytes
constraints: allocator_convergence_95pct <= 60 s after last task-set/claim change (else penalty)

Normalization bases come from scenario (speed, duration, link cap).

## Files
- `evaluator.py`  — main simulator & scorer
- `scenarios/S1..S5.json` — official benchmark sequences
- `teams/template_agent.py` — a minimal template for participants
- `baselines/greedy_cbba.py` — a simple greedy baseline

## Reproducibility
- Fixed seeds per scenario (see JSON)
- Deterministic physics & radio
- Single-threaded

## License
MIT (for this harness). Teams keep their IP for their agent code.
