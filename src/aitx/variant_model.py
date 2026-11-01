"""Deterministic variant analysis (no LLM).

Parses HGVS c./p. notation, builds a transcript model from the NCBI RefSeq
feature table (exon boundaries in transcript coordinates + CDS), and derives
features that the bespoke-therapy and mechanism reasoning depends on:

* consequence class (missense, nonsense, frameshift, splice, in-frame indel ...)
* exon number / total, exon coding length and reading-frame preservation
  (relevant for exon-skipping ASO amenability)
* premature-termination-codon (PTC) position and rule-based NMD prediction
  (last-exon and 50-nt rules; start-proximal caveat)
* distance to exon boundaries / splice-site class for intronic variants

All outputs are labelled as computed predictions, not experimental evidence.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from typing import Any

AA3 = {
    "Ala": "A", "Arg": "R", "Asn": "N", "Asp": "D", "Cys": "C", "Gln": "Q", "Glu": "E", "Gly": "G",
    "His": "H", "Ile": "I", "Leu": "L", "Lys": "K", "Met": "M", "Phe": "F", "Pro": "P", "Ser": "S",
    "Thr": "T", "Trp": "W", "Tyr": "Y", "Val": "V", "Ter": "*", "Sec": "U", "Xaa": "X",
}
AA1_TO_3 = {v: k for k, v in AA3.items()}

# ----------------------------------------------------------------- HGVS c.

_POS = r"([-*]?\d+)([+-]\d+)?"
C_RE = re.compile(
    rf"^c\.(?P<start>{_POS})(?:_(?P<end>{_POS}))?"
    r"(?P<rest>(?:[ACGTN]+>[ACGTN]+)|del[ACGTN]*ins[ACGTN]+|delins[ACGTN]+|del[ACGTN0-9]*|dup[ACGTN0-9]*|ins[ACGTN0-9]+|inv[ACGTN0-9]*|=)$",
    re.IGNORECASE,
)


@dataclass
class CPos:
    base: int  # c. coordinate of the anchoring exonic base (negative = 5'UTR, star = 3'UTR flagged separately)
    offset: int = 0  # intronic offset
    utr3: bool = False

    @classmethod
    def parse(cls, s: str) -> "CPos":
        m = re.fullmatch(r"([-*]?)(\d+)([+-]\d+)?", s)
        if not m:
            raise ValueError(f"bad c. position {s!r}")
        sign, num, off = m.groups()
        base = int(num)
        if sign == "-":
            base = -base
        return cls(base=base, offset=int(off) if off else 0, utr3=(sign == "*"))


@dataclass
class ParsedCDNA:
    raw: str
    start: CPos
    end: CPos
    kind: str  # substitution | deletion | duplication | insertion | delins | inversion | identity
    ref: str = ""
    alt: str = ""
    length_change: int | None = None  # net nt change (alt - ref) when derivable

    @property
    def intronic(self) -> bool:
        return self.start.offset != 0 or self.end.offset != 0


def parse_cdna(hgvs: str) -> ParsedCDNA:
    s = hgvs.strip()
    if ":" in s:
        s = s.split(":", 1)[1]
    m = C_RE.match(s)
    if not m:
        raise ValueError(f"Unsupported or invalid cDNA HGVS: {hgvs!r}")
    start = CPos.parse(m.group("start"))
    end = CPos.parse(m.group("end")) if m.group("end") else CPos(start.base, start.offset, start.utr3)
    rest = m.group("rest")
    span = (end.base - start.base + 1) if (start.offset == 0 and end.offset == 0 and not start.utr3) else None
    low = rest.lower()
    if ">" in rest:
        ref, alt = rest.split(">")
        return ParsedCDNA(hgvs, start, end, "substitution", ref.upper(), alt.upper(), 0)
    if low.startswith("delins") or (low.startswith("del") and "ins" in low):
        ins = rest[rest.lower().index("ins") + 3 :].upper()
        lc = (len(ins) - span) if span else None
        return ParsedCDNA(hgvs, start, end, "delins", "", ins, lc)
    if low.startswith("del"):
        return ParsedCDNA(hgvs, start, end, "deletion", rest[3:].upper(), "", -span if span else None)
    if low.startswith("dup"):
        return ParsedCDNA(hgvs, start, end, "duplication", rest[3:].upper(), "", span)
    if low.startswith("ins"):
        ins = rest[3:].upper()
        return ParsedCDNA(hgvs, start, end, "insertion", "", ins, len(ins) if ins.isalpha() else None)
    if low.startswith("inv"):
        return ParsedCDNA(hgvs, start, end, "inversion", "", "", 0)
    return ParsedCDNA(hgvs, start, end, "identity", "", "", 0)


# ----------------------------------------------------------------- HGVS p.


@dataclass
class ParsedProtein:
    raw: str
    predicted: bool  # parentheses = predicted consequence
    kind: str  # missense | nonsense | frameshift | inframe_del | inframe_ins | inframe_delins | synonymous | start_loss | stop_loss | extension | unknown | no_protein
    ref_aa: str = ""
    position: int | None = None
    alt_aa: str = ""
    fs_ter_offset: int | None = None  # N in fsTerN
    end_position: int | None = None

    @property
    def ptc_codon(self) -> int | None:
        if self.kind == "nonsense":
            return self.position
        if self.kind == "frameshift" and self.position and self.fs_ter_offset:
            return self.position + self.fs_ter_offset - 1
        return None


def _aa(tok: str) -> str:
    if tok in AA3:
        return tok
    if len(tok) == 1 and tok in AA1_TO_3:
        return AA1_TO_3[tok]
    if tok == "*":
        return "Ter"
    return tok


def parse_protein(hgvs: str) -> ParsedProtein:
    s = (hgvs or "").strip()
    if ":" in s:
        s = s.split(":", 1)[1]
    if not s:
        return ParsedProtein(hgvs, False, "unknown")
    predicted = "(" in s
    body = s.replace("p.", "", 1).strip("()")
    if body in ("=", "(=)"):
        return ParsedProtein(hgvs, predicted, "synonymous")
    if body in ("0", "0?"):
        return ParsedProtein(hgvs, predicted, "no_protein")
    if body == "?":
        return ParsedProtein(hgvs, predicted, "unknown")
    aa = r"(Ter|[A-Z][a-z]{2}|[A-Z*])"
    m = re.fullmatch(rf"{aa}(\d+){aa}?fs(?:Ter|\*|X)?(\d+|\?)?", body)
    if m:
        off = m.group(4)
        return ParsedProtein(hgvs, predicted, "frameshift", _aa(m.group(1)), int(m.group(2)), _aa(m.group(3) or ""),
                             int(off) if off and off.isdigit() else None)
    m = re.fullmatch(rf"Met1(\?|[A-Z][a-z]{{2}}|ext.*)?", body)
    if m and (m.group(1) in (None, "?") or (m.group(1) or "").startswith("ext")):
        return ParsedProtein(hgvs, predicted, "start_loss", "Met", 1)
    m = re.fullmatch(rf"(Ter|\*)(\d+){aa}ext.*", body)
    if m:
        return ParsedProtein(hgvs, predicted, "stop_loss", "Ter", int(m.group(2)), _aa(m.group(3)))
    m = re.fullmatch(rf"{aa}(\d+)(?:_{aa}(\d+))?(del|dup|delins[A-Za-z*]+|ins[A-Za-z*]+)", body)
    if m:
        op = m.group(5)
        kind = "inframe_del" if op == "del" else "inframe_ins" if op.startswith("ins") or op == "dup" else "inframe_delins"
        return ParsedProtein(hgvs, predicted, kind, _aa(m.group(1)), int(m.group(2)), "", None,
                             int(m.group(4)) if m.group(4) else None)
    m = re.fullmatch(rf"{aa}(\d+){aa}", body)
    if m:
        ref, pos, alt = _aa(m.group(1)), int(m.group(2)), _aa(m.group(3))
        if alt == "Ter":
            kind = "nonsense"
        elif ref == alt:
            kind = "synonymous"
        else:
            kind = "missense"
        return ParsedProtein(hgvs, predicted, kind, ref, pos, alt)
    return ParsedProtein(hgvs, predicted, "unknown")


# ----------------------------------------------------------- transcript model


@dataclass
class TranscriptModel:
    accession: str
    exons: list[tuple[int, int]]  # transcript coordinates (1-based, inclusive)
    cds_start: int
    cds_end: int  # includes stop codon
    gene: str = ""
    source_url: str = ""

    @property
    def n_exons(self) -> int:
        return len(self.exons)

    @property
    def protein_length(self) -> int:
        return (self.cds_end - self.cds_start + 1) // 3 - 1

    def c_to_tx(self, c: int) -> int:
        return c + self.cds_start - 1 if c > 0 else c + self.cds_start

    def tx_to_c(self, tx: int) -> int:
        return tx - self.cds_start + 1

    def exon_index_for_c(self, c: int) -> int | None:
        tx = self.c_to_tx(c)
        for i, (a, b) in enumerate(self.exons):
            if a <= tx <= b:
                return i
        return None

    def exon_coding_length(self, i: int) -> int:
        a, b = self.exons[i]
        lo, hi = max(a, self.cds_start), min(b, self.cds_end)
        return max(0, hi - lo + 1)

    def exon_c_bounds(self, i: int) -> tuple[int, int]:
        a, b = self.exons[i]
        return self.tx_to_c(a), self.tx_to_c(b)

    def last_junction_c(self) -> int | None:
        """c. coordinate of the last base of the penultimate exon."""
        if self.n_exons < 2:
            return None
        return self.tx_to_c(self.exons[-2][1])


def parse_feature_table(text: str, accession: str) -> TranscriptModel:
    """Parse NCBI efetch rettype=ft output for a RefSeq mRNA."""
    exons: list[tuple[int, int]] = []
    cds: tuple[int, int] | None = None
    gene = ""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        parts = line.split("\t")
        if len(parts) >= 3 and parts[0].strip().lstrip("<>").isdigit() and parts[1].strip().lstrip("<>").isdigit():
            a, b, feat = int(parts[0].strip("<>")), int(parts[1].strip("<>")), parts[2].strip()
            if feat == "exon":
                exons.append((a, b))
            elif feat == "CDS" and cds is None:
                cds = (a, b)
        elif len(parts) >= 5 and parts[3].strip() == "gene" and not gene:
            gene = parts[4].strip()
    if not exons or cds is None:
        raise ValueError(f"No exon/CDS features found for {accession}")
    exons.sort()
    return TranscriptModel(accession, exons, cds[0], cds[1], gene,
                           f"https://www.ncbi.nlm.nih.gov/nuccore/{accession}")


# ----------------------------------------------------------- analysis


@dataclass
class VariantFeatures:
    hgvs_c: str
    hgvs_p: str
    cdna_kind: str
    protein_kind: str
    consequence_class: str
    exon_number: int | None = None
    exon_total: int | None = None
    intron_number: int | None = None
    exon_length_nt: int | None = None
    exon_coding_length_nt: int | None = None
    exon_frame_preserving_if_skipped: bool | None = None
    is_first_or_last_exon: bool | None = None
    distance_to_nearest_exon_boundary_nt: int | None = None
    splice_class: str = ""
    aa_position: int | None = None
    protein_length: int | None = None
    ptc_codon: int | None = None
    ptc_c_position: int | None = None
    nmd_prediction: str = ""
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def summary(self) -> str:
        bits = [f"{self.hgvs_c} / {self.hgvs_p or 'p.?'}: {self.consequence_class}"]
        if self.exon_number:
            bits.append(f"exon {self.exon_number}/{self.exon_total}")
        if self.intron_number:
            bits.append(f"intron {self.intron_number}")
        if self.exon_coding_length_nt is not None:
            bits.append(
                f"exon coding length {self.exon_coding_length_nt} nt ("
                + ("in-frame" if self.exon_frame_preserving_if_skipped else "frame-shifting")
                + " if skipped)"
            )
        if self.aa_position and self.protein_length:
            bits.append(f"residue {self.aa_position} of {self.protein_length}")
        if self.ptc_codon:
            bits.append(f"PTC at codon ~{self.ptc_codon} (c.~{self.ptc_c_position})")
        if self.nmd_prediction:
            bits.append(f"NMD: {self.nmd_prediction}")
        if self.splice_class:
            bits.append(f"splice: {self.splice_class}")
        return "; ".join(bits)


def classify(c: ParsedCDNA, p: ParsedProtein) -> str:
    if c.intronic:
        off = c.start.offset or c.end.offset
        if abs(off) <= 2:
            return "canonical_splice_site"
        if abs(off) <= 10:
            return "splice_region_intronic"
        if abs(off) > 50:
            return "deep_intronic"
        return "intronic"
    if c.start.utr3:
        return "3_prime_utr"
    if c.start.base < 0 and c.end.base < 0:
        return "5_prime_utr"
    if p.kind in ("frameshift", "nonsense", "missense", "synonymous", "start_loss", "stop_loss"):
        return p.kind
    if p.kind.startswith("inframe"):
        return p.kind
    if c.kind in ("deletion", "duplication", "insertion", "delins") and c.length_change is not None:
        return "frameshift" if c.length_change % 3 else "inframe_indel"
    return "unknown"


def analyse_variant(hgvs_c: str, hgvs_p: str, tm: TranscriptModel | None) -> VariantFeatures:
    c = parse_cdna(hgvs_c)
    p = parse_protein(hgvs_p)
    vf = VariantFeatures(hgvs_c, hgvs_p, c.kind, p.kind, classify(c, p))
    vf.aa_position = p.position
    if p.kind == "missense" or p.kind.startswith("inframe"):
        vf.notes.append("Protein-altering, reading frame preserved: transcript expected to escape NMD; mechanism must be established from variant/gene evidence (LoF, DN, or GoF all possible).")
    if tm is None:
        vf.notes.append("Transcript model unavailable; exon-level features not computed.")
        return vf
    vf.exon_total = tm.n_exons
    vf.protein_length = tm.protein_length
    anchor = c.start.base
    if c.start.utr3:
        anchor = tm.tx_to_c(tm.cds_end) + c.start.base
    idx = tm.exon_index_for_c(anchor)
    if idx is not None:
        if c.intronic:
            off = c.start.offset
            # +offset -> intron after this exon; -offset -> intron before this exon
            vf.intron_number = idx + 1 if off > 0 else idx
            vf.distance_to_nearest_exon_boundary_nt = abs(off)
            vf.splice_class = (
                "canonical donor (+1/+2)" if 0 < off <= 2 else
                "canonical acceptor (-1/-2)" if -2 <= off < 0 else
                "near-splice intronic" if abs(off) <= 10 else
                "deep intronic (possible pseudoexon/cryptic splice; splice-switching ASO candidate only if aberrant splicing is shown)" if abs(off) > 50 else
                "intronic"
            )
        else:
            vf.exon_number = idx + 1
            a, b = tm.exons[idx]
            vf.exon_length_nt = b - a + 1
            vf.exon_coding_length_nt = tm.exon_coding_length(idx)
            vf.exon_frame_preserving_if_skipped = vf.exon_coding_length_nt % 3 == 0 and vf.exon_coding_length_nt == vf.exon_length_nt
            vf.is_first_or_last_exon = idx in (0, tm.n_exons - 1)
            cs, ce = tm.exon_c_bounds(idx)
            d = min(anchor - cs, ce - anchor)
            vf.distance_to_nearest_exon_boundary_nt = d
            if d <= 2 and not vf.is_first_or_last_exon:
                vf.splice_class = "exonic, within 3 nt of exon boundary (possible splice effect)"
            if vf.is_first_or_last_exon:
                vf.notes.append("First/last exon: exon skipping not applicable (contains start or stop/3' end).")
            elif not vf.exon_frame_preserving_if_skipped:
                vf.notes.append("Skipping this exon alone would shift the reading frame.")
    # PTC / NMD
    ptc = p.ptc_codon
    if ptc is None and vf.consequence_class == "frameshift":
        vf.notes.append("Frameshift without stated termination position; PTC location not computed.")
    if ptc:
        vf.ptc_codon = ptc
        # PTC position in mutant-mRNA coordinates, mapped back to reference c.
        # coordinates (positions downstream of an indel shift by its net length).
        mutant_c = (ptc - 1) * 3 + 1
        shift = c.length_change if (vf.consequence_class == "frameshift" and c.length_change and c.start.base < mutant_c) else 0
        ptc_c = mutant_c - shift
        vf.ptc_c_position = ptc_c
        last_j = tm.last_junction_c()
        ptc_exon = tm.exon_index_for_c(min(ptc_c, tm.tx_to_c(tm.exons[-1][1])))
        if ptc_c > tm.tx_to_c(tm.cds_end) - 3 and p.kind == "frameshift":
            vf.nmd_prediction = "PTC beyond annotated stop / in 3' region: NMD escape likely; C-terminally altered/extended protein"
        elif ptc_exon == tm.n_exons - 1:
            vf.nmd_prediction = "escape predicted (PTC in last exon) - truncated protein may be produced"
        elif last_j is not None and ptc_c > last_j - 50:
            vf.nmd_prediction = "escape predicted (PTC within ~50 nt of last exon-exon junction)"
        else:
            vf.nmd_prediction = "NMD predicted (PTC >50-55 nt upstream of last exon-exon junction)"
            if ptc <= 100:
                vf.notes.append("Start-proximal PTC: NMD efficiency may be reduced by downstream translation re-initiation.")
    return vf
