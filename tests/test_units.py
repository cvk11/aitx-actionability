from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

from aitx import prompts
from aitx.clinical import parse_clinical_context
from aitx.ledger import EvidenceLedger, EvidenceRecord, extract_keys, normalize_key
from aitx.qc import check_report_structure, validate_submission
from aitx.retrieval.registry import TOOL_SPECS
from aitx.schemas import PatientCase
from aitx.template import EXPECTED_HEADINGS, required_headings
from aitx.variant_model import TranscriptModel, analyse_variant, parse_cdna, parse_protein

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------- official files
def test_official_file_hashes():
    table = (ROOT / "data" / "official" / "SOURCES.md").read_text()
    for path, sha in re.findall(r"\| `([^`]+)` \| https://\S+ \| ([0-9a-f]{64}) \|", table):
        assert hashlib.sha256((ROOT / path).read_bytes()).hexdigest() == sha, path


def test_official_cases_validate(official_cases):
    assert [c["id"] for c in official_cases] == ["AITX-40001", "AITX-40002", "AITX-40003", "AITX-40004"]
    for c in official_cases:
        PatientCase.model_validate(c)


def test_template_headings_match_official_file():
    assert required_headings() == EXPECTED_HEADINGS


# ---------------------------------------------------------------- anti-hardcoding
def test_no_official_case_specifics_in_code_or_prompts(official_cases):
    from aitx.evidence import variant_notations

    banned = set()
    for c in official_cases:
        banned.add(c["id"])
        for v in c["patient"]["genotype"]:
            banned |= {v["gene"], v["transcript"], v["variant_cdna"]}
            # also every spelling of the variant (p.Asp34Asn, Asp34Asn, D34N, ...): a short protein notation is
            # case content too, and tuning production code or prompts to it would be case-specific tuning
            banned |= variant_notations(v["variant_cdna"], v["variant_protein"])
    files = list((ROOT / "src").rglob("*.py")) + list((ROOT / "prompts").glob("*.md")) + [ROOT / "deployment" / "app.py"]
    for f in files:
        if not f.exists():
            continue
        text = f.read_text()
        for b in banned:
            assert not re.search(rf"(?<![A-Za-z0-9]){re.escape(b)}(?![A-Za-z0-9])", text), f"{b} hardcoded in {f}"


# ---------------------------------------------------------------- HGVS / variant model
@pytest.mark.parametrize("hgvs,kind,lc", [("c.100G>A", "substitution", 0), ("c.1099dup", "duplication", 1), ("c.1521_1523del", "deletion", -3),
                                          ("c.33dup", "duplication", 1), ("c.123+1G>A", "substitution", 0), ("c.10453_10454delinsTA", "delins", 0),
                                          ("c.-12C>T", "substitution", 0), ("c.*45A>G", "substitution", 0)])
def test_parse_cdna(hgvs, kind, lc):
    p = parse_cdna(hgvs)
    assert p.kind == kind and p.length_change == lc


@pytest.mark.parametrize("hgvs,kind,pos,ptc", [("p.Asp34Asn", "missense", 34, None), ("p.(Gln367ProfsTer48)", "frameshift", 367, 414),
                                               ("p.(Leu3485Ter)", "nonsense", 3485, 3485), ("p.Phe508del", "inframe_del", 508, None),
                                               ("p.(Lys12GlnfsTer156)", "frameshift", 12, 167), ("p.R12*", "nonsense", 12, 12), ("p.Met1?", "start_loss", 1, None)])
def test_parse_protein(hgvs, kind, pos, ptc):
    p = parse_protein(hgvs)
    assert (p.kind, p.position, p.ptc_codon) == (kind, pos, ptc)


def _tm():  # 4 exons; CDS 51..653 (200 aa + stop)
    return TranscriptModel("NM_TEST.1", [(1, 150), (151, 300), (301, 480), (481, 900)], 51, 653)


def test_exon_frame_and_location():
    vf = analyse_variant("c.100G>A", "p.Asp34Asn", _tm())  # tx 150 -> exon 1
    assert vf.exon_number == 1 and vf.is_first_or_last_exon
    vf = analyse_variant("c.200G>A", "p.Arg67His", _tm())  # tx 250 -> exon 2, length 150 (in frame)
    assert vf.exon_number == 2 and vf.exon_frame_preserving_if_skipped is True
    vf = analyse_variant("c.300G>A", "p.Arg100His", _tm())  # exon 3 length 180 (in frame)
    assert vf.exon_number == 3


def test_nmd_rules():
    tm = _tm()  # last junction at c. 430 (tx 480)
    assert "NMD predicted" in analyse_variant("c.100C>T", "p.Arg34Ter", tm).nmd_prediction
    assert "escape" in analyse_variant("c.500C>T", "p.Arg167Ter", tm).nmd_prediction  # last exon
    assert "50 nt" in analyse_variant("c.400C>T", "p.Arg134Ter", tm).nmd_prediction  # within 50 nt of junction


def test_intronic_splice_class():
    vf = analyse_variant("c.100+1G>A", "p.?", _tm())
    assert vf.consequence_class == "canonical_splice_site" and vf.intron_number == 1


# ---------------------------------------------------------------- clinical parsing
def test_clinical_parsing_on_official_contexts(official_cases):
    got = {c["id"]: parse_clinical_context(c["patient"]["clinical_context"]) for c in official_cases}
    assert [round(got[k].age_years, 2) for k in sorted(got)] == [3.0, 7.0, 14.0, 1.5]
    assert [got[k].sex for k in sorted(got)] == ["male", "male", "male", "female"]


def test_clinical_unknowns():
    f = parse_clinical_context("Progressive muscle weakness")
    assert f.age_years is None and f.sex == "unknown"


# ---------------------------------------------------------------- ledger / prompts / tools
def test_ledger_keys():
    assert normalize_key(" pmid 123 ") == "PMID:123"
    assert normalize_key("nct01234567") == "NCT01234567"
    assert extract_keys("a [PMID:1] b [NCT01234567] c [GENEREVIEWS:NBK1#X] [junk]") == ["PMID:1", "NCT01234567", "GENEREVIEWS:NBK1#X"]
    L = EvidenceLedger()
    L.add(EvidenceRecord("PMID:1", "pubmed", "t", "u", content="short"))
    L.add(EvidenceRecord("PMID:1", "pubmed", "t", "u", content="much longer abstract", content_scope="abstract"))
    assert L.get("PMID:1").content_scope == "abstract" and len(L) == 1


def test_prompts_render_and_hash():
    for name in ("interpreter", "research_established", "research_emerging", "research_hypothesized", "synthesis"):
        kw = {"CONTEXT": "{{X}} braces {} ok"} if name != "interpreter" else {}
        if name == "synthesis":
            kw |= {"ITEMS": "[]", "EVIDENCE": ""}
        txt = prompts.render(name, **kw)
        assert "{{COMMON_RULES}}" not in txt and "Shared rules" in txt
    with pytest.raises(KeyError):
        prompts.render("verifier", CLAIMS="[]")
    assert all(len(h) == 64 for h in prompts.manifest().values())


def test_tool_schemas_are_strict():
    for t in TOOL_SPECS:
        s = t["input_schema"]
        assert s["additionalProperties"] is False and set(s["required"]) == set(s["properties"]), t["name"]


def test_structured_output_schemas_transform():
    import anthropic
    from aitx import schemas as S
    for m in (S.Interpretation, S.SectionResearchOutput, S.VerificationBatch, S.RevisionPlan, S.ReviewReport, S.Adjudication, S.Synthesis, S.JudgeReport):
        sch = anthropic.transform_schema(m)
        assert sch["type"] == "object" and sch.get("additionalProperties") is False


# ---------------------------------------------------------------- QC
def _minimal_report(case_id="AITX-1"):
    parts = [f"# Therapeutic Actionability Report for Patient {case_id}", ""]
    for lvl, t in EXPECTED_HEADINGS:
        parts += ["#" * lvl + " " + t, "", "- text", ""]
    return "\n".join(parts)


def test_structure_ok_and_errors():
    assert check_report_structure(_minimal_report(), "AITX-1").ok
    bad = _minimal_report().replace("### Drug Repurposing", "### Repurposing")
    assert not check_report_structure(bad, "AITX-1").ok
    swapped = _minimal_report().replace("### Supportive Management", "### TMP").replace("### Existing Targeted Therapies", "### Supportive Management").replace("### TMP", "### Existing Targeted Therapies")
    r = check_report_structure(swapped, "AITX-1")
    assert not r.ok and any("order" in e for e in r.errors)
    assert not check_report_structure(_minimal_report() + "\n[PMID:123]", "AITX-1").ok


def test_validate_submission():
    ids = ["A-1", "A-2"]
    good = [{"id": i, "report_markdown": _minimal_report(i)} for i in ids]
    assert validate_submission(json.dumps(good), ids).ok
    assert not validate_submission(good[:1], ids).ok
    assert not validate_submission([{**good[0], "extra": 1}, good[1]], ids).ok
    assert not validate_submission("{not json", ids).ok


def test_clinvar_exact_title_match():
    from aitx.retrieval.ncbi import _clinvar_title_matches as m
    assert m("NM_000492.3(CFTR):c.1521_1523delCTT (p.Phe508del)", "c.1521_1523del", "NM_000492.4")  # version may differ
    assert not m("NM_000492.3(CFTR):c.[1521_1523delCTT;3080T>C]", "c.1521_1523del", "NM_000492.4")  # complex allele
    assert not m("NM_000492.4(CFTR):c.1624G>T (p.Gly542Ter)", "c.1521_1523del", "NM_000492.4")  # other variant
    assert not m("NM_111111.1(X):c.100G>A", "c.100G>A", "NM_999999.1")  # other transcript
