from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time
import webbrowser

from .bundles import create_bundle
from .http_client import HarnessClient

ARM_CONTEXT_DIR = ".harness"
ARM_CONTEXT_FILE = "context.json"


PUBLIC_HARNESS_URL = "https://scorebench.dev/"


def _env_first(*names: str) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return None


@dataclass(frozen=True)
class HarnessCliContext:
    url: str
    token: str
    arm: str | None = None
    run_dir: str | None = None
    experiment_id: str | None = None
    source: str = "environment"


@dataclass(frozen=True)
class HarnessAdminProfile:
    url: str
    username: str
    session_token: str
    profile: str = "default"


def admin_config_path() -> Path:
    explicit = _env_first("SCOREBENCH_CLI_CONFIG", "HARNESS_CLI_CONFIG")
    if explicit:
        return Path(explicit).expanduser()
    config_home = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return config_home / "harness" / "cli.json"


def load_admin_config() -> dict:
    path = admin_config_path()
    if not path.exists():
        return {"current_profile": "default", "profiles": {}}
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise SystemExit(f"invalid harness CLI config: {path}")
    loaded.setdefault("current_profile", "default")
    loaded.setdefault("profiles", {})
    if not isinstance(loaded["profiles"], dict):
        raise SystemExit(f"invalid harness CLI config profiles: {path}")
    return loaded


def save_admin_config(config: dict) -> None:
    path = admin_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def load_admin_profile(profile_name: str | None = None) -> HarnessAdminProfile:
    config = load_admin_config()
    profile_name = profile_name or str(config.get("current_profile") or "default")
    profiles = config.get("profiles") or {}
    profile = profiles.get(profile_name)
    if not isinstance(profile, dict) or not profile.get("session_token") or not profile.get("url"):
        raise SystemExit(
            f"Harness CLI profile {profile_name!r} is not logged in. "
            "Run: harness admin login --url <harness-url> --username <user>"
        )
    return HarnessAdminProfile(
        url=str(profile["url"]),
        username=str(profile.get("username") or ""),
        session_token=str(profile["session_token"]),
        profile=profile_name,
    )


def make_admin_client(profile_name: str | None = None) -> tuple[HarnessClient, HarnessAdminProfile]:
    profile = load_admin_profile(profile_name)
    return HarnessClient(profile.url, admin_session=profile.session_token), profile


def _load_workspace_context(cwd: Path) -> dict | None:
    try:
        search_root = cwd.resolve()
    except FileNotFoundError:
        search_root = cwd.absolute()
    for path in (search_root, *search_root.parents):
        context_path = path / ARM_CONTEXT_DIR / ARM_CONTEXT_FILE
        if context_path.exists():
            loaded = json.loads(context_path.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                raise SystemExit(f"invalid harness context: {context_path}")
            loaded["_source"] = str(context_path)
            return loaded
    return None


def load_cli_context(cwd: Path | None = None) -> HarnessCliContext:
    url = _env_first("SCOREBENCH_URL", "HARNESS_URL")
    run_token = _env_first("SCOREBENCH_RUN_TOKEN", "HARNESS_RUN_TOKEN")
    token = run_token or _env_first("SCOREBENCH_ARM_TOKEN", "HARNESS_ARM_TOKEN")
    context = None if token and url else _load_workspace_context(cwd or Path.cwd())
    source = "environment"
    arm = run_dir = experiment_id = None

    if context:
        source = str(context.get("_source") or "workspace")
        url = url or context.get("harness_url")
        token = token or context.get("harness_arm_token")
        arm = str(context["arm"]) if context.get("arm") else None
        run_dir = str(context["run_dir"]) if context.get("run_dir") else None
        experiment_id = str(context["experiment_id"]) if context.get("experiment_id") else None

    url = url or PUBLIC_HARNESS_URL
    if not token:
        raise SystemExit(
            "ScoreBench context not found. Run from a harness arm workspace "
            "(runs/<run>/arms/<arm>/workspace) after harnessd starts, or set "
            "SCOREBENCH_URL with SCOREBENCH_RUN_TOKEN (the legacy HARNESS_URL "
            "and HARNESS_RUN_TOKEN names also work)."
        )
    return HarnessCliContext(url=str(url), token=str(token), arm=arm, run_dir=run_dir, experiment_id=experiment_id, source=source)


def make_client() -> HarnessClient:
    context = load_cli_context()
    return HarnessClient(context.url, arm_token=context.token)


def print_json(payload) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def parse_metadata(metadata_json: str | None, meta_items: list[str]) -> dict:
    metadata = {}
    if metadata_json:
        loaded = json.loads(metadata_json)
        if not isinstance(loaded, dict):
            raise SystemExit("--metadata-json must decode to an object")
        metadata.update(loaded)
    for item in meta_items:
        if "=" not in item:
            raise SystemExit(f"--meta must be key=value, got: {item}")
        key, value = item.split("=", 1)
        key = key.strip()
        if not key:
            raise SystemExit("--meta key cannot be empty")
        try:
            metadata[key] = json.loads(value)
        except ValueError:
            metadata[key] = value
    return metadata


def submit_usage(args) -> dict:
    usage = {
        "input_tokens": args.input_tokens,
        "output_tokens": args.output_tokens,
        "cache_creation_tokens": args.cache_creation_tokens,
        "cache_read_tokens": args.cache_read_tokens,
        "cached_input_tokens": args.cached_input_tokens,
        "total_tokens": args.total_tokens,
        "tokens_delta": args.tokens_delta,
        "cost_usd": args.cost_usd,
    }
    if any(value is not None for value in usage.values()):
        usage["source"] = args.usage_source
        usage["confidence"] = args.usage_confidence
        if args.tokens_total_source:
            usage["tokens_total_source"] = args.tokens_total_source
        return usage
    return {}


def add_usage_arguments(parser: argparse.ArgumentParser, *, require_total: bool = False, include_breakdown: bool = False) -> None:
    parser.add_argument("--input-tokens", type=int)
    parser.add_argument("--output-tokens", type=int)
    parser.add_argument(
        "--cache-creation-tokens",
        type=int,
        help="cumulative cache-creation input tokens; counted toward the working token total",
    )
    parser.add_argument(
        "--cache-read-tokens",
        type=int,
        help="cumulative cache-read input tokens; EXCLUDED from the working total (re-reads of cached context, not new work)",
    )
    # Legacy/alias for cache reads (OpenAI/Codex reports cached prompt tokens here); the
    # harness treats it identically to --cache-read-tokens.
    parser.add_argument("--cached-input-tokens", type=int)
    if include_breakdown:
        parser.add_argument("--reasoning-output-tokens", type=int)
    parser.add_argument("--total-tokens", type=int, required=require_total, help="cumulative token count for this harness run")
    parser.add_argument("--tokens-delta", type=int)
    parser.add_argument("--cost-usd", type=float)
    parser.add_argument("--usage-source", default="agent_reported")
    parser.add_argument("--usage-confidence", default="estimated")
    parser.add_argument("--tokens-total-source", help="provenance for --total-tokens, for example codex_goal, provider_usage, or runner_measured")


def run_usage(args) -> dict:
    usage = {
        "input_tokens": args.input_tokens,
        "cached_input_tokens": args.cached_input_tokens,
        "cache_creation_tokens": args.cache_creation_tokens,
        "cache_read_tokens": args.cache_read_tokens,
        "output_tokens": args.output_tokens,
        "reasoning_output_tokens": args.reasoning_output_tokens,
        "total_tokens": args.total_tokens,
        "tokens_delta": args.tokens_delta,
        "cost_usd": args.cost_usd,
        "source": args.usage_source,
        "confidence": args.usage_confidence,
    }
    if args.tokens_total_source:
        usage["tokens_total_source"] = args.tokens_total_source
    return {key: value for key, value in usage.items() if value is not None}


def slug(value: str, default: str = "run") -> str:
    normalized = re.sub(r"[^A-Za-z0-9_.:-]+", "-", value.strip()).strip("-")
    return normalized or default


def read_text_arg(value: str | None, path: str | None, label: str) -> str:
    if value and path:
        raise SystemExit(f"provide either --{label} or --{label}-file, not both")
    if path:
        return Path(path).read_text(encoding="utf-8").strip()
    return (value or "").strip()


def run_token_handoff(token: dict, harness_url: str, *, include_token: bool = True) -> str:
    lines = [
        "# Use the scorebench skill.",
        "# Send this whole block to the agent.",
        f"export SCOREBENCH_URL={harness_url}",
        f"export HARNESS_URL={harness_url}  # legacy CLI name",
    ]
    if include_token:
        lines.append(f"export SCOREBENCH_RUN_TOKEN={token['token']}")
        lines.append(f"export HARNESS_RUN_TOKEN={token['token']}  # legacy CLI name")
    else:
        lines.append(f"# SCOREBENCH_RUN_TOKEN={token.get('token_prefix', 'hrun_')}...")
    lines.extend(
        [
            "scorebench context",
            "scorebench exercise",
            "scorebench run current",
            "scorebench run ping --event start",
        ]
    )
    return "\n".join(lines)


def build_worker_prompt(token: dict, harness_url: str, goal: str, prompt: str = "") -> str:
    run_id = str(token.get("run_id") or token.get("run_name") or "")
    token_metadata = token.get("metadata") if isinstance(token.get("metadata"), dict) else {}
    skills = token_metadata.get("skills") or token.get("skills") or []
    if isinstance(skills, str):
        skills_text = skills
    elif isinstance(skills, list):
        skills_text = ",".join(str(skill) for skill in skills)
    else:
        skills_text = ""
    model = str(token_metadata.get("model") or token.get("model") or "")
    effort = str(token_metadata.get("effort") or token.get("effort") or "")
    autonomy = str(token_metadata.get("autonomy") or token.get("autonomy") or "")
    gpu = str(token_metadata.get("gpu") or token.get("gpu") or "")
    goal = goal.strip()
    if goal and not goal.lstrip().startswith("/goal"):
        goal = f"/goal {goal}"
    run_start = ["scorebench", "run", "start", "--id", run_id]
    if skills_text:
        run_start.extend(["--skills", shlex.quote(skills_text)])
    if model:
        run_start.extend(["--model", shlex.quote(model)])
    if effort:
        run_start.extend(["--effort", shlex.quote(effort)])
    if autonomy:
        run_start.extend(["--autonomy", shlex.quote(autonomy)])
    if gpu:
        run_start.extend(["--gpu", shlex.quote(gpu)])
    parts = [
        "Use the scorebench skill.",
        "",
        "ScoreBench assignment:",
        f"- SCOREBENCH_URL: {harness_url}",
        f"- SCOREBENCH_RUN_TOKEN: {token['token']}",
        "- legacy env names HARNESS_URL / HARNESS_RUN_TOKEN take the same values",
        f"- connector: {token.get('connector', '')}",
        f"- credential profile: {token.get('credential_name', '')}",
        f"- exercise: {token.get('exercise', '')}",
        f"- run id: {run_id}",
        f"- skills: {skills_text or '(not specified)'}",
        f"- model: {model or '(not specified)'}",
        f"- effort: {effort or '(not specified)'}",
        f"- autonomy: {autonomy or '(not specified)'}",
        (f"- gpu: {gpu}" if gpu else None),
        "",
        "Before solving, run:",
        "scorebench context",
        "scorebench exercise",
        " ".join(run_start),
        "scorebench run ping --event start --note \"starting work\"",
        "",
        goal,
    ]
    if prompt.strip():
        parts.extend(["", "Additional instructions:", prompt.strip()])
    return "\n".join(part for part in parts if part is not None).rstrip() + "\n"


def current_tmux_session() -> str | None:
    if not os.environ.get("TMUX"):
        return None
    completed = subprocess.run(
        ["tmux", "display-message", "-p", "#S"],
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        return None
    return completed.stdout.strip() or None


def build_agent_shell_command(
    *,
    cwd: Path,
    harness_url: str,
    run_token: str,
    agent_command: str,
    prompt_file: Path,
) -> str:
    command = agent_command.strip() or "codex"
    prompt_path = shlex.quote(str(prompt_file))
    if "{prompt_file}" in command:
        command = command.replace("{prompt_file}", shlex.quote(str(prompt_file)))
    elif "{prompt}" in command:
        command = command.replace("{prompt}", f"\"$(cat {prompt_path})\"")
    else:
        command = f"{command} \"$(cat {prompt_path})\""
    return " && ".join(
        [
            f"cd {shlex.quote(str(cwd))}",
            f"export SCOREBENCH_URL={shlex.quote(harness_url)}",
            f"export SCOREBENCH_RUN_TOKEN={shlex.quote(run_token)}",
            f"export HARNESS_URL={shlex.quote(harness_url)}",
            f"export HARNESS_RUN_TOKEN={shlex.quote(run_token)}",
            command,
        ]
    )


def launch_tmux_windows(
    jobs: list[dict],
    *,
    session: str | None,
    new_session: bool,
    dry_run: bool,
) -> None:
    if dry_run:
        return
    if not jobs:
        return
    if new_session:
        target = session or f"harness-{int(time.time())}"
        first, *rest = jobs
        subprocess.run(
            ["tmux", "new-session", "-d", "-s", target, "-n", first["window"], first["command"]],
            check=True,
        )
        for job in rest:
            subprocess.run(["tmux", "new-window", "-t", target, "-n", job["window"], job["command"]], check=True)
        return
    target = session or current_tmux_session()
    if not target:
        raise SystemExit("not inside tmux; pass --new-tmux-session or --tmux-session <name>")
    for job in jobs:
        subprocess.run(["tmux", "new-window", "-t", target, "-n", job["window"], job["command"]], check=True)


def create_admin_run_token(client: HarnessClient, payload: dict) -> dict:
    return client.post("/api/admin/run-tokens", payload, admin=True)


def save_admin_profile(profile_name: str, url: str, result: dict) -> None:
    config = load_admin_config()
    profiles = config.setdefault("profiles", {})
    profiles[profile_name] = {
        "url": url.rstrip("/"),
        "username": result["username"],
        "session_token": result["session_token"],
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "expires_in_seconds": result.get("expires_in_seconds"),
    }
    config["current_profile"] = profile_name
    save_admin_config(config)


def refresh_admin_profile_metadata(profile: HarnessAdminProfile, context: dict) -> None:
    admin_session = context.get("session") if isinstance(context.get("session"), dict) else {}
    if not admin_session:
        admin_session = context.get("admin_session") if isinstance(context.get("admin_session"), dict) else {}
    ttl = context.get("admin_session_ttl_seconds") or admin_session.get("expires_in_seconds")
    try:
        ttl_seconds = int(ttl)
    except (TypeError, ValueError):
        ttl_seconds = None

    config = load_admin_config()
    profiles = config.setdefault("profiles", {})
    record = profiles.get(profile.profile)
    if not isinstance(record, dict) or record.get("session_token") != profile.session_token:
        return

    changed = False
    if ttl_seconds is not None and record.get("expires_in_seconds") != ttl_seconds:
        record["expires_in_seconds"] = ttl_seconds
        changed = True
    record["last_validated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    changed = True
    if changed:
        save_admin_config(config)


def browser_admin_login(args) -> dict:
    client = HarnessClient(args.url)
    created = client.post("/api/admin/cli-login/start", {})
    authorize_url = str(created.get("authorize_url") or "")
    if not authorize_url:
        raise SystemExit("harness server did not return a CLI login authorization URL")

    print("Open this URL to authorize Harness CLI login:", file=sys.stderr)
    print(authorize_url, file=sys.stderr)
    if not args.no_browser:
        try:
            webbrowser.open(authorize_url)
        except Exception:
            print("Could not open a browser automatically; open the URL manually.", file=sys.stderr)

    timeout_seconds = max(1.0, float(args.timeout_seconds))
    poll_interval = max(
        0.25,
        float(args.poll_interval_seconds or created.get("poll_interval_seconds") or 2),
    )
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        result = client.post(
            "/api/admin/cli-login/poll",
            {
                "request_id": created["request_id"],
                "poll_secret": created["poll_secret"],
            },
        )
        status = str(result.get("status") or "")
        if status == "authorized" and result.get("session_token"):
            return result
        if status == "expired":
            raise SystemExit("browser login expired; rerun harness admin login")
        time.sleep(poll_interval)
    raise SystemExit("timed out waiting for browser login authorization")


def handle_admin_command(args) -> int:
    if args.admin_command == "login":
        password = args.password
        if args.password_stdin:
            password = input().rstrip("\n")
        if password is None:
            result = browser_admin_login(args)
        else:
            client = HarnessClient(args.url)
            result = client.post("/api/admin/login", {"username": args.username, "password": password})
        save_admin_profile(args.profile, args.url, result)
        print_json(
            {
                "ok": True,
                "profile": args.profile,
                "url": args.url.rstrip("/"),
                "username": result["username"],
                "config": str(admin_config_path()),
            }
        )
        return 0

    if args.admin_command == "logout":
        config = load_admin_config()
        profile_name = args.profile or str(config.get("current_profile") or "default")
        profiles = config.setdefault("profiles", {})
        profiles.pop(profile_name, None)
        if config.get("current_profile") == profile_name:
            config["current_profile"] = next(iter(profiles), "default")
        save_admin_config(config)
        print_json({"ok": True, "removed_profile": profile_name})
        return 0

    client, profile = make_admin_client(getattr(args, "profile", None))

    if args.admin_command == "whoami":
        context = client.get("/api/admin/context", admin=True)
        refresh_admin_profile_metadata(profile, context)
        print_json(
            {
                "profile": profile.profile,
                "url": profile.url,
                "username": context["username"],
                "admin_session": context.get("admin_session", {}),
                "credentials": [
                    {
                        "connector": record.get("connector"),
                        "name": record.get("name"),
                        "status": record.get("status"),
                        "expires_at": record.get("expires_at"),
                    }
                    for record in context.get("credential_records", [])
                ],
                "run_tokens": len(context.get("run_tokens", [])),
            }
        )
        return 0

    if args.admin_command == "create-run-token":
        payload = {
            "connector": args.connector,
            "credential_name": args.credential_name,
            "exercise": args.exercise,
            "run_id": args.run_id or "",
            "run_name": args.run_name or "",
            "strategy": args.strategy,
            "hypothesis": args.hypothesis,
            "notes": args.notes,
        }
        if args.skills:
            payload["skills"] = args.skills
        if args.model:
            payload["model"] = args.model
        if args.effort:
            payload["effort"] = args.effort
        if args.autonomy:
            payload["autonomy"] = args.autonomy
        if args.gpu:
            payload["gpu"] = args.gpu
        if args.user_name:
            payload["user_name"] = args.user_name
        created = create_admin_run_token(client, payload)
        if args.json:
            print_json(created)
        else:
            print(run_token_handoff(created, profile.url))
        return 0

    if args.admin_command == "launch":
        if args.count <= 0:
            raise SystemExit("--count must be positive")
        goal = read_text_arg(args.goal, args.goal_file, "goal")
        prompt = read_text_arg(args.prompt, args.prompt_file, "prompt")
        if not goal:
            raise SystemExit("harness admin launch requires --goal or --goal-file")
        root = Path(args.workspace_root or Path.cwd() / ".harness" / "agent-runs" / str(int(time.time())))
        root.mkdir(parents=True, exist_ok=True)
        jobs: list[dict] = []
        tokens: list[dict] = []
        for offset in range(args.count):
            seq = args.start_index + offset
            run_id = f"{slug(args.run_prefix)}{seq:03d}"
            run_dir = root / run_id
            run_dir.mkdir(parents=True, exist_ok=True)
            token = create_admin_run_token(
                client,
                {
                    "connector": args.connector,
                    "credential_name": args.credential_name,
                    "exercise": args.exercise,
                    "run_id": run_id,
                    "run_name": run_id,
                    "strategy": args.strategy or args.run_prefix,
                    "hypothesis": args.hypothesis,
                    "notes": args.notes,
                    "skills": args.skills,
                    "model": args.model,
                    "effort": args.effort,
                    "autonomy": args.autonomy,
                    "gpu": args.gpu,
                },
            )
            prompt_file = run_dir / "prompt.md"
            prompt_file.write_text(build_worker_prompt(token, profile.url, goal, prompt), encoding="utf-8")
            try:
                prompt_file.chmod(0o600)
            except OSError:
                pass
            command = build_agent_shell_command(
                cwd=run_dir,
                harness_url=profile.url,
                run_token=token["token"],
                agent_command=args.agent_command,
                prompt_file=prompt_file,
            )
            job = {
                "run_id": run_id,
                "window": run_id,
                "cwd": str(run_dir),
                "prompt_file": str(prompt_file),
                "command": command,
                "token": token,
            }
            jobs.append(job)
            tokens.append(token)
        manifest = {
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "harness_url": profile.url,
            "connector": args.connector,
            "credential_name": args.credential_name,
            "exercise": args.exercise,
            "tmux_session": args.tmux_session or ("new" if args.new_tmux_session else current_tmux_session()),
            "new_tmux_session": bool(args.new_tmux_session),
            "dry_run": bool(args.dry_run),
            "jobs": jobs,
        }
        (root / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        try:
            (root / "manifest.json").chmod(0o600)
        except OSError:
            pass
        launch_tmux_windows(jobs, session=args.tmux_session, new_session=args.new_tmux_session, dry_run=args.dry_run)
        if args.json:
            print_json(manifest)
        else:
            print(f"created {len(jobs)} harness run key(s)")
            print(f"workspace root: {root}")
            if args.dry_run:
                print("dry run: tmux windows were not started")
            for job in jobs:
                print(f"{job['run_id']}: {job['prompt_file']}")
        return 0

    raise SystemExit(f"unknown admin command: {args.admin_command}")


def _run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="harness")
    sub = parser.add_subparsers(dest="command", required=True)
    admin = sub.add_parser("admin", help="Harness user login, run-token creation, and tmux launch helpers")
    admin_sub = admin.add_subparsers(dest="admin_command", required=True)
    admin_login = admin_sub.add_parser("login", help="log the local CLI into a Harness user account")
    admin_login.add_argument(
        "--url",
        default=_env_first("SCOREBENCH_URL", "HARNESS_URL") or PUBLIC_HARNESS_URL,
    )
    admin_login.add_argument("--username", "-u", default=_env_first("SCOREBENCH_USERNAME", "HARNESS_USERNAME") or "admin")
    admin_login.add_argument("--password", help="password; skips browser-link login")
    admin_login.add_argument("--password-stdin", action="store_true", help="read the password from stdin and skip browser-link login")
    admin_login.add_argument("--profile", default="default", help="local CLI profile name")
    admin_login.add_argument("--no-browser", action="store_true", help="print the browser login URL but do not open it")
    admin_login.add_argument("--timeout-seconds", type=float, default=600.0, help="seconds to wait for browser authorization")
    admin_login.add_argument("--poll-interval-seconds", type=float, default=2.0, help="seconds between browser authorization polls")
    admin_sub.add_parser("whoami", help="show the logged-in Harness user scope")
    admin_logout = admin_sub.add_parser("logout", help="remove a local Harness CLI profile")
    admin_logout.add_argument("--profile", default=None)
    admin_token = admin_sub.add_parser("create-run-token", help="create one scoped exercise/run API key")
    admin_token.add_argument("--profile", default=None)
    admin_token.add_argument("--user", dest="user_name", help="user owner; only honored for master-token profiles")
    admin_token.add_argument("--connector", required=True)
    admin_token.add_argument("--credential", "--credential-profile", dest="credential_name", required=True)
    admin_token.add_argument("--exercise", "--problem", dest="exercise", required=True)
    admin_token.add_argument("--run-id", "--id", dest="run_id")
    admin_token.add_argument("--run-name", "--label", dest="run_name")
    admin_token.add_argument("--strategy", default="")
    admin_token.add_argument("--hypothesis", default="")
    admin_token.add_argument("--notes", default="")
    admin_token.add_argument("--skills", help="comma-separated skills expected for this run, e.g. scorebench,problem-agnostic-optimization")
    admin_token.add_argument("--model", help="model expected for this run, e.g. gpt-5-codex or claude-sonnet")
    admin_token.add_argument("--effort", help="reasoning/effort level expected for this run, e.g. low, medium, high, max")
    admin_token.add_argument("--autonomy", choices=["autonomous", "steered", "mixed"], help="whether the run is fully autonomous, human steered, or mixed")
    admin_token.add_argument("--gpu", help="GPU this run targets on GPU-backed connectors (tensara/GPU Mode), e.g. T4 or H100; submissions in the run are pinned to it")
    admin_token.add_argument("--json", action="store_true", help="print JSON instead of a handoff block")
    admin_launch = admin_sub.add_parser("launch", help="create N run-scoped keys and start agents in tmux")
    admin_launch.add_argument("--profile", default=None)
    admin_launch.add_argument("--connector", required=True)
    admin_launch.add_argument("--credential", "--credential-profile", dest="credential_name", required=True)
    admin_launch.add_argument("--exercise", "--problem", dest="exercise", required=True)
    admin_launch.add_argument("--count", type=int, required=True)
    admin_launch.add_argument("--run-prefix", default="run")
    admin_launch.add_argument("--start-index", type=int, default=1)
    admin_launch.add_argument("--strategy", default="")
    admin_launch.add_argument("--hypothesis", default="")
    admin_launch.add_argument("--notes", default="")
    admin_launch.add_argument("--skills", help="comma-separated skills expected for each launched run")
    admin_launch.add_argument("--model", help="model expected for each launched run")
    admin_launch.add_argument("--effort", help="reasoning/effort level expected for each launched run")
    admin_launch.add_argument("--autonomy", choices=["autonomous", "steered", "mixed"], help="whether launched runs are fully autonomous, human steered, or mixed")
    admin_launch.add_argument("--gpu", help="GPU each launched run targets on GPU-backed connectors (tensara/GPU Mode), e.g. T4 or H100")
    admin_launch.add_argument("--goal", help="goal text; wrapped as a /goal if needed")
    admin_launch.add_argument("--goal-file", help="file containing goal text")
    admin_launch.add_argument("--prompt", help="additional prompt text")
    admin_launch.add_argument("--prompt-file", help="file containing additional prompt text")
    admin_launch.add_argument("--agent-command", default="codex", help="agent command; {prompt_file} placeholder is supported")
    admin_launch.add_argument("--workspace-root", default=None, help="root for isolated per-run working directories")
    admin_launch.add_argument("--tmux-session", help="target tmux session")
    admin_launch.add_argument("--new-tmux-session", action="store_true", help="create a new detached tmux session")
    admin_launch.add_argument("--dry-run", action="store_true", help="create keys and prompt files but do not start tmux windows")
    admin_launch.add_argument("--json", action="store_true", help="print JSON details")
    exercise = sub.add_parser("exercise")
    exercise.add_argument("--exercise", "--problem", dest="exercise", help="connector exercise/problem id; defaults to the run config")
    run = sub.add_parser("run")
    run_sub = run.add_subparsers(dest="run_command", required=True)
    run_start = run_sub.add_parser("start")
    run_start.add_argument("--id", "--run-id", dest="run_id", help="stable run id such as run001 or mr-mont-v1")
    run_start.add_argument("--label", help="short human-readable run name; defaults to --id")
    run_start.add_argument("--strategy", help="strategy or treatment this run is testing")
    run_start.add_argument("--hypothesis", help="specific hypothesis for this run")
    run_start.add_argument("--notes", default="", help="optional setup notes")
    run_start.add_argument("--skills", help="comma-separated skills used by this run, e.g. scorebench,problem-agnostic-optimization")
    run_start.add_argument("--model", help="REQUIRED: model used by this run (or inherit from a pre-bound exercise API key), e.g. claude-fable-5 or gpt-5-codex")
    run_start.add_argument("--effort", help="REQUIRED: reasoning/effort level for this run (or inherit from a pre-bound exercise API key), e.g. low, medium, high, max")
    run_start.add_argument("--autonomy", choices=["autonomous", "steered", "mixed"], help="whether this run is fully autonomous, human steered, or mixed")
    run_start.add_argument("--gpu", help="GPU this run targets on GPU-backed connectors (tensara/GPU Mode), e.g. T4 or H100; submissions in the run are pinned to it")
    run_start.add_argument("--confirm-new-run", action="store_true", help="explicitly start a new independent run when the harness finds an existing run for this profile/exercise")
    run_start.add_argument("--metadata-json", help="JSON object with extra run metadata")
    run_start.add_argument("--meta", action="append", default=[], help="extra run metadata as key=value; may be repeated")
    run_sub.add_parser("current")
    run_ping = run_sub.add_parser("ping")
    run_ping.add_argument("--event", default="heartbeat", help="start, resume, heartbeat, or another short event label")
    run_ping.add_argument("--note", "--notes", dest="note", default="", help="optional ping note")
    run_ping.add_argument("--metadata-json", help="JSON object with extra ping metadata")
    run_ping.add_argument("--meta", action="append", default=[], help="extra ping metadata as key=value; may be repeated")
    run_usage_parser = run_sub.add_parser("usage", help="record final or current run-level token usage for reports")
    add_usage_arguments(run_usage_parser, require_total=True, include_breakdown=True)
    run_usage_parser.add_argument("--note", "--notes", dest="note", default="recording run token usage", help="optional usage note")
    submit = sub.add_parser("submit")
    submit.add_argument("path")
    submit.add_argument("--exercise", "--problem", dest="exercise", help="connector exercise/problem id; defaults to the run config")
    submit.add_argument("--language", "--lang", dest="language", help="connector language override for this submission")
    submit.add_argument("--compiler", help="connector compiler id for this submission, e.g. gcc_cpp for CPU.mode")
    submit.add_argument("--compiler-options", "--compiler-args", dest="compiler_options", help="connector compiler options for this submission")
    submit.add_argument("--system", help="connector target system override, e.g. raptor_cove_p for CPU.mode")
    submit.add_argument("--solution-file", help="connector solution file path inside the submitted bundle")
    submit.add_argument("--gpu", help="GPU override for GPU-backed connectors, e.g. B200 for GPU Mode or T4/H100 for tensara; must match the run's declared gpu when one was set")
    submit.add_argument("--leaderboard", help="GPU Mode leaderboard override; defaults to the scoped exercise")
    submit.add_argument("--submission-mode", "--mode", dest="submission_mode", help="GPU Mode submit mode: test, benchmark, leaderboard, or profile")
    submit.add_argument("--profile-brev", action="store_true", help="GPU Mode profile with B200 Brev through popcorn --profile-brev")
    submit.add_argument("--benchmark-index", type=int, help="GPU Mode benchmark index for --profile-brev")
    submit.add_argument("--notes", default="")
    submit.add_argument("--label", help="short candidate label for deterministic progress TSVs")
    add_usage_arguments(submit, require_total=True)
    submit.add_argument("--idempotency-key", help="stable key to make retries return the original candidate instead of resubmitting")
    submit.add_argument("--metadata-json", help="JSON object with extra candidate metadata")
    submit.add_argument("--meta", action="append", default=[], help="extra candidate metadata as key=value; may be repeated")
    sub.add_parser("best")
    sub.add_parser("history")
    refresh = sub.add_parser("refresh")
    refresh.add_argument("candidate_id", nargs="?", help="candidate id to refresh; defaults to latest refreshable submitted candidate")
    invalidate = sub.add_parser("invalidate", help="mark one of this run's candidates invalid without deleting evidence")
    invalidate.add_argument("candidate_id", nargs="?", help="candidate id to invalidate; defaults to the latest visible candidate")
    invalidate.add_argument("--reason", required=True, help="why the candidate should not count, e.g. exploit, bug, or invalid assumption")
    invalidate.add_argument("--metadata-json", help="JSON object with extra invalidation metadata")
    invalidate.add_argument("--meta", action="append", default=[], help="extra invalidation metadata as key=value; may be repeated")
    solution = sub.add_parser("solution")
    solution.add_argument("solution_id", help="connector solution id to fetch through the harness")
    solution.add_argument("--exercise", "--problem", dest="exercise", help="connector exercise/problem id; defaults to the run config")
    solution.add_argument("--no-code", action="store_true", help="omit source code when the connector supports it, e.g. popcorn submissions show --no-code")
    solutions = sub.add_parser("solutions", help="list connector-visible solutions for the scoped exercise")
    solutions.add_argument("--exercise", "--problem", dest="exercise", help="connector exercise/problem id; defaults to the scoped exercise")
    solutions.add_argument("--page")
    solutions.add_argument("--sort")
    solutions.add_argument("--order")
    solutions.add_argument("--status")
    solutions.add_argument("--mine")
    solutions.add_argument("--lang", "--language", dest="lang")
    solutions.add_argument("--raw", action="store_true", help="include raw connector HTML when supported")
    leaderboard = sub.add_parser("leaderboard", help="read the connector leaderboard for the scoped exercise")
    leaderboard.add_argument("--exercise", "--problem", dest="exercise", help="connector exercise/problem id; defaults to the scoped exercise")
    leaderboard.add_argument("--raw", action="store_true", help="include raw connector HTML when supported")
    inspect_solution = sub.add_parser("inspect-solution", help="read connector-visible details for any public/visible solution id")
    inspect_solution.add_argument("solution_id")
    inspect_solution.add_argument("--exercise", "--problem", dest="exercise", help="connector exercise/problem id; defaults to the scoped exercise")
    inspect_solution.add_argument("--include-source", action="store_true", help="include source files when the connector/account can see them")
    inspect_solution.add_argument("--raw", action="store_true", help="include raw connector HTML when supported")
    challenge_page = sub.add_parser("challenge-page", help="read a connector challenge page section such as leaderboard or generators")
    challenge_page.add_argument("section")
    challenge_page.add_argument("--exercise", "--problem", dest="exercise", help="connector exercise/problem id; defaults to the scoped exercise")
    challenge_page.add_argument("--raw", action="store_true", help="include raw connector HTML when supported")
    solve_form = sub.add_parser("solve-form", help="read connector solve-form defaults such as compiler and compiler flags")
    solve_form.add_argument("--exercise", "--problem", dest="exercise", help="connector exercise/problem id; defaults to the scoped exercise")
    solve_form.add_argument("--language", "--lang", dest="language")
    solve_form.add_argument("--raw", action="store_true", help="include raw connector HTML when supported")
    sub.add_parser("context")
    args = parser.parse_args(argv)
    if args.command == "admin":
        return handle_admin_command(args)
    context = load_cli_context()
    client = HarnessClient(context.url, arm_token=context.token)

    if args.command == "exercise":
        params = {}
        if args.exercise:
            params["exercise"] = args.exercise
        print_json(client.get("/exercise", **params))
    elif args.command == "run":
        if args.run_command == "start":
            payload = {
                "notes": args.notes,
                "confirm_new_run": args.confirm_new_run,
            }
            if args.run_id:
                payload["run_id"] = args.run_id
            if args.label:
                payload["label"] = args.label
            if args.strategy:
                payload["strategy"] = args.strategy
            if args.hypothesis:
                payload["hypothesis"] = args.hypothesis
            if args.skills:
                payload["skills"] = args.skills
            if args.model:
                payload["model"] = args.model
            if args.effort:
                payload["effort"] = args.effort
            if args.autonomy:
                payload["autonomy"] = args.autonomy
            if args.gpu:
                payload["gpu"] = args.gpu
            metadata = parse_metadata(args.metadata_json, args.meta)
            if metadata:
                payload["metadata"] = metadata
            print_json(client.post("/run/start", payload))
        elif args.run_command == "current":
            print_json(client.get("/run/current"))
        elif args.run_command == "ping":
            payload = {
                "event": args.event,
                "note": args.note,
            }
            metadata = parse_metadata(args.metadata_json, args.meta)
            if metadata:
                payload["metadata"] = metadata
            print_json(client.post("/run/ping", payload))
        elif args.run_command == "usage":
            payload = {
                "event": "usage",
                "note": args.note,
                "metadata": {"usage": run_usage(args)},
            }
            print_json(client.post("/run/ping", payload))
    elif args.command == "submit":
        if args.total_tokens is None:
            raise SystemExit("harness submit requires --total-tokens so token expenditure can be recorded")
        current_run = client.get("/run/current")
        bundle = create_bundle(Path(args.path))
        payload = {
            "bundle_b64": base64.b64encode(bundle.data).decode("ascii"),
            "bundle_sha256": bundle.sha256,
            "manifest": bundle.manifest,
            "notes": args.notes,
            "run_id": current_run["run"]["run_id"],
        }
        if args.exercise:
            payload["exercise"] = args.exercise
        connector_options = {}
        if args.language:
            connector_options["language"] = args.language
        if args.compiler:
            connector_options["compiler"] = args.compiler
        if args.compiler_options:
            connector_options["compiler_options"] = args.compiler_options
        if args.system:
            connector_options["system"] = args.system
        if args.solution_file:
            connector_options["solution_file"] = args.solution_file
        if args.gpu:
            connector_options["gpu"] = args.gpu
        if args.leaderboard:
            connector_options["leaderboard"] = args.leaderboard
        if args.submission_mode:
            connector_options["submission_mode"] = args.submission_mode
        if args.profile_brev:
            connector_options["profile_brev"] = "true"
        if args.benchmark_index is not None:
            connector_options["benchmark_index"] = str(args.benchmark_index)
        if connector_options:
            payload["connector_options"] = connector_options
        if args.label:
            payload["label"] = args.label
        if args.idempotency_key:
            payload["idempotency_key"] = args.idempotency_key
        usage = submit_usage(args)
        if usage:
            payload["usage"] = usage
        metadata = parse_metadata(args.metadata_json, args.meta)
        if metadata:
            payload["metadata"] = metadata
        print_json(client.post("/submit", payload))
    elif args.command == "best":
        print_json(client.get("/best"))
    elif args.command == "history":
        print_json(client.get("/history"))
    elif args.command == "refresh":
        payload = {}
        if args.candidate_id:
            payload["candidate_id"] = args.candidate_id
        print_json(client.post("/refresh", payload))
    elif args.command == "invalidate":
        payload = {"reason": args.reason}
        if args.candidate_id:
            payload["candidate_id"] = args.candidate_id
        metadata = parse_metadata(args.metadata_json, args.meta)
        if metadata:
            payload["metadata"] = metadata
        print_json(client.post("/invalidate", payload))
    elif args.command == "solution":
        params = {"solution_id": args.solution_id}
        if args.exercise:
            params["exercise"] = args.exercise
        if args.no_code:
            params["no_code"] = "1"
        print_json(client.get("/solution", **params))
    elif args.command == "solutions":
        params = {}
        for key in ("exercise", "page", "sort", "order", "status", "mine", "lang"):
            value = getattr(args, key)
            if value:
                params[key] = value
        if args.raw:
            params["raw"] = "1"
        print_json(client.get("/solutions", **params))
    elif args.command == "leaderboard":
        params = {}
        if args.exercise:
            params["exercise"] = args.exercise
        if args.raw:
            params["raw"] = "1"
        print_json(client.get("/leaderboard", **params))
    elif args.command == "inspect-solution":
        params = {"solution_id": args.solution_id}
        if args.exercise:
            params["exercise"] = args.exercise
        if args.include_source:
            params["include_source"] = "1"
        if args.raw:
            params["raw"] = "1"
        print_json(client.get("/public-solution", **params))
    elif args.command == "challenge-page":
        params = {"section": args.section}
        if args.exercise:
            params["exercise"] = args.exercise
        if args.raw:
            params["raw"] = "1"
        print_json(client.get("/challenge-page", **params))
    elif args.command == "solve-form":
        params = {}
        if args.exercise:
            params["exercise"] = args.exercise
        if args.language:
            params["language"] = args.language
        if args.raw:
            params["raw"] = "1"
        print_json(client.get("/solve-form", **params))
    elif args.command == "context":
        payload = {
            "url": context.url,
            "arm": context.arm,
            "run_dir": context.run_dir,
            "experiment_id": context.experiment_id,
            "source": context.source,
            "token": "present",
        }
        server_ok = True
        try:
            payload["server"] = client.get("/context")
        except RuntimeError as exc:
            payload["server_error"] = str(exc)
            server_ok = False
        print_json(payload)
        return 0 if server_ok else 1
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        return _run(argv)
    except SystemExit:
        raise
    except KeyboardInterrupt:
        print("harness: interrupted", file=sys.stderr)
        return 130
    except (RuntimeError, ConnectionError, TimeoutError, OSError, json.JSONDecodeError, PermissionError) as exc:
        print(f"harness: error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
