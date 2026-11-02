"""Claude Code PreToolUse hook: scan any GitHub repo with NVIDIA SkillSpector before it is installed.

Catches shell commands that bring in code from GitHub (git clone, npx skills add, pip/uv/pipx install git+…,
npm install github:…). Any CRITICAL finding or a failed scan → blocked; any other risk (CAUTION, or a high
score built from lesser findings, which static mode piles up as false positives) → Claude asks you first,
showing the findings; SAFE → the normal permission flow continues. Commands it cannot resolve to a repo
(claude mcp add, curl … | sh) always ask.

Static scan by default (free). Set SKILLSPECTOR_PROVIDER=anthropic and ANTHROPIC_API_KEY in your environment
to add SkillSpector's LLM review, which cuts false positives (costs a few cents per scan).

Standalone on purpose (stdlib only): it runs with your system Python, not Jarvis's environment.
Install the scanner once:  uv tool install git+https://github.com/NVIDIA/SkillSpector.git@v2.13.2
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys

SCAN_TIMEOUT_S = 540
INSTALL_HINT = "uv tool install git+https://github.com/NVIDIA/SkillSpector.git@v2.13.2"

_INSTALL_VERB = re.compile(
    r"\bgit\s+clone\b|\bskills?\s+add\b|\b(pip3?|pipx|uv|npm|pnpm|yarn|bun)\b[^|;&]*\b(install|add|i)\b",
    re.IGNORECASE,
)
_GITHUB_URL = re.compile(
    r"(?:https?://|git@|git\+https://)github\.com[/:]([\w.-]+)/([\w.-]+?)(?:\.git)?(?=[/@#\s'\"]|$)"
)
_GITHUB_SHORT = re.compile(r"(?:github:|\bskills?\s+add\s+)([\w.-]+)/([\w.-]+)")
_UNRESOLVABLE = re.compile(r"\bclaude\s+mcp\s+add\b|\b(curl|wget|iwr|Invoke-WebRequest)\b[^|]*\|\s*(sh|bash|iex)\b")


def github_targets(command: str) -> list[str]:
    """Repos a command would bring in, as https://github.com/owner/repo URLs (empty if it installs nothing)."""
    if not _INSTALL_VERB.search(command):
        return []
    found: list[str] = []
    for owner, repo in [*_GITHUB_URL.findall(command), *_GITHUB_SHORT.findall(command)]:
        url = f"https://github.com/{owner}/{repo.removesuffix('.git')}"
        if url not in found:
            found.append(url)
    return found


_RANK = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}


def verdict(stdout: str, returncode: int) -> tuple[str, str]:
    """(SAFE | REVIEW | BLOCK, summary). Anything unreadable fails closed as BLOCK."""
    if returncode not in (0, 1):
        return "BLOCK", f"el escáner falló (código {returncode})"
    try:
        report = json.loads(stdout)
        assessment = report["risk_assessment"]
        recommendation = str(assessment["recommendation"]).upper()
    except (json.JSONDecodeError, KeyError, TypeError):
        return "BLOCK", "el informe del escáner no se puede leer"
    if recommendation not in ("SAFE", "CAUTION", "DO_NOT_INSTALL"):
        return "BLOCK", f"recomendación desconocida: {recommendation}"
    issues = [i for i in report.get("issues") or [] if isinstance(i, dict)]
    issues.sort(key=lambda i: _RANK.get(str(i.get("severity", "")).upper(), 9))
    severities = {str(i.get("severity", "")).upper() for i in issues}
    critical = "CRITICAL" in severities or str(assessment.get("max_issue_severity", "")).upper() == "CRITICAL"
    detail = "; ".join(
        f"{i.get('severity', '?')} {i.get('category') or i.get('id', '?')}"
        f" en {(i.get('location') or {}).get('file', '?')}"
        for i in issues[:3]
    )
    summary = f"{recommendation} {assessment.get('score')}/100, {len(issues)} hallazgos" + (
        f": {detail}" if detail else ""
    )
    if critical:
        return "BLOCK", summary
    return ("SAFE" if recommendation == "SAFE" else "REVIEW"), summary


def scan(url: str, executable: str) -> tuple[str, str]:
    mode = [] if os.environ.get("SKILLSPECTOR_PROVIDER") else ["--no-llm"]
    try:
        result = subprocess.run(
            [executable, "scan", url, *mode, "--format", "json"],
            capture_output=True,
            text=True,
            timeout=SCAN_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return "BLOCK", f"el escaneo superó {SCAN_TIMEOUT_S} s"
    except OSError as exc:
        return "BLOCK", f"no pude lanzar SkillSpector: {exc}"
    return verdict(result.stdout or "", result.returncode)


def decide(command: str, executable: str | None) -> tuple[str, str] | None:
    """(permissionDecision, reason), or None to let Claude Code's normal permission flow continue."""
    targets = github_targets(command)
    if not targets:
        if _UNRESOLVABLE.search(command):
            return "ask", "Instala algo que no puedo escanear automáticamente con SkillSpector: revísalo tú."
        return None
    if executable is None:
        return (
            "deny",
            f"Antes de instalar repos de GitHub hay que escanearlos y SkillSpector no está. Instálalo: {INSTALL_HINT}",
        )
    results = [(url, *scan(url, executable)) for url in targets]
    blocked = [r for r in results if r[1] == "BLOCK"]
    if blocked:
        return "deny", "SkillSpector lo bloquea (hallazgo crítico o escaneo fallido): " + " | ".join(
            f"{u} → {why}" for u, _, why in blocked
        )
    review = [r for r in results if r[1] == "REVIEW"]
    if review:
        return "ask", "SkillSpector pide revisarlo (nada crítico; pueden ser falsos positivos): " + " | ".join(
            f"{u} → {why}" for u, _, why in review
        )
    return None


def _decision(raw: bytes) -> tuple[str, str] | None:
    # utf-8-sig: Windows PowerShell 5.1 can prepend a BOM when it pipes text into a program.
    try:
        payload = json.loads(raw.decode("utf-8-sig", errors="replace"))
        command = str((payload.get("tool_input") or {}).get("command", ""))
    except (json.JSONDecodeError, AttributeError):
        return "ask", "El hook de SkillSpector no pudo leer el comando; revísalo tú."
    try:
        return decide(command, shutil.which("skillspector"))
    except Exception as exc:  # fail closed on anything unexpected
        why = f"el hook de SkillSpector falló ({type(exc).__name__}); revísalo tú."
        return ("deny" if _INSTALL_VERB.search(command) else "ask"), why


def main() -> int:
    # Bytes in and ASCII out: Windows Python defaults to cp1252 on pipes, which can't encode "→" and
    # would crash the hook, and Claude Code lets a command run when its hook crashes.
    decision = _decision(sys.stdin.buffer.read())
    if decision is not None:
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": decision[0],
                        "permissionDecisionReason": decision[1],
                    }
                }
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
