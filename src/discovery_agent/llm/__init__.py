"""Claude-powered layer: hypothesis synthesis and agentic investigation (optional; needs ``anthropic``)."""

from .client import LLMError, LLMRefusal
from .investigator import InvestigationResult, Investigator
from .synthesizer import HypothesisSynthesizer

__all__ = ["HypothesisSynthesizer", "Investigator", "InvestigationResult", "LLMError", "LLMRefusal"]
