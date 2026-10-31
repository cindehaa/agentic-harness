"""Shared data types for the agentic harness."""

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

Value = Any  # what a grader returns; must be JSON-serializable, e.g. True, 0.75, "C", {"passed": True}


@dataclass
class Score:
    """A benchmark's verdict on one trial."""

    value: Value
    explanation: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


# --- Message content: the pieces a message is made of (A2A calls these parts) ---


@dataclass(frozen=True)
class Text:
    text: str


@dataclass(frozen=True)
class File:
    path: str  # where the file lives in the world (inside the container), e.g. "/workspace/shot.png"
    media_type: str  # MIME type, e.g. "image/png", "application/pdf"


@dataclass(frozen=True)
class Data:
    data: Any  # any JSON-serializable value (frozen stops reassignment, not mutation of a dict inside)


Content = Text | File | Data


@dataclass(frozen=True)
class Message:
    """One message exchanged between the benchmark and the agent. Benchmarks send "user" messages; agents reply as "agent"."""

    role: Literal["user", "agent"]
    content: tuple[Content, ...]


# Every message of a trial's turns, in order; grade() gets a read-only snapshot.
# The agent's individual commands are in events.jsonl, not here.
Transcript = tuple[Message, ...]


@dataclass(frozen=True)
class Task:
    """One task from a benchmark. Adapters subclass it with their own fields, so these have no defaults (otherwise
    every subclass field would need one too); `workdir` is keyword-only, which keeps that rule out of its way."""

    benchmark: str  # e.g. "bash-operations"; makes a Task self-describing in logs and the DB
    id: str  # e.g. "bash-001"
    image: str  # Docker image for the world (prebuilt by the benchmark, or built in Benchmark.setup())
    # The world's working directory: the trial's workspace volume is mounted here (starting with the image's own
    # files at that path), commands run here by default, agents start here, and workspace.tar is taken from here
    workdir: str = field(default="/workspace", kw_only=True)


@dataclass(frozen=True)
class McpServer:
    """An MCP server a benchmark serves to the agent (its benchmark-specific actions), run as a sidecar on the trial's
    network and reached over streamable HTTP at `url` from inside the agent's world."""

    name: str  # what the agent knows it by, e.g. "airline"
    image: str
    command: tuple[str, ...]  # starts the server, listening on `port`
    port: int

    @property
    def url(self) -> str:
        return f"http://localhost:{self.port}/mcp"


@dataclass(frozen=True)
class ReferenceSolution:
    """A known-good answer to one task: what `harness validate`'s oracle agent applies, in the world, instead of an
    agent's work. If the adapter is wired right, the grader then passes it."""

    # who wrote it: the benchmark itself (independent of the adapter), or us (it checks the wiring, not the benchmark)
    source: Literal["benchmark-provided", "harness-written"]
    files: dict[str, str] = field(default_factory=dict)  # written into the world first: path -> text
    script: str = ""  # then run in the world through `sh -c`
    cwd: str | None = None  # where the script runs; None: the world's working directory


@dataclass(frozen=True)
class OriginalResult:
    """One task run the benchmark's original way (Benchmark.original_workflow), for `harness parity`."""

    score: Score
    cost_usd: float = 0.0
    agent_exit_status: str | None = None


@dataclass(frozen=True)
class Limits:
    """What one trial may spend."""

    trial_timeout_s: float = 600  # the first agent command after this deadline ends the agent's run
    cost_usd: float = 1.0  # model spend per trial
    max_steps: int = 50  # model calls per trial
    command_timeout_s: float = 60  # one agent command
    max_turns: int = 20  # benchmark -> agent messages per trial (a looping benchmark can't run forever)
    # per container (docker run --memory / --cpus / --pids-limit); going over the memory limit fails the trial
    memory: str = "2g"
    cpus: float = 1.0
    pids_limit: int = 512  # processes; stops a fork bomb from taking the host down
    grade_timeout_s: float = 120  # all of grade(): no grading command runs past it


@dataclass(frozen=True)
class LimitOverrides:
    """Some of the Limits, each None unless set: what a benchmark declares for a task (Benchmark.limits), or the
    flags a run was given explicitly. Only Limits' fields exist here, so operator controls (--max-running,
    --concurrency, --max-run-cost) can't be set by a benchmark."""

    trial_timeout_s: float | None = None
    cost_usd: float | None = None
    max_steps: int | None = None
    command_timeout_s: float | None = None
    max_turns: int | None = None
    memory: str | None = None
    cpus: float | None = None
    pids_limit: int | None = None
    grade_timeout_s: float | None = None

    def given(self) -> dict[str, Any]:
        return {name: value for name, value in asdict(self).items() if value is not None}


LimitSource = Literal["default", "benchmark", "flag"]


def effective_limits(defaults: Limits, declared: LimitOverrides, flags: LimitOverrides) -> tuple[Limits, dict[str, LimitSource]]:
    """A trial's limits, layered: harness defaults < what the benchmark declares for the task < explicit run flags.
    Also where each one came from."""
    values = asdict(defaults)
    sources: dict[str, LimitSource] = dict.fromkeys(values, "default")
    for source, layer in (("benchmark", declared), ("flag", flags)):
        for name, value in layer.given().items():
            values[name], sources[name] = value, source
    return Limits(**values), sources


@dataclass(frozen=True)
class Outcome:
    """How one attempt at one trial ended. Accuracy counts only "graded"."""

    status: Literal["graded", "grader_error", "infra_error", "unsupported"]
    score: Score | None = None
    score_float: float | None = None  # what accuracy averages; from Benchmark.score_to_float
    error: str | None = None  # why it isn't graded (or the traceback)
    retryable: bool = False  # infra_error only: a transient failure (Docker, network, provider overloaded) worth another attempt
    agent_exit_status: str | None = None  # the agent's own reason for stopping, e.g. "Submitted", "TimeLimit"
    cost_usd: float = 0.0  # what the agent spent, also when it crashed or ran out of time
    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    duration_s: float = 0.0
    turns: int = 0
    # when each phase began or ended (time.time()): env_ready, agent_start, agent_end, grade_start, grade_end; a phase
    # the attempt never reached is missing
    phases: dict[str, float] = field(default_factory=dict)
    # the limits this attempt ran under: name -> {"value", "source": default | benchmark | flag}, plus
    # "enforced": False for one the agent can't keep (e.g. a step limit for an agent that doesn't count steps)
    limits: dict[str, dict[str, Any]] = field(default_factory=dict)


def score_to_float(value: Value) -> float:
    """Inspect-style default: True/"C" -> 1, "P" (partial) -> 0.5, False/"I"/"N" -> 0, numbers as they are."""
    if value is True or value == "C":
        return 1.0
    if value == "P":
        return 0.5
    if value is False or value == "I" or value == "N":
        return 0.0
    if isinstance(value, int | float):
        return float(value)
    raise ValueError(f"no default conversion to a number for score value {value!r}")
