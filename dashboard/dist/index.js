// Hermes Fleet is a native Desktop plugin. The legacy web dashboard still
// loads every enabled dashboard manifest, including hidden tabs, and requires
// each script to register successfully. Register a non-visible null component
// so backend route discovery remains healthy without exposing duplicate UI.
window.__HERMES_PLUGINS__?.register("hermes-fleet", () => null)
