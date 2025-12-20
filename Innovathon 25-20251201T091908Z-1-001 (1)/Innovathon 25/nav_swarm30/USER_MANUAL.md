# SWARMBENCH-30+ Simulator Manual

This guide shows hackathon participants how to run the simulator, write an agent, and capture outputs for the SWARMBENCH-30 challenge.

## Prerequisites
- Python 3.8+ (no external dependencies)
- Repo layout:
  - `evaluator.py` — main simulator/scorer
  - `scenarios/` — JSON scenarios `S1..S7`
  - `teams/` — template for your agent
  - `baselines/` — reference baseline (`greedy_cbba.py`)
  - `run_all.py` — convenience runner for all scenarios

## Running the simulator
Single scenario:
python evaluator.py --scenario scenarios/S1.json --team teams/template_agent.py --log out_S1_summary.json --trace out_S1_trace.json
- `--log` writes a compact summary JSON (score, value ratio, penalties, etc.).
- `--trace` writes a per-tick JSON trace (agent poses, bytes used, leader term, task remaining); useful for plots/debugging. Omit it for faster runs.

All scenarios:
python run_all.py --team baselines/greedy_cbba.py --out results.csv

- Produces one summary per scenario in the console and a CSV with aggregated scores. Swap in your agent file for `--team`.

## Agent API (what you must implement)
Create a Python file with a class `Agent`:
class Agent:
    def __init__(self, agent_id, world_bounds, speed, seed):
        ...

    def step(self, t, dt, self_state, tasks_visible, inbox):
        return action, outbox

Inputs to `step`:
- `t`, `dt`: current sim time (seconds) and step size.
- `self_state`: `{"x","y","battery","speed"}`.
- `tasks_visible`: pending tasks in view with `id,x,y,t0,deadline,service,value,remaining,cap`.
- `inbox`: messages delivered this tick (already subject to loss), each as `{"from": sender_id, "msg": payload}`.

Outputs from `step`:
- `action`: `{"vx": float, "vy": float}` desired velocity (sim clamps to speed).
- `outbox`: list of broadcast messages (JSON-serializable). Message size is counted toward bytes via JSON encoding; per-agent cap is `kbps * 125` bytes per second.

### Required/expected messages
- Leader beacon (for S6): `{"type":"role","role":"leader","term":INT,"agent":ID}` broadcast periodically by the current leader.
- Task claim example: `{"type":"claim","agent":ID,"task_id":INT,"bid":FLOAT?}` — you may extend this schema.

## Scenarios
- S1–S5: varied kbps/loss/task density; no capabilities; no leader failure.
- S6: leader failure at `roles.fail_at` (300 s). You must detect loss and elect a new leader within ≤10 s to avoid penalty.
- S7: capability-tagged tasks (`task["cap"]`) and per-agent capabilities (`agent_caps`). A task only services if the agent has the required capability.
- Common fields: `area` (xmin,xmax,ymin,ymax), `agent_speed`, `service_radius`, `comm.kbps`, `comm.loss`, `tasks` list with time windows (`t0`, `deadline`) and service time (`service` seconds inside radius).

## Scoring and gates
Printed summary (and `--log`) includes:
- `score = 0.6*value_ratio - 0.2*norm_distance - 0.2*norm_bytes - penalty`
- `value_ratio = achieved task value / total task value`
- `norm_distance = total distance / (N * speed * sim_time)`
- `norm_bytes = total bytes / (N * kbps * 125 * sim_time)`
- Penalties: +0.05 if convergence <95% within 60 s **after the last task-set or claim change** (task appears/disappears or ownership changes); unclaimed tasks are ignored for the gate. +0.05 if leader re-election in S6 exceeds 10 s or missing.
- Additional fields: `convergence_s`, `leader_election_s`, `cap_tasks_done/total`, `total_dist_m`, `total_bytes`.

## Debugging with traces
- `--trace path.json` writes `{"trace":[...]}`. Each entry has time `t`, current `leader` (`id`, `term`), per-agent pose/alive/bytes/distance, per-task `remaining/done`, and `inbox_counts`.
- Use traces to plot trajectories, bytes usage, leader term changes, and task completion progress.

## Building your own agent
1. Copy `teams/template_agent.py` as a starting point.
2. Implement:
   - Leader election + periodic beacons (see S6 requirement).
   - Capability-aware tasking for S7 (only attempt tasks you can service).
   - Event-triggered messaging to stay under bandwidth caps and counter loss.
3. Run locally:
python evaluator.py --scenario scenarios/S3.json --team teams/my_agent.py --log my_s3.json
python run_all.py --team teams/my_agent.py --out my_results.csv

4. Inspect summaries and, if needed, enable `--trace` to debug assignment stability or leader handoffs.

## Tips
- Keep messages small and infrequent; the simulator drops messages when per-second byte budgets are exceeded or by random loss.
- Ensure a clear tie-break for leader election to avoid oscillations.
- For convergence, avoid frequent task swapping; stabilize claims within 60 s of task-set/ownership changes. Ensure you broadcast `claim` messages; unclaimed tasks are ignored for convergence scoring.
