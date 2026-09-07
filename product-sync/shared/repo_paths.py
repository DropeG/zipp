from pathlib import Path


# Clients, configuration and runtime state belong to this self-contained
# product-sync package, not to the repository that happens to contain it.
REPO_ROOT = Path(__file__).resolve().parents[1]
