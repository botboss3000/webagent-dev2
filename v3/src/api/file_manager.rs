//! Administrator File Manager admission. Browser identity and CSRF are checked
//! by the router; the isolated worker confines every path to /live-checkout.
use super::*;

pub(crate) async fn action(
    State(app): State<App>,
    Json(body): Json<Value>,
) -> Api {
    let action = body["action"].as_str().unwrap_or("");
    let git = matches!(action, "git_status" | "git_graph" | "git_commit_detail"
        | "git_branches" | "git_tree_status" | "git_line_stats"
        | "git_ignore" | "git_tool" | "git_commit" | "git_settings");
    if !git && !matches!(action, "tree" | "read" | "write" | "create" | "rename" | "delete") {
        return Err(bad("Unsupported File Manager action"));
    }
    // This admin route owns admission. The worker owns checkout confinement;
    // repository secrets are passed only for operations that need transport.
    let needs_transport = action == "git_settings" || action == "git_tool"
        && matches!(body["operation"].as_str(), Some("fetch" | "pull" | "push"));
    let trusted = if needs_transport {
        Some(json!({"repository": repository::transport_for_agent(&app,"shared_default").await?}))
    } else { None };
    let result = tools::execute_trusted(
        if git { "administrator_file_git" } else { "administrator_file_manager" }, &body,
        "00000000-0000-0000-0000-000000000000",
        &app.tool_socket, trusted, CancellationToken::new(),
    ).await?;
    if result["uncertain"] == true {
        return Err(crate::conflict("Git effect uncertain; inspect repository before retry"));
    }
    if result["status"] != "success" {
        let detail = result["output"].as_str().unwrap_or("File Manager operation failed");
        return Err(bad(detail));
    }
    Ok(Json(result["output"].clone()))
}
