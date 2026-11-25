"""Similarity provider architecture (Master Project Directive
Phase 4c / D-05).

ONE seam between Cinemora and any candidate-relevance similarity
algorithm:

- :class:`SimilarityProvider` is the interface every consumer
  codes against: ``score(...) -> SimilarityScore`` (a total plus
  its named components) and ``topical_overlap(wanted, candidate)``
  for the lexical overlap fraction those components are built
  from.
- :class:`TokenOverlapSimilarity` is the single concrete
  implementation — and it is named honestly: the score is
  DETERMINISTIC STEMMED TOKEN OVERLAP over candidate metadata
  text, plus fixed subject / era / wrong-person terms. It is NOT
  semantic similarity, NOT embeddings, and NOT an ML model. No
  embedding/vector provider exists in Cinemora (D-05); adding one
  would be a new class behind this interface, gated on measured
  evidence — the standing rule is that the acceptance thresholds
  (0.55 / 0.34 / 0.22, in story_understanding.ACCEPT_THRESHOLD)
  change only on measured evidence, and none is gathered here.

The weights below are EXACTLY the pre-seam verify_candidate
formula; the seam changes no number:

  subject alias hit ............ +0.45
  topical token overlap ........ +0.45 * min(1, overlap * 1.5)
  era year present ............. +0.10
  names a different person ..... -0.30
"""

from dataclasses import dataclass, field


@dataclass
class SimilarityScore:
    """One scored pair: the total plus its named components.

    ``components`` keys are the verdict's long-standing
    contribution keys (subject / topical / era / wrong_person) —
    an API/record contract, unchanged by the seam."""

    total: float
    components: dict = field(default_factory=dict)


class SimilarityProvider:
    """The similarity interface. Concrete providers implement
    :meth:`score` and :meth:`topical_overlap`."""

    name = "abstract"

    def topical_overlap(self, wanted_tokens, candidate_tokens):
        """Fraction of the wanted (stemmed) token set present in
        the candidate's (stemmed) token set, in [0, 1]."""
        raise NotImplementedError

    def score(self, *, subject_hit=False, topical_overlap=None,
              era_hit=False, wrong_person_hit=False) -> SimilarityScore:
        """Score one candidate from its judged facts. Returns a
        SimilarityScore; never raises for ordinary inputs."""
        raise NotImplementedError


class TokenOverlapSimilarity(SimilarityProvider):
    """Deterministic stemmed token-overlap similarity (lexical,
    not semantic). This is the pre-D-05 verify_candidate formula,
    moved behind the seam unchanged."""

    name = "token_overlap"

    SUBJECT_WEIGHT = 0.45
    TOPICAL_WEIGHT = 0.45
    TOPICAL_SATURATION = 1.5
    ERA_WEIGHT = 0.10
    WRONG_PERSON_WEIGHT = -0.30

    def topical_overlap(self, wanted_tokens, candidate_tokens):
        wanted = set(wanted_tokens or ())
        if not wanted:
            return 0.0
        candidate = set(candidate_tokens or ())
        return len(wanted & candidate) / len(wanted)

    def score(self, *, subject_hit=False, topical_overlap=None,
              era_hit=False, wrong_person_hit=False) -> SimilarityScore:
        total = 0.0
        components = {
            "subject": 0.0, "topical": 0.0, "era": 0.0, "wrong_person": 0.0,
        }
        if subject_hit:
            total += self.SUBJECT_WEIGHT
            components["subject"] = self.SUBJECT_WEIGHT
        if topical_overlap is not None:
            topical = self.TOPICAL_WEIGHT * min(
                1.0, topical_overlap * self.TOPICAL_SATURATION
            )
            total += topical
            components["topical"] = round(topical, 3)
        if era_hit:
            total += self.ERA_WEIGHT
            components["era"] = self.ERA_WEIGHT
        if wrong_person_hit:
            total += self.WRONG_PERSON_WEIGHT
            components["wrong_person"] = self.WRONG_PERSON_WEIGHT
        return SimilarityScore(total=total, components=components)


_default_provider = None


def get_provider() -> SimilarityProvider:
    """The deployment's similarity provider (the token-overlap
    implementation — the only one that exists)."""
    global _default_provider
    if _default_provider is None:
        _default_provider = TokenOverlapSimilarity()
    return _default_provider
