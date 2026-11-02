import pytest

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(BACKEND.parents[1] / "Deployment" / "scripts"))


@pytest.fixture(autouse=True)
def default_redaction():
    """The redaction list is per process; each test starts from the default (hide everything)."""
    from chatbot import privacy

    privacy.configure(privacy.CATEGORIES)
    yield
    privacy.configure(privacy.CATEGORIES)
