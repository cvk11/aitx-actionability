#!/usr/bin/env python
"""Build the local GeneReviews snapshot used by the `genereviews_*` tools.

Why: GeneReviews chapter pages on ncbi.nlm.nih.gov/books are protected by a
browser challenge for automated clients (we do not bypass it). NCBI publishes
the complete GeneReviews book through its official literature archive
(https://ftp.ncbi.nlm.nih.gov/pub/litarch/, file listed in file_list.csv) and
gene->chapter maps in https://ftp.ncbi.nlm.nih.gov/pub/GeneReviews/.

This script streams the archive, extracts only chapter NXML files (no images
/PDFs), downloads the gene/chapter maps and writes a dated manifest. The
snapshot is NOT committed (see .gitignore); it is rebuilt on each deployment.
Citations always point to the canonical Bookshelf URL of the chapter.

Usage: python scripts/build_genereviews_index.py [--out data/genereviews]
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import sys
import tarfile
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
LITARCH = "https://ftp.ncbi.nlm.nih.gov/pub/litarch/"
MAPS = {
    "GRshortname_NBKid_genesymbol_dzname.txt": "https://ftp.ncbi.nlm.nih.gov/pub/GeneReviews/GRshortname_NBKid_genesymbol_dzname.txt",
    "GRtitle_shortname_NBKid.txt": "https://ftp.ncbi.nlm.nih.gov/pub/GeneReviews/GRtitle_shortname_NBKid.txt",
    "NBKid_shortname_OMIM.txt": "https://ftp.ncbi.nlm.nih.gov/pub/GeneReviews/NBKid_shortname_OMIM.txt",
}


def find_archive(client: httpx.Client) -> tuple[str, str]:
    r = client.get(LITARCH + "file_list.csv")
    r.raise_for_status()
    for row in csv.DictReader(io.StringIO(r.text)):
        if row.get("Accession ID") == "NBK1116":
            return LITARCH + row["File"], row.get("Last Updated (YYYY-MM-DD HH:MM:SS)", "")
    raise RuntimeError("GeneReviews (NBK1116) not found in litarch file_list.csv")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "data" / "genereviews"))
    args = ap.parse_args()
    out = Path(args.out)
    (out / "nxml").mkdir(parents=True, exist_ok=True)
    with httpx.Client(timeout=httpx.Timeout(120, connect=30), follow_redirects=True,
                      headers={"User-Agent": "aitx-actionability/0.1 (research)"}) as client:
        for name, url in MAPS.items():
            r = client.get(url)
            r.raise_for_status()
            (out / name).write_text(r.text, encoding="utf-8")
        url, updated = find_archive(client)
        print(f"Streaming {url} (archive last updated {updated})", flush=True)
        n = 0
        sha = hashlib.sha256()
        with client.stream("GET", url) as resp:
            resp.raise_for_status()

            class _Stream(io.RawIOBase):
                def __init__(self, it):
                    self.it, self.buf = it, b""

                def readable(self):
                    return True

                def readinto(self, b):
                    while not self.buf:
                        try:
                            chunk = next(self.it)
                        except StopIteration:
                            return 0
                        sha.update(chunk)
                        self.buf = chunk
                    k = min(len(b), len(self.buf))
                    b[:k], self.buf = self.buf[:k], self.buf[k:]
                    return k

            raw = io.BufferedReader(_Stream(resp.iter_bytes(1 << 20)), buffer_size=1 << 20)
            with tarfile.open(fileobj=raw, mode="r|gz") as tar:
                for m in tar:
                    if not m.isfile() or not m.name.endswith(".nxml"):
                        continue
                    name = Path(m.name).name
                    if "/" in name or name.startswith("."):
                        continue
                    f = tar.extractfile(m)
                    if f is None:
                        continue
                    (out / "nxml" / name).write_bytes(f.read())
                    n += 1
                    if n % 200 == 0:
                        print(f"  {n} chapters", flush=True)
    manifest = {
        "source_archive": url,
        "archive_last_updated": updated,
        "archive_sha256": sha.hexdigest(),
        "built_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "n_nxml_files": n,
        "maps": MAPS,
        "license_note": "GeneReviews content (c) University of Washington; used for non-commercial research retrieval only. Not redistributed.",
    }
    (out / "MANIFEST.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
