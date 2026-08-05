"""Tests for strict, versioned and transport-independent Fleet envelopes."""

from __future__ import annotations

import json

import pytest


def test_envelope_accepts_a_bounded_hermes_run_for_its_configured_peer() -> None:
    """The only Phase-1 work envelope is schema-checked before dispatch exists."""
    from hermes_fleet.envelope import parse_envelope
    from hermes_fleet.models import FleetDefaults, NodeConfig

    node = NodeConfig(name="alpha", peer_id="peer-alpha")
    message = json.dumps(
        {
            "version": 1,
            "operation": "fleet.hermes.run",
            "target": {"name": "alpha", "peer_id": "peer-alpha"},
            "input": {
                "prompt": "Run focused tests.",
                "export_paths": ["reports/out.txt"],
            },
            "limits": {"deadline_seconds": 60},
        }
    )

    envelope = parse_envelope(message, target=node, defaults=FleetDefaults())

    assert envelope.operation == "fleet.hermes.run"
    assert envelope.input["export_paths"] == ("reports/out.txt",)


def test_envelope_accepts_a_bounded_direct_node_message() -> None:
    from hermes_fleet.envelope import parse_envelope
    from hermes_fleet.models import FleetDefaults, NodeConfig

    node = NodeConfig(name="vps", peer_id="peer-vps")
    message = json.dumps(
        {
            "version": 1,
            "operation": "fleet.message",
            "target": {"name": "vps", "peer_id": "peer-vps"},
            "input": {
                "text": "Hello from Katana",
                "topic": "smoke-test",
                "correlation_id": "corr-1",
            },
            "limits": {"deadline_seconds": 30},
        }
    )

    envelope = parse_envelope(message, target=node, defaults=FleetDefaults())

    assert envelope.operation == "fleet.message"
    assert envelope.input == {
        "text": "Hello from Katana",
        "topic": "smoke-test",
        "correlation_id": "corr-1",
    }


@pytest.mark.parametrize(
    "input_data",
    (
        {},
        {"text": "x" * 4_097},
        {"text": "ok", "topic": "x" * 65},
        {"text": "ok", "correlation_id": "x" * 129},
        {"text": "ok", "extra": "not allowed"},
    ),
)
def test_envelope_rejects_invalid_direct_node_messages(input_data) -> None:
    from hermes_fleet.envelope import parse_envelope
    from hermes_fleet.models import FleetDefaults, NodeConfig

    message = json.dumps(
        {
            "version": 1,
            "operation": "fleet.message",
            "target": {"name": "vps", "peer_id": "peer-vps"},
            "input": input_data,
            "limits": {"deadline_seconds": 30},
        }
    )

    with pytest.raises(ValueError, match="message"):
        parse_envelope(
            message,
            target=NodeConfig(name="vps", peer_id="peer-vps"),
            defaults=FleetDefaults(),
        )


@pytest.mark.parametrize("input_kind", ("non-json", "cyclic", "hostile-nested-list"))
def test_envelope_serialization_normalizes_encoder_errors(input_kind: str) -> None:
    """Direct construction cannot expose raw JSON encoder exceptions."""
    from hermes_fleet.envelope import FleetEnvelope

    input_data = {"bad": object()}
    if input_kind == "cyclic":
        input_data = {}
        input_data["self"] = input_data
    elif input_kind == "hostile-nested-list":

        class ExplosiveList(list):
            def __iter__(self):
                raise RuntimeError("list hook ran")

        input_data = {"nested": ExplosiveList()}
    envelope = FleetEnvelope(
        version=1,
        operation="fleet.health",
        target_name="alpha",
        target_peer_id="peer-alpha",
        input=input_data,
        deadline_seconds=1,
    )

    with pytest.raises(ValueError, match="JSON serializable") as error:
        envelope.to_json()
    assert type(error.value) is ValueError


@pytest.mark.parametrize("behavior", ("plain-subclass", "hostile-subclass"))
def test_envelope_serialization_requires_an_exact_input_mapping(behavior: str) -> None:
    """Mapping subclasses cannot invoke hooks during direct serialization."""
    from hermes_fleet.envelope import FleetEnvelope

    attributes = {}
    if behavior == "hostile-subclass":

        def explode(self):
            raise RuntimeError("items hook ran")

        attributes["items"] = explode
    Input = type("Input", (dict,), attributes)
    envelope = FleetEnvelope(
        version=1,
        operation="fleet.health",
        target_name="alpha",
        target_peer_id="peer-alpha",
        input=Input(),
        deadline_seconds=1,
    )

    with pytest.raises(ValueError, match="input must be a JSON object"):
        envelope.to_json()


@pytest.mark.parametrize(
    "field",
    ("version", "operation", "target_name", "target_peer_id", "deadline_seconds"),
)
def test_envelope_serialization_requires_exact_scalar_primitives(field: str) -> None:
    """Direct envelope scalar fields cannot retain primitive subclasses."""
    from hermes_fleet.envelope import FleetEnvelope

    values = {
        "version": 1,
        "operation": "fleet.health",
        "target_name": "alpha",
        "target_peer_id": "peer-alpha",
        "input": {},
        "deadline_seconds": 1,
    }
    base = int if field in {"version", "deadline_seconds"} else str
    Primitive = type("Primitive", (base,), {})
    values[field] = Primitive(values[field])

    with pytest.raises(ValueError, match=field):
        FleetEnvelope(**values).to_json()


@pytest.mark.parametrize(
    ("container_type", "hook_name"),
    ((dict, "items"), (list, "__iter__"), (tuple, "__iter__")),
)
@pytest.mark.parametrize("error_type", (RuntimeError, KeyError))
def test_envelope_serialization_rejects_nested_container_subclasses(
    container_type, hook_name: str, error_type: type[Exception]
) -> None:
    """Nested container hooks cannot escape the serializer boundary."""
    from hermes_fleet.envelope import FleetEnvelope

    def explode(self):
        raise error_type("nested container hook ran")

    Container = type("Container", (container_type,), {hook_name: explode})
    envelope = FleetEnvelope(
        version=1,
        operation="fleet.health",
        target_name="alpha",
        target_peer_id="peer-alpha",
        input={"nested": Container()},
        deadline_seconds=1,
    )

    with pytest.raises(ValueError, match="JSON serializable") as error:
        envelope.to_json()
    assert type(error.value) is ValueError


@pytest.mark.parametrize("value", (float("nan"), float("inf"), float("-inf")))
def test_envelope_serialization_rejects_nonfinite_floats(value: float) -> None:
    """Direct envelopes serialize only standards-compliant JSON numbers."""
    from hermes_fleet.envelope import FleetEnvelope

    envelope = FleetEnvelope(
        version=1,
        operation="fleet.hermes.run",
        target_name="alpha",
        target_peer_id="peer-alpha",
        input={"prompt": "ok", "nonfinite": value},
        deadline_seconds=1,
    )

    with pytest.raises(ValueError, match="JSON serializable") as error:
        envelope.to_json()
    assert type(error.value) is ValueError
    assert str(error.value) == "envelope input must be JSON serializable"
    assert error.value.__cause__ is None
    assert error.value.__context__ is None


def test_envelope_serialization_preserves_finite_floats() -> None:
    """Strict non-finite rejection does not reject valid JSON numbers."""
    from hermes_fleet.envelope import FleetEnvelope

    envelope = FleetEnvelope(
        version=1,
        operation="fleet.hermes.run",
        target_name="alpha",
        target_peer_id="peer-alpha",
        input={"value": 1.5},
        deadline_seconds=1,
    )

    assert json.loads(envelope.to_json())["input"]["value"] == 1.5


@pytest.mark.parametrize(
    ("target", "defaults", "error_label"),
    (
        ({}, None, "target"),
        (None, {}, "defaults"),
    ),
)
def test_envelope_rejects_invalid_domain_collaborators(
    target, defaults, error_label: str
) -> None:
    """Wrong collaborator types cannot escape as attribute errors."""
    from hermes_fleet.envelope import parse_envelope
    from hermes_fleet.models import FleetDefaults, NodeConfig

    actual_target = (
        target if target is not None else NodeConfig(name="alpha", peer_id="peer-alpha")
    )
    actual_defaults = defaults if defaults is not None else FleetDefaults()
    payload = json.dumps(
        {
            "version": 1,
            "operation": "fleet.health",
            "target": {"name": "alpha", "peer_id": "peer-alpha"},
            "input": {},
            "limits": {"deadline_seconds": 1},
        }
    )

    with pytest.raises(ValueError, match=error_label):
        parse_envelope(payload, target=actual_target, defaults=actual_defaults)


@pytest.mark.parametrize("field", ("target", "defaults"))
@pytest.mark.parametrize("behavior", ("plain-subclass", "hostile-subclass"))
def test_envelope_rejects_domain_collaborator_subclasses(
    field: str, behavior: str
) -> None:
    """Domain subclasses cannot carry hooks across envelope boundaries."""
    from hermes_fleet.envelope import parse_envelope
    from hermes_fleet.models import FleetDefaults, NodeConfig

    class TargetSubclass(NodeConfig):
        armed = False

        def __getattribute__(self, name):
            if type(self).armed and name == "name":
                raise RuntimeError("target hook ran")
            return object.__getattribute__(self, name)

    class DefaultsSubclass(FleetDefaults):
        armed = False

        def __getattribute__(self, name):
            if type(self).armed and name == "max_payload_bytes":
                raise RuntimeError("defaults hook ran")
            return object.__getattribute__(self, name)

    target = TargetSubclass(name="alpha", peer_id="peer-alpha")
    defaults = DefaultsSubclass()
    if behavior == "hostile-subclass":
        TargetSubclass.armed = True
        DefaultsSubclass.armed = True
    payload = json.dumps(
        {
            "version": 1,
            "operation": "fleet.health",
            "target": {"name": "alpha", "peer_id": "peer-alpha"},
            "input": {},
            "limits": {"deadline_seconds": 1},
        }
    )

    with pytest.raises(ValueError, match=f"{field} must be"):
        parse_envelope(
            payload,
            target=target
            if field == "target"
            else NodeConfig(name="alpha", peer_id="peer-alpha"),
            defaults=defaults if field == "defaults" else FleetDefaults(),
        )


@pytest.mark.parametrize("behavior", ("plain-subclass", "bad-encode"))
def test_envelope_requires_an_exact_primitive_payload_string(behavior: str) -> None:
    """Payload subclasses cannot override encoding or parsing behavior."""
    from hermes_fleet.envelope import parse_envelope
    from hermes_fleet.models import FleetDefaults, NodeConfig

    attributes = {}
    if behavior == "bad-encode":
        attributes["encode"] = lambda self, encoding: object()
    Payload = type("Payload", (str,), attributes)
    payload = Payload(
        json.dumps(
            {
                "version": 1,
                "operation": "fleet.health",
                "target": {"name": "alpha", "peer_id": "peer-alpha"},
                "input": {},
                "limits": {"deadline_seconds": 1},
            }
        )
    )

    with pytest.raises(ValueError, match="payload must be a string"):
        parse_envelope(
            payload,
            target=NodeConfig(name="alpha", peer_id="peer-alpha"),
            defaults=FleetDefaults(),
        )


def test_envelope_rejects_target_mismatches_bad_json_and_unsafe_bounds() -> None:
    """Inputs are not routing guesses and size/path limits are checked locally."""
    from hermes_fleet.envelope import parse_envelope
    from hermes_fleet.models import FleetDefaults, NodeConfig

    node = NodeConfig(name="alpha", peer_id="peer-alpha")
    defaults = FleetDefaults(
        max_deadline_seconds=60, max_prompt_chars=8, max_export_paths=1
    )
    base = {
        "version": 1,
        "operation": "fleet.hermes.run",
        "target": {"name": "alpha", "peer_id": "wrong-peer"},
        "input": {
            "prompt": "too-long-prompt",
            "export_paths": ["../unsafe", "again.txt"],
        },
        "limits": {"deadline_seconds": True},
    }

    with pytest.raises(ValueError, match="target"):
        parse_envelope(json.dumps(base), target=node, defaults=defaults)
    with pytest.raises(ValueError, match="JSON object"):
        parse_envelope("[]", target=node, defaults=defaults)

    base["target"]["peer_id"] = "peer-alpha"
    base["version"] = True
    with pytest.raises(ValueError, match="version"):
        parse_envelope(json.dumps(base), target=node, defaults=defaults)
    base["version"] = 1
    with pytest.raises(ValueError, match="deadline_seconds"):
        parse_envelope(json.dumps(base), target=node, defaults=defaults)


def test_envelope_rejects_oversize_bytes_before_json_parsing() -> None:
    """The byte ceiling wins even when an oversized payload is malformed JSON."""
    from hermes_fleet.envelope import parse_envelope
    from hermes_fleet.models import FleetDefaults, NodeConfig

    with pytest.raises(ValueError, match="size limit"):
        parse_envelope(
            "not-json-at-all",
            target=NodeConfig(name="alpha", peer_id="peer-alpha"),
            defaults=FleetDefaults(max_payload_bytes=8),
        )


def test_envelope_converts_parser_recursion_to_value_error() -> None:
    """Deep byte-bounded JSON cannot leak a parser recursion exception."""
    from hermes_fleet.envelope import parse_envelope
    from hermes_fleet.models import FleetDefaults, NodeConfig

    payload = "[" * 10_000 + "]" * 10_000
    with pytest.raises(ValueError, match="JSON object") as error:
        parse_envelope(
            payload,
            target=NodeConfig(name="alpha", peer_id="peer-alpha"),
            defaults=FleetDefaults(),
        )
    assert type(error.value) is ValueError


def test_envelope_converts_oversized_integer_parser_error() -> None:
    """Python's JSON integer digit limit cannot leak parser-specific text."""
    from hermes_fleet.envelope import parse_envelope
    from hermes_fleet.models import FleetDefaults, NodeConfig

    payload = '{"version":' + "9" * 5_000 + "}"
    with pytest.raises(ValueError, match="JSON object") as error:
        parse_envelope(
            payload,
            target=NodeConfig(name="alpha", peer_id="peer-alpha"),
            defaults=FleetDefaults(),
        )
    assert str(error.value) == "payload must be a JSON object"


@pytest.mark.parametrize(
    "mutation", ("extra-top-level", "missing-target", "extra-limit")
)
def test_envelope_requires_exact_top_level_and_limits_shapes(mutation: str) -> None:
    """Envelope and limits objects reject missing or additional members."""
    from hermes_fleet.envelope import parse_envelope
    from hermes_fleet.models import FleetDefaults, NodeConfig

    document = {
        "version": 1,
        "operation": "fleet.health",
        "target": {"name": "alpha", "peer_id": "peer-alpha"},
        "input": {},
        "limits": {"deadline_seconds": 1},
    }
    if mutation == "extra-top-level":
        document["extra"] = True
    elif mutation == "missing-target":
        del document["target"]
    else:
        document["limits"]["extra"] = True

    with pytest.raises(ValueError, match="shape"):
        parse_envelope(
            json.dumps(document),
            target=NodeConfig(name="alpha", peer_id="peer-alpha"),
            defaults=FleetDefaults(),
        )


@pytest.mark.parametrize("operation", ("fleet.health", "fleet.inventory"))
def test_health_and_inventory_envelopes_reject_nonempty_input(operation: str) -> None:
    """Read-only operations accept no caller-controlled input fields."""
    from hermes_fleet.envelope import parse_envelope
    from hermes_fleet.models import FleetDefaults, NodeConfig

    document = {
        "version": 1,
        "operation": operation,
        "target": {"name": "alpha", "peer_id": "peer-alpha"},
        "input": {"unexpected": True},
        "limits": {"deadline_seconds": 1},
    }

    with pytest.raises(ValueError, match="input must be empty"):
        parse_envelope(
            json.dumps(document),
            target=NodeConfig(name="alpha", peer_id="peer-alpha"),
            defaults=FleetDefaults(),
        )


_DUPLICATE_JSON_KEY_ERROR = "payload must not contain duplicate JSON object keys"


@pytest.mark.parametrize(
    "payload",
    (
        # Equal duplicates must not be treated as harmless.
        '{"version":1,"version":1,"operation":"fleet.health",'
        '"target":{"name":"alpha","peer_id":"peer-alpha"},'
        '"input":{},"limits":{"deadline_seconds":1}}',
        # A later malicious value must not replace an earlier valid value.
        '{"version":1,"operation":"fleet.health","operation":"fleet.evil",'
        '"target":{"name":"alpha","peer_id":"peer-alpha"},'
        '"input":{},"limits":{"deadline_seconds":1}}',
        # A later valid value must not hide an earlier malicious value.
        '{"version":1,"operation":"fleet.evil","operation":"fleet.health",'
        '"target":{"name":"alpha","peer_id":"peer-alpha"},'
        '"input":{},"limits":{"deadline_seconds":1}}',
        '{"version":1,"operation":"fleet.health",'
        '"target":{"name":"alpha","name":"alpha","peer_id":"peer-alpha"},'
        '"input":{},"limits":{"deadline_seconds":1}}',
        '{"version":1,"operation":"fleet.health",'
        '"target":{"name":"alpha","peer_id":"peer-alpha","peer_id":"peer-evil"},'
        '"input":{},"limits":{"deadline_seconds":1}}',
        '{"version":1,"operation":"fleet.hermes.run",'
        '"target":{"name":"alpha","peer_id":"peer-alpha"},'
        '"input":{"prompt":"safe","prompt":"malicious","export_paths":[]},'
        '"limits":{"deadline_seconds":1}}',
        '{"version":1,"operation":"fleet.hermes.run",'
        '"target":{"name":"alpha","peer_id":"peer-alpha"},'
        '"input":{"prompt":"safe","export_paths":[],"export_paths":["secret"]},'
        '"limits":{"deadline_seconds":1}}',
        '{"version":1,"operation":"fleet.health",'
        '"target":{"name":"alpha","peer_id":"peer-alpha"},'
        '"input":{},"limits":{"deadline_seconds":1,"deadline_seconds":1}}',
        # Duplicate envelope containers are rejected before shape validation.
        '{"version":1,"operation":"fleet.health",'
        '"target":{"name":"alpha","peer_id":"peer-alpha"},'
        '"target":{"name":"alpha","peer_id":"peer-alpha"},'
        '"input":{},"limits":{"deadline_seconds":1}}',
        '{"version":1,"operation":"fleet.health",'
        '"target":{"name":"alpha","peer_id":"peer-alpha"},'
        '"input":{},"input":{},"limits":{"deadline_seconds":1}}',
        '{"version":1,"operation":"fleet.health",'
        '"target":{"name":"alpha","peer_id":"peer-alpha"},'
        '"input":{},"limits":{"deadline_seconds":1},'
        '"limits":{"deadline_seconds":1}}',
        # Future-compatible structures cannot hide duplicates several levels deep.
        '{"version":1,"operation":"fleet.hermes.run",'
        '"target":{"name":"alpha","peer_id":"peer-alpha"},'
        '"input":{"prompt":"safe","future":{"nested":{"flag":true,"flag":false}}},'
        '"limits":{"deadline_seconds":1}}',
    ),
)
def test_envelope_rejects_duplicate_json_keys_at_every_depth(payload: str) -> None:
    """JSON object members are unique before envelope shape validation runs."""
    from hermes_fleet.envelope import parse_envelope
    from hermes_fleet.models import FleetDefaults, NodeConfig

    with pytest.raises(ValueError) as error:
        parse_envelope(
            payload,
            target=NodeConfig(name="alpha", peer_id="peer-alpha"),
            defaults=FleetDefaults(),
        )

    assert type(error.value) is ValueError
    assert str(error.value) == _DUPLICATE_JSON_KEY_ERROR
    assert error.value.__cause__ is None
    assert error.value.__context__ is None


_NONSTANDARD_JSON_VALUE_ERROR = "payload must contain only standard JSON values"


def _payload_with_nonstandard_constant(location: str, constant: str) -> str:
    if location == "root":
        return constant
    version = constant if location == "top-level" else "1"
    deadline = constant if location == "deadline" else "1"
    if location == "input":
        input_value = f'{{"prompt":{constant},"export_paths":[]}}'
    elif location == "nested-array":
        input_value = f'{{"prompt":"safe","future":{{"values":[0,{constant}]}}}}'
    elif location == "nested-object":
        input_value = (
            f'{{"prompt":"safe","future":{{"nested":{{"value":{constant}}}}}}}'
        )
    else:
        input_value = "{}"
    operation = "fleet.health" if input_value == "{}" else "fleet.hermes.run"
    return (
        f'{{"version":{version},"operation":"{operation}",'
        '"target":{"name":"alpha","peer_id":"peer-alpha"},'
        f'"input":{input_value},"limits":{{"deadline_seconds":{deadline}}}}}'
    )


@pytest.mark.parametrize("constant", ("NaN", "Infinity", "-Infinity"))
@pytest.mark.parametrize(
    "location",
    ("root", "top-level", "deadline", "input", "nested-array", "nested-object"),
)
def test_envelope_rejects_nonstandard_json_constants(
    location: str, constant: str
) -> None:
    """Python-only numeric constants never cross the Fleet JSON boundary."""
    from hermes_fleet.envelope import parse_envelope
    from hermes_fleet.models import FleetDefaults, NodeConfig

    payload = _payload_with_nonstandard_constant(location, constant)
    with pytest.raises(ValueError) as error:
        parse_envelope(
            payload,
            target=NodeConfig(name="alpha", peer_id="peer-alpha"),
            defaults=FleetDefaults(),
        )

    assert type(error.value) is ValueError
    assert str(error.value) == _NONSTANDARD_JSON_VALUE_ERROR
    assert error.value.__cause__ is None
    assert error.value.__context__ is None


@pytest.mark.parametrize("surrogate", ("\\ud800", "\\udfff"))
@pytest.mark.parametrize(
    "input_json",
    (
        '{"prompt":"SURROGATE","export_paths":[]}',
        '{"prompt":"safe","future":["SURROGATE"]}',
        '{"prompt":"safe","future":{"nested":"SURROGATE"}}',
        '{"prompt":"safe","SURROGATE":"value"}',
    ),
)
def test_envelope_rejects_escaped_lone_surrogates_at_every_depth(
    input_json: str, surrogate: str
) -> None:
    """ASCII JSON escapes cannot decode into invalid Unicode scalar strings."""
    from hermes_fleet.envelope import parse_envelope
    from hermes_fleet.models import FleetDefaults, NodeConfig

    payload = (
        '{"version":1,"operation":"fleet.hermes.run",'
        '"target":{"name":"alpha","peer_id":"peer-alpha"},'
        f'"input":{input_json.replace("SURROGATE", surrogate)},'
        '"limits":{"deadline_seconds":1}}'
    )
    with pytest.raises(ValueError) as error:
        parse_envelope(
            payload,
            target=NodeConfig(name="alpha", peer_id="peer-alpha"),
            defaults=FleetDefaults(),
        )

    assert type(error.value) is ValueError
    assert str(error.value) == "payload must be valid UTF-8"
    assert error.value.__cause__ is None
    assert error.value.__context__ is None


def test_envelope_preserves_valid_escaped_surrogate_pairs() -> None:
    """Lone-surrogate rejection preserves valid supplementary Unicode values."""
    from hermes_fleet.envelope import parse_envelope
    from hermes_fleet.models import FleetDefaults, NodeConfig

    payload = (
        '{"version":1,"operation":"fleet.hermes.run",'
        '"target":{"name":"alpha","peer_id":"peer-alpha"},'
        '"input":{"prompt":"launch \\ud83d\\ude80","export_paths":[]},'
        '"limits":{"deadline_seconds":1}}'
    )
    envelope = parse_envelope(
        payload,
        target=NodeConfig(name="alpha", peer_id="peer-alpha"),
        defaults=FleetDefaults(),
    )

    assert envelope.input["prompt"] == "launch 🚀"


def test_envelope_parser_errors_never_echo_payload_contents() -> None:
    """Malformed payload diagnostics do not disclose caller-controlled content."""
    from hermes_fleet.envelope import parse_envelope
    from hermes_fleet.models import FleetDefaults, NodeConfig

    secret = "TOP-SECRET-PAYLOAD-CONTENT"
    payload = f'{{"version":1,"input":"{secret}"'
    with pytest.raises(ValueError) as error:
        parse_envelope(
            payload,
            target=NodeConfig(name="alpha", peer_id="peer-alpha"),
            defaults=FleetDefaults(),
        )

    assert secret not in str(error.value)
    assert str(error.value) == "payload must be a JSON object"
    assert error.value.__cause__ is None
    assert error.value.__context__ is None


@pytest.mark.parametrize(
    "field,value",
    (
        ("input", {"prompt": "\ud800", "export_paths": []}),
        ("input", {"prompt": "safe", "future": ["\udfff"]}),
        ("input", {"prompt": "safe", "\ud800": "value"}),
        ("operation", "\ud800"),
        ("target_name", "\udfff"),
        ("target_peer_id", "\ud800"),
    ),
)
def test_envelope_serialization_rejects_lone_surrogates(
    field: str, value: object
) -> None:
    """Direct envelope construction cannot emit invalid Unicode scalar strings."""
    from hermes_fleet.envelope import FleetEnvelope

    values = {
        "version": 1,
        "operation": "fleet.hermes.run",
        "target_name": "alpha",
        "target_peer_id": "peer-alpha",
        "input": {"prompt": "safe", "export_paths": []},
        "deadline_seconds": 1,
    }
    values[field] = value
    envelope = FleetEnvelope(**values)

    with pytest.raises(ValueError) as error:
        envelope.to_json()

    assert str(error.value) == "envelope input must be JSON serializable"
    assert error.value.__cause__ is None
    assert error.value.__context__ is None


def test_envelope_serialization_preserves_valid_unicode() -> None:
    """Lone-surrogate rejection preserves normal supplementary characters."""
    from hermes_fleet.envelope import FleetEnvelope

    wire = FleetEnvelope(
        version=1,
        operation="fleet.hermes.run",
        target_name="alpha",
        target_peer_id="peer-alpha",
        input={"prompt": "launch 🚀", "export_paths": []},
        deadline_seconds=1,
    ).to_json()

    assert json.loads(wire)["input"]["prompt"] == "launch 🚀"
