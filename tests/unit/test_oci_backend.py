from __future__ import annotations

import json

import pytest

from hermes_fleet.backend_capabilities import BackendCapabilities
from hermes_fleet.execution_backend import (
    BackendExecutionHandle,
    BackendExecutionState,
    ExecutionBackendError,
    ExecutionBackendErrorCode,
    ExecutionPlan,
)
from hermes_fleet.oci_backend import (
    DockerExecutionBackend,
    DockerWorkshopBackend,
    OciRealizationSpec,
)
from hermes_fleet.recipes import ResolvedRecipe

IMAGE = "debian@sha256:" + "3" * 64


def resolved_recipe() -> ResolvedRecipe:
    return ResolvedRecipe.from_dict(
        {
            "schema": "fleet.resolved-recipe.v1",
            "recipe_hash": "sha256:" + "1" * 64,
            "agent": {
                "kind": "agency_profile",
                "repository": "https://example.invalid/agency.git",
                "revision": "a" * 40,
                "name": "researcher",
                "version": "1.0.0",
                "content_digest": "sha256:" + "2" * 64,
            },
            "extensions": {},
        }
    )


def capabilities() -> BackendCapabilities:
    return BackendCapabilities.from_dict(
        {
            "schema": "fleet.backend-capabilities.v1",
            "backend_kind": "fleet.dev/docker-oci",
            "platform": {"os": "linux", "architecture": "x86_64"},
            "isolation": ["container"],
            "network": ["none"],
            "resources": {"cpu_millis": 1000, "memory_bytes": 268_435_456},
            "filesystem": {"ephemeral_root": True, "read_only_inputs": True},
            "materialization": {"agency_profile": True, "artifacts": False},
            "extensions": {},
        }
    )


def plan() -> ExecutionPlan:
    return ExecutionPlan(
        execution_id="exec-1",
        idempotency_key="request-1",
        resolved_recipe=resolved_recipe(),
        required_capabilities_hash=capabilities().content_hash,
    )


class FakeDocker:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.container: dict[str, object] | None = None
        self.lose_create_response = False
        self.lose_start_response = False
        self.inspect_unavailable = False

    def run(self, argv: list[str]) -> str:
        self.calls.append(argv)
        operation = argv[1]
        if operation == "inspect":
            if self.container is None:
                if self.inspect_unavailable:
                    raise ExecutionBackendError(
                        ExecutionBackendErrorCode.INSPECTION_UNAVAILABLE,
                        "Docker is unavailable",
                    )
                return "[]"
            return json.dumps([self.container])
        if operation == "image":
            return json.dumps(
                [
                    {
                        "RepoDigests": [IMAGE],
                        "Config": {
                            "Labels": {
                                "dev.hermes.agency.repository": "https://example.invalid/agency.git",
                                "dev.hermes.agency.revision": "a" * 40,
                                "dev.hermes.agency.profile": "researcher",
                                "dev.hermes.agency.version": "1.0.0",
                                "dev.hermes.agency.content": "sha256:" + "2" * 64,
                            }
                        },
                    }
                ]
            )
        if operation == "create":
            labels = {
                item.split("=", 1)[0]: item.split("=", 1)[1]
                for index, item in enumerate(argv)
                if index > 0 and argv[index - 1] == "--label"
            }

            def value(flag: str, default: str = "") -> str:
                return argv[argv.index(flag) + 1] if flag in argv else default

            def values(flag: str) -> list[str]:
                return [
                    argv[index + 1]
                    for index, item in enumerate(argv[:-1])
                    if item == flag
                ]

            tmpfs = {
                item.split(":", 1)[0]: item.split(":", 1)[1]
                for item in values("--tmpfs")
            }
            cpus = value("--cpus", "0")
            self.container = {
                "Id": "container-1",
                "Config": {
                    "Image": IMAGE,
                    "Labels": labels,
                    "User": value("--user"),
                    "WorkingDir": value("--workdir"),
                    "Env": values("--env"),
                },
                "HostConfig": {
                    "NetworkMode": value("--network"),
                    "ReadonlyRootfs": "--read-only" in argv,
                    "Privileged": False,
                    "CapDrop": values("--cap-drop"),
                    "CapAdd": None,
                    "SecurityOpt": values("--security-opt"),
                    "PidsLimit": int(value("--pids-limit", "0")),
                    "Memory": int(value("--memory", "0")),
                    "MemorySwap": int(value("--memory-swap", "0")),
                    "NanoCpus": int(float(cpus) * 1_000_000_000),
                    "Init": "--init" in argv,
                    "Binds": None,
                    "Tmpfs": tmpfs,
                    "LogConfig": {"Type": value("--log-driver")},
                },
                "State": {"Status": "created", "ExitCode": 0},
                "Mounts": [],
            }
            if self.lose_create_response:
                raise ExecutionBackendError(
                    ExecutionBackendErrorCode.PREPARE_FAILED,
                    "Docker create outcome is uncertain",
                )
            return "container-1\n"
        if operation == "start":
            assert self.container is not None
            self.container["State"] = {
                "Status": "exited" if self.lose_start_response else "running",
                "ExitCode": 0,
            }
            if self.lose_start_response:
                raise ExecutionBackendError(
                    ExecutionBackendErrorCode.START_FAILED,
                    "Docker start response was lost",
                )
            return "container-1\n"
        if operation == "stop":
            assert self.container is not None
            self.container["State"] = {"Status": "exited", "ExitCode": 137}
            return "container-1\n"
        if operation == "rm":
            self.container = None
            return "container-1\n"
        raise AssertionError(argv)


def backend(fake: FakeDocker, *, image: str = IMAGE) -> DockerExecutionBackend:
    return DockerExecutionBackend(
        capabilities=capabilities(),
        realization=OciRealizationSpec(
            image=image,
            argv=("/bin/sh", "-c", "exit 0"),
            network="none",
            cpu_millis=500,
            memory_bytes=67_108_864,
            pids_limit=32,
        ),
        command=fake.run,
    )


def workshop_backend(fake: FakeDocker) -> DockerWorkshopBackend:
    return DockerWorkshopBackend(
        capabilities=capabilities(),
        realization=OciRealizationSpec(
            image=IMAGE,
            argv=("sleep", "infinity"),
            network="none",
            cpu_millis=500,
            memory_bytes=67_108_864,
            pids_limit=32,
        ),
        deadline_ms=20_000,
        now_ms=lambda: 10_000,
        command=fake.run,
    )


def test_workshop_prepare_is_non_root_read_only_and_ephemeral() -> None:
    fake = FakeDocker()
    handle = workshop_backend(fake).prepare(plan())

    create = next(call for call in fake.calls if call[1] == "create")
    assert handle.state == BackendExecutionState.PREPARED
    assert ["--user", "65532:65532"] == create[create.index("--user") :][:2]
    assert ["--workdir", "/workspace"] == create[create.index("--workdir") :][:2]
    assert "--read-only" in create
    assert "--init" in create
    assert ["--network", "none"] == create[create.index("--network") :][:2]
    assert ["--cap-drop", "ALL"] == create[create.index("--cap-drop") :][:2]
    assert ["--security-opt", "no-new-privileges:true"] == create[
        create.index("--security-opt") :
    ][:2]
    tmpfs_values = [
        create[index + 1]
        for index, value in enumerate(create[:-1])
        if value == "--tmpfs"
    ]
    workspace_tmpfs = next(
        value for value in tmpfs_values if value.startswith("/workspace:rw,")
    )
    assert "uid=65532" in workspace_tmpfs
    assert "gid=65532" in workspace_tmpfs
    assert "mode=0711" in workspace_tmpfs
    input_tmpfs = next(
        value for value in tmpfs_values if value.startswith("/workspace/inputs:rw,")
    )
    assert "uid=65533" in input_tmpfs
    assert "gid=65533" in input_tmpfs
    assert "mode=0755" in input_tmpfs
    assert any(value.startswith("/tmp:rw,nosuid,nodev,exec,") for value in tmpfs_values)
    assert any(
        value.startswith("/home/fleet:rw,nosuid,nodev,exec,") for value in tmpfs_values
    )
    assert "HOME=/home/fleet" in create
    assert "TMPDIR=/tmp" in create
    assert not any(
        value
        in {
            "--volume",
            "-v",
            "--mount",
            "--privileged",
            "--cap-add",
            "--device",
            "--env-file",
        }
        for value in create
    )
    forwarded_env = [
        create[index + 1] for index, value in enumerate(create[:-1]) if value == "--env"
    ]
    assert forwarded_env == ["HOME=/home/fleet", "TMPDIR=/tmp"]
    labels = {
        create[index + 1].split("=", 1)[0]: create[index + 1].split("=", 1)[1]
        for index, value in enumerate(create[:-1])
        if value == "--label"
    }
    assert labels["dev.hermes.fleet.plan"] == plan().fingerprint
    assert labels["dev.hermes.fleet.role"] == "workshop"
    assert labels["dev.hermes.fleet.deadline_ms"] == "20000"
    assert labels["hermes-agent"] == "1"
    assert labels["hermes-task-id"] == "fleet_exec-1"
    assert "hermes-profile" not in labels
    assert labels["hermes-egress"] == "off"
    assert create[-3:] == [IMAGE, "sleep", "infinity"]


def test_workshop_cleanup_is_idempotent_without_cleanup_realization() -> None:
    fake = FakeDocker()
    service = workshop_backend(fake)

    running = service.ensure(plan())
    assert running.state == BackendExecutionState.RUNNING
    service.cleanup_plan(plan(), handle=running)
    assert fake.container is None

    creates_before = sum(call[1] == "create" for call in fake.calls)
    service.cleanup_plan(plan())
    assert sum(call[1] == "create" for call in fake.calls) == creates_before
    assert sum(call[1] == "start" for call in fake.calls) == 1
    assert sum(call[1] == "stop" for call in fake.calls) == 1
    assert sum(call[1] == "rm" for call in fake.calls) == 1


def test_workshop_image_needs_digest_pin_but_not_agency_labels() -> None:
    fake = FakeDocker()
    original = fake.run

    def no_agency_labels(argv: list[str]) -> str:
        if argv[1] == "image":
            return json.dumps([{"RepoDigests": [IMAGE], "Config": {"Labels": {}}}])
        return original(argv)

    service = DockerWorkshopBackend(
        capabilities=capabilities(),
        realization=OciRealizationSpec(
            image=IMAGE,
            argv=("sleep", "infinity"),
            network="none",
            cpu_millis=500,
            memory_bytes=67_108_864,
            pids_limit=32,
        ),
        deadline_ms=20_000,
        now_ms=lambda: 10_000,
        command=no_agency_labels,
    )
    assert service.prepare(plan()).state == BackendExecutionState.PREPARED


def test_workshop_rejects_arbitrary_container_command() -> None:
    fake = FakeDocker()
    with pytest.raises(ExecutionBackendError):
        DockerWorkshopBackend(
            capabilities=capabilities(),
            realization=OciRealizationSpec(
                image=IMAGE,
                argv=("/bin/sh", "-c", "sleep infinity"),
                network="none",
                cpu_millis=500,
                memory_bytes=67_108_864,
                pids_limit=32,
            ),
            deadline_ms=20_000,
            now_ms=lambda: 10_000,
            command=fake.run,
        )


def test_workshop_refuses_prepare_or_start_after_deadline() -> None:
    fake = FakeDocker()
    now = [10_000]
    service = DockerWorkshopBackend(
        capabilities=capabilities(),
        realization=OciRealizationSpec(
            image=IMAGE,
            argv=("sleep", "infinity"),
            network="none",
            cpu_millis=500,
            memory_bytes=67_108_864,
            pids_limit=32,
        ),
        deadline_ms=20_000,
        now_ms=lambda: now[0],
        command=fake.run,
    )
    prepared = service.prepare(plan())
    now[0] = 20_000

    with pytest.raises(ExecutionBackendError) as start_error:
        service.start(prepared)
    assert start_error.value.code == ExecutionBackendErrorCode.INVALID_TRANSITION
    assert not any(call[1] == "start" for call in fake.calls)

    second = FakeDocker()
    expired = DockerWorkshopBackend(
        capabilities=capabilities(),
        realization=service._realization,
        deadline_ms=20_000,
        now_ms=lambda: 20_000,
        command=second.run,
    )
    with pytest.raises(ExecutionBackendError) as prepare_error:
        expired.prepare(plan())
    assert prepare_error.value.code == ExecutionBackendErrorCode.INVALID_TRANSITION
    assert second.calls == []


@pytest.mark.parametrize(
    ("section", "key", "unsafe"),
    [
        ("Config", "User", "0:0"),
        ("HostConfig", "NetworkMode", "bridge"),
        ("HostConfig", "ReadonlyRootfs", False),
        ("HostConfig", "Privileged", True),
        ("HostConfig", "Privileged", None),
        ("HostConfig", "CapDrop", []),
        ("HostConfig", "CapAdd", ["SYS_ADMIN"]),
        ("HostConfig", "SecurityOpt", []),
        ("HostConfig", "PidsLimit", 0),
        ("HostConfig", "Memory", 0),
        ("HostConfig", "MemorySwap", 0),
        ("HostConfig", "NanoCpus", 0),
        ("HostConfig", "Binds", ["/home:/workspace"]),
        ("HostConfig", "Devices", [{"PathOnHost": "/dev/kvm"}]),
        ("HostConfig", "DeviceRequests", [{"Capabilities": [["gpu"]]}]),
        ("HostConfig", "Tmpfs", {"/tmp": "rw", "/home/fleet": "rw"}),
    ],
)
def test_workshop_rejects_observed_security_drift(section, key, unsafe) -> None:
    fake = FakeDocker()
    service = workshop_backend(fake)
    prepared = service.prepare(plan())
    assert fake.container is not None
    document = fake.container[section]
    assert isinstance(document, dict)
    document[key] = unsafe

    with pytest.raises(ExecutionBackendError) as raised:
        service.inspect(prepared)
    assert raised.value.code == ExecutionBackendErrorCode.CAPABILITY_MISMATCH


@pytest.mark.parametrize(
    "extra_environment",
    [
        "SSH_AUTH_SOCK=/tmp/agent.sock",
        "KERYX_NODE_TOKEN=opaque",
        "FLEET_CONTROL_SOCKET=/run/fleet.sock",
        "NODESCALE_SOCKET=/run/nodescale.sock",
        "DOCKER_HOST=unix:///var/run/docker.sock",
        "DOCKER_CONTEXT=host-control",
        "HERMES_HOME=/host/hermes",
        "OPENAI_API_KEY=opaque",
        "AWS_ACCESS_KEY_ID=opaque",
    ],
)
def test_workshop_rejects_forbidden_environment_authority(extra_environment) -> None:
    fake = FakeDocker()
    service = workshop_backend(fake)
    prepared = service.prepare(plan())
    assert fake.container is not None
    environment = fake.container["Config"]["Env"]  # type: ignore[index]
    environment.append(extra_environment)

    with pytest.raises(ExecutionBackendError) as raised:
        service.inspect(prepared)
    assert raised.value.code == ExecutionBackendErrorCode.CAPABILITY_MISMATCH


def test_workshop_rejects_missing_or_duplicate_required_environment() -> None:
    fake = FakeDocker()
    service = workshop_backend(fake)
    prepared = service.prepare(plan())
    assert fake.container is not None
    environment = fake.container["Config"]["Env"]  # type: ignore[index]
    environment.remove("HOME=/home/fleet")
    with pytest.raises(ExecutionBackendError) as missing:
        service.inspect(prepared)
    assert missing.value.code == ExecutionBackendErrorCode.CAPABILITY_MISMATCH

    fake = FakeDocker()
    service = workshop_backend(fake)
    prepared = service.prepare(plan())
    assert fake.container is not None
    environment = fake.container["Config"]["Env"]  # type: ignore[index]
    environment.append("HOME=/different")
    with pytest.raises(ExecutionBackendError) as duplicate:
        service.inspect(prepared)
    assert duplicate.value.code == ExecutionBackendErrorCode.CAPABILITY_MISMATCH


def test_workshop_rejects_workspace_tmpfs_identity_drift() -> None:
    fake = FakeDocker()
    service = workshop_backend(fake)
    prepared = service.prepare(plan())
    assert fake.container is not None
    tmpfs = fake.container["HostConfig"]["Tmpfs"]  # type: ignore[index]
    tmpfs["/workspace/inputs"] = (
        "rw,nosuid,nodev,exec,size=134217728,uid=65532,gid=65532,mode=0755"
    )
    with pytest.raises(ExecutionBackendError) as input_error:
        service.inspect(prepared)
    assert input_error.value.code == ExecutionBackendErrorCode.CAPABILITY_MISMATCH

    fake = FakeDocker()
    service = workshop_backend(fake)
    prepared = service.prepare(plan())
    assert fake.container is not None
    tmpfs = fake.container["HostConfig"]["Tmpfs"]  # type: ignore[index]
    tmpfs["/workspace"] = (
        "rw,nosuid,nodev,exec,size=268435456,uid=65532,gid=65532,mode=0700"
    )
    with pytest.raises(ExecutionBackendError) as workspace_error:
        service.inspect(prepared)
    assert workspace_error.value.code == ExecutionBackendErrorCode.CAPABILITY_MISMATCH


def test_workshop_rejects_persistent_mount_or_identity_label_drift() -> None:
    fake = FakeDocker()
    service = workshop_backend(fake)
    prepared = service.prepare(plan())
    assert fake.container is not None
    fake.container["Mounts"] = None
    with pytest.raises(ExecutionBackendError) as missing_mounts:
        service.inspect(prepared)
    assert missing_mounts.value.code == ExecutionBackendErrorCode.CAPABILITY_MISMATCH

    fake = FakeDocker()
    service = workshop_backend(fake)
    prepared = service.prepare(plan())
    assert fake.container is not None
    fake.container["Mounts"] = [
        {"Type": "bind", "Source": "/var/run/docker.sock", "Destination": "/sock"}
    ]
    with pytest.raises(ExecutionBackendError) as mount_error:
        service.inspect(prepared)
    assert mount_error.value.code == ExecutionBackendErrorCode.CAPABILITY_MISMATCH

    fake = FakeDocker()
    service = workshop_backend(fake)
    prepared = service.prepare(plan())
    assert fake.container is not None
    labels = fake.container["Config"]["Labels"]  # type: ignore[index]
    labels["dev.hermes.fleet.plan"] = "sha256:" + "9" * 64
    with pytest.raises(ExecutionBackendError) as plan_error:
        service.inspect(prepared)
    assert plan_error.value.code == ExecutionBackendErrorCode.CAPABILITY_MISMATCH

    labels["dev.hermes.fleet.plan"] = plan().fingerprint
    labels["dev.hermes.fleet.deadline_ms"] = "99999"
    with pytest.raises(ExecutionBackendError) as deadline_error:
        service.inspect(prepared)
    assert deadline_error.value.code == ExecutionBackendErrorCode.CAPABILITY_MISMATCH


def test_prepare_uses_digest_pinned_hardened_create_argv() -> None:
    fake = FakeDocker()

    handle = backend(fake).prepare(plan())

    create = next(call for call in fake.calls if call[1] == "create")
    assert handle.state == BackendExecutionState.PREPARED
    assert create[:2] == ["docker", "create"]
    assert "--read-only" in create
    assert ["--cap-drop", "ALL"] == create[create.index("--cap-drop") :][:2]
    assert ["--security-opt", "no-new-privileges:true"] == create[
        create.index("--security-opt") :
    ][:2]
    assert ["--network", "none"] == create[create.index("--network") :][:2]
    assert ["--pids-limit", "32"] == create[create.index("--pids-limit") :][:2]
    assert IMAGE in create
    assert all("request-1" not in argument for argument in create)


def test_prepare_recovers_create_response_loss_by_exact_owned_inspection() -> None:
    fake = FakeDocker()
    fake.lose_create_response = True

    handle = backend(fake).prepare(plan())

    assert handle.state == BackendExecutionState.PREPARED
    assert sum(call[1] == "create" for call in fake.calls) == 1


def test_prepare_rejects_image_without_exact_resolved_agency_identity() -> None:
    fake = FakeDocker()
    original = fake.run

    def missing_identity(argv: list[str]) -> str:
        if argv[1] == "image":
            return json.dumps([{"RepoDigests": [IMAGE], "Config": {"Labels": {}}}])
        return original(argv)

    service = DockerExecutionBackend(
        capabilities=capabilities(),
        realization=backend(fake)._realization,
        command=missing_identity,
    )

    with pytest.raises(ExecutionBackendError) as raised:
        service.prepare(plan())

    assert raised.value.code == ExecutionBackendErrorCode.CAPABILITY_MISMATCH
    assert not any(call[1] == "create" for call in fake.calls)


def test_prepare_rejects_conflicting_deterministic_container() -> None:
    fake = FakeDocker()
    service = backend(fake)
    first = service.prepare(plan())
    assert fake.container is not None
    fake.container["Config"]["Labels"]["dev.hermes.fleet.recipe"] = "sha256:" + "9" * 64  # type: ignore[index]

    with pytest.raises(ExecutionBackendError) as raised:
        service.prepare(plan())

    assert raised.value.code == ExecutionBackendErrorCode.CAPABILITY_MISMATCH
    assert first.realization_id


def test_prepare_rejects_same_execution_with_different_idempotency_key() -> None:
    fake = FakeDocker()
    service = backend(fake)
    service.prepare(plan())
    conflicting = ExecutionPlan(
        execution_id="exec-1",
        idempotency_key="request-2",
        resolved_recipe=resolved_recipe(),
        required_capabilities_hash=capabilities().content_hash,
    )

    with pytest.raises(ExecutionBackendError) as raised:
        service.prepare(conflicting)

    assert raised.value.code == ExecutionBackendErrorCode.CAPABILITY_MISMATCH
    assert sum(call[1] == "create" for call in fake.calls) == 1


def test_existing_realization_recovers_plan_fingerprint_from_fx4_labels() -> None:
    fake = FakeDocker()
    service = backend(fake)
    prepared = service.prepare(plan())
    assert fake.container is not None
    labels = fake.container["Config"]["Labels"]  # type: ignore[index]
    labels.pop("dev.hermes.fleet.plan", None)
    assert "dev.hermes.fleet.plan" not in labels

    recovered = service.inspect(prepared)

    assert recovered.plan_fingerprint == plan().fingerprint


def test_start_inspect_stop_cleanup_map_real_runtime_state_and_are_idempotent() -> None:
    fake = FakeDocker()
    service = backend(fake)
    prepared = service.prepare(plan())

    running = service.start(prepared)
    assert service.start(running) == running
    stopped = service.stop(running)
    assert stopped.state == BackendExecutionState.STOPPED
    cleaned = service.cleanup(stopped)
    assert cleaned.state == BackendExecutionState.CLEANED
    assert service.cleanup(cleaned) == cleaned

    assert fake.container is None
    assert sum(call[1] == "start" for call in fake.calls) == 1
    assert sum(call[1] == "rm" for call in fake.calls) == 1


def test_start_recovers_response_loss_after_work_already_completed() -> None:
    fake = FakeDocker()
    fake.lose_start_response = True
    service = backend(fake)

    completed = service.start(service.prepare(plan()))

    assert completed.state == BackendExecutionState.COMPLETED
    assert sum(call[1] == "start" for call in fake.calls) == 1


def test_cleanup_does_not_mistake_unavailable_inspection_for_absence() -> None:
    fake = FakeDocker()
    service = backend(fake)
    prepared = service.prepare(plan())
    fake.container = None
    fake.inspect_unavailable = True

    with pytest.raises(ExecutionBackendError) as raised:
        service.cleanup(prepared)

    assert raised.value.code == ExecutionBackendErrorCode.INSPECTION_UNAVAILABLE


def test_cleanup_rejects_foreign_backend_handle_even_when_realization_is_absent() -> (
    None
):
    fake = FakeDocker()
    service = backend(fake)
    foreign = BackendExecutionHandle(
        execution_id="exec-1",
        backend_kind="example.org/native",
        realization_id="missing-realization",
        plan_fingerprint=plan().fingerprint,
        state=BackendExecutionState.STOPPED,
    )

    with pytest.raises(ExecutionBackendError) as raised:
        service.cleanup(foreign)

    assert raised.value.code == ExecutionBackendErrorCode.INVALID_INPUT


def test_lifecycle_rejects_handle_with_rebound_plan_fingerprint() -> None:
    fake = FakeDocker()
    service = backend(fake)
    prepared = service.prepare(plan())
    rebound = BackendExecutionHandle(
        execution_id=prepared.execution_id,
        backend_kind=prepared.backend_kind,
        realization_id=prepared.realization_id,
        plan_fingerprint="sha256:" + "9" * 64,
        state=prepared.state,
    )

    with pytest.raises(ExecutionBackendError) as raised:
        service.start(rebound)

    assert raised.value.code == ExecutionBackendErrorCode.PLAN_CONFLICT
    assert not any(call[1] == "start" for call in fake.calls)


def test_cleanup_rejects_handle_with_rebound_plan_fingerprint() -> None:
    fake = FakeDocker()
    service = backend(fake)
    prepared = service.prepare(plan())
    rebound = BackendExecutionHandle(
        execution_id=prepared.execution_id,
        backend_kind=prepared.backend_kind,
        realization_id=prepared.realization_id,
        plan_fingerprint="sha256:" + "9" * 64,
        state=prepared.state,
    )

    with pytest.raises(ExecutionBackendError) as raised:
        service.cleanup(rebound)

    assert raised.value.code == ExecutionBackendErrorCode.PLAN_CONFLICT
    assert not any(call[1] == "rm" for call in fake.calls)
    assert fake.container is not None


def test_lifecycle_rejects_missing_plan_evidence_as_inspection_unavailable() -> None:
    fake = FakeDocker()
    service = backend(fake)
    prepared = service.prepare(plan())
    assert fake.container is not None
    labels = fake.container["Config"]["Labels"]  # type: ignore[index]
    del labels["dev.hermes.fleet.recipe"]

    with pytest.raises(ExecutionBackendError) as raised:
        service.inspect(prepared)

    assert raised.value.code == ExecutionBackendErrorCode.INSPECTION_UNAVAILABLE


def test_realization_requires_digest_pin_and_non_secret_bounded_argv() -> None:
    with pytest.raises(ExecutionBackendError):
        OciRealizationSpec(
            image="debian:latest",
            argv=("/bin/true",),
            network="none",
            cpu_millis=100,
            memory_bytes=1024,
            pids_limit=4,
        )
    with pytest.raises(ExecutionBackendError):
        OciRealizationSpec(
            image=IMAGE,
            argv=("/bin/sh", "token=secret"),
            network="none",
            cpu_millis=100,
            memory_bytes=1024,
            pids_limit=4,
        )


def test_realization_accepts_exact_oci_content_digest() -> None:
    value = OciRealizationSpec(
        image="sha256:" + "4" * 64,
        argv=("/bin/true",),
        network="none",
        cpu_millis=100,
        memory_bytes=1024,
        pids_limit=4,
    )

    assert value.image == "sha256:" + "4" * 64
