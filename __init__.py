"""ds4 (DwarfStar) as a Hermes model provider. Loading this registers the profile and nothing else."""

from providers import register_provider

from .provider import Ds4Profile

register_provider(Ds4Profile())
