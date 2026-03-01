# Copyright (c) 2026 Steve Flinter. MIT License.
from __future__ import annotations

import os
import stat
from dataclasses import dataclass, field

from conductor.config import JobConfig

import logging
log = logging.getLogger(__name__)


@dataclass
class ValidationResult:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return len(self.errors) == 0


def validate(configs: list[JobConfig], check_gpu: bool = True) -> ValidationResult:
    result = ValidationResult()

    if not configs:
        result.errors.append("No jobs configured")
        return result

    _check_required_fields(configs, result)
    _check_ssh_key(configs, result)
    _check_local_dirs(configs, result)
    _check_dependencies(configs, result)
    _check_budgets(configs, result)

    if check_gpu:
        _check_gpu_ids(configs, result)

    return result


def _check_required_fields(configs: list[JobConfig], result: ValidationResult) -> None:
    names = set()
    for cfg in configs:
        if not cfg.name:
            result.errors.append("Job has empty name")
        if not cfg.run_command:
            result.errors.append(f"Job '{cfg.name}' has empty run_command")
        if cfg.name in names:
            result.errors.append(f"Duplicate job name: '{cfg.name}'")
        names.add(cfg.name)


def _check_ssh_key(configs: list[JobConfig], result: ValidationResult) -> None:
    checked = set()
    for cfg in configs:
        key = cfg.ssh_key_path
        if key in checked:
            continue
        checked.add(key)

        if not os.path.exists(key):
            result.errors.append(f"SSH key not found: {key}")
            continue

        mode = os.stat(key).st_mode
        perms = stat.S_IMODE(mode)
        if perms & 0o077:
            result.warnings.append(f"SSH key {key} has loose permissions ({oct(perms)}), expected 0o600")


def _check_local_dirs(configs: list[JobConfig], result: ValidationResult) -> None:
    checked = set()
    for cfg in configs:
        if cfg.deploy_method != "rsync":
            continue
        d = cfg.local_project_dir
        if d in checked:
            continue
        checked.add(d)
        if not os.path.isdir(d):
            result.errors.append(f"local_project_dir does not exist: {d}")


def _check_dependencies(configs: list[JobConfig], result: ValidationResult) -> None:
    names = {cfg.name for cfg in configs}

    for cfg in configs:
        for dep in cfg.depends_on:
            if dep not in names:
                result.errors.append(f"Job '{cfg.name}' depends on unknown job '{dep}'")

    # Cycle detection via DFS coloring
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {cfg.name: WHITE for cfg in configs}
    deps_map = {cfg.name: cfg.depends_on for cfg in configs}

    def dfs(node: str) -> bool:
        color[node] = GRAY
        for dep in deps_map.get(node, []):
            if dep not in color:
                continue
            if color[dep] == GRAY:
                result.errors.append(f"Dependency cycle detected involving '{node}' and '{dep}'")
                return True
            if color[dep] == WHITE:
                if dfs(dep):
                    return True
        color[node] = BLACK
        return False

    for cfg in configs:
        if color[cfg.name] == WHITE:
            dfs(cfg.name)


def _check_budgets(configs: list[JobConfig], result: ValidationResult) -> None:
    for cfg in configs:
        if cfg.budget_usd < 0:
            result.errors.append(f"budget_usd is negative: {cfg.budget_usd}")
        if cfg.job_budget_usd < 0:
            result.errors.append(f"Job '{cfg.name}' has negative job_budget_usd")


def _check_gpu_ids(configs: list[JobConfig], result: ValidationResult) -> None:
    try:
        from conductor.gpu_pricing import validate_gpu_id
    except Exception:
        result.warnings.append("Could not import gpu_pricing, skipping GPU validation")
        return

    checked = set()
    for cfg in configs:
        if cfg.auto_select_cheapest_gpu:
            continue
        for gpu_id in [cfg.gpu_type_id] + cfg.gpu_type_ids_fallback:
            if not gpu_id or gpu_id in checked:
                continue
            checked.add(gpu_id)
            try:
                if not validate_gpu_id(gpu_id):
                    result.warnings.append(f"GPU type '{gpu_id}' not found in RunPod API")
            except Exception:
                result.warnings.append(f"Could not validate GPU type '{gpu_id}' (API error)")
