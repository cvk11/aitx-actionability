# Official source files (verbatim copies)

Retrieved 2026-11-01 (UTC) with `curl` from the official AI-Tx Challenge website. Files are byte-for-byte copies; do not edit.

| Local file | Official URL | SHA-256 |
|---|---|---|
| `data/official_cases/aitx_final_patients.json` | https://aitxchallenge.org/assets/json/final_challenge/aitx_final_patients.json | a47f5a36bd187127373c8ecccea1502f086efc8dff4bcde7d012e682144f1583 |
| `data/official_cases/AITX-40001.json` | https://aitxchallenge.org/assets/json/final_challenge/AITX-40001.json | fb0cc1dce87a1ab23b38557e1500c0486b33c91fbe88e3ee1c048ba19f04ab04 |
| `data/official_cases/AITX-40002.json` | https://aitxchallenge.org/assets/json/final_challenge/AITX-40002.json | 4f703247ce81fa05aca36c2024963595a4f4c1793a7d86b68a30962429ca7a55 |
| `data/official_cases/AITX-40003.json` | https://aitxchallenge.org/assets/json/final_challenge/AITX-40003.json | dc14da7f912879b8700bd7ffbdcb9501a03185031c243b9a5879d881e8a68013 |
| `data/official_cases/AITX-40004.json` | https://aitxchallenge.org/assets/json/final_challenge/AITX-40004.json | 75b0735e5611c25a99822b9e7ca42864e347c544cf950baf0baa8f81d19e6c04 |
| `data/official/aitxchallenge_report_template.md` | https://aitxchallenge.org/assets/md/aitxchallenge_report_template.md | 8f551c4fe5b45e2114b849d93712861b4341d3c67f5eb838cc6c7ab6406df2e2 |

The per-case files are single JSON objects; the combined file is a JSON array of the same four objects (verified equal, field by field).
`tests/test_official_files.py` re-checks these hashes.
