from datetime import datetime
from enum import StrEnum
from ipaddress import ip_address
from typing import Annotated
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    StringConstraints,
    model_validator,
)

JobName = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=100,
        pattern=r"^[^\x00-\x1f\x7f]+$",
    ),
]


class JobType(StrEnum):
    SLEEP = "sleep"
    DATASET_SCRIPT = "dataset_script"
    PYTHON_BATCH = "python_batch"


class JobStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class SleepParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    seconds: int = Field(ge=1, le=30)


class ScriptName(StrEnum):
    CSV_SUMMARY = "csv_summary"


class DatasetReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: HttpUrl
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=1, le=100 * 1024**3)

    @model_validator(mode="after")
    def require_safe_https_source(self) -> "DatasetReference":
        if self.url.scheme != "https":
            raise ValueError("dataset URL must use HTTPS")
        hostname = self.url.host
        if hostname is None or hostname.lower() == "localhost":
            raise ValueError("dataset URL must have a public hostname")
        try:
            address = ip_address(hostname)
        except ValueError:
            return self
        if not address.is_global:
            raise ValueError("dataset URL cannot use a private or local IP address")
        return self


class UploadedDatasetReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    upload_id: UUID
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=1, le=10 * 1024**2)


class UploadedScriptReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    upload_id: UUID
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=1, le=256 * 1024)


DatasetSource = Annotated[
    DatasetReference | UploadedDatasetReference,
    Field(union_mode="left_to_right"),
]


class DatasetScriptParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    script: ScriptName
    dataset: DatasetSource
    timeout_seconds: int = Field(default=300, ge=1, le=3600)


class PythonBatchParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    script: UploadedScriptReference
    dataset: DatasetSource
    timeout_seconds: int = Field(default=1800, ge=1, le=6 * 3600)
    cpu_limit: float = Field(default=2.0, ge=0.5, le=4.0)
    memory_mb: int = Field(default=2048, ge=256, le=4096)


JobParameters = Annotated[
    SleepParameters | DatasetScriptParameters | PythonBatchParameters,
    Field(union_mode="left_to_right"),
]


class JobCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: JobType
    parameters: JobParameters
    name: JobName | None = None

    @model_validator(mode="after")
    def type_matches_parameters(self) -> "JobCreate":
        if self.type is JobType.SLEEP and not isinstance(
            self.parameters, SleepParameters
        ):
            raise ValueError("sleep jobs require sleep parameters")
        if self.type is JobType.DATASET_SCRIPT and not isinstance(
            self.parameters, DatasetScriptParameters
        ):
            raise ValueError("dataset_script jobs require dataset script parameters")
        if self.type is JobType.PYTHON_BATCH and not isinstance(
            self.parameters, PythonBatchParameters
        ):
            raise ValueError("python_batch jobs require Python batch parameters")
        return self


class SleepResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slept_seconds: int = Field(ge=1, le=30)


class DatasetScriptResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    script: ScriptName
    dataset_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    dataset_bytes: int = Field(ge=1)
    rows: int = Field(ge=0)
    columns: list[Annotated[str, Field(max_length=200)]] = Field(max_length=1000)
    artifact_uri: str = Field(
        min_length=1,
        max_length=500,
        pattern=r"^worker://[A-Za-z0-9._-]+/[0-9a-f-]+/summary\.json$",
    )


class PythonBatchResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    script_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    dataset_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    exit_code: int = Field(ge=0, le=0)
    stdout: str = Field(max_length=8000)
    stderr: str = Field(max_length=8000)
    output_files: list[
        Annotated[
            str,
            Field(
                min_length=1,
                max_length=200,
                pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
            ),
        ]
    ] = Field(default_factory=list, max_length=100)
    artifact_uri: str = Field(
        min_length=1,
        max_length=500,
        pattern=r"^worker://[A-Za-z0-9._-]+/[0-9a-f-]+/$",
    )


JobResult = Annotated[
    SleepResult | DatasetScriptResult | PythonBatchResult,
    Field(union_mode="left_to_right"),
]


class JobRead(BaseModel):
    id: UUID
    name: JobName | None = None
    type: JobType
    parameters: JobParameters
    status: JobStatus
    created_at: datetime
    updated_at: datetime
    worker_id: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    result: JobResult | None = None
    error: str | None = None
    attempt: int = 0
    max_attempts: int = 3
    lease_token: UUID | None = None
    lease_expires_at: datetime | None = None


class JobCompletion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    worker_id: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9._-]+$",
    )
    lease_token: UUID
    result: JobResult


class JobFailure(BaseModel):
    model_config = ConfigDict(extra="forbid")

    worker_id: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9._-]+$",
    )
    lease_token: UUID
    error: str = Field(min_length=1, max_length=1000)


class GpuMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    percent: float | None = Field(default=None, ge=0, le=100)
    memory_used: int | None = Field(default=None, ge=0)
    memory_total: int | None = Field(default=None, ge=0)
    temperature_c: float | None = Field(default=None, ge=-50, le=200)


class WorkerMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid")

    platform: str = Field(min_length=1, max_length=64)
    logical_cores: int = Field(ge=1)
    cpu_percent: float = Field(ge=0, le=100)
    memory_percent: float = Field(ge=0, le=100)
    memory_available: int = Field(ge=0)
    memory_total: int = Field(ge=0)
    storage_percent: float = Field(ge=0, le=100)
    storage_free: int = Field(ge=0)
    storage_total: int = Field(ge=0)
    temperature_c: float | None = Field(default=None, ge=-50, le=200)
    gpus: list[GpuMetrics] = Field(default_factory=list, max_length=16)


class WorkerClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")

    worker_id: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9._-]+$",
    )
    supported_types: list[JobType] = Field(
        default_factory=lambda: [JobType.SLEEP],
        min_length=1,
    )
    metrics: WorkerMetrics | None = None


class WorkerHeartbeat(BaseModel):
    model_config = ConfigDict(extra="forbid")

    worker_id: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9._-]+$",
    )
    supported_types: list[JobType] = Field(
        default_factory=lambda: [JobType.SLEEP],
        min_length=1,
    )
    current_job_id: UUID | None = None
    lease_token: UUID | None = None
    metrics: WorkerMetrics | None = None

    @model_validator(mode="after")
    def job_and_lease_are_paired(self) -> "WorkerHeartbeat":
        if (self.current_job_id is None) != (self.lease_token is None):
            raise ValueError("current_job_id and lease_token must be provided together")
        return self


class WorkerUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(strict=True)


class WorkerState(StrEnum):
    ONLINE = "ONLINE"
    BUSY = "BUSY"
    STALE = "STALE"


class WorkerRead(BaseModel):
    id: str
    enabled: bool
    supported_types: list[JobType]
    registered_at: datetime
    last_seen: datetime
    current_job_id: UUID | None = None
    metrics: WorkerMetrics | None = None
    state: WorkerState
