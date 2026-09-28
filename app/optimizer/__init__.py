"""
Skill Optimizer for WebAgent — self-improving skill layer.

Pipeline: prefilter → proposer → executor → reviewer → proposer(present) → deployer
"""

from app.optimizer.runner import run_optimizer_async

__all__ = ["run_optimizer_async"]
