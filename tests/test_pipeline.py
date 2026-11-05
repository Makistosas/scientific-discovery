from discovery_agent.models import Modulation, Stance
from discovery_agent.pipeline import DiscoveryPipeline


def test_discover_disease_ranks_under_explored_causal_target_first(sources, settings):
    result = DiscoveryPipeline(sources, settings).discover_disease("test fibrosis", top_k=5, enrich_k=5)
    symbols = [o.target.symbol for o in result.opportunities]
    # GENEB already has an approved drug for a subtype -> excluded; GENEC has too little evidence
    assert symbols[0] == "GENEA"
    assert "GENEB" not in symbols and "GENEC" not in symbols
    assert "GENED" in symbols
    top = result.opportunities[0]
    assert top.enriched
    assert top.direction.modulation is Modulation.ACTIVATION  # LoF -> risk
    assert top.attention.publications == 3
    assert top.attention.parent_publications == 5 and top.attention.parent_disease == "parent fibrosis"
    assert top.tractability.potent_compounds == 25 and top.tractability.pdb_with_ligand == 2
    assert [e.id for e in top.evidence] == [f"E{i}" for i in range(1, len(top.evidence) + 1)]
    assert result.candidates_considered == 4


def test_include_known_keeps_clinical_targets(sources, settings):
    result = DiscoveryPipeline(sources, settings).discover_disease("test fibrosis", top_k=5, enrich_k=5,
                                                                   include_known=True)
    assert "GENEB" in [o.target.symbol for o in result.opportunities]


def test_contradicting_trial_is_captured(sources, settings):
    result = DiscoveryPipeline(sources, settings).discover_disease("test fibrosis", top_k=5, enrich_k=5)
    gened = next(o for o in result.opportunities if o.target.symbol == "GENED")
    contra = [e for e in gened.evidence if e.stance is Stance.CONTRADICTS]
    assert contra and "NCT00000001" in contra[0].summary
    assert gened.attention.trials_stopped == 1
    assert gened.scores.safety_risk > 0.3  # essential + safety events + pleiotropy


def test_discover_target_skips_broad_categories(sources, settings):
    result = DiscoveryPipeline(sources, settings).discover_target("GENEA", top_k=5, enrich_k=5)
    names = [o.disease.name for o in result.opportunities]
    assert "hereditary disease" not in names
    assert "test fibrosis" in names


def test_analyze_pair(sources, settings):
    result = DiscoveryPipeline(sources, settings).analyze_pair("GENEA", "test fibrosis")
    assert result.mode == "pair" and len(result.opportunities) == 1
    opp = result.opportunities[0]
    assert opp.association.score == 0.8
    assert opp.enriched and opp.scores.opportunity > 0


def test_failing_source_becomes_warning(sources, settings):
    def boom(*a, **k):
        raise RuntimeError("UniProt down")

    sources.uniprot.entry = boom
    result = DiscoveryPipeline(sources, settings).analyze_pair("GENEA", "test fibrosis")
    opp = result.opportunities[0]
    assert any("UniProt down" in w for w in opp.warnings)
    assert opp.enriched


def test_qtl_colocalisation_sets_direction(sources, settings):
    opp = DiscoveryPipeline(sources, settings).analyze_pair("GENED", "test fibrosis").opportunities[0]
    assert opp.locus and opp.locus.supported and opp.locus.coloc[0]["tissue"] == "lung"
    # GWAS row has no LoF/GoF annotation; expression up-regulation (expression atlas) and eQTL both say inhibit
    assert opp.direction.modulation is Modulation.INHIBITION
    locus_items = [e for e in opp.evidence if "colocalisation" in e.source]
    assert locus_items and locus_items[0].effect_direction is Modulation.INHIBITION
    assert locus_items[0].references[0].url.endswith("/credible-set/LOC1")
