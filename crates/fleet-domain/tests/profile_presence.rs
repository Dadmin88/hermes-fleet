use fleet_domain::NodeObservation;
use serde_json::{Value, json};

fn observation_json(profiles: Value) -> Value {
    json!({
        "admission_generation": 1,
        "observed_at_ms": 1_000,
        "network": "reachable",
        "keryx": "available",
        "hermes": "available",
        "worker": "available",
        "capacity": {"active_workers": 0, "max_workers": 2},
        "profiles": profiles,
        "resources": {}
    })
}

#[test]
fn legacy_observation_without_profiles_defaults_to_empty_inventory() {
    let observation: NodeObservation = serde_json::from_value(json!({
        "admission_generation": 1,
        "observed_at_ms": 1_000,
        "network": "reachable",
        "keryx": "available",
        "hermes": "available",
        "worker": "available",
        "capacity": {"active_workers": 0, "max_workers": 2},
        "resources": {}
    }))
    .unwrap();

    assert!(observation.profiles.is_empty());
    assert!(observation.validate().is_ok());
}

#[test]
fn canonical_profile_inventory_is_valid_and_round_trips() {
    let observation: NodeObservation = serde_json::from_value(observation_json(json!([
        {"name": "agency-ai-engineer", "version": "0.1.0"},
        {"name": "agency-backend-engineer", "version": "0.1.0"}
    ])))
    .unwrap();

    assert!(observation.validate().is_ok());
    let serialized = serde_json::to_value(&observation).unwrap();
    assert_eq!(serialized["profiles"][0]["name"], "agency-ai-engineer");
    assert_eq!(serialized["profiles"][1]["name"], "agency-backend-engineer");
}

#[test]
fn duplicate_or_noncanonical_profile_order_fails_closed() {
    let duplicate: NodeObservation = serde_json::from_value(observation_json(json!([
        {"name": "agency-backend-engineer", "version": "0.1.0"},
        {"name": "agency-backend-engineer", "version": "0.2.0"}
    ])))
    .unwrap();
    assert!(duplicate.validate().is_err());

    let unsorted: NodeObservation = serde_json::from_value(observation_json(json!([
        {"name": "agency-backend-engineer", "version": "0.1.0"},
        {"name": "agency-ai-engineer", "version": "0.1.0"}
    ])))
    .unwrap();
    assert!(unsorted.validate().is_err());
}

#[test]
fn invalid_profile_identity_material_fails_closed() {
    for profiles in [
        json!([{"name": "", "version": "0.1.0"}]),
        json!([{"name": "agency backend", "version": "0.1.0"}]),
        json!([{"name": ".", "version": "0.1.0"}]),
        json!([{"name": "agency-backend", "version": ""}]),
        json!([{"name": "agency-backend", "version": "0.1 0"}]),
        json!([{"name": "a".repeat(129), "version": "0.1.0"}]),
        json!([{"name": "agency-backend", "version": "v".repeat(129)}]),
    ] {
        let observation: NodeObservation =
            serde_json::from_value(observation_json(profiles)).unwrap();
        assert!(observation.validate().is_err());
    }
}

#[test]
fn profile_inventory_count_is_bounded() {
    let profiles = (0..257)
        .map(|index| {
            json!({
                "name": format!("agency-profile-{index:03}"),
                "version": "0.1.0"
            })
        })
        .collect::<Vec<_>>();
    let observation: NodeObservation =
        serde_json::from_value(observation_json(Value::Array(profiles))).unwrap();

    assert!(observation.validate().is_err());
}
