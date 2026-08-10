use fleet_domain::{NodeObservation, ReadinessPolicy, canonical_projection_hash};
use fleet_state::{FleetStateStore, StateError};
use serde_json::json;
use tempfile::tempdir;

const DIGEST_A: &str = "7a9480c8d1d3e34ee64f66cfc8c06d7bfdcc6f9c7fdeee6d433cbdb637259b0f";
const DIGEST_B: &str = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";

fn projection(device_id: &str, generation: u64) -> fleet_domain::ProjectionDocument {
    let generation = generation.to_string();
    let mut document: fleet_domain::ProjectionDocument = serde_json::from_value(json!({
        "source": "nodescale",
        "network_id": "network-1",
        "device_id": device_id,
        "projection_generation": generation,
        "membership_generation": generation,
        "binding_generation": generation,
        "content_hash": "",
        "operation": "upsert",
        "generated_operations": ["fleet.health", "fleet.inventory", "fleet.message"],
        "provenance": {
            "source": "nodescale",
            "network_id": "network-1",
            "device_id": device_id,
            "snapshot": generation,
            "controller": "nodescale"
        }
    }))
    .unwrap();
    document.content_hash = canonical_projection_hash(&document);
    document
}

fn observation(
    admission_generation: u64,
    observed_at_ms: u64,
    profile_name: &str,
    version: &str,
    digest: Option<&str>,
) -> NodeObservation {
    let mut profile = json!({
        "name": profile_name,
        "version": version,
    });
    if let Some(digest) = digest {
        profile["content_digest"] = json!(digest);
    }
    serde_json::from_value(json!({
        "admission_generation": admission_generation,
        "observed_at_ms": observed_at_ms,
        "network": "reachable",
        "keryx": "available",
        "hermes": "available",
        "worker": "available",
        "capacity": {"active_workers": 0, "max_workers": 2},
        "profiles": [profile],
        "resources": {}
    }))
    .unwrap()
}

fn add_node(
    store: &FleetStateStore,
    device_id: &str,
    profile_name: &str,
    version: &str,
    digest: Option<&str>,
) {
    store.apply_projection(projection(device_id, 1)).unwrap();
    store
        .record_observation(
            "nodescale",
            "network-1",
            device_id,
            observation(1, 1_000, profile_name, version, digest),
            1_100,
        )
        .unwrap();
}

fn policy() -> ReadinessPolicy {
    ReadinessPolicy::new(1_000).unwrap()
}

#[test]
fn exact_lookup_filters_digest_without_narrowing_general_presence() {
    let temporary = tempdir().unwrap();
    let store = FleetStateStore::open(temporary.path().join("fleet.sqlite3")).unwrap();
    let profile_name = "agency-backend-engineer";

    for (device_id, version, digest) in [
        ("mismatch", "0.1.0", Some(DIGEST_B)),
        ("exact-b", "0.1.0", Some(DIGEST_A)),
        ("digestless", "0.1.0", None),
        ("exact-a", "0.1.0", Some(DIGEST_A)),
        ("wrong-version", "0.2.0", Some(DIGEST_A)),
    ] {
        add_node(&store, device_id, profile_name, version, digest);
    }
    add_node(
        &store,
        "frontend",
        "agency-frontend-engineer",
        "0.1.0",
        Some(DIGEST_A),
    );

    let general = store
        .find_profile_candidates(profile_name, Some("0.1.0"), 1_200, policy())
        .unwrap();
    assert_eq!(
        general
            .iter()
            .map(|candidate| candidate.device_id.as_str())
            .collect::<Vec<_>>(),
        ["digestless", "exact-a", "exact-b", "mismatch"]
    );
    assert_eq!(general[0].profile_content_digest, None);
    assert_eq!(general[1].profile_content_digest.as_deref(), Some(DIGEST_A));
    assert_eq!(general[2].profile_content_digest.as_deref(), Some(DIGEST_A));
    assert_eq!(general[3].profile_content_digest.as_deref(), Some(DIGEST_B));

    let exact = store
        .find_exact_profile_candidates(profile_name, Some("0.1.0"), DIGEST_A, 1_200, policy())
        .unwrap();
    assert_eq!(
        exact
            .iter()
            .map(|candidate| candidate.device_id.as_str())
            .collect::<Vec<_>>(),
        ["exact-a", "exact-b"]
    );
    assert!(exact.iter().all(|candidate| {
        candidate.profile_version == "0.1.0"
            && candidate.profile_content_digest.as_deref() == Some(DIGEST_A)
            && candidate.readiness.scheduler_ready
    }));

    let any_version = store
        .find_exact_profile_candidates(profile_name, None, DIGEST_A, 1_200, policy())
        .unwrap();
    assert_eq!(
        any_version
            .iter()
            .map(|candidate| candidate.device_id.as_str())
            .collect::<Vec<_>>(),
        ["exact-a", "exact-b", "wrong-version"]
    );

    let other_digest = store
        .find_exact_profile_candidates(profile_name, Some("0.1.0"), DIGEST_B, 1_200, policy())
        .unwrap();
    assert_eq!(other_digest.len(), 1);
    assert_eq!(other_digest[0].device_id, "mismatch");
}

#[test]
fn exact_lookup_rejects_noncanonical_digest_queries() {
    let temporary = tempdir().unwrap();
    let store = FleetStateStore::open(temporary.path().join("fleet.sqlite3")).unwrap();
    let invalid = [
        String::new(),
        "a".repeat(63),
        "a".repeat(65),
        "A".repeat(64),
        "g".repeat(64),
    ];

    for digest in invalid {
        assert!(matches!(
            store.find_exact_profile_candidates(
                "agency-backend-engineer",
                Some("0.1.0"),
                &digest,
                1_200,
                policy(),
            ),
            Err(StateError::InvalidInput(_))
        ));
    }
}

#[test]
fn exact_lookup_survives_restart_and_readmission_fences_old_presence() {
    let temporary = tempdir().unwrap();
    let path = temporary.path().join("fleet.sqlite3");
    let profile_name = "agency-backend-engineer";

    {
        let store = FleetStateStore::open(&path).unwrap();
        add_node(&store, "device-1", profile_name, "0.1.0", Some(DIGEST_A));
        assert_eq!(
            store
                .find_exact_profile_candidates(
                    profile_name,
                    Some("0.1.0"),
                    DIGEST_A,
                    1_200,
                    policy(),
                )
                .unwrap()
                .len(),
            1
        );
    }

    let restarted = FleetStateStore::open(&path).unwrap();
    assert_eq!(
        restarted
            .find_exact_profile_candidates(profile_name, Some("0.1.0"), DIGEST_A, 1_200, policy(),)
            .unwrap()
            .len(),
        1
    );

    restarted
        .apply_projection(projection("device-1", 2))
        .unwrap();
    assert!(
        restarted
            .find_exact_profile_candidates(profile_name, Some("0.1.0"), DIGEST_A, 1_300, policy(),)
            .unwrap()
            .is_empty()
    );
    assert!(matches!(
        restarted.record_observation(
            "nodescale",
            "network-1",
            "device-1",
            observation(1, 1_250, profile_name, "0.1.0", Some(DIGEST_A)),
            1_260,
        ),
        Err(StateError::InvalidTransition(_))
    ));

    restarted
        .record_observation(
            "nodescale",
            "network-1",
            "device-1",
            observation(2, 1_400, profile_name, "0.1.0", Some(DIGEST_A)),
            1_410,
        )
        .unwrap();
    assert_eq!(
        restarted
            .find_exact_profile_candidates(profile_name, Some("0.1.0"), DIGEST_A, 1_500, policy(),)
            .unwrap()
            .len(),
        1
    );
}
