"""`almanac eval`: does the cheatsheet change what Claude Code does?

Each task runs under each variant through the real launcher in headless mode (`claude -p`), with routing off and one
model for every arm, in a fresh copy of the task repository and a fresh ALMANAC_HOME, so each run has its own request
log. Variants: `off` (cheatsheet.mode off), `current` (the cheatsheets as committed at a git ref) and `local` (the
working tree). These runs spend the user's allowance, so the plan is printed first and `--yes` is required.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from . import cheatsheet

VARIANTS = ("off", "current", "local")
REQUESTS_PER_RUN = 15  # a guess for the plan: the sample tasks take about this many requests
MAX_TURNS = 30
RUN_SECONDS = 900
ALLOWED = "Read Edit Write Glob Grep Bash(python:*) Bash(python3:*)"
METRICS = ("requests", "input_tokens", "output_tokens", "cache_read_input_tokens", "tool_calls", "spawns")

SAMPLE = {
    "fix-test": ("fix_test", {
        "calc.py": "def average(xs):\n    return sum(xs) / (len(xs) + 1)\n",
        "test_calc.py": "import unittest\nfrom calc import average\n\n\nclass T(unittest.TestCase):\n"
                        "    def test_average(self):\n        self.assertEqual(average([2, 4, 6]), 4)\n\n\n"
                        "if __name__ == '__main__':\n    unittest.main()\n"},
        "The tests in this folder fail. Fix the bug in calc.py so they pass. Run them with: python -m unittest",
        "python -m unittest"),
    "find-def": ("find_def", {
        "app.py": "from net.retry import backoff_seconds\n\n\ndef run():\n    return backoff_seconds(3)\n",
        "net/__init__.py": "",
        "net/retry.py": "def backoff_seconds(attempt):\n    return min(60, 2 ** attempt)\n",
        "net/client.py": "def get(url):\n    return url\n",
        "util.py": "def clamp(x, lo, hi):\n    return max(lo, min(hi, x))\n"},
        "Find where the function backoff_seconds is defined. Write its file name to answer.txt and change nothing else.",
        "python -c \"import sys; sys.exit('retry.py' not in open('answer.txt').read())\""),
    "refactor": ("refactor", {
        "report.py": "def line(name, price):\n    return f'{name}: ${price:,.2f}'\n\n\n"
                     "def total(prices):\n    return f'Total: ${sum(prices):,.2f}'\n",
        "test_report.py": "import unittest\nfrom report import line, total\n\n\nclass T(unittest.TestCase):\n"
                          "    def test_line(self):\n        self.assertEqual(line('a', 1234.5), 'a: $1,234.50')\n\n"
                          "    def test_total(self):\n        self.assertEqual(total([1, 2]), 'Total: $3.00')\n\n\n"
                          "if __name__ == '__main__':\n    unittest.main()\n"},
        "In report.py both functions repeat the same price formatting. Move it into one helper money(value) and use it "
        "in both. Keep the tests passing.",
        "python -m unittest && python -c \"import sys; sys.exit('def money(' not in open('report.py').read())\""),
}


def make_sample(root: Path):
    """A git repository with the three sample tasks, and the task list that describes it."""
    tasks = []
    for name, (folder, files, prompt, check) in SAMPLE.items():
        for rel, text in files.items():
            path = root / folder / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        tasks.append({"name": name, "prompt": prompt, "cwd": folder, "check": check})
    git = ["git", "-c", "user.name=eval", "-c", "user.email=eval@localhost"]
    for step in (["init", "-q"], ["add", "."], ["commit", "-q", "-m", "sample tasks"]):
        subprocess.run([*git, *step], cwd=root, check=True, capture_output=True)
    return tasks


def checkout_dir() -> Path:
    return Path(cheatsheet.__file__).parent / "cheatsheets"


def git(*args, cwd) -> str:
    done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8")
    if done.returncode:
        raise RuntimeError(f"git {' '.join(args)}: {done.stderr.strip()}")
    return done.stdout


def rm(path):
    """Remove a folder tree; git's object files are read-only, which Windows will not delete."""
    shutil.rmtree(path, onerror=lambda fn, p, _exc: (os.chmod(p, 0o700), fn(p)))


def export_ref(ref: str, folder: Path):
    """The cheatsheets as committed at `ref`, written to `folder`."""
    folder.mkdir(parents=True, exist_ok=True)
    for name in cheatsheet.NAMES:
        try:
            text = git("show", f"{ref}:./{name}.md", cwd=checkout_dir())
        except RuntimeError:
            continue  # a file the ref does not have falls back to the packaged one
        (folder / f"{name}.md").write_text(text, encoding="utf-8", newline="\n")


def totals(path: Path) -> dict:
    """Sums over one run's request log (answered messages only)."""
    out = dict.fromkeys(METRICS, 0)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("kind") != "messages" or rec.get("status") != 200:
            continue
        out["requests"] += 1
        for key in ("input_tokens", "output_tokens", "cache_read_input_tokens"):
            out[key] += (rec.get("usage") or {}).get(key, 0)
        out["tool_calls"] += rec.get("tool_calls", 0)
        out["spawns"] += rec.get("spawns", 0)
    return out


def run_claude(cmd, env, cwd):
    """The real launcher in headless mode. Returns its exit code (-1: timed out)."""
    try:
        return subprocess.run(cmd, env=env, cwd=cwd, timeout=RUN_SECONDS, capture_output=True).returncode
    except subprocess.TimeoutExpired:
        return -1


def run_check(check, cwd):
    if not check:
        return None
    return subprocess.run(check, shell=True, cwd=cwd, capture_output=True).returncode == 0


def one_run(task, variant, repo, work, model, cheat_dir, runner):
    """One task under one variant, in a fresh copy of the repository and a fresh almanac home."""
    shutil.copytree(repo, work / "repo")
    home = work / "home"
    home.mkdir()
    (home / "config.json").write_text(json.dumps({
        "routing": {"mode": "off"}, "cheatsheet": {"mode": "off" if variant == "off" else "on"},
        "memory": {"summary_model": "", "semantic": False, "auto_import": False}}), encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k not in ("ALMANAC_CHEATSHEETS", "ALMANAC_DISABLE", "ALMANAC_UPSTREAM")}
    env["ALMANAC_HOME"] = str(home)
    if variant == "current":
        env["ALMANAC_CHEATSHEETS"] = str(cheat_dir)
    cwd = work / "repo" / task.get("cwd", ".")
    cmd = [sys.executable, "-m", "getalmanac", "-p", task["prompt"], "--model", model, "--max-turns", str(MAX_TURNS),
           "--permission-mode", "acceptEdits", "--allowedTools", ALLOWED]
    code = runner(cmd, env, cwd)
    return {"task": task["name"], "variant": variant, "exit": code, "check": run_check(task.get("check"), cwd),
            **totals(home / "logs" / "requests.jsonl")}


def tables(runs, variants) -> str:
    out = []
    for variant in variants:
        mine = [r for r in runs if r["variant"] == variant]
        if not mine:
            continue
        out.append(f"{variant}  (per-run averages over {len(mine)} runs)")
        out.append(f"{'task':<12}{'runs':>5}{'pass':>6}{'reqs':>7}{'input':>10}{'output':>9}{'cache read':>12}{'tools':>7}{'agents':>8}")
        for name in [*dict.fromkeys(r["task"] for r in mine), None]:
            rows = [r for r in mine if name in (None, r["task"])]
            avg = {k: sum(r[k] for r in rows) / len(rows) for k in METRICS}
            checked = [r["check"] for r in rows if r["check"] is not None]
            out.append(f"{name or 'all':<12}{len(rows):>5}{(str(sum(checked)) + '/' + str(len(checked))) if checked else '-':>6}"
                       f"{avg['requests']:>7.1f}{avg['input_tokens']:>10.0f}{avg['output_tokens']:>9.0f}"
                       f"{avg['cache_read_input_tokens']:>12.0f}{avg['tool_calls']:>7.1f}{avg['spawns']:>8.1f}")
        out.append("")
    return "\n".join(out)


def main(rest, runner=run_claude):
    parser = argparse.ArgumentParser(prog="almanac eval")
    parser.add_argument("--tasks", help="JSON file: a list of {name, prompt, cwd, check}; default: three built-in sample tasks")
    parser.add_argument("--repo", help="the repository the tasks run in (default: the folder of --tasks; the sample repo)")
    parser.add_argument("--variants", default=",".join(VARIANTS), help="comma list of off, current, local")
    parser.add_argument("--ref", help="git ref for `current` (default: the commit of this checkout now)")
    parser.add_argument("-n", type=int, default=1, help="runs per task and variant")
    parser.add_argument("--model", default="sonnet", help="the one model for every arm")
    parser.add_argument("--out", default="eval-results.json")
    parser.add_argument("--yes", action="store_true", help="run it: this spends your allowance")
    args = parser.parse_args(rest)
    variants = [v for v in args.variants.split(",") if v]
    if args.n < 1 or not variants or any(v not in VARIANTS for v in variants):
        print(f"almanac eval: -n must be at least 1 and --variants some of {', '.join(VARIANTS)}", file=sys.stderr)
        return 2
    scratch = Path(tempfile.mkdtemp(prefix="almanac-eval-"))
    try:
        if args.tasks:
            tasks = json.loads(Path(args.tasks).read_text(encoding="utf-8"))
            repo = Path(args.repo or Path(args.tasks).resolve().parent)
        else:
            repo = Path(args.repo) if args.repo else scratch / "sample"
            repo.mkdir(parents=True, exist_ok=True)
            tasks = make_sample(repo)
        ref = None
        if "current" in variants:
            ref = git("rev-parse", "--verify", f"{args.ref or 'HEAD'}^{{commit}}", cwd=checkout_dir()).strip()
            export_ref(ref, scratch / "current")
        total = len(tasks) * len(variants) * args.n
        print(f"almanac eval plan\n  tasks: {', '.join(t['name'] for t in tasks)}\n  variants: {', '.join(variants)}"
              f"{f' (current = {ref[:10]})' if ref else ''}\n  runs per task and variant: {args.n}\n  model: {args.model}, routing off\n"
              f"  total runs: {total}, about {total * REQUESTS_PER_RUN} requests (up to {MAX_TURNS} turns each)\n  results: {args.out}")
        if not args.yes:
            print("This spends your allowance. Run again with --yes to go ahead.")
            return 1
        runs = []
        for task in tasks:  # variants alternate inside each task and repeat, so drift in the service spreads over all of them
            for i in range(args.n):
                for variant in variants:
                    print(f"  {task['name']} / {variant} / {i + 1}", flush=True)
                    runs.append({"repeat": i + 1, **one_run(task, variant, repo, scratch / f"run{len(runs)}", args.model, scratch / "current", runner)})
        print("\n" + tables(runs, variants))
        Path(args.out).write_text(json.dumps({"ref": ref, "model": args.model, "runs": runs}, indent=2), encoding="utf-8")
        return 0
    finally:
        rm(scratch)
