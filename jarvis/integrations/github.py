"""Official GitHub MCP server ("hands" on repos), with a fine-grained token the owner limits to chosen repos."""

from __future__ import annotations

from typing import Any

from callgate import Decision, Effect, PolicyContext, Tier, ToolSpec

SERVER_NAME = "github"
REMOTE_URL = "https://api.githubcopilot.com/mcp/"
DEFAULT_TOOLSETS = ("repos", "issues", "pull_requests", "actions")
PROTECTED_BRANCHES = frozenset({"main", "master", "production", "prod", "release"})
_REPO_KEY = "github_repos"

_BRANCH_WRITES = frozenset({"create_or_update_file", "push_files"})

SPECS: tuple[ToolSpec, ...] = (
    *(ToolSpec(p, Effect.READ, "Consulta de GitHub.") for p in ("get_*", "list_*", "search_*", "*_read")),
    ToolSpec("actions_list", Effect.READ, "Consulta de GitHub."),
    ToolSpec("actions_get", Effect.READ, "Consulta de GitHub."),
    # Branch writes: notice on a feature branch; the classifier asks for approval on protected ones.
    *(ToolSpec(n, Effect.WRITE, f"{n} en una rama.") for n in sorted(_BRANCH_WRITES)),
    *(
        ToolSpec(n, Effect.WRITE, f"{n}.")
        for n in ("create_branch", "create_pull_request", "update_pull_request_branch", "request_copilot_review")
    ),
    *(
        ToolSpec(n, Effect.WRITE, f"{n} (cambia algo visible).", tier=Tier.APPROVAL)
        for n in ("update_pull_request", "resolve_review_thread", "add_comment_to_pending_review")
    ),
    # Publishes, notifies people, deletes or runs workflows: cannot be taken back.
    *(
        ToolSpec(n, Effect.IRREVERSIBLE, f"{n} (publica o cambia algo que no se puede deshacer).")
        for n in (
            "merge_pull_request",
            "enable_pr_auto_merge",
            "delete_file",
            "actions_run_trigger",
            "create_repository",
            "fork_repository",
            "issue_write",
            "add_issue_comment",
            "add_reply_to_pull_request_comment",
            "pull_request_review_write",
            "update_issue_comment",
            "create_or_update_release",
            "sub_issue_write",
        )
    ),
)


def mcp_config(token: str, toolsets: tuple[str, ...], mode: str, local_command: str) -> dict[str, Any]:
    if mode == "local":
        return {
            "type": "stdio",
            "command": local_command,
            "args": ["stdio", "--toolsets", ",".join(toolsets)],
            "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": token},
        }
    return {
        "type": "http",
        "url": REMOTE_URL,
        "headers": {"Authorization": f"Bearer {token}", "X-MCP-Toolsets": ",".join(toolsets)},
    }


def _repo_of(tool_input: dict[str, Any]) -> str | None:
    owner, repo = tool_input.get("owner"), tool_input.get("repo")
    return f"{owner}/{repo}".lower() if owner and repo else None


def classify(tool_name: str, tool_input: dict[str, Any], ctx: PolicyContext) -> Decision | None:
    """Repo allowlist for every GitHub tool (declared or not) and branch-aware tiers for writes."""
    action = tool_name.split("__")[-1]
    allowed = ctx.extra.get(_REPO_KEY) or ()
    repo = _repo_of(tool_input)
    if repo and allowed and repo not in allowed:
        return Decision(Tier.BLOCK, f"El repositorio {repo} no está en github.repos de jarvis.yaml.")
    if action in _BRANCH_WRITES:
        branch = str(tool_input.get("branch", "")).strip().lower()
        if not branch or branch in PROTECTED_BRANCHES:
            return Decision(Tier.APPROVAL, f"Escribir directamente en la rama {branch or 'por defecto'} de {repo}.")
        return Decision(Tier.NOTIFY, f"Push a la rama {branch} de {repo}.")
    return None


def install_repo_allowlist(ctx: PolicyContext, repos: tuple[str, ...]) -> None:
    ctx.extra[_REPO_KEY] = tuple(r.lower() for r in repos)
