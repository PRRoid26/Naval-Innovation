
"""
run_all.py — convenience runner for SWARMBENCH-30+ (S1..S7)
Usage:
  python run_all.py --team teams/template_agent.py
  python run_all.py --team baselines/greedy_cbba.py --out results.csv
"""
import argparse, json, importlib.util, os
from pathlib import Path

def load_eval(path: Path):
    spec = importlib.util.spec_from_file_location("evaluator_mod", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--team", required=True, help="Path to team .py file")
    ap.add_argument("--out", default="results.csv", help="Where to write CSV")
    args = ap.parse_args()

    base_dir = Path(__file__).resolve().parent
    eval_path = base_dir / "evaluator.py"
    if not eval_path.exists():
        alt = base_dir / "nav_swarm30" / "evaluator.py"
        if alt.exists():
            eval_path = alt
    if not eval_path.exists():
        raise FileNotFoundError(f"Cannot find evaluator.py near {base_dir}")

    ev = load_eval(eval_path)

    scenarios = []
    for i in range(1, 8):
        for candidate in [base_dir / f"scenarios/S{i}.json", base_dir / "nav_swarm30" / f"scenarios/S{i}.json"]:
            if candidate.exists():
                scenarios.append(candidate)
                break

    team_path = Path(args.team)
    if not team_path.is_absolute():
        if (base_dir / args.team).exists():
            team_path = base_dir / args.team
        elif (base_dir / "nav_swarm30" / args.team).exists():
            team_path = base_dir / "nav_swarm30" / args.team

    rows = []
    for scn in scenarios:
        with open(scn,"r") as f:
            sc = json.load(f)
        Agent = ev.load_team(str(team_path))
        summary = ev.run(sc, Agent, log_path=None)
        rows.append(summary)

    # CSV
    header = ["scenario","score","value_ratio","norm_distance","norm_bytes","convergence_s","leader_election_s","penalty","cap_tasks_done","cap_tasks_total","total_dist_m","total_bytes"]
    with open(args.out,"w") as f:
        f.write(",".join(header)+"\n")
        for r in rows:
            f.write(",".join(str(r.get(k, "")) for k in header) + "\n")
        if rows:
            avg = sum(r["score"] for r in rows) / len(rows)
            f.write(f"AVERAGE,{avg}\n")
    print(f"Wrote {args.out}")

if __name__=="__main__":
    main()
