# AI-Tx Resmî Kurallar Denetimi (bağımsız doğrulama)

**Erişim tarihi:** 2026-11-01 (UTC). Tüm sayfalar `curl` ile doğrudan indirildi ve metne dönüştürülerek okundu. İngilizce alıntılar birebirdir; açıklamalar Türkçedir.

## 0. İncelenen kaynaklar

| Kaynak | URL | Durum |
|---|---|---|
| Ana sayfa (görev, giriş/çıkış formatı, değerlendirme) | https://aitxchallenge.org/ | HTTP 200, tamamı okundu |
| Model submission | https://aitxchallenge.org/models/ | HTTP 200, tamamı okundu |
| FAQ | https://aitxchallenge.org/faq/ | HTTP 200, tamamı okundu |
| Tutorials | https://aitxchallenge.org/tutorial/ | HTTP 200, tamamı okundu |
| Register & key dates | https://aitxchallenge.org/participate/ | HTTP 200 (kullanıcının listesinde yoktu) |
| Previous phases (Faz 0/1 arşivi, örnek sorular) | https://aitxchallenge.org/previous-phases/ | HTTP 200 (listede yoktu) |
| Organizatörler / uzman paneli | https://aitxchallenge.org/people/ | HTTP 200 (listede yoktu) |
| Sponsorship | https://aitxchallenge.org/sponsorship/ | HTTP 200 (teknik içerik yok) |
| Rapor şablonu | https://aitxchallenge.org/assets/md/aitxchallenge_report_template.md | birebir kopya: `data/official/` |
| 4 vaka (birleşik + tekil) | `/assets/json/final_challenge/aitx_final_patients.json`, `AITX-4000{1..4}.json` | birebir kopya + SHA-256: `data/official/SOURCES.md` |
| HF doğrulayıcı Space kaynak kodu | https://huggingface.co/spaces/aitxchallenge/aitxchallenge_submission (`app.py`, `utils.py`) | HF API ile indirildi, okundu |
| Doğrulayıcı örnek veri seti | https://huggingface.co/datasets/aitxchallenge/Phase1_Model_Validator | indirildi |
| Organizatör demo ajanı (tutorial'dan) | https://huggingface.co/spaces/cyrilzakka/TAAIC-Validation | `app.py` okundu |

**Erişilemeyenler / sınırlamalar:** YouTube video tutorial'ları (3 adet) metin olarak incelenemedi; sayfa açıklamaları okundu. GeneReviews bölüm HTML sayfaları otomatik istemcilere reCAPTCHA gösteriyor (atlatılmadı; bkz. §9). OMIM sitesi 403 döndürüyor (API anahtarı gerekir). PharmGKB eski API alan adı (`api.pharmgkb.org`) proxy üzerinden 502; yeni ClinPGx API'si (`api.clinpgx.org`) çalışıyor.

---

## 1. Amaç ve görev

- **Kural:** Final görev "Actionability Report Generation"; hipotetik bir nadir hastalık hastası için kapsamlı, hastaya özgü terapötik eyleme dönüştürülebilirlik raporu.
- **Kaynak:** https://aitxchallenge.org/ — *"This final task tests an AI system's ability to generate a comprehensive therapeutic actionability report for a hypothetical rare disease patient."* Ayrıca: *"This challenge was previously planned as an invite-only task ... but any team may now participate."*
- **Sonuç:** Tek giriş → tek Markdown rapor; tüm vakalar aynı genel pipeline'dan geçer.

## 2. Girdi şeması (resmî dosyalarla çapraz kontrol edildi)

- **Kural:** `id` (string); `patient.genotype` (1+ nesne: `gene` HGNC sembolü, `transcript` RefSeq veya Ensembl, `variant_cdna` cDNA HGVS, `variant_protein` protein HGVS, `zygosity` örn. "heterozygous"/"homozygous"/"hemizygous"); `patient.clinical_context` (serbest metin).
- **Kaynak:** https://aitxchallenge.org/#input-format — *"Users can assume that patient.genotype corresponds to a true diagnosis."*
- **Resmî dosya kontrolü:** 4 vakanın tümü tam olarak bu alanları içeriyor; ek alan yok. Tekil dosyalar tek nesne, birleşik dosya bunların dizisi (alan alan eşit). Protein notasyonu iki biçimde geliyor: parantezli tahmin `p.(Gln367ProfsTer48)` ve parantezsiz `p.Asp34Asn` → ayrıştırıcı ikisini de destekliyor.
- **FAQ ek bilgiler:** Basit varyantlar (SNV/indel) — *"We are limiting question submissions to simple (single-nucleotide and insertion/deletion) variants"*; birden fazla varyant (ör. bileşik heterozigot) olabilir; *"This description is free text and may just be a comma-separated list"*; klinik bağlamda yaş, aile öyküsü, ilaçlar vb. olabilir.
- **Sonuç:** `PatientCase` Pydantic modeli; `genotype` min 1; bilinmeyen üst-düzey alanlar yok sayılır (doğrulayıcı farklı meta veri ekleyebilir), `id`/`patient` zorunlu. Yaş/cinsiyet deterministik ayrıştırılır.
- **Belirsizlik:** Ensembl transkript (ENST) girişi de mümkün; transkript modeli şu an RefSeq `NM_/XM_/NR_` için kuruluyor, ENST için VEP/VariantValidator kullanılır ama ekson modeli hesaplanmaz (bilinen sınırlama).

## 3. Çıktı formatı

- **Kural:** `{"id": <girdi id>, "report_markdown": <string>}`; *"Subheadings, bullets, links, and references beyond the template are encouraged."*
- **Kaynak:** https://aitxchallenge.org/#output-format
- **Sonuç:** Çıktı nesnesinde yalnızca bu iki alan (gönderim doğrulayıcımız fazladan alanı hata sayar). İç denetim izi ayrı dosyalarda tutulur.

## 4. Zorunlu başlıklar ve şablon kuralları

- **Kural:** Şablondaki her bölüm zorunlu ve bu sırada: `# Therapeutic Actionability Report for Patient AITX-XXXXX`, `## Summary`, `## Tier 1: Established Actions` (`### Existing Targeted Therapies`, `### Supportive Management`, `### Contraindications & Precautions`), `## Tier 2: Emerging Actions` (`### Clinical Trial Eligibility`, `### Off-label or Expanded-Access Therapies`), `## Tier 3: Hypothesized Actions` (`### Bespoke Therapies`, `### Drug Repurposing`, `### Outcome Measures & Ranking`).
- **Kaynak:** şablon dosyası — *"Every section below is required and must appear in this order. ... delete this comment block."*
- **Her bölüm için üç kural (birebir):** (1) *"Cite a verifiable source for every claim, or show your reasoning. ... Never cite a reference that doesn't support the claim."* (2) *"State conditional actions."* (3) *"Don't repeat across sections."*
- **Sonuç:** Başlıklar resmî şablon dosyasından **kodla türetiliyor** (`aitx/template.py`), Markdown deterministik oluşturuluyor; yorum bloğu/yer tutucu metin kalırsa QC hata verir. Boş bölümler silinmez; neyin arandığını belirten deterministik bir negatif ifade yazılır.
- **Önemli tanım ayrımları (şablondan):**
  - Off-label: *"... and for which human treatment precedent exists, such as at least one published report of the therapy administered to a patient with the same gene, disease, or variant context."*
  - Drug Repurposing: *"... for which no such human treatment precedent exists, but with a mechanistic rationale"*; şablon satırı *"No human treatment precedent exists in this context."*
  - Bespoke: her strateji için *"Verdict: [AMENABLE / NOT AMENABLE / UNCERTAIN]"*.
  - Supportive: *"Surveillance is currently excluded."* (FAQ de surveillance önerilerini kapsam dışı sayıyor).
  - Clinical Trial Eligibility: *"Actively recruiting clinical trials"*; karşılanan, değerlendirilemeyen kriterler ve dışlayıcı kriter.
  - Outcome Measures: *"A full trial protocol is not expected."*
- **Sonuç:** Bu tanımlar `rules.py` içinde deterministik kurallara çevrildi (insan emsali yoksa off-label → repurposing; etiket/kılavuz kanıtı yoksa "existing targeted" → aşağı taşınır; açık olmayan deneme düşürülür; yaş/cinsiyet uyumsuz deneme düşürülür).

## 5. Değerlendirme kriterleri

- **Kural:** Uzman paneli (klinik genetik, translasyonel araştırma, ilaç geliştirme) beş boyutta puanlar: Factual Accuracy, Completeness, Evidence & Justification, Prioritization & Calibration, Clinical Utility.
- **Kaynak:** https://aitxchallenge.org/#evaluation — *"Incorrect and misleading interpretations will be scored negatively, even if all other required key facts are present and correct."* ve *"Confidence and speculation should reflect the strength of the supporting evidence, with major uncertainties explicitly acknowledged."*
- **Ayrıntılı puanlama metodolojisi:** **Yayımlanmamış.** Ağırlıklar, ölçek, cevap anahtarının içeriği bilinmiyor (*"anticipated answer keys are being developed by a panel of experts"*). Yerel puan kartımız bu nedenle yalnızca "development heuristic" olarak etiketlendi.
- **Sonuç:** Yanlış iddianın cezası yüksek → mimari "az ama doğrulanmış" yönünde optimize edildi (iddia düzeyinde doğrulama, desteklenmeyeni düşürme).
- **Uzman paneli bileşimi (people sayfası):** Panelde N=1 Collaborative üyeleri (M. Lauffer, W. X. Yan) var → Bespoke Therapies bölümü ASO/varyant uygunluğu konusunda uzmanlarca okunacak; N1C kılavuzlarına (tutorial'da bağlantılı Bolt Rubio et al., AJHG 2025) dayalı gerekçe kritik.

## 6. Katmanlar (tier) ve Tier 3 kuralları

- **Kural:** Tier 1 ≤32B açık ağırlık; Tier 2 ≤500B açık ağırlık; **Tier 3 "Unrestricted — Any size · closed-source OK: >500B total parameters, closed weights, or private and commercial APIs."** Örnek frontier API'ler: "GPT-6 Astra, Claude Opus 5, Gemini 3.8".
- **Kaynak:** https://aitxchallenge.org/models/
- **Ajanlar:** FAQ — *"we do not consider them models themselves, but rather an 'orchestration layer'. Therefore, you can use an agent in any tier, but the underlying LLM must follow the tier rules"*.
- **Çoklu model:** Açık yasak yok; "Pin your base model(s)" çoğul ifade → birden fazla temel modele izin veriliyor şeklinde okunuyor. Tier 3'te Claude + Gemini kombinasyonu kurallara uygun.
- **Tier 3'te yapıt paylaşımı:** "Release your artifacts" adımı **yalnızca Tier 1 & 2** için zorunlu; FAQ: Tier 3'te prompt/kod paylaşmak zorunlu değil, ancak model sabitleme zorunlu.
- **Tutarsızlık (raporlanması gereken):** HF doğrulayıcı Space kodundaki tier açıklamaları eski (≤8B / ≤70B / >70B). Web sitesi güncel ve yetkili kabul edildi.

## 7. Gönderim kısıtları

- **Kural:** Tek JSON dosyası, dört rapor; *"it must be valid JSON, contain exactly one report for each of the four patient IDs, and include every required report section heading, in order."* Maks. 5 MB. Kişi/ekip başına bir gönderim; yeniden gönderim mümkün, en son gönderim değerlendirilir. Formspree formu (ad, e-posta, kurum, tier, sistem açıklaması, dosya).
- **İnsan düzenleme yasağı:** *"I confirm that these reports were generated by the system described above, without manual editing."*
- **Yayın izni:** raporlar/alıntılar yayında kullanılabilir.
- **Kayıt:** *"Before you begin: please register"* (Participate sayfası; "Idea Abstract (required)").
- **Kaynak:** https://aitxchallenge.org/models/
- **Sonuç:** `scripts/validate_submission.py` aynı kontrolleri uygular (+ mock çıktıyı reddeder). `outputs/aitx_submission.json` yalnızca gerçek çıkarım + QC başarılıysa yazılır. Hiçbir rapor elle düzeltilmez.

## 8. Tarihler (güncel)

| Tarih | Olay | Kaynak |
|---|---|---|
| 2026-10-08 | Hipotetik hastalarda dahili model değerlendirmesi başladı | participate, ana sayfa |
| **2027-01-05** | **Final gönderim son tarihi** | models, participate |
| 2027-02-26 | Kazananlar (en geç) | models, participate |
| 2027-05-11/14 | SAIL 2027 (Porto Riko) sunumu | participate |

Ödül: final görev kazananlarına toplam 12.500 USD; NEJM AI makale daveti.

## 9. Literatür/veri erişimi ve kaynak gösterme

- **Kural:** Belirli bir kaynak kısıtı yok; tutorial ve FAQ canlı kayıtların kullanılmasını öneriyor: *"You should always prefer live records to a static snapshot"*; *"A prebuilt index is a fine way to surface information quickly, but you should pull the live record before you rely on what it says."* Kaynak = makale, veritabanı kaydı, etiket, kılavuz veya deneme kaydı (URL/erişim numarasıyla).
- **Kaynak:** https://aitxchallenge.org/faq/ ("Is RAG no longer relevant?"), https://aitxchallenge.org/tutorial/
- **Sonuç:** Canlı API'ler (PubMed, CT.gov v2, ClinVar, openFDA/DailyMed, Europe PMC, Ensembl, VariantValidator, UniProt, gnomAD, ClinGen). GeneReviews için NCBI'nin resmî litarch arşivinden tarihli bir anlık görüntü (MANIFEST + SHA-256); atıflar her zaman kanonik Bookshelf URL'sine gider; bölüm revizyon tarihi raporlanır. Deneme durumu doğrulama aşamasında önbellek atlanarak canlı tekrar kontrol edilir.
- **Uyarı (tutorial):** *"LLMs are prone to the same bias as search engines ... search-engine-optimized articles from unreputable or predatory journals are presented as fact."* → web aramasını kapalı tuttuk; yalnızca yapılandırılmış API'ler + alan adı izin listeli `fetch_url`.

## 10. Kazanan doğrulaması, Hugging Face uç noktası, tekrar üretilebilirlik

- **Kural:** Kazananlar bir HF Space kurar: *"Implement an /actionability_report endpoint that accepts the input format and returns the output format. You may host or call any model, including private and API-based ones"*. Public veya private Space (private ise yalnızca o Space'e salt-okur ince ayarlı token). AI-Tx submission Space'inde doğrulayıcı çalıştırılır. *"Agree to no-logging rules: no logging, storing, redistributing, or training on evaluation queries or responses."* Temel model(ler) tam sürümle sabitlenir.
- **FAQ:** *"we will query your exposed API endpoint and expect its output to match, or closely resemble, the reports you submitted."* "latest" gibi takma adlar önerilmez.
- **Kaynak:** https://aitxchallenge.org/models/#top-of-tier, https://aitxchallenge.org/faq/
- **Doğrulayıcı kodu (gerçek davranış):** Mevcut Space (son değişiklik 2026-05-03) hâlâ **Faz 1** doğrulayıcısı: `gradio_client.Client(space_id, token=...)` ile `client.predict(payload, api_name="/answer_question")` çağırıyor ve `{id, answer, explanation, evidence_sources}` bekliyor.
- **Sonuç:** Uygulamamız Gradio API'sinde `api_name="/actionability_report"` (gradio_client `predict` ile çağrılabilir; test edildi) **ve** düz HTTP `POST /actionability_report` sunar. Dağıtım modunda disk önbelleği, artefakt yazımı, girdi/çıktı günlüğü kapalı; Gradio analytics/run history/monitoring/flagging kapalı.
- **Belirsizlikler (organizatöre sorulmalı):** (a) Final doğrulayıcı `/actionability_report`'u gradio_client ile mi, HTTP POST ile mi çağıracak (ikisi de destekleniyor)? (b) Doğrulayıcı tek nesne mi dizi mi gönderecek (ikisi de destekleniyor)? (c) Rapor üretimi dakikalar sürer; doğrulayıcının zaman aşımı nedir? (d) Canlı retrieval nedeniyle (ör. deneme durumu değişirse) "closely resemble" toleransı ne kadar?

## 11. Doğrulama sırasında harici API kullanımı

- **Kural:** Model API'leri açıkça serbest (*"call any model, including private and API-based ones"*). Biyomedikal veri API'leri için açık bir kural yok; FAQ/tutorial canlı API kullanımını tavsiye ettiği için izinli olduğu sonucuna varıldı.
- **Belirsizlik:** Açık yazılı bir izin cümlesi yok → organizatöre teyit sorusu önerilir.

## 12. Kapsam dışı

- Tümör somatik dizileme/precision oncology kapsam dışı; germline kanser yatkınlığı kapsam içi (FAQ).
- Gözetim (surveillance) önerileri istenmiyor (FAQ ve şablon).
- *"Established therapies and active clinical trials may not exist for every case, but when they are available, identifying them accurately is especially important."* (FAQ) → boş bölüm doğru bir cevap olabilir; doldurmak için zayıf öğe eklenmez.

## 13. Organizatörlere önerilen sorular

1. Final doğrulayıcının çağrı biçimi (gradio_client vs HTTP), istek zaman aşımı ve tek/dizi girdi.
2. Kazanan doğrulamasında canlı biyomedikal API kullanımı açıkça serbest mi; zamanla değişen kayıtlar (deneme durumu) nedeniyle oluşan farklar nasıl değerlendirilecek?
3. Ek alt başlıklara (ör. "References") izin var mı? (Şablon "encouraged" diyor; zorunlu başlıkların tekrarlanmaması kaydıyla eklenebilir görünüyor.)
4. Sistem açıklaması alanı için uzunluk sınırı?
