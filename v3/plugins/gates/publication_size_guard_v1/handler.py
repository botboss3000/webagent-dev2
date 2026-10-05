"""Optional publication-size gate logic, loaded by the isolated gate worker."""
MODULE_ID = "publication_size_guard_v1"
def publication_limit(manifest):
    # The optional plugin's own JSON is packaged with this isolated worker.
    # Rust validates activation and still enforces the native publication limit.
    if not isinstance(manifest, dict) or set(manifest) != {
        "schema_version", "id", "checkpoint", "runtime", "config"
    }:
        raise ValueError("invalid publication plugin manifest")
    if (type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1
            or manifest["id"] != MODULE_ID or manifest["checkpoint"] != "publication"
            or manifest["runtime"] != "python_gate"):
        raise ValueError("invalid publication plugin binding")
    config = manifest["config"]
    if not isinstance(config, dict) or set(config) != {"max_text_bytes"}:
        raise ValueError("invalid publication plugin config")
    value = config["max_text_bytes"]
    if type(value) is not int or not 1 <= value <= 262144:
        raise ValueError("invalid publication plugin size")
    return value



def evaluate(request, max_publication_bytes):
    exceeds = request["facts"]["text_bytes"] > max_publication_bytes
    return {
        "protocol_version": request["protocol_version"],
        "request_id": request["request_id"],
        "module_id": request["module_id"],
        "decision": "reject" if exceeds else "pass",
        "reason_code": "publication_size_exceeded" if exceeds else None,
    }
