import logging
import subprocess
from pathlib import Path

from agentic_harness.environments import docker
from agentic_harness.types import Limits

SCRIPT_TIMEOUT_S = 600

log = logging.getLogger("harness.original")  # else mini's environment logger prints every docker command


def run_script(image: str, mounts: dict[Path, str], argv: list[str]) -> subprocess.CompletedProcess:
    volumes = [a for local, remote in mounts.items() for a in ("-v", f"{local.resolve()}:{remote}")]
    return subprocess.run(["docker", "run", "--rm", "--label", docker.LABEL, *volumes, image, *argv],
                          capture_output=True, text=True, timeout=SCRIPT_TIMEOUT_S)


class OriginalAgentWorld:
    def __init__(self, agent_name: str, model: str, image: str, mounts: dict[Path, str], cwd: str, out_dir: Path,
                 limits: Limits = Limits()) -> None:
        if agent_name != "mini-swe-agent":
            raise NotImplementedError(f"no original workflow for agent {agent_name!r} (only mini-swe-agent's Docker mode)")
        self.model, self.image, self.mounts, self.cwd, self.out_dir, self.limits = model, image, mounts, cwd, out_dir, limits
        self.cost = 0.0
        self.exit_status: str | None = None

    def __enter__(self) -> "OriginalAgentWorld":
        from minisweagent.config import get_config_from_spec
        from minisweagent.environments.docker import DockerEnvironment

        self.config = get_config_from_spec("mini.yaml")
        volumes = [a for local, remote in self.mounts.items() for a in ("-v", f"{local.resolve()}:{remote}")]
        self.env = DockerEnvironment(
            image=self.image, cwd=self.cwd, env=self.config.get("environment", {}).get("env", {}),
            timeout=int(self.limits.command_timeout_s), run_args=["--rm", "--label", docker.LABEL, *volumes],
            interpreter=["bash", "-lc"], logger=log,
        )
        # alpine has no bash: use sh, as mini's docs say to configure
        if self.exec(["sh", "-c", "command -v bash"]).returncode != 0:
            self.env.config.interpreter = ["sh", "-lc"]
        return self

    def exec(self, argv: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(["docker", "exec", "-w", self.cwd, self.env.container_id, *argv],
                              capture_output=True, text=True, timeout=SCRIPT_TIMEOUT_S)

    def copy_in(self, local: Path, remote: str) -> None:
        subprocess.run(["docker", "cp", f"{local}/.", f"{self.env.container_id}:{remote}"], check=True, capture_output=True)

    def run_agent(self, task: str) -> None:
        from minisweagent.agents.default import DefaultAgent
        from minisweagent.models import get_model

        agent = DefaultAgent(get_model(self.model, self.config["model"]), self.env, **self.config["agent"] | {
            "step_limit": self.limits.max_steps, "cost_limit": self.limits.cost_usd,
            "output_path": self.out_dir / "trajectory.json",
        })
        try:
            self.exit_status = agent.run(task).get("exit_status")
        finally:
            self.cost = agent.cost

    def __exit__(self, *exc_info) -> None:
        if getattr(self, "env", None) and self.env.container_id:  # synchronous: mini's own cleanup() runs in the background
            subprocess.run(["docker", "rm", "-f", self.env.container_id], capture_output=True)
