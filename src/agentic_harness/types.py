from dataclasses import asdict, dataclass, field
from typing import Any, Literal

Value = Any


@dataclass
class Score:
    value: Value
    explanation: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Text:
    text: str


@dataclass(frozen=True)
class File:
    path: str
    media_type: str


@dataclass(frozen=True)
class Data:
    data: Any


Content = Text | File | Data


@dataclass(frozen=True)
class Message:
    role: Literal["user", "agent"]
    content: tuple[Content, ...]


Transcript = tuple[Message, ...]


@dataclass(frozen=True)
class Task:
    """Adapters subclass it, so these fields have no defaults (workdir is kw_only)."""

    benchmark: str
    id: str
    image: str
    workdir: str = field(default="/workspace", kw_only=True)


@dataclass(frozen=True)
class McpServer:
    name: str
    image: str
    command: tuple[str, ...]
    port: int

    @property
    def url(self) -> str:
        return f"http://localhost:{self.port}/mcp"


@dataclass(frozen=True)
class ReferenceSolution:
    source: Literal["benchmark-provided", "harness-written"]
    files: dict[str, str] = field(default_factory=dict)
    script: str = ""
    cwd: str | None = None


@dataclass(frozen=True)
class OriginalResult:
    score: Score
    cost_usd: float = 0.0
    agent_exit_status: str | None = None


@dataclass(frozen=True)
class Limits:
    trial_timeout_s: float = 600
    cost_usd: float = 1.0
    max_steps: int = 50
    command_timeout_s: float = 60
    max_turns: int = 20
    memory: str = "2g"
    cpus: float = 1.0
    pids_limit: int = 512
    grade_timeout_s: float = 120


@dataclass(frozen=True)
class LimitOverrides:
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
    values = asdict(defaults)
    sources: dict[str, LimitSource] = dict.fromkeys(values, "default")
    for source, layer in (("benchmark", declared), ("flag", flags)):
        for name, value in layer.given().items():
            values[name], sources[name] = value, source
    return Limits(**values), sources


@dataclass(frozen=True)
class Outcome:
    status: Literal["graded", "grader_error", "infra_error", "unsupported"]
    score: Score | None = None
    score_float: float | None = None
    error: str | None = None
    retryable: bool = False
    agent_exit_status: str | None = None
    cost_usd: float = 0.0
    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    duration_s: float = 0.0
    turns: int = 0
    phases: dict[str, float] = field(default_factory=dict)
    limits: dict[str, dict[str, Any]] = field(default_factory=dict)


def score_to_float(value: Value) -> float:
    if value is True or value == "C":
        return 1.0
    if value == "P":
        return 0.5
    if value is False or value == "I" or value == "N":
        return 0.0
    if isinstance(value, int | float):
        return float(value)
    raise ValueError(f"no default conversion to a number for score value {value!r}")
