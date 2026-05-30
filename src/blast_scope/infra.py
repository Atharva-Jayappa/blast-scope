"""Infrastructure consequences — files whose blast radius leaves the repo.

The dependency graph only sees code symbols. It is blind to infrastructure
descriptors: a Dockerfile, a Terraform module, a Kubernetes manifest, a CI
workflow. These have *zero* AST in-degree yet deleting or rewriting one can
break deployments, tear down cloud resources, or disable the pipeline that
ships every other change. This module recognises such files by name/extension
and assigns a moderate floor so they are never scored as inert.
"""

from __future__ import annotations

import logging
from pathlib import Path

from blast_scope.consequences import Consequence

logger = logging.getLogger(__name__)


# Exact filenames (case-insensitive) that are infrastructure descriptors.
_INFRA_NAMES: dict[str, str] = {
    "dockerfile": "a Dockerfile — defines the image every deployment is built from",
    "docker-compose.yml": "a docker-compose file — defines the local/CI service topology",
    "docker-compose.yaml": "a docker-compose file — defines the local/CI service topology",
    "compose.yml": "a docker-compose file — defines the local/CI service topology",
    "compose.yaml": "a docker-compose file — defines the local/CI service topology",
    "makefile": "a Makefile — drives builds and deploy targets",
    "procfile": "a Procfile — declares the process types a platform runs",
    "vagrantfile": "a Vagrantfile — defines the dev VM",
    "jenkinsfile": "a Jenkinsfile — defines the CI/CD pipeline",
    ".gitlab-ci.yml": "a GitLab CI pipeline definition",
    ".travis.yml": "a Travis CI pipeline definition",
    "skaffold.yaml": "a Skaffold config — drives k8s build/deploy",
}

# Extensions that mark infrastructure-as-code.
_INFRA_SUFFIXES: dict[str, str] = {
    ".tf": "a Terraform module — applying changes can create or destroy cloud resources",
    ".tfvars": "Terraform variables — feed real infrastructure applies",
    ".hcl": "an HCL config (Terraform/Nomad/Packer) — drives infrastructure",
}

# Path fragments (POSIX-normalised) that mark CI / k8s / helm trees.
_INFRA_PATH_FRAGMENTS: dict[str, str] = {
    ".github/workflows": "a GitHub Actions workflow — controls CI/CD for the repo",
    ".circleci": "a CircleCI config — controls the build pipeline",
    "helm": "a Helm chart file — templates Kubernetes deployments",
    "k8s": "a Kubernetes manifest — describes live cluster resources",
    "kubernetes": "a Kubernetes manifest — describes live cluster resources",
    "terraform": "part of a Terraform configuration — drives infrastructure",
}

# Floor for infrastructure files: serious, but rarely as final as a lost secret.
_INFRA_FLOOR = 0.6


def classify_infra(path: Path) -> Consequence | None:
    """Return an infra ``Consequence`` for ``path``, or ``None``.

    Recognises Dockerfiles, compose files, Terraform/HCL, CI configs, and
    Kubernetes/Helm manifests by name, extension, or enclosing directory.

    Args:
        path: A target path (absolute or relative) the command would touch.

    Returns:
        A ``Consequence`` in the ``infra`` domain with a moderate floor, or
        ``None`` if the path is not an infrastructure descriptor.

    Example::

        >>> classify_infra(Path("/proj/Dockerfile")).domain
        'infra'
        >>> classify_infra(Path("/proj/main.tf")).floor
        0.6
        >>> classify_infra(Path("/proj/src/app.py")) is None
        True
    """
    name = path.name.lower()

    if name in _INFRA_NAMES:
        return Consequence("infra", _INFRA_FLOOR, _infra_msg(_INFRA_NAMES[name]))

    suffix = path.suffix.lower()
    if suffix in _INFRA_SUFFIXES:
        return Consequence("infra", _INFRA_FLOOR, _infra_msg(_INFRA_SUFFIXES[suffix]))

    # Directory-context heuristics (CI workflows, helm/k8s trees).
    posix = path.as_posix().lower()
    for fragment, reason in _INFRA_PATH_FRAGMENTS.items():
        if f"/{fragment}/" in posix or posix.endswith(f"/{fragment}"):
            return Consequence("infra", _INFRA_FLOOR, _infra_msg(reason))

    return None


def _infra_msg(reason: str) -> str:
    return f"touches {reason} — impact reaches deploy/runtime, not just the code graph"
